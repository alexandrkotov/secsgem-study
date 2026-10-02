"""Equipment side: a simulated single-chamber etch tool that speaks GEM over HSMS.

The tool is the HSMS *passive* side (it listens on a TCP port), the host connects to it.
That is the usual setup in a fab: the cell controller / host connects out to each tool.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

import secsgem.common
import secsgem.gem
import secsgem.hsms
from secsgem.gem.communication_state_machine import CommunicationState
from secsgem.secs.variables import F4, U4, String

from . import ids
from .hsms_fixes import FixedHsmsSettings

log = logging.getLogger(__name__)


class EtchToolEquipment(secsgem.gem.GemEquipmentHandler):
    """GEM equipment simulator.

    What it adds on top of secsgem's GemEquipmentHandler:
      * tool-specific SVs, DVs, ECs, CEs, an alarm and START/STOP remote commands
      * a small process simulation (start lot -> complete wafers -> complete lot)
      * an *ordered* S6F11 sender with a simple spool (see `trigger_collection_events`)
    """

    def __init__(self, settings: secsgem.common.Settings, mdln: str = "ETCH01", softrev: str = "1.0.0"):
        # The event pipeline must exist before super().__init__(), because the GEM control
        # state model starts inside the base constructor and may already trigger events.
        self._outbox: collections.deque[tuple[int, dict]] = collections.deque()
        self._outbox_cv = threading.Condition()
        self._event_seq = 0
        self._stopped = False
        self.s6f11_retry_delay = 0.2  # seconds to wait before resending an un-acked S6F11
        self.s6f11_send_attempts = 0  # counts every S6F11 put on the wire (resends included)

        super().__init__(settings)

        # secsgem 0.3.0 defines on_connection_closed() but never calls it, so without this line
        # the GEM communication state stays COMMUNICATING after the TCP link is gone.
        self.protocol.events.disconnected += lambda _: self.on_connection_closed(None)

        self._mdln = mdln
        self._softrev = softrev

        # Live tool state
        self.chamber_temp = 25.0
        self.chamber_pressure = 760000.0
        self.process_state = "IDLE"
        self.wafers_processed = 0
        self.lot_id = ""
        self.recipe_id = ""
        self.wafer_id = ""

        self._define_variables()
        self._define_events()
        self._define_alarms()
        self._define_remote_commands()

        self._sender = threading.Thread(target=self._sender_loop, name="fabsim_s6f11_sender", daemon=True)
        self._sender.start()

    # ------------------------------------------------------------------ definitions

    def _define_variables(self):
        # use_callback=True -> value comes from on_sv_value_request / on_dv_value_request below
        self.status_variables.update(
            {
                ids.SV_CHAMBER_TEMP: secsgem.gem.StatusVariable(ids.SV_CHAMBER_TEMP, "ChamberTemp", "degC", F4),
                ids.SV_CHAMBER_PRESSURE: secsgem.gem.StatusVariable(
                    ids.SV_CHAMBER_PRESSURE, "ChamberPressure", "mTorr", F4
                ),
                ids.SV_PROCESS_STATE: secsgem.gem.StatusVariable(ids.SV_PROCESS_STATE, "ProcessState", "", String),
                ids.SV_WAFERS_PROCESSED: secsgem.gem.StatusVariable(
                    ids.SV_WAFERS_PROCESSED, "WafersProcessed", "", U4
                ),
            }
        )
        self.data_values.update(
            {
                ids.DV_LOT_ID: secsgem.gem.DataValue(ids.DV_LOT_ID, "LotID", String),
                ids.DV_WAFER_ID: secsgem.gem.DataValue(ids.DV_WAFER_ID, "WaferID", String),
                ids.DV_RECIPE_ID: secsgem.gem.DataValue(ids.DV_RECIPE_ID, "RecipeID", String),
                ids.DV_EVENT_SEQ: secsgem.gem.DataValue(ids.DV_EVENT_SEQ, "EventSeq", U4),
            }
        )
        self.equipment_constants[ids.EC_MAX_CHAMBER_TEMP] = secsgem.gem.EquipmentConstant(
            ids.EC_MAX_CHAMBER_TEMP, "MaxChamberTemp", 20, 400, 250, "degC", F4, use_callback=False
        )

    def _define_events(self):
        dvs = [ids.DV_LOT_ID, ids.DV_RECIPE_ID, ids.DV_EVENT_SEQ]
        self.collection_events.update(
            {
                ids.CE_PROCESS_STARTED: secsgem.gem.CollectionEvent(ids.CE_PROCESS_STARTED, "ProcessStarted", dvs),
                ids.CE_PROCESS_COMPLETED: secsgem.gem.CollectionEvent(
                    ids.CE_PROCESS_COMPLETED, "ProcessCompleted", dvs
                ),
                ids.CE_WAFER_COMPLETED: secsgem.gem.CollectionEvent(
                    ids.CE_WAFER_COMPLETED, "WaferCompleted", [*dvs, ids.DV_WAFER_ID]
                ),
                ids.CE_ALARM_OVERTEMP_SET: secsgem.gem.CollectionEvent(
                    ids.CE_ALARM_OVERTEMP_SET, "AlarmOverTempSet", [ids.DV_EVENT_SEQ]
                ),
                ids.CE_ALARM_OVERTEMP_CLEARED: secsgem.gem.CollectionEvent(
                    ids.CE_ALARM_OVERTEMP_CLEARED, "AlarmOverTempCleared", [ids.DV_EVENT_SEQ]
                ),
            }
        )

    def _define_alarms(self):
        self.alarms[ids.ALID_OVERTEMP] = secsgem.gem.Alarm(
            ids.ALID_OVERTEMP,
            "OverTemp",
            "Chamber temperature above MaxChamberTemp",
            self.settings.data_items.ALCD.EQUIPMENT_SAFETY,
            ids.CE_ALARM_OVERTEMP_SET,
            ids.CE_ALARM_OVERTEMP_CLEARED,
        )

    def _define_remote_commands(self):
        # Replace the default START (no params) with one that takes LOT_ID and RECIPE.
        self.remote_commands[ids.RCMD_START] = secsgem.gem.RemoteCommand(
            ids.RCMD_START, "Start", [ids.CP_LOT_ID, ids.CP_RECIPE], ids.CE_CMD_START_DONE
        )

    # ------------------------------------------------------------------ value callbacks

    def on_sv_value_request(self, svid, status_variable):
        values = {
            ids.SV_CHAMBER_TEMP: self.chamber_temp,
            ids.SV_CHAMBER_PRESSURE: self.chamber_pressure,
            ids.SV_PROCESS_STATE: self.process_state,
            ids.SV_WAFERS_PROCESSED: self.wafers_processed,
        }
        return status_variable.value_type(values[status_variable.svid])

    def on_dv_value_request(self, dvid, data_value):
        values = {
            ids.DV_LOT_ID: self.lot_id,
            ids.DV_WAFER_ID: self.wafer_id,
            ids.DV_RECIPE_ID: self.recipe_id,
            ids.DV_EVENT_SEQ: self._event_seq,
        }
        return data_value.value_type(values[data_value.dvid])

    # ------------------------------------------------------------------ remote commands (S2F41)
    # secsgem finds these by name: "_on_rcmd_" + RCMD. It replies S2F42 HCACK=4
    # ("acknowledged, will finish later") *before* calling them, then fires the
    # command's ce_finished event (CE 20 / 21) after they return.

    def _on_rcmd_START(self, LOT_ID="", RECIPE=""):  # noqa: N802, N803
        self.start_lot(LOT_ID, RECIPE)

    def _on_rcmd_STOP(self):  # noqa: N802
        self.complete_lot()

    # ------------------------------------------------------------------ process simulation

    def start_lot(self, lot_id: str, recipe_id: str):
        self.lot_id = lot_id
        self.recipe_id = recipe_id
        self.process_state = "PROCESSING"
        self.trigger_collection_events([ids.CE_PROCESS_STARTED])

    def complete_wafer(self, wafer_id: str):
        self.wafer_id = wafer_id
        self.wafers_processed += 1
        self.trigger_collection_events([ids.CE_WAFER_COMPLETED])

    def complete_lot(self):
        self.process_state = "IDLE"
        self.trigger_collection_events([ids.CE_PROCESS_COMPLETED])

    def set_chamber_temp(self, value: float):
        """Change the temperature and raise / clear the OverTemp alarm against EC MaxChamberTemp."""
        self.chamber_temp = value
        limit = self.equipment_constants[ids.EC_MAX_CHAMBER_TEMP].value
        if value > limit:
            self.set_alarm(ids.ALID_OVERTEMP)
        else:
            self.clear_alarm(ids.ALID_OVERTEMP)

    # ------------------------------------------------------------------ ordered S6F11 sender + spool

    def trigger_collection_events(self, ceids):
        """Queue S6F11 event reports.

        secsgem's own version starts a new thread for every call and reads the values inside
        that thread, so nothing *guarantees* order or that values belong to the right moment
        (in a local run of 200 back-to-back events it happened to stay in order - it is a
        missing guarantee, not an observed bug). It also has no spool: if the link is down the
        report is just lost. Here the report is built right away (values are a snapshot of
        *this* moment), numbered with EventSeq, and put into one FIFO outbox that a single
        sender thread drains.

        If the link is down the reports simply stay in the outbox (this is our "spool") and are
        sent in the original order once the host is communicating again. Real E30 spooling is
        richer (S2F43 selects which streams to spool, S6F23 lets the host request or purge the
        spool) - see README.
        """
        if not isinstance(ceids, list):
            ceids = [ceids]

        with self._outbox_cv:
            for ceid in ceids:
                if isinstance(ceid, secsgem.gem.CollectionEventId):
                    ceid = ceid.value
                link = self.registered_collection_events.get(ceid)
                if link is None or not link.enabled:
                    continue
                self._event_seq += 1
                reports = self._build_collection_event(ceid)
                self._outbox.append((ceid, {"DATAID": self._event_seq, "CEID": ceid, "RPT": reports}))
            self._outbox_cv.notify()

    @property
    def is_communicating(self) -> bool:
        return self.communication_state.current == CommunicationState.COMMUNICATING

    @property
    def spooled_count(self) -> int:
        """Reports waiting to be delivered (queued while offline, or not yet acked)."""
        with self._outbox_cv:
            return len(self._outbox)

    def wait_outbox_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.spooled_count == 0:
                return True
            time.sleep(0.02)
        return False

    def _sender_loop(self):
        while True:
            with self._outbox_cv:
                while not self._stopped and not (self._outbox and self.is_communicating):
                    # wake up periodically to re-check the communication state
                    self._outbox_cv.wait(timeout=0.1)
                if self._stopped:
                    return
                ceid, payload = self._outbox[0]  # peek, remove only after S6F12 arrives

            self.s6f11_send_attempts += 1
            response = self.send_and_waitfor_response(self.stream_function(6, 11)(payload))

            if response is None:
                # T3 reply timeout or link broken: keep it at the head of the queue and retry.
                # This is at-least-once delivery - the host may get the same report twice.
                log.warning("S6F11 CEID=%s DATAID=%s not acknowledged, will resend", ceid, payload["DATAID"])
                time.sleep(self.s6f11_retry_delay)
                continue

            with self._outbox_cv:
                if self._outbox and self._outbox[0][1] is payload:
                    self._outbox.popleft()

    def _on_s01f14(self, handler, message):
        # Both sides may send S1F13 at the same moment (E30 allows it). The second S1F14 then
        # arrives when we are already COMMUNICATING - nothing to do, just don't log it as unexpected.
        return None

    def close(self):
        """Stop the sender thread and the HSMS connection (end of life, not just a disconnect)."""
        with self._outbox_cv:
            self._stopped = True
            self._outbox_cv.notify_all()
        self.disable()


def make_equipment(port: int, address: str = "127.0.0.1", **timeouts) -> EtchToolEquipment:
    settings = FixedHsmsSettings(
        address=address,
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        device_type=secsgem.common.DeviceType.EQUIPMENT,
        **timeouts,
    )
    return EtchToolEquipment(settings)
