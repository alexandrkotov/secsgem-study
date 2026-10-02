"""Data collection: store what the host receives (S6F11 events, S5F1 alarms) in SQL tables,
then answer typical Manufacturing IT questions with plain SQL - WIP by lot, wafers per lot,
open alarms, and data-quality checks (gaps / duplicates in the event stream).

SQLite (stdlib) keeps it dependency-free; the SQL is standard enough to move to SQL Server
or Oracle with small changes (window functions, CASE, GROUP BY).
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import threading
import typing

from . import ids

if typing.TYPE_CHECKING:
    from .host import CellControllerHost, ReceivedAlarm, ReceivedEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id  TEXT    NOT NULL,
    seq           INTEGER,            -- DV EventSeq from the tool
    ceid          INTEGER NOT NULL,
    event_name    TEXT    NOT NULL,
    lot_id        TEXT,
    wafer_id      TEXT,
    recipe_id     TEXT,
    chamber_temp  REAL,
    values_json   TEXT    NOT NULL,   -- every VID/value of the report, for anything not in a column
    received_at   TEXT    NOT NULL,
    UNIQUE (equipment_id, seq)        -- a resent S6F11 (lost S6F12) cannot create a second row
);

CREATE TABLE IF NOT EXISTS alarm_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id  TEXT    NOT NULL,
    alid          INTEGER NOT NULL,
    is_set        INTEGER NOT NULL,   -- 1 = set, 0 = cleared
    alcd          INTEGER NOT NULL,
    altx          TEXT    NOT NULL,
    received_at   TEXT    NOT NULL
);
"""

# One row per lot: recipe, start / end time, wafers done, and a status for WIP views.
LOT_SUMMARY_SQL = """
SELECT
    lot_id,
    MAX(recipe_id)                                                   AS recipe_id,
    MIN(CASE WHEN event_name = 'ProcessStarted'   THEN received_at END) AS started_at,
    MAX(CASE WHEN event_name = 'ProcessCompleted' THEN received_at END) AS completed_at,
    SUM(CASE WHEN event_name = 'WaferCompleted'   THEN 1 ELSE 0 END)    AS wafers_done,
    CASE WHEN SUM(event_name = 'ProcessCompleted') > 0 THEN 'COMPLETE' ELSE 'IN_PROCESS' END AS status
FROM tool_event
WHERE equipment_id = :equipment_id AND lot_id IS NOT NULL AND lot_id <> ''
GROUP BY lot_id
ORDER BY started_at
"""

# WIP on the tool = lots that started but have no ProcessCompleted yet.
WIP_SQL = f"""
SELECT lot_id, recipe_id, started_at, wafers_done
FROM ({LOT_SUMMARY_SQL})
WHERE status = 'IN_PROCESS'
"""

# Alarms that are set right now: the latest row per ALID says "set".
OPEN_ALARMS_SQL = """
SELECT alid, altx, received_at AS set_at
FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY alid ORDER BY id DESC) AS rn
    FROM alarm_log
    WHERE equipment_id = :equipment_id
)
WHERE rn = 1 AND is_set = 1
ORDER BY alid
"""

# Data quality: holes in EventSeq = events the tool sent that never reached the table.
SEQ_GAPS_SQL = """
SELECT prev_seq + 1 AS first_missing, seq - 1 AS last_missing
FROM (
    SELECT seq, LAG(seq) OVER (ORDER BY seq) AS prev_seq
    FROM tool_event
    WHERE equipment_id = :equipment_id AND seq IS NOT NULL
)
WHERE seq - prev_seq > 1
ORDER BY first_missing
"""


class EventStore:
    """Thread-safe SQLite store. Use ':memory:' for tests or a file path for a real run."""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.duplicates_ignored = 0

    def insert_event(self, equipment_id: str, event: ReceivedEvent) -> bool:
        """Insert one report. Returns False if it was a duplicate (same equipment + EventSeq)."""
        values = event.values
        row = {
            "equipment_id": equipment_id,
            "seq": event.seq,
            "ceid": event.ceid,
            "event_name": ids.CE_NAMES.get(event.ceid, f"CE{event.ceid}"),
            "lot_id": values.get(ids.DV_LOT_ID),
            "wafer_id": values.get(ids.DV_WAFER_ID),
            "recipe_id": values.get(ids.DV_RECIPE_ID),
            "chamber_temp": values.get(ids.SV_CHAMBER_TEMP),
            "values_json": json.dumps({str(k): v for k, v in values.items()}),
            "received_at": _now(),
        }
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO tool_event
                   (equipment_id, seq, ceid, event_name, lot_id, wafer_id, recipe_id, chamber_temp,
                    values_json, received_at)
                   VALUES (:equipment_id, :seq, :ceid, :event_name, :lot_id, :wafer_id, :recipe_id,
                           :chamber_temp, :values_json, :received_at)""",
                row,
            )
            if cur.rowcount == 0:
                self.duplicates_ignored += 1
                return False
            return True

    def insert_alarm(self, equipment_id: str, alarm: ReceivedAlarm) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO alarm_log (equipment_id, alid, is_set, alcd, altx, received_at) VALUES (?, ?, ?, ?, ?, ?)",
                (equipment_id, alarm.alid, int(alarm.is_set), alarm.alcd, alarm.altx, _now()),
            )

    def query(self, sql: str, **params) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def lot_summary(self, equipment_id: str) -> list[dict]:
        return self.query(LOT_SUMMARY_SQL, equipment_id=equipment_id)

    def wip(self, equipment_id: str) -> list[dict]:
        return self.query(WIP_SQL, equipment_id=equipment_id)

    def open_alarms(self, equipment_id: str) -> list[dict]:
        return self.query(OPEN_ALARMS_SQL, equipment_id=equipment_id)

    def seq_gaps(self, equipment_id: str) -> list[dict]:
        return self.query(SEQ_GAPS_SQL, equipment_id=equipment_id)

    def close(self) -> None:
        self._conn.close()


# Report layout the collector needs: the same 5 VIDs on every process event.
COLLECTION_VIDS = [ids.DV_EVENT_SEQ, ids.DV_LOT_ID, ids.DV_RECIPE_ID, ids.DV_WAFER_ID, ids.SV_CHAMBER_TEMP]
COLLECTION_EVENTS = [ids.CE_PROCESS_STARTED, ids.CE_WAFER_COMPLETED, ids.CE_PROCESS_COMPLETED]


def attach_collector(host: CellControllerHost, store: EventStore, equipment_id: str, first_rptid: int = 900) -> None:
    """Configure event reports on the tool (S2F33/35/37) and store everything the host receives.

    Run it again after the tool restarts: a tool may lose its report setup on restart, and then
    the host just stops getting data - a classic 'tool stopped reporting' incident.
    """
    host.add_event_listener(lambda event: store.insert_event(equipment_id, event))
    host.add_alarm_listener(lambda alarm: store.insert_alarm(equipment_id, alarm))
    for offset, ceid in enumerate(COLLECTION_EVENTS):
        acks = host.setup_event_report(ceid, first_rptid + offset, COLLECTION_VIDS)
        if acks != (0, 0, 0):
            raise RuntimeError(f"event report setup for CEID {ceid} failed: DRACK/LRACK/ERACK = {acks}")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
