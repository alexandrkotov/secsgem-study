"""E37 HSMS session + E30 communication and control state models."""

from secsgem.gem.communication_state_machine import CommunicationState
from secsgem.gem.control_state_machine import ControlState
from secsgem.hsms.connection_state_machine import ConnectionState

from fabsim import ids


def test_hsms_selected_and_gem_communicating(connected):
    # E37: TCP connected + Select.req/rsp done -> CONNECTED_SELECTED (data messages allowed)
    assert connected.host.protocol.connection_state.current == ConnectionState.CONNECTED_SELECTED
    assert connected.equipment.protocol.connection_state.current == ConnectionState.CONNECTED_SELECTED
    # E30: S1F13/S1F14 Establish Communications done -> COMMUNICATING
    assert connected.host.communication_state.current == CommunicationState.COMMUNICATING
    assert connected.equipment.communication_state.current == CommunicationState.COMMUNICATING


def test_establish_communication_s1f13_returns_model_and_softrev(connected):
    s1f14 = connected.host.settings.streams_functions.decode(
        connected.host.send_and_waitfor_response(connected.host.stream_function(1, 13)())
    )
    assert s1f14.COMMACK.get() == 0
    assert s1f14.MDLN.get() == ["ETCH01", "1.0.0"]


def test_are_you_there_s1f1(connected):
    s1f2 = connected.host.settings.streams_functions.decode(connected.host.are_you_there())
    assert (s1f2.stream, s1f2.function) == (1, 2)
    assert s1f2.get() == ["ETCH01", "1.0.0"]


def test_equipment_starts_host_offline(connected):
    # The tool tried to go online at start-up (ATTEMPT_ONLINE) but no host was there yet.
    assert connected.equipment.control_state.current == ControlState.HOST_OFFLINE


def test_request_online_s1f17_goes_online_remote(connected):
    assert connected.host.go_online() == 0  # ONLACK 0 = accepted
    assert connected.equipment.control_state.current == ControlState.ONLINE_REMOTE
    # ControlState SV (1002): 5 = online remote (1 equip offline, 2 attempt online, 3 host offline, 4 local)
    assert connected.host.read_svs([ids.SV_CONTROL_STATE]) == [5]


def test_request_online_when_already_online_is_rejected(online):
    assert online.host.go_online() == 2  # ONLACK 2 = already online


def test_request_offline_s1f15(online):
    assert online.host.go_offline() == 0  # OFLACK 0 = acknowledged
    assert online.equipment.control_state.current == ControlState.HOST_OFFLINE


def test_operator_switches_to_local_then_remote(online):
    online.equipment.control_switch_online_local()
    assert online.equipment.control_state.current == ControlState.ONLINE_LOCAL
    online.equipment.control_switch_online_remote()
    assert online.equipment.control_state.current == ControlState.ONLINE_REMOTE
