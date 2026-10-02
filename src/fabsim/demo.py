"""Happy-path demo: run the tool and the host in one process and print every SECS message.

    python -m fabsim.demo            # short output: one line per step
    python -m fabsim.demo --sml      # also print every message in SML (the text form of SECS-II)
"""

from __future__ import annotations

import argparse
import logging

from . import ids
from .equipment import make_equipment
from .host import make_host
from .lifecycle import free_port, stop_pair, wait_listening


def step(text: str) -> None:
    print(f"\n=== {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sml", action="store_true", help="log every SECS message in SML")
    parser.add_argument("--port", type=int, default=0, help="HSMS port (default: a free one)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.sml:
        logging.getLogger("communication").setLevel(logging.INFO)

    port = args.port or free_port()
    tool = make_equipment(port)
    host = make_host(port)

    step(f"E37 HSMS: tool listens on 127.0.0.1:{port} (passive), host connects (active), Select.req/rsp")
    tool.enable()
    wait_listening(tool)
    host.enable()
    host.waitfor_communicating(10)
    print("E30 S1F13/S1F14 Establish Communications -> COMMUNICATING")
    print("tool control state:", tool.control_state.current.name)

    try:
        step("S1F1/S1F2 Are You There")
        print("reply MDLN/SOFTREV:", host.settings.streams_functions.decode(host.are_you_there()).get())

        step("S1F17/S1F18 Request Online")
        print("ONLACK:", host.go_online(), "-> tool control state:", tool.control_state.current.name)

        step("S1F3/S1F4 read status variables")
        tool.chamber_temp = 42.0
        print("ChamberTemp, ProcessState:", host.read_svs([ids.SV_CHAMBER_TEMP, ids.SV_PROCESS_STATE]))

        step("S2F33 define report -> S2F35 link to event -> S2F37 enable event")
        print(
            "DRACK, LRACK, ERACK:",
            host.setup_event_report(
                ids.CE_WAFER_COMPLETED, 1, [ids.DV_EVENT_SEQ, ids.DV_LOT_ID, ids.DV_WAFER_ID]
            ),
        )

        step("S2F41/S2F42 remote command START (LOT_ID, RECIPE)")
        print("HCACK:", host.remote_command(ids.RCMD_START, {ids.CP_LOT_ID: "LOT-001", ids.CP_RECIPE: "ETCH_OX_60S"}))

        step("tool processes 3 wafers -> S6F11 event reports -> host answers S6F12")
        for n in range(1, 4):
            tool.complete_wafer(f"W{n:02d}")
        for event in host.wait_for_event(ids.CE_WAFER_COMPLETED, count=3):
            print(f"CEID {event.ceid} RPTID {event.rptid}:", event.values)

        step("S5F3 enable alarm, then over-temperature -> S5F1 alarm set / clear")
        host.enable_alarm(ids.ALID_OVERTEMP)
        tool.set_chamber_temp(300.0)
        tool.set_chamber_temp(100.0)
        for alarm in host.wait_for_alarm(ids.ALID_OVERTEMP, count=2):
            print(f"ALID {alarm.alid} {'SET  ' if alarm.is_set else 'CLEAR'} '{alarm.altx}'")

        step("S2F41 STOP")
        print("HCACK:", host.remote_command(ids.RCMD_STOP), "ProcessState:", host.read_svs([ids.SV_PROCESS_STATE]))
    finally:
        stop_pair(tool, host)
    print("\ndone")


if __name__ == "__main__":
    main()
