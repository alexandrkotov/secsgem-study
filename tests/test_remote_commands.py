"""E30 remote control: S2F41 Host Command Send / S2F42 ack, plus the 'command done' events."""

from fabsim import ids

RPT_CMD = 20


def test_start_with_params_runs_and_reports_done(online):
    host, tool = online.host, online.equipment
    host.setup_event_report(ids.CE_PROCESS_STARTED, RPT_CMD, [ids.DV_LOT_ID, ids.DV_RECIPE_ID])
    host.define_report(RPT_CMD + 1, [ids.SV_PROCESS_STATE])
    host.link_event_report(ids.CE_CMD_START_DONE, [RPT_CMD + 1])
    host.enable_events([ids.CE_CMD_START_DONE])

    hcack = host.remote_command(ids.RCMD_START, {ids.CP_LOT_ID: "LOT-77", ids.CP_RECIPE: "ETCH_NIT_90S"})

    assert hcack == 4  # HCACK 4 = acknowledged, command will be performed, completion signalled by event
    [started] = host.wait_for_event(ids.CE_PROCESS_STARTED)
    assert started.values == {ids.DV_LOT_ID: "LOT-77", ids.DV_RECIPE_ID: "ETCH_NIT_90S"}
    [done] = host.wait_for_event(ids.CE_CMD_START_DONE)
    assert done.values[ids.SV_PROCESS_STATE] == "PROCESSING"
    assert host.read_svs([ids.SV_PROCESS_STATE]) == ["PROCESSING"]
    assert tool.lot_id == "LOT-77"


def test_stop_returns_tool_to_idle(online):
    host = online.host
    host.remote_command(ids.RCMD_START, {ids.CP_LOT_ID: "LOT-1", ids.CP_RECIPE: "R1"})
    assert host.remote_command(ids.RCMD_STOP) == 4
    assert host.read_svs([ids.SV_PROCESS_STATE]) == ["IDLE"]


def test_unknown_command_is_rejected(online):
    assert online.host.remote_command("SELF_DESTRUCT") == 1  # HCACK 1 = invalid command


def test_unknown_parameter_is_rejected(online):
    hcack = online.host.remote_command(ids.RCMD_START, {"BOGUS": "1"})
    assert hcack == 3  # HCACK 3 = at least one parameter is invalid
    assert online.equipment.process_state == "IDLE"
