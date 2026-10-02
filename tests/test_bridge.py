"""Bonus: S6F11 -> message bus bridge and the stream validator (loss / duplicates / order)."""

import os
import uuid

import pytest

from fabsim import ids
from fabsim.bridge import EventBridge, InMemorySink, seqs_from_records, validate_sequence

from .conftest import FAST_TIMEOUTS

RPT = 50


# ---------------------------------------------------------------- validator unit tests


def test_validator_clean_stream():
    assert validate_sequence([1, 2, 3, 4]).ok


def test_validator_finds_gap():
    assert validate_sequence([1, 2, 4, 5]).missing == [3]


def test_validator_finds_missing_tail_when_last_is_known():
    assert validate_sequence([1, 2, 3], expected_last=5).missing == [4, 5]


def test_validator_finds_duplicates():
    assert validate_sequence([1, 2, 2, 3, 3]).duplicates == [2, 3]


def test_validator_finds_reorder():
    report = validate_sequence([1, 3, 2, 4])
    assert report.out_of_order == [(3, 2)]
    assert report.missing == []


# ---------------------------------------------------------------- end to end with an in-memory sink


def _wire(online, dedup):
    sink = InMemorySink()
    EventBridge(online.host, sink, equipment_id="ETCH01", dedup=dedup)
    online.host.setup_event_report(ids.CE_WAFER_COMPLETED, RPT, [ids.DV_EVENT_SEQ, ids.DV_WAFER_ID])
    return sink


def test_bridge_publishes_every_event_in_order(online):
    sink = _wire(online, dedup=False)
    for n in range(1, 21):
        online.equipment.complete_wafer(f"W{n:02d}")
    online.host.wait_for_event(ids.CE_WAFER_COMPLETED, count=20, timeout=10)

    records = sink.values()
    assert {key for key, _ in sink.records} == {"ETCH01"}
    assert records[0]["values"][str(ids.DV_WAFER_ID)] == "W01"
    assert validate_sequence(seqs_from_records(records), expected_last=20).ok


def test_bridge_without_dedup_passes_duplicates_through(online):
    sink = _wire(online, dedup=False)
    online.host.drop_next_s6f12 = 1
    online.equipment.complete_wafer("W01")
    online.host.wait_for_event(ids.CE_WAFER_COMPLETED, count=2, timeout=FAST_TIMEOUTS["t3"] + 5)

    assert validate_sequence(seqs_from_records(sink.values())).duplicates == [1]


def test_bridge_with_dedup_publishes_each_event_once(online):
    sink = _wire(online, dedup=True)
    online.host.drop_next_s6f12 = 1
    online.equipment.complete_wafer("W01")
    online.equipment.complete_wafer("W02")
    online.host.wait_for_event(ids.CE_WAFER_COMPLETED, count=3, timeout=FAST_TIMEOUTS["t3"] + 5)

    assert seqs_from_records(sink.values()) == [1, 2]


# ---------------------------------------------------------------- real Kafka (optional)


@pytest.mark.kafka
@pytest.mark.skipif(not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"), reason="KAFKA_BOOTSTRAP_SERVERS not set")
def test_round_trip_through_real_kafka(online):
    confluent_kafka = pytest.importorskip("confluent_kafka")
    from fabsim.bridge import KafkaSink

    bootstrap = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    topic = f"fab.etch01.events.{uuid.uuid4().hex[:8]}"
    sink = KafkaSink(bootstrap, topic)
    EventBridge(online.host, sink, equipment_id="ETCH01", dedup=True)
    online.host.setup_event_report(ids.CE_WAFER_COMPLETED, RPT, [ids.DV_EVENT_SEQ, ids.DV_WAFER_ID])
    online.host.drop_next_s6f12 = 1  # one duplicate on the SECS side, the bridge must hide it

    for n in range(1, 31):
        online.equipment.complete_wafer(f"W{n:02d}")
    online.host.wait_for_event(ids.CE_WAFER_COMPLETED, count=31, timeout=20)
    sink.flush()

    import json

    consumer = confluent_kafka.Consumer(
        {"bootstrap.servers": bootstrap, "group.id": topic, "auto.offset.reset": "earliest"}
    )
    consumer.subscribe([topic])
    records = []
    try:
        for _ in range(100):
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                if len(records) >= 30:
                    break
                continue
            records.append(json.loads(msg.value()))
    finally:
        consumer.close()

    report = validate_sequence(seqs_from_records(records), expected_last=30)
    assert report.ok, report
