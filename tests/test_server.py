import copy
import queue
import sys
from unittest.mock import MagicMock

# nuxbt's package __init__ pulls in dbus; mock it for headless tests.
if 'dbus' not in sys.modules:
    sys.modules['dbus'] = MagicMock()

# nuxbt.controller.server imports fcntl, which is not available on Windows.
if 'fcntl' not in sys.modules:
    sys.modules['fcntl'] = MagicMock()

# nuxbt.nuxbt forces 'fork' start method, which is unavailable on Windows.
import multiprocessing
multiprocessing.set_start_method = MagicMock()

# nuxbt.agent imports PyGObject's GLib, which may not be installed on Windows.
if 'gi' not in sys.modules:
    sys.modules['gi'] = MagicMock()
    sys.modules['gi.repository'] = MagicMock()
    sys.modules['gi.repository.GLib'] = MagicMock()

from nuxbt.controller.server import ControllerServer
from nuxbt.controller.controller import ControllerTypes
from nuxbt.controller.input import InputParser, DIRECT_INPUT_IDLE_PACKET
from nuxbt.controller.protocol import ControllerProtocol


class DummyServer:
    """Minimal stand-in for ControllerServer so _sync_controller_input can be
    unit-tested without spinning up Bluetooth/BlueZ."""
    pass


def _make_server():
    proto = ControllerProtocol(ControllerTypes.PRO_CONTROLLER, "00:11:22:33:44:55")
    server = DummyServer()
    server.input = InputParser(proto)
    server.task_queue = queue.Queue()
    server.state = {}
    return server


def _packet_with_a(pressed):
    pkt = copy.deepcopy(DIRECT_INPUT_IDLE_PACKET)
    pkt["A"] = pressed
    return pkt


def test_sync_uses_shared_state_as_authoritative_source():
    """Direct input queue events are wakeup hints, but shared state is the
    authoritative source. If the queue and shared state disagree, shared
    state wins."""
    server = _make_server()

    # Queue claims A is pressed, but shared state already moved on to idle.
    server.task_queue.put({"type": "direct", "input": _packet_with_a(True)})
    server.state["direct_input"] = copy.deepcopy(DIRECT_INPUT_IDLE_PACKET)

    ControllerServer._sync_controller_input(server)

    assert not server.input.active_input_queued()
    assert server.task_queue.empty()


def test_sync_reads_shared_state_on_direct_event():
    """When a direct input event arrives, the controller wakes up and reads
    the latest input from shared state."""
    server = _make_server()

    pressed = _packet_with_a(True)
    server.task_queue.put({"type": "direct", "input": pressed})
    server.state["direct_input"] = pressed

    ControllerServer._sync_controller_input(server)

    assert server.input.active_input_queued()


def test_sync_reads_shared_state_while_active():
    """If the controller believes a button/stick is held but no queue event
    arrived, it still polls shared state to recover from a dropped release."""
    server = _make_server()

    # Simulate a stale held input (release queue event was lost).
    server.input.set_controller_input(_packet_with_a(True))
    assert server.input.active_input_queued()

    # Meanwhile the main process updated shared state to idle.
    server.state["direct_input"] = copy.deepcopy(DIRECT_INPUT_IDLE_PACKET)

    ControllerServer._sync_controller_input(server)

    assert not server.input.active_input_queued()


def test_sync_skips_shared_state_when_idle_and_no_event():
    """When controller_input is idle and no direct event arrived, avoid the
    Manager dict read to keep idle cycles cheap."""
    server = _make_server()
    server.input.set_controller_input(copy.deepcopy(DIRECT_INPUT_IDLE_PACKET))
    # Intentionally invalid shared state; if it were read it would corrupt
    # controller_input. Because we are idle with no event, it must be ignored.
    server.state["direct_input"] = {"should_not_be_read": True}

    ControllerServer._sync_controller_input(server)

    assert not server.input.active_input_queued()
    assert server.input.controller_input == DIRECT_INPUT_IDLE_PACKET


def test_sync_still_processes_macro_events():
    """Macro events on the queue are still drained and processed."""
    server = _make_server()
    server.task_queue.put({"type": "macro", "macro": "A 0.1s", "macro_id": "abc"})

    ControllerServer._sync_controller_input(server)

    assert server.input.macro_buffer
    assert server.task_queue.empty()
