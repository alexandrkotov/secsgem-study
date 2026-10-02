"""E37 HSMS link behaviour: Linktest, T3 reply timeout, S9 errors, disconnect and reconnect."""

import time

from secsgem.gem.communication_state_machine import CommunicationState
from secsgem.hsms.header import HsmsSType

from fabsim import ids

from .conftest import FAST_TIMEOUTS, wait_until


def test_linktest(connected):
    """Linktest.req / Linktest.rsp is the HSMS 'heartbeat' (no stream/function, header only)."""
    reply = connected.host.protocol.send_linktest_req()
    assert reply is not None
    assert reply.header.s_type == HsmsSType.LINKTEST_RSP


def test_t3_reply_timeout(online):
    """If the tool never answers a primary message, the host gives up after T3."""
    host, tool = online.host, online.equipment
    tool.register_stream_function(1, 3, lambda handler, message: None)  # tool 'hangs' on S1F3

    started = time.monotonic()
    reply = host.send_and_waitfor_response(host.stream_function(1, 3)([ids.SV_CHAMBER_TEMP]))
    elapsed = time.monotonic() - started

    assert reply is None
    assert FAST_TIMEOUTS["t3"] - 0.1 <= elapsed < FAST_TIMEOUTS["t3"] + 1.0
    # the link itself is still fine after a T3 timeout
    tool.unregister_stream_function(1, 3)
    assert host.read_svs([ids.SV_PROCESS_STATE]) == ["IDLE"]


def test_unsupported_message_gets_s9f5(online):
    """The tool does not implement S7F19 (process program list), so it answers S9F5 'unrecognized function'."""
    host = online.host
    reply = host.send_and_waitfor_response(host.stream_function(7, 19)())
    assert (reply.header.stream, reply.header.function) == (9, 5)


def test_host_drops_and_reconnects(online):
    host, tool = online.host, online.equipment

    host.disable()
    assert wait_until(lambda: tool.communication_state.current != CommunicationState.COMMUNICATING)

    host.enable()
    assert host.waitfor_communicating(10)
    assert tool.waitfor_communicating(10)
    assert host.read_svs([ids.SV_PROCESS_STATE]) == ["IDLE"]


def test_tool_reconnects_after_its_own_restart(online):
    """The passive side goes away; the active host retries every T5 until the tool listens again."""
    host, tool = online.host, online.equipment

    tool.disable()
    assert wait_until(lambda: host.communication_state.current != CommunicationState.COMMUNICATING)

    tool.enable()
    assert host.waitfor_communicating(5 * FAST_TIMEOUTS["t5"] + 5)
    assert host.are_you_there() is not None
