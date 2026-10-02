"""All IDs used by the simulated etch tool, in one place.

In a real fab these come from the tool vendor's GEM manual
(the "SVID / DVID / CEID / ALID / ECID list"). The host must use
the same numbers, so both sides import them from here.
"""

# Status variables (SV) - live tool state, readable any time with S1F3.
# 1001-1005 are built into secsgem (Clock, ControlState, EventsEnabled, ...).
SV_CLOCK = 1001
SV_CONTROL_STATE = 1002
SV_CHAMBER_TEMP = 2001  # F4, degC
SV_CHAMBER_PRESSURE = 2002  # F4, mTorr
SV_PROCESS_STATE = 2003  # A, "IDLE" / "PROCESSING"
SV_WAFERS_PROCESSED = 2004  # U4

# Data values (DV) - only meaningful when an event happens, sent inside S6F11 reports.
DV_LOT_ID = 3001
DV_WAFER_ID = 3002
DV_RECIPE_ID = 3003
DV_EVENT_SEQ = 3004  # U4, increments for every S6F11 - used to detect loss/duplicates/reorder

# Equipment constants (EC) - settings the host can read (S2F13) and change (S2F15).
# 1-2 are built into secsgem (EstablishCommunicationsTimeout, TimeFormat).
EC_MAX_CHAMBER_TEMP = 4001  # F4, degC, range 20..400

# Collection events (CE) - things that happen on the tool.
# 1, 2, 3, 20, 21 are built into secsgem.
CE_EQUIPMENT_OFFLINE = 1
CE_CONTROL_STATE_LOCAL = 2
CE_CONTROL_STATE_REMOTE = 3
CE_CMD_START_DONE = 20
CE_CMD_STOP_DONE = 21
CE_PROCESS_STARTED = 100
CE_PROCESS_COMPLETED = 101
CE_WAFER_COMPLETED = 102
CE_ALARM_OVERTEMP_SET = 200
CE_ALARM_OVERTEMP_CLEARED = 201

CE_NAMES = {
    CE_EQUIPMENT_OFFLINE: "EquipmentOffline",
    CE_CONTROL_STATE_LOCAL: "ControlStateLocal",
    CE_CONTROL_STATE_REMOTE: "ControlStateRemote",
    CE_CMD_START_DONE: "CmdStartDone",
    CE_CMD_STOP_DONE: "CmdStopDone",
    CE_PROCESS_STARTED: "ProcessStarted",
    CE_PROCESS_COMPLETED: "ProcessCompleted",
    CE_WAFER_COMPLETED: "WaferCompleted",
    CE_ALARM_OVERTEMP_SET: "AlarmOverTempSet",
    CE_ALARM_OVERTEMP_CLEARED: "AlarmOverTempCleared",
}

# Alarms
ALID_OVERTEMP = 5001

# Remote commands (RCMD) for S2F41
RCMD_START = "START"
RCMD_STOP = "STOP"
CP_LOT_ID = "LOT_ID"
CP_RECIPE = "RECIPE"
