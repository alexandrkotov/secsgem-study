# fabsim: a SECS/GEM study project

A small **GEM equipment simulator** (a pretend single-chamber etch tool) and a **host / cell controller**
that talk over **HSMS** on localhost, built on the open-source Python library
[secsgem](https://github.com/bparzella/secsgem) (v0.3.0) and covered with **pytest integration tests**.

> **Scope, honestly:** this is a study project. I built it to learn SECS/GEM hands-on, not as production code.
> I worked on a wafer fab floor (MES: PROMIS) and I am a test-automation engineer. This repo is where those two meet.

```
 ┌──────────────────────────┐      HSMS (TCP)       ┌────────────────────────────┐
 │  CellControllerHost      │  ── active connect ─▶ │  EtchToolEquipment         │
 │  (GemHostHandler)        │                       │  (GemEquipmentHandler)     │
 │                          │  S1F13 S1F17 S1F3     │  SVs  DVs  ECs             │
 │  define/link/enable      │  S2F33 S2F35 S2F37    │  collection events         │
 │  event reports           │  S2F41 S5F3 ...  ───▶ │  alarm OverTemp            │
 │                          │  ◀─── S6F11 S5F1      │  RCMD START / STOP         │
 │  SQL store / Kafka bridge│                       │  ordered S6F11 outbox+spool│
 └──────────────────────────┘                       └────────────────────────────┘
```

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m fabsim.demo          # happy path, one line per step
python -m fabsim.demo --sml    # same, plus every SECS-II message in SML text form
pytest -q                      # 55 integration tests, under 1.5 minutes
```

The `--sml` log shows each message twice (`>` sent, `<` received) because both sides run in one process.

Optional Kafka round trip (needs Docker):

```bash
docker compose -f docker-compose.kafka.yml up -d
pip install -e ".[dev,kafka]"
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 pytest -m kafka -q
```

## SECS/GEM in five minutes

SECS/GEM is the SEMI standard way for a fab's host software (MES, cell controller, FDC, recipe management) to talk to
process tools. It is a stack of standards:

| Layer | Standard | What it is | In this repo |
|---|---|---|---|
| Transport | **E37 HSMS** | SECS messages over TCP/IP. One side is *active* (connects), the other *passive* (listens). Control messages: Select, Deselect, **Linktest** (heartbeat), Separate, Reject. | host = active, tool = passive |
| (old transport) | E4 SECS-I | The same messages over RS-232 serial. | not used |
| Message format | **E5 SECS-II** | Messages are named **SxFy** (Stream x, Function y). Odd function = primary (request), even = reply. A "W" bit says a reply is wanted. Data is a tree of typed items: `L` list, `A` ASCII, `U1..U8`, `I1..I8`, `F4/F8`, `B` binary, `BOOLEAN`. The text form is called **SML**. | `--sml` demo output |
| Behaviour | **E30 GEM** | Which messages a tool must support and *how it behaves*: state models, events, alarms, remote commands, variables, spooling. | the whole project |

### Main E30 GEM concepts

* **Communication state model.** After HSMS select, one side sends **S1F13 Establish Communications**, the other
  replies **S1F14** (COMMACK = 0) and both are *COMMUNICATING*. Either side may start it.
* **Control state model.** `EQUIPMENT_OFFLINE`, `ATTEMPT_ONLINE`, `HOST_OFFLINE`, then `ONLINE_LOCAL` or `ONLINE_REMOTE`.
  The host asks with **S1F17 Request Online** (ONLACK 0 = ok, 2 = already online) and **S1F15 Request Offline**.
  In *REMOTE* the host may control the tool; in *LOCAL* the operator does.
* **Variables.**
  **SV** (status variables, live values, read any time with **S1F3/S1F4**, names via **S1F11**);
  **DV** (data values, only meaningful at an event, e.g. LotID, WaferID);
  **EC** (equipment constants, settings: read **S2F13**, write **S2F15**, the tool answers EAC 0 ok / 3 out of range).
* **Collection events and dynamic event reports.** The host chooses what data it wants with each event:
  1. **S2F33 Define Report**: RPTID = list of VIDs (DRACK 0 ok, 3 RPTID exists, 4 unknown VID)
  2. **S2F35 Link Event Report**: CEID → RPTIDs (LRACK 4 unknown CEID, 5 unknown RPTID)
  3. **S2F37 Enable/Disable Event Report** (ERACK)
  4. When the event happens the tool sends **S6F11 Event Report** with the values; the host answers **S6F12**.
* **Remote commands.** **S2F41 Host Command Send** (RCMD + name/value params). **S2F42** HCACK: 0 done,
  1 invalid command, 3 invalid parameter, **4 = accepted, completion will be signalled by an event**.
* **Alarms.** The host enables alarms with **S5F3**, lists them with **S5F5/S5F7**. When an alarm sets or clears
  the tool sends **S5F1** (ALCD bit 8 = set/clear, low bits = category), the host answers **S5F2**.
  Alarms usually also have their own collection events.
* **Spooling.** If the link is down, the tool keeps messages (e.g. S6F11) in a spool and delivers them later
  (E30 adds **S2F43** to choose which streams to spool, **S6F23** for the host to request or purge the spool).
* **Errors.** Stream 9: e.g. **S9F5** unrecognized function, **S9F9** transaction timer (T3) timeout.
* **HSMS timers.** **T3** reply timeout, **T5** connect separation (wait between reconnect attempts),
  **T6** control transaction timeout, **T7** not-selected timeout, **T8** network inter-character timeout.

## The simulated tool

All IDs are in [`src/fabsim/ids.py`](src/fabsim/ids.py).

| Kind | ID | Name |
|---|---|---|
| SV | 2001 / 2002 / 2003 / 2004 | ChamberTemp (F4 °C), ChamberPressure, ProcessState (IDLE/PROCESSING), WafersProcessed |
| DV | 3001 / 3002 / 3003 / 3004 | LotID, WaferID, RecipeID, **EventSeq** (counter in every report) |
| EC | 4001 | MaxChamberTemp (20..400, default 250) |
| CE | 100 / 101 / 102 | ProcessStarted, ProcessCompleted, WaferCompleted |
| CE | 200 / 201 | AlarmOverTempSet / Cleared |
| CE | 20 / 21 | START / STOP command done |
| Alarm | 5001 | OverTemp: ChamberTemp > EC MaxChamberTemp |
| RCMD | START (LOT_ID, RECIPE), STOP | |

## What the tests cover

| File | What |
|---|---|
| `test_communication.py` | HSMS selected, S1F13/F14, S1F1/F2, control state: HOST_OFFLINE → S1F17 → ONLINE_REMOTE, S1F15, local/remote switch |
| `test_event_reports.py` | S2F33/35/37 → S6F11 happy path, values are a snapshot per event, disable, all the DRACK/LRACK/ERACK error codes, delete reports |
| `test_remote_commands.py` | START with params → HCACK 4 + ProcessStarted + CE 20, STOP, unknown command (1), bad parameter (3), HCACK 4 really means "finish later" (deterministic race test) |
| `test_alarms_and_variables.py` | S1F3, S1F11, S2F13, S2F15 (+ out of range, unknown EC), S5F5, S5F3 enable, S5F1 set/clear, alarm CE, alarm limit follows the EC |
| `test_hsms_link.py` | Linktest, **T3 timeout**, S9F5 for an unsupported message, host drop + reconnect, tool restart + host reconnect after T5 |
| `test_delivery.py` | 50-event burst keeps order, **spooling** while the host is down then in-order delivery, **lost S6F12 → resend → duplicate** |
| `test_bridge.py` | sequence validator unit tests, bridge to an in-memory sink, dedup on/off, optional real-Kafka round trip |
| `test_datastore.py` | events and alarms land in SQL with lot/wafer columns, lot summary + WIP query, open alarms, resent event stored once, EventSeq gap query |

Tests use short timers (T3 = 2 s, T5 = 1 s) so failure cases run in seconds.

## Things the tests found (in secsgem 0.3.0, and in my own code)

1. **HSMS disconnect does not reach the GEM layer.** `GemHandler.on_connection_closed()` exists but nothing calls it,
   so after the TCP link drops the communication state stays `COMMUNICATING` and the tool's control state does not go
   offline. Fix: one line in each handler, `self.protocol.events.disconnected += ...`.
   Without it, 3 tests fail (both reconnect tests and the spool test).
2. **Shutdown races.** The passive side's `disable()` can spin forever (it closes the listen socket under `accept()`),
   and the active side can leave a non-daemon reconnect thread behind, so the Python process never exits.
   Found because pytest hung after "all passed". Worked around in [`lifecycle.py`](src/fabsim/lifecycle.py).
3. **Reconnect race: "connected but never selected".** On a new connection `HsmsProtocol` starts its receive threads
   *before* it sets the HSMS state to CONNECTED. If the peer's Select.req arrives in that gap, the select fails, the
   host never gets Select.rsp and the link stays not-selected. Also, every reconnect leaves the old dispatcher thread
   running, so two threads read the same message queue. Found as a flaky reconnect test (it failed in 1 of 2 full runs, and again on the first isolated rerun);
   fixed in [`hsms_fixes.py`](src/fabsim/hsms_fixes.py), then 12/12 runs of that test and 3/3 full runs passed.
4. **My own bug, caught by CI: treating HCACK 4 as "done".** The tool replies S2F42 HCACK=4 *before* it runs the
   command. A test sent START and immediately simulated wafers; on the slower GitHub runner (Python 3.13 job) the
   wafers were reported before ProcessStarted. Locally it always passed; pinned to one CPU core it failed the same way.
   Fix: wait for the completion event, as a real host must. `test_hcack_4_means_finish_later_wait_for_completion_event`
   slows the command down so this race is reproduced on every machine.
5. **No delivery guarantees for S6F11.** The stock `trigger_collection_events` starts one thread per call,
   reads values inside that thread, does not retry, and has no spool. In a local run of 200 back-to-back events it
   happened to stay in order, so this is a *missing guarantee*, not an observed bug. The simulator replaces it with a
   FIFO outbox: values snapshotted at trigger time, one sender thread, resend after T3, and keep-while-offline (a simple spool).

## Delivery guarantees and the Kafka bridge

Every report carries **EventSeq** (DV 3004). That makes the S6F11 stream checkable the same way you check a CDC
stream (e.g. Debezium → Kafka): [`validate_sequence`](src/fabsim/bridge.py) reports **gaps** (lost events),
**duplicates** and **out-of-order** items.

* S6F11 delivery is **at-least-once**: if S6F12 is lost, the tool resends after T3 and the host sees the report twice.
  `test_lost_ack_causes_resend_and_a_duplicate` shows it.
* `EventBridge(dedup=True)` drops repeats by EventSeq before publishing, so the topic gets each event once.
* `KafkaSink` uses the equipment ID as the message key (one partition per tool, so Kafka keeps per-tool order)
  and an idempotent producer.

## Data collection and SQL reporting

[`datastore.py`](src/fabsim/datastore.py) is the "data collection" part of a cell controller: `attach_collector()`
sets up the event reports on the tool (S2F33/35/37) and writes every S6F11 report and S5F1 alarm into SQL tables
(SQLite here, plain enough SQL to move to SQL Server or Oracle).

* `tool_event`: one row per report: equipment, EventSeq, event name, lot, wafer, recipe, chamber temp, all values as JSON.
  `UNIQUE (equipment_id, seq)` makes the load idempotent: a resent S6F11 does not create a second row.
* `alarm_log`: every set / clear with its time.

Queries (all in the module, as readable SQL):

| Query | Question it answers |
|---|---|
| `LOT_SUMMARY_SQL` | per lot: recipe, start / end time, wafers done, COMPLETE or IN_PROCESS |
| `WIP_SQL` | which lots are on the tool right now |
| `OPEN_ALARMS_SQL` | which alarms are set right now (latest row per ALID, `ROW_NUMBER()`) |
| `SEQ_GAPS_SQL` | data quality: which EventSeq ranges never reached the table (`LAG()`) |

## Simplifications (on purpose)

* Spooling is "keep while offline, send in order on reconnect". No S2F43/S6F23, no spool size limit or overwrite policy.
* The tool sends S6F11 even when the control state is not ONLINE (real GEM would not).
* No process programs (S7), no terminal services (S10), no GEM300 (E40/E87/E90/E94 carriers, jobs, substrate tracking).
* One host, one tool, localhost only. Real cell controllers manage many tools and survive restarts (persistent spool, state).

## Project layout

```
src/fabsim/
  ids.py          all SV/DV/EC/CE/ALID/RCMD numbers
  equipment.py    EtchToolEquipment: the simulated tool (passive HSMS)
  host.py         CellControllerHost: the host (active HSMS), ack-code helpers, event/alarm logs, fault injection
  bridge.py       S6F11 -> message bus bridge, sequence validator
  datastore.py    data collection into SQL + WIP / lot / alarm / data-quality queries
  hsms_fixes.py   HsmsProtocol subclass that fixes the reconnect races
  lifecycle.py    start/stop helpers + workarounds for the shutdown races
  demo.py         python -m fabsim.demo
tests/            pytest integration tests (real TCP on 127.0.0.1)
```

## Links

* secsgem docs: https://secsgem.readthedocs.io/
* SEMI standards (paid): E5 SECS-II, E30 GEM, E37 HSMS, plus E4 SECS-I and the GEM300 family.
