"""Start / stop helpers for a host + equipment pair.

secsgem 0.3.0 has two shutdown races that made test runs hang:

* passive side (TcpServerConnection): disable() closes the listen socket under a thread that
  may be inside accept(). The thread dies with EBADF before it resets its stop flag, and
  disable() then waits for that flag forever.
* active side (TcpClientConnection): if the peer drops first, the client starts a reconnect
  thread. If disable() runs just before that, nobody stops the new thread - it is not a
  daemon thread, so the Python process never exits.

So: stop the host (active) first, then let the tool's listen thread exit by itself (it checks
its stop flag every select timeout, 0.5 s) before calling disable(). Every disable() also has
a time limit, so a test run can never hang on teardown.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def disable_with_timeout(handler, timeout: float = 5.0, close: bool = False) -> bool:
    action = handler.close if close and hasattr(handler, "close") else handler.disable
    worker = threading.Thread(target=action, name=f"disable_{type(handler).__name__}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        log.warning("%s.disable() did not return in %.1fs (known secsgem race), abandoning", handler, timeout)
        return False
    return True


def stop_reconnect_thread(handler) -> None:
    """Ask a leftover TcpClientConnection reconnect thread to exit (it checks this flag every 0.2 s)."""
    connection = handler.protocol._connection  # noqa: SLF001 - working around a library race
    thread = getattr(connection, "connection_thread", None)
    if thread is not None and thread.is_alive() and not connection.enabled:
        connection.stop_connection_thread = True
        thread.join(2.0)


def stop_listener(handler, timeout: float = 2.0) -> None:
    """Stop a TcpServerConnection listen thread the gentle way (flag only, no socket close)."""
    connection = handler.protocol._connection  # noqa: SLF001 - working around a library race
    thread = getattr(connection, "_server_thread", None)
    if thread is None or not thread.is_alive():
        return
    connection._stop_server_thread = True  # noqa: SLF001
    thread.join(timeout)


def wait_listening(handler, timeout: float = 2.0) -> bool:
    """Wait until a passive handler's listen socket is bound, so the first connect attempt succeeds."""
    connection = handler.protocol._connection  # noqa: SLF001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = getattr(connection, "_server_sock", None)
        if sock is not None and sock.fileno() != -1:
            time.sleep(0.05)  # bind() -> listen() are back to back, give it a moment
            return True
        time.sleep(0.01)
    return False


def stop_pair(equipment, host) -> None:
    disable_with_timeout(host)
    stop_reconnect_thread(host)
    # The tool notices the drop on its own receiver thread and then restarts its listen thread.
    # Wait for that, otherwise the restarted (non-daemon) listener outlives the test.
    deadline = time.monotonic() + 2.0
    while equipment.protocol._connected and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)
    stop_listener(equipment)
    disable_with_timeout(equipment, close=True)
    stop_listener(equipment)
