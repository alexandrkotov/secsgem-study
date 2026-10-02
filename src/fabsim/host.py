"""Host side: a minimal cell controller that connects to one tool over HSMS.

It wraps secsgem's GemHostHandler with:
  * typed helpers that return the GEM ack codes (DRACK, LRACK, ERACK, HCACK, EAC...)
    so tests can assert on them
  * thread-safe logs of received S6F11 events and S5F1 alarms + "wait for" helpers
  * a fault-injection switch to drop S6F12 acks (to test at-least-once delivery)
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import typing

import secsgem.common
import secsgem.gem
import secsgem.hsms

from . import ids
from .hsms_fixes import FixedHsmsSettings

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ReceivedEvent:
    """One collection event report (one RPT inside an S6F11) as the host saw it."""

    dataid: int
    ceid: int
    rptid: int
    values: dict[int, typing.Any]  # VID -> value
    received_at: float

    @property
    def seq(self) -> int | None:
        return self.values.get(ids.DV_EVENT_SEQ)


@dataclasses.dataclass(frozen=True)
class ReceivedAlarm:
    alid: int
    alcd: int
    altx: str

    @property
    def is_set(self) -> bool:
        return bool(self.alcd & 0x80)  # bit 8 of ALCD: 1 = alarm set, 0 = cleared


class CellControllerHost(secsgem.gem.GemHostHandler):
    def __init__(self, settings: secsgem.common.Settings):
        super().__init__(settings)
        # see the same line in EtchToolEquipment.__init__ (secsgem 0.3.0 never calls on_connection_closed)
        self.protocol.events.disconnected += lambda _: self.on_connection_closed(None)
        self._lock = threading.Condition()
        self.events_log: list[ReceivedEvent] = []
        self.alarms_log: list[ReceivedAlarm] = []
        self.drop_next_s6f12 = 0  # fault injection: don't ack the next N S6F11 messages
        self._listeners: list[typing.Callable[[ReceivedEvent], None]] = []
        self._alarm_listeners: list[typing.Callable[[ReceivedAlarm], None]] = []

    # ------------------------------------------------------------------ dynamic event reports

    def define_report(self, rptid: int, vids: list[int]) -> int:
        """S2F33 Define Report. Returns DRACK (0 = ok, 3 = RPTID already defined, 4 = unknown VID)."""
        resp = self._ask(2, 33, {"DATAID": 0, "DATA": [{"RPTID": rptid, "VID": vids}]})
        if resp == 0:
            self.report_subscriptions[rptid] = vids
        return resp

    def delete_all_reports(self) -> int:
        """S2F33 with an empty list deletes every report definition (and its links)."""
        self.report_subscriptions.clear()
        return self._ask(2, 33, {"DATAID": 0, "DATA": []})

    def link_event_report(self, ceid: int, rptids: list[int]) -> int:
        """S2F35 Link Event Report. Returns LRACK (0 = ok, 4 = unknown CEID, 5 = unknown RPTID)."""
        return self._ask(2, 35, {"DATAID": 0, "DATA": [{"CEID": ceid, "RPTID": rptids}]})

    def enable_events(self, ceids: list[int], enable: bool = True) -> int:
        """S2F37 Enable/Disable Event Report. Empty list = all events. Returns ERACK."""
        return self._ask(2, 37, {"CEED": enable, "CEID": ceids})

    def setup_event_report(self, ceid: int, rptid: int, vids: list[int]) -> tuple[int, int, int]:
        """The usual 3-step sequence: define report -> link to event -> enable event."""
        return (
            self.define_report(rptid, vids),
            self.link_event_report(ceid, [rptid]),
            self.enable_events([ceid]),
        )

    # ------------------------------------------------------------------ other requests

    def remote_command(self, rcmd: str, params: dict[str, str] | None = None) -> int:
        """S2F41 Host Command Send. Returns HCACK (0 ok, 1 invalid cmd, 3 bad param, 4 ok/finish later)."""
        s2f42 = self.send_remote_command(rcmd, [[k, v] for k, v in (params or {}).items()])
        return s2f42.HCACK.get()

    def read_svs(self, svids: list[int]) -> list:
        """S1F3 Selected Equipment Status Request -> S1F4 values, same order as svids."""
        return self.request_svs(svids).get()

    def read_ecs(self, ecids: list[int]) -> list:
        """S2F13 Equipment Constant Request -> S2F14 values."""
        return self.request_ecs(ecids).get()

    def write_ec(self, ecid: int, value) -> int:
        """S2F15 New Equipment Constant Send. Returns EAC (0 ok, 1 unknown ECID, 3 out of range)."""
        return self.set_ec(ecid, value)  # already decoded to the EAC int by secsgem

    def _ask(self, stream: int, function: int, data) -> typing.Any:
        reply = self.send_and_waitfor_response(self.stream_function(stream, function)(data))
        if reply is None:
            raise TimeoutError(f"no reply to S{stream}F{function} within T3")
        return self.settings.streams_functions.decode(reply).get()

    # ------------------------------------------------------------------ incoming messages

    def _on_s01f14(self, handler, message):
        # see EtchToolEquipment._on_s01f14
        return None

    def _on_s06f11(self, handler, message):
        """S6F11 Event Report Send: record every report, then answer S6F12 ACKC6=0."""
        s6f11 = self.settings.streams_functions.decode(message)
        dataid = s6f11.DATAID.get()
        ceid = s6f11.CEID.get()

        for report in s6f11.RPT:
            rptid = report.RPTID.get()
            vids = self.report_subscriptions.get(rptid)
            values = report.V.get()
            if vids is None:
                log.warning("S6F11 with unknown RPTID %s", rptid)
                vids = list(range(len(values)))
            event = ReceivedEvent(dataid, ceid, rptid, dict(zip(vids, values)), time.monotonic())
            with self._lock:
                self.events_log.append(event)
                self._lock.notify_all()
            for listener in list(self._listeners):
                listener(event)

        with self._lock:
            if self.drop_next_s6f12 > 0:
                self.drop_next_s6f12 -= 1
                log.warning("fault injection: dropping S6F12 for DATAID=%s", dataid)
                return None

        return self.stream_function(6, 12)(self.settings.data_items.ACKC6.ACCEPTED)

    def _on_alarm_received(self, handler, alarm_id, alarm_code, alarm_text):
        alarm = ReceivedAlarm(alarm_id.get(), alarm_code.get(), alarm_text.get())
        with self._lock:
            self.alarms_log.append(alarm)
            self._lock.notify_all()
        for listener in list(self._alarm_listeners):
            listener(alarm)
        return self.settings.data_items.ACKC5.ACCEPTED

    def add_event_listener(self, listener: typing.Callable[[ReceivedEvent], None]):
        self._listeners.append(listener)

    def add_alarm_listener(self, listener: typing.Callable[[ReceivedAlarm], None]):
        self._alarm_listeners.append(listener)

    # ------------------------------------------------------------------ waiting helpers for tests

    def wait_for_event(
        self, ceid: int, timeout: float = 5.0, count: int = 1, match: dict[int, typing.Any] | None = None
    ) -> list[ReceivedEvent]:
        """Wait for `count` reports of `ceid`; `match` = {VID: value} narrows it down (e.g. one lot)."""
        match = match or {}

        def select():
            return [
                e for e in self.events_log
                if e.ceid == ceid and all(e.values.get(vid) == value for vid, value in match.items())
            ]

        return self._wait(select, count, timeout, f"CEID {ceid} {match or ''}".strip())

    def wait_for_alarm(self, alid: int, timeout: float = 5.0, count: int = 1) -> list[ReceivedAlarm]:
        return self._wait(lambda: [a for a in self.alarms_log if a.alid == alid], count, timeout, f"ALID {alid}")

    def _wait(self, select, count, timeout, what):
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                found = select()
                if len(found) >= count:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"expected {count} x {what}, got {len(found)}")
                self._lock.wait(remaining)

    def clear_logs(self):
        with self._lock:
            self.events_log.clear()
            self.alarms_log.clear()


def make_host(port: int, address: str = "127.0.0.1", **timeouts) -> CellControllerHost:
    settings = FixedHsmsSettings(
        address=address,
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE,
        device_type=secsgem.common.DeviceType.HOST,
        **timeouts,
    )
    return CellControllerHost(settings)
