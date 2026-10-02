"""E30 alarm management (S5F1-S5F8), status variables (S1F3, S1F11) and equipment constants (S2F13/15)."""

import pytest

from fabsim import ids


# ---------------------------------------------------------------- status variables


def test_read_status_variables_s1f3(online):
    tool = online.equipment
    tool.chamber_temp = 123.25
    tool.chamber_pressure = 15.5
    temp, pressure, state, wafers = online.host.read_svs(
        [ids.SV_CHAMBER_TEMP, ids.SV_CHAMBER_PRESSURE, ids.SV_PROCESS_STATE, ids.SV_WAFERS_PROCESSED]
    )
    assert temp == pytest.approx(123.25)
    assert pressure == pytest.approx(15.5)
    assert (state, wafers) == ("IDLE", 0)


def test_status_variable_namelist_s1f11(online):
    namelist = online.host.list_svs([ids.SV_CHAMBER_TEMP]).get()
    assert namelist == [{"SVID": ids.SV_CHAMBER_TEMP, "SVNAME": "ChamberTemp", "UNITS": "degC"}]


# ---------------------------------------------------------------- equipment constants


def test_read_equipment_constant_s2f13(online):
    assert online.host.read_ecs([ids.EC_MAX_CHAMBER_TEMP]) == [pytest.approx(250.0)]


def test_write_equipment_constant_s2f15(online):
    assert online.host.write_ec(ids.EC_MAX_CHAMBER_TEMP, 300.0) == 0  # EAC 0 = ok
    assert online.host.read_ecs([ids.EC_MAX_CHAMBER_TEMP]) == [pytest.approx(300.0)]


def test_write_equipment_constant_out_of_range(online):
    assert online.host.write_ec(ids.EC_MAX_CHAMBER_TEMP, 1000.0) == 3  # EAC 3 = out of range
    assert online.host.read_ecs([ids.EC_MAX_CHAMBER_TEMP]) == [pytest.approx(250.0)]


def test_write_unknown_equipment_constant(online):
    assert online.host.write_ec(987654, 1) == 1  # EAC 1 = constant does not exist


# ---------------------------------------------------------------- alarms


def test_alarm_list_s5f5(online):
    [alarm] = online.host.list_alarms([ids.ALID_OVERTEMP])
    assert alarm["ALID"] == ids.ALID_OVERTEMP
    assert alarm["ALTX"] == "Chamber temperature above MaxChamberTemp"


def test_alarm_not_sent_until_enabled(online):
    online.equipment.set_chamber_temp(260.0)  # above default limit 250
    with pytest.raises(TimeoutError):
        online.host.wait_for_alarm(ids.ALID_OVERTEMP, timeout=1.0)
    assert online.equipment.alarms[ids.ALID_OVERTEMP].set  # the alarm is set on the tool, just not reported


def test_alarm_set_and_clear_s5f1(online):
    host, tool = online.host, online.equipment
    assert host.enable_alarm(ids.ALID_OVERTEMP) == 0  # ACKC5 0 = accepted
    assert host.list_enabled_alarms()[0]["ALID"] == ids.ALID_OVERTEMP

    tool.set_chamber_temp(260.0)
    tool.set_chamber_temp(200.0)

    set_msg, clear_msg = host.wait_for_alarm(ids.ALID_OVERTEMP, count=2)
    assert set_msg.is_set and not clear_msg.is_set
    assert set_msg.alcd & 0x7F == 2  # category 2 = equipment safety
    assert host.read_svs([1005]) == [[]]  # SV 1005 AlarmsSet: nothing set any more


def test_alarm_also_fires_collection_events(online):
    host, tool = online.host, online.equipment
    host.setup_event_report(ids.CE_ALARM_OVERTEMP_SET, 30, [ids.SV_CHAMBER_TEMP])

    tool.set_chamber_temp(275.0)

    [event] = host.wait_for_event(ids.CE_ALARM_OVERTEMP_SET)
    assert event.values[ids.SV_CHAMBER_TEMP] == pytest.approx(275.0)


def test_alarm_limit_follows_equipment_constant(online):
    host, tool = online.host, online.equipment
    host.enable_alarm(ids.ALID_OVERTEMP)
    host.write_ec(ids.EC_MAX_CHAMBER_TEMP, 300.0)

    tool.set_chamber_temp(260.0)  # fine under the new limit
    with pytest.raises(TimeoutError):
        host.wait_for_alarm(ids.ALID_OVERTEMP, timeout=1.0)
