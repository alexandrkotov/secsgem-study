"""Fixtures: a real equipment simulator and a real host talking HSMS over localhost TCP."""

from __future__ import annotations

import dataclasses
import time

import pytest

from fabsim.equipment import EtchToolEquipment, make_equipment
from fabsim.host import CellControllerHost, make_host
from fabsim.lifecycle import free_port, stop_pair, wait_listening

# Short timeouts so failure tests run in seconds, not minutes.
# Defaults in secsgem: T3 reply = 45 s, T5 connect separation = 10 s, T6 control = 5 s.
FAST_TIMEOUTS = {"t3": 2.0, "t5": 1.0, "t6": 2.0}


@dataclasses.dataclass
class Link:
    equipment: EtchToolEquipment
    host: CellControllerHost
    port: int


def wait_until(condition, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


@pytest.fixture
def connected():
    """Equipment + host after HSMS select and S1F13/S1F14 - but still HOST_OFFLINE (no S1F17 yet)."""
    port = free_port()
    equipment = make_equipment(port, **FAST_TIMEOUTS)
    host = make_host(port, **FAST_TIMEOUTS)
    equipment.enable()  # passive side listens first
    assert wait_listening(equipment)
    host.enable()  # active side connects, sends Select.req, then both sides send S1F13
    assert host.waitfor_communicating(10), "host never reached COMMUNICATING"
    assert equipment.waitfor_communicating(10), "equipment never reached COMMUNICATING"

    yield Link(equipment, host, port)

    stop_pair(equipment, host)


@pytest.fixture
def online(connected):
    """Same as `connected`, plus S1F17 Request Online accepted -> equipment is ONLINE / REMOTE."""
    assert connected.host.go_online() == 0  # ONLACK 0 = accepted
    return connected


