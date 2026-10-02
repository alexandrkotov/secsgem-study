"""Delivery guarantees for S6F11 event reports: order, spooling while the link is down, resend.

Every report carries DV EventSeq (3004), a counter the tool increments per report. The host side
checks it the same way you would check a CDC stream: no gaps, no duplicates, no reordering.
"""

from secsgem.gem.communication_state_machine import CommunicationState

from fabsim import ids
from fabsim.bridge import validate_sequence

from .conftest import FAST_TIMEOUTS, wait_until

RPT = 40


def _subscribe_wafers(host):
    assert host.setup_event_report(ids.CE_WAFER_COMPLETED, RPT, [ids.DV_EVENT_SEQ, ids.DV_WAFER_ID]) == (0, 0, 0)


def test_burst_of_events_keeps_order(online):
    host, tool = online.host, online.equipment
    _subscribe_wafers(host)

    for n in range(1, 51):
        tool.complete_wafer(f"W{n:03d}")

    events = host.wait_for_event(ids.CE_WAFER_COMPLETED, count=50, timeout=15)
    report = validate_sequence([e.seq for e in events], expected_last=50)
    assert report.ok, report
    assert [e.values[ids.DV_WAFER_ID] for e in events] == [f"W{n:03d}" for n in range(1, 51)]


def test_events_are_spooled_while_host_is_down_and_sent_in_order_after_reconnect(online):
    host, tool = online.host, online.equipment
    _subscribe_wafers(host)
    tool.complete_wafer("W001")
    host.wait_for_event(ids.CE_WAFER_COMPLETED)

    host.disable()  # host / network goes down
    assert wait_until(lambda: tool.communication_state.current != CommunicationState.COMMUNICATING)

    for n in range(2, 6):
        tool.complete_wafer(f"W{n:03d}")  # the tool keeps working - reports go to the spool
    assert wait_until(lambda: tool.spooled_count == 4)

    host.enable()
    assert host.waitfor_communicating(10)
    assert tool.wait_outbox_empty(10)

    events = host.wait_for_event(ids.CE_WAFER_COMPLETED, count=5)
    assert [e.values[ids.DV_WAFER_ID] for e in events] == ["W001", "W002", "W003", "W004", "W005"]
    assert validate_sequence([e.seq for e in events], expected_last=5).ok


def test_lost_ack_causes_resend_and_a_duplicate(online):
    """S6F12 never arrives -> tool waits T3, resends. The host gets the same report twice.
    That is at-least-once delivery: the consumer must de-duplicate (by EventSeq)."""
    host, tool = online.host, online.equipment
    _subscribe_wafers(host)
    host.drop_next_s6f12 = 1

    tool.complete_wafer("W001")

    events = host.wait_for_event(ids.CE_WAFER_COMPLETED, count=2, timeout=FAST_TIMEOUTS["t3"] + 5)
    assert events[0].seq == events[1].seq == 1
    assert tool.wait_outbox_empty(5)
    assert tool.s6f11_send_attempts == 2

    report = validate_sequence([e.seq for e in host.events_log])
    assert report.duplicates == [1]
    assert not report.missing and not report.out_of_order
