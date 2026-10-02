"""Data collection into SQL and the reporting queries (WIP, lots, alarms, data quality)."""

import pytest

from fabsim import ids
from fabsim.datastore import EventStore, attach_collector

from .conftest import FAST_TIMEOUTS, wait_until

TOOL = "ETCH01"


@pytest.fixture
def collected(online):
    store = EventStore()
    attach_collector(online.host, store, TOOL)
    yield online, store
    store.close()


def _run_lot(link, lot_id, wafers, complete=True):
    assert link.host.remote_command(ids.RCMD_START, {ids.CP_LOT_ID: lot_id, ids.CP_RECIPE: "ETCH_OX_60S"}) == 4
    # HCACK 4 = "accepted, will finish later": the tool runs START *after* replying S2F42.
    # Like a real host, wait for the event that says it actually started (CI caught this race).
    link.host.wait_for_event(ids.CE_PROCESS_STARTED, match={ids.DV_LOT_ID: lot_id})
    for n in range(1, wafers + 1):
        link.equipment.complete_wafer(f"{lot_id}-W{n:02d}")
    if complete:
        link.host.remote_command(ids.RCMD_STOP)


def _rows(store):
    return store.query("SELECT COUNT(*) AS n FROM tool_event")[0]["n"]


def test_events_land_in_sql_with_lot_and_wafer(collected):
    link, store = collected
    _run_lot(link, "LOT-A", wafers=3)
    assert wait_until(lambda: _rows(store) == 5)  # started + 3 wafers + completed

    rows = store.query(
        "SELECT event_name, lot_id, wafer_id FROM tool_event WHERE equipment_id = :t ORDER BY seq", t=TOOL
    )
    assert [r["event_name"] for r in rows] == [
        "ProcessStarted", "WaferCompleted", "WaferCompleted", "WaferCompleted", "ProcessCompleted",
    ]
    assert {r["lot_id"] for r in rows} == {"LOT-A"}
    assert [r["wafer_id"] for r in rows if r["event_name"] == "WaferCompleted"] == [
        "LOT-A-W01", "LOT-A-W02", "LOT-A-W03",
    ]


def test_lot_summary_and_wip(collected):
    link, store = collected
    _run_lot(link, "LOT-A", wafers=2)
    _run_lot(link, "LOT-B", wafers=4, complete=False)  # still on the tool
    assert wait_until(lambda: _rows(store) == 9)

    summary = {r["lot_id"]: r for r in store.lot_summary(TOOL)}
    assert summary["LOT-A"]["status"] == "COMPLETE" and summary["LOT-A"]["wafers_done"] == 2
    assert summary["LOT-B"]["status"] == "IN_PROCESS" and summary["LOT-B"]["wafers_done"] == 4

    [wip] = store.wip(TOOL)
    assert (wip["lot_id"], wip["wafers_done"]) == ("LOT-B", 4)


def test_open_alarms(collected):
    link, store = collected
    link.host.enable_alarm(ids.ALID_OVERTEMP)

    link.equipment.set_chamber_temp(280.0)
    assert wait_until(lambda: len(store.open_alarms(TOOL)) == 1)
    assert store.open_alarms(TOOL)[0]["alid"] == ids.ALID_OVERTEMP

    link.equipment.set_chamber_temp(200.0)
    assert wait_until(lambda: store.open_alarms(TOOL) == [])
    assert store.query("SELECT COUNT(*) AS n FROM alarm_log")[0]["n"] == 2  # history keeps both


def test_resent_event_is_stored_once(collected):
    """Lost S6F12 -> the tool resends -> the UNIQUE(equipment_id, seq) key drops the duplicate."""
    link, store = collected
    link.host.drop_next_s6f12 = 1
    link.equipment.start_lot("LOT-D", "R1")

    link.host.wait_for_event(ids.CE_PROCESS_STARTED, count=2, timeout=FAST_TIMEOUTS["t3"] + 5)
    assert _rows(store) == 1
    assert store.duplicates_ignored == 1


def test_seq_gap_query_finds_missing_events(collected):
    """If rows are missing (e.g. a bad delete or a broken loader), the gap query shows the hole."""
    link, store = collected
    _run_lot(link, "LOT-E", wafers=4)
    assert wait_until(lambda: _rows(store) == 6)
    assert store.seq_gaps(TOOL) == []

    store.query("DELETE FROM tool_event WHERE seq IN (3, 4)")
    assert store.seq_gaps(TOOL) == [{"first_missing": 3, "last_missing": 4}]
