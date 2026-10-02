"""Fixes for two reconnect races in secsgem 0.3.0's HsmsProtocol (found by the reconnect tests).

1. `_on_connected` starts the receive/dispatch threads *before* it moves the HSMS state to
   CONNECTED. If the peer's Select.req is already in the socket, it is handled while the state is
   still NOT_CONNECTED, the select transition fails, the peer never gets Select.rsp, and the link
   stays "connected but not selected" forever. Fix: change the state first, then start threads.

2. `_on_disconnected` stops the receiver thread but never the dispatcher thread. After each
   reconnect one more dispatcher thread reads from the same queue, so two threads can handle
   incoming messages at the same time. Fix: stop and join the old dispatcher on disconnect.
"""

from __future__ import annotations

import threading
import typing

import secsgem.hsms
from secsgem.hsms.protocol import HsmsProtocol


class FixedHsmsProtocol(HsmsProtocol):
    def _on_connected(self, _: dict[str, typing.Any]):
        self._connected = True
        self._connection_state.connect()  # moved up: state first ...
        self._thread.start()  # ... then start handling incoming blocks
        self.events.fire("connected", {"connection": self})

    def _on_disconnected(self, data: dict[str, typing.Any]):
        super()._on_disconnected(data)
        dispatcher_runner = self._thread
        dispatcher = dispatcher_runner._dispatcher_thread  # noqa: SLF001
        if dispatcher is not None and dispatcher.is_alive() and dispatcher is not threading.current_thread():
            dispatcher_runner._stop_dispatcher_thread = True  # noqa: SLF001
            dispatcher_runner._dispatcher_thread_trigger.set()  # noqa: SLF001
            dispatcher.join(1.0)


class FixedHsmsSettings(secsgem.hsms.HsmsSettings):
    """HsmsSettings that builds FixedHsmsProtocol instead of HsmsProtocol."""

    def create_protocol(self):
        return FixedHsmsProtocol(self)
