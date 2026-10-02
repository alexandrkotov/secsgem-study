"""Bridge: every S6F11 event report the host receives -> one JSON record on a message bus (Kafka).

This is the same shape of problem as CDC (Debezium -> Kafka): a source emits change events,
a pipeline moves them, and the consumer must be able to prove nothing was lost, duplicated
or reordered. EventSeq (DV 3004) plays the role of the source's log position / LSN.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import typing

if typing.TYPE_CHECKING:
    from .host import CellControllerHost, ReceivedEvent


class Sink(typing.Protocol):
    def send(self, key: str, value: bytes) -> None: ...

    def flush(self) -> None: ...


class InMemorySink:
    """Test double for Kafka: keeps (key, value) pairs in a list."""

    def __init__(self):
        self.records: list[tuple[str, bytes]] = []
        self._lock = threading.Lock()

    def send(self, key: str, value: bytes) -> None:
        with self._lock:
            self.records.append((key, value))

    def flush(self) -> None:
        pass

    def values(self) -> list[dict]:
        with self._lock:
            return [json.loads(v) for _, v in self.records]


class KafkaSink:
    """Real Kafka producer (pip install confluent-kafka). Key = equipment id -> one partition per tool,
    so Kafka keeps per-tool order."""

    def __init__(self, bootstrap_servers: str, topic: str):
        from confluent_kafka import Producer  # optional dependency

        self.topic = topic
        # acks=all + idempotence: the producer itself won't create duplicates on retry
        self._producer = Producer(
            {"bootstrap.servers": bootstrap_servers, "acks": "all", "enable.idempotence": True}
        )

    def send(self, key: str, value: bytes) -> None:
        self._producer.produce(self.topic, key=key, value=value)
        self._producer.poll(0)

    def flush(self) -> None:
        self._producer.flush(10)


class EventBridge:
    """Listens to the host's received events and publishes them to a sink.

    dedup=True drops a report whose EventSeq was already published. That turns the
    at-least-once S6F11 delivery (resend after a lost S6F12) into effectively-once on the bus.
    """

    def __init__(self, host: CellControllerHost, sink: Sink, equipment_id: str, dedup: bool = False):
        self.sink = sink
        self.equipment_id = equipment_id
        self.dedup = dedup
        self._published_seqs: set[int] = set()
        self._lock = threading.Lock()
        host.add_event_listener(self._on_event)

    def _on_event(self, event: ReceivedEvent) -> None:
        with self._lock:
            if self.dedup and event.seq is not None:
                if event.seq in self._published_seqs:
                    return
                self._published_seqs.add(event.seq)
            record = {
                "equipment_id": self.equipment_id,
                "seq": event.seq,
                "ceid": event.ceid,
                "rptid": event.rptid,
                "dataid": event.dataid,
                "values": {str(vid): value for vid, value in event.values.items()},
            }
            self.sink.send(self.equipment_id, json.dumps(record).encode())


@dataclasses.dataclass
class SequenceReport:
    """Result of checking a stream of sequence numbers."""

    count: int
    missing: list[int]
    duplicates: list[int]
    out_of_order: list[tuple[int, int]]  # (previous seq, seq that came after it but is smaller)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.duplicates or self.out_of_order)


def validate_sequence(seqs: list[int], expected_first: int = 1, expected_last: int | None = None) -> SequenceReport:
    """Check a consumed stream for loss (gaps), duplicates and reordering."""
    seen: set[int] = set()
    duplicates: list[int] = []
    out_of_order: list[tuple[int, int]] = []
    highest = None
    for seq in seqs:
        if seq in seen:
            duplicates.append(seq)
            continue
        if highest is not None and seq < highest:
            out_of_order.append((highest, seq))
        seen.add(seq)
        highest = seq if highest is None else max(highest, seq)

    last = expected_last if expected_last is not None else (max(seen) if seen else expected_first - 1)
    missing = [s for s in range(expected_first, last + 1) if s not in seen]
    return SequenceReport(len(seqs), missing, duplicates, out_of_order)


def seqs_from_records(records: typing.Iterable[dict]) -> list[int]:
    return [r["seq"] for r in records if r.get("seq") is not None]
