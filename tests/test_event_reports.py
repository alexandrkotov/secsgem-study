"""E30 dynamic event report configuration: S2F33 / S2F35 / S2F37 -> S6F11 / S6F12."""

import pytest

from fabsim import ids

RPT_LOT = 10
RPT_WAFER = 11


def test_define_link_enable_then_event_arrives(online):
    host, tool = online.host, online.equipment

    drack, lrack, erack = host.setup_event_report(
        ids.CE_PROCESS_STARTED, RPT_LOT, [ids.DV_LOT_ID, ids.DV_RECIPE_ID, ids.SV_CHAMBER_TEMP]
    )
    assert (drack, lrack, erack) == (0, 0, 0)

    tool.chamber_temp = 180.5
    tool.start_lot("LOT-A1", "ETCH_OX_60S")

    [event] = host.wait_for_event(ids.CE_PROCESS_STARTED)
    assert event.rptid == RPT_LOT
    assert event.values[ids.DV_LOT_ID] == "LOT-A1"
    assert event.values[ids.DV_RECIPE_ID] == "ETCH_OX_60S"
    assert event.values[ids.SV_CHAMBER_TEMP] == pytest.approx(180.5)


def test_report_values_are_a_snapshot_at_event_time(online):
    """Each WaferCompleted report must carry *its own* wafer id, even when events fire back to back."""
    host, tool = online.host, online.equipment
    host.setup_event_report(ids.CE_WAFER_COMPLETED, RPT_WAFER, [ids.DV_WAFER_ID, ids.SV_WAFERS_PROCESSED])

    tool.start_lot("LOT-B2", "R1")
    for n in range(1, 6):
        tool.complete_wafer(f"W{n:02d}")

    events = host.wait_for_event(ids.CE_WAFER_COMPLETED, count=5)
    assert [e.values[ids.DV_WAFER_ID] for e in events] == ["W01", "W02", "W03", "W04", "W05"]
    assert [e.values[ids.SV_WAFERS_PROCESSED] for e in events] == [1, 2, 3, 4, 5]


def test_disabled_event_is_not_reported(online):
    host, tool = online.host, online.equipment
    host.setup_event_report(ids.CE_PROCESS_STARTED, RPT_LOT, [ids.DV_LOT_ID])
    assert host.enable_events([ids.CE_PROCESS_STARTED], enable=False) == 0

    tool.start_lot("LOT-C3", "R1")
    tool.complete_lot()  # CE 101 was never linked, so this must not be reported either

    with pytest.raises(TimeoutError):
        host.wait_for_event(ids.CE_PROCESS_STARTED, timeout=1.0)
    assert host.events_log == []


def test_define_report_with_unknown_vid_is_rejected(online):
    assert online.host.define_report(RPT_LOT, [999999]) == 4  # DRACK 4 = VID unknown


def test_define_same_report_twice_is_rejected(online):
    assert online.host.define_report(RPT_LOT, [ids.DV_LOT_ID]) == 0
    assert online.host.define_report(RPT_LOT, [ids.DV_WAFER_ID]) == 3  # DRACK 3 = RPTID already defined


def test_link_to_unknown_event_is_rejected(online):
    online.host.define_report(RPT_LOT, [ids.DV_LOT_ID])
    assert online.host.link_event_report(424242, [RPT_LOT]) == 4  # LRACK 4 = CEID unknown


def test_link_unknown_report_is_rejected(online):
    assert online.host.link_event_report(ids.CE_PROCESS_STARTED, [777]) == 5  # LRACK 5 = RPTID unknown


def test_enable_unknown_event_is_rejected(online):
    assert online.host.enable_events([424242]) == 1  # ERACK 1 = CEID does not exist


def test_delete_all_reports_stops_reporting(online):
    host, tool = online.host, online.equipment
    host.setup_event_report(ids.CE_PROCESS_STARTED, RPT_LOT, [ids.DV_LOT_ID])
    assert host.delete_all_reports() == 0

    tool.start_lot("LOT-D4", "R1")
    with pytest.raises(TimeoutError):
        host.wait_for_event(ids.CE_PROCESS_STARTED, timeout=1.0)


def test_events_enabled_sv_reflects_s2f37(online):
    host = online.host
    host.setup_event_report(ids.CE_PROCESS_STARTED, RPT_LOT, [ids.DV_LOT_ID])
    [enabled] = host.read_svs([1003])  # SV 1003 EventsEnabled
    assert ids.CE_PROCESS_STARTED in enabled
