"""
Test harness: runs the real server, bridge and LED code against fake `linuxcnc` and `gpiozero`
modules, so the suite needs no Pi, no LinuxCNC and no GPIO.

The fakes are installed in sys.modules before anything under test is imported.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fake LinuxCNC
# ---------------------------------------------------------------------------

class FakeMachine:
    """The 'machine' behind the fake linuxcnc module. Tests poke it directly."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.estop = False
        self.enabled = True
        self.task_state = 1
        self.task_mode = 3            # MODE_MDI
        self.motion_mode = 1          # TRAJ_MODE_FREE
        self.homed = [1]
        self.inpos = True
        self.queue = 0
        self.interp_state = 1         # INTERP_IDLE
        self.errors = []              # messages waiting in the error channel: (type, text)
        self.mdi_log = []             # every MDI command received
        self.abort_count = 0
        self.move_polls = 3           # how many stat polls a move stays "in progress" for
        self._moving_polls_left = 0
        self.on_mdi = None            # optional hook: called with the command string


MACHINE = FakeMachine()


def _build_fake_linuxcnc() -> types.ModuleType:
    mod = types.ModuleType('linuxcnc')

    class error(Exception):
        pass

    class stat:
        def __init__(self):
            self.__dict__['_m'] = MACHINE

        def poll(self):
            m = self._m
            if m._moving_polls_left > 0:
                m._moving_polls_left -= 1
                m.inpos = False
                m.interp_state = 2
                if m._moving_polls_left == 0:
                    m.inpos, m.interp_state = True, 1

        def __getattr__(self, name):
            return getattr(self.__dict__['_m'], name)

    class command:
        def state(self, s): pass
        def mode(self, m): pass
        def home(self, j): pass
        def teleop_enable(self, e): pass

        def mdi(self, text):
            MACHINE.mdi_log.append(text)
            MACHINE._moving_polls_left = MACHINE.move_polls
            MACHINE.inpos = False
            if MACHINE.on_mdi:
                MACHINE.on_mdi(text)

        def abort(self):
            MACHINE.abort_count += 1
            MACHINE._moving_polls_left = 0
            MACHINE.inpos, MACHINE.interp_state = True, 1

    class error_channel:
        def poll(self):
            return MACHINE.errors.pop(0) if MACHINE.errors else None

    mod.error = error
    mod.stat, mod.command, mod.error_channel = stat, command, error_channel
    mod.STATE_ESTOP, mod.STATE_ESTOP_RESET, mod.STATE_ON = 1, 2, 4
    mod.MODE_MANUAL, mod.MODE_AUTO, mod.MODE_MDI = 1, 2, 3
    mod.INTERP_IDLE = 1
    mod.TRAJ_MODE_FREE = 1
    mod.NML_ERROR, mod.OPERATOR_ERROR = 1, 11
    return mod


# ---------------------------------------------------------------------------
# Fake gpiozero
# ---------------------------------------------------------------------------

class FakeRGBLED:
    """Records what the LED was told to do. `history` holds ('color', rgb) / ('blink', on_color)."""
    instances = []

    def __init__(self, red, green, blue):
        self.history = []
        FakeRGBLED.instances.append(self)

    @property
    def color(self):
        return self.history[-1][1] if self.history else None

    @color.setter
    def color(self, value):
        self.history.append(('color', value))

    def blink(self, on_color, off_color, on_time, off_time):
        self.history.append(('blink', on_color))


def _build_fake_gpiozero() -> types.ModuleType:
    mod = types.ModuleType('gpiozero')
    mod.RGBLED = FakeRGBLED
    mod.Button = object  # shutdown_button.py is not imported by the tests
    return mod


sys.modules['linuxcnc'] = _build_fake_linuxcnc()
sys.modules['gpiozero'] = _build_fake_gpiozero()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def machine():
    MACHINE.reset()
    return MACHINE


@pytest.fixture
def bridge(machine):
    """A real LinuxCNCBridge on the fake machine, with fast polling and recorded callbacks."""
    from lcnc_bridge import LinuxCNCBridge

    calls = []
    b = LinuxCNCBridge(
        on_ready=lambda: calls.append(('ready',)),
        on_move_complete=lambda: calls.append(('complete',)),
        on_error=lambda msg, recoverable=True: calls.append(('error', msg, recoverable)),
        on_move_aborted=lambda: calls.append(('aborted',)),
    )
    b.POLL_INTERVAL = 0.001
    b.calls = calls
    return b


@pytest.fixture
def server(machine, monkeypatch):
    """
    The real turntable_server module, wired to a fake machine, in a clean 'ready' state.
    Background tasks are not started; tests call the pieces they care about.
    """
    import ipaddress
    import turntable_server as srv
    from lcnc_bridge import LinuxCNCBridge

    srv._system_error = ''
    srv._error_recoverable = True
    srv._booted = True
    srv._in_estop = False
    srv._shutting_down = False
    srv._monitor_heartbeat = None
    srv.ALLOWED_NETWORK = ipaddress.IPv4Network('127.0.0.0/8')

    b = LinuxCNCBridge(
        on_ready=srv.on_ready,
        on_move_complete=srv.on_move_complete,
        on_error=srv.on_error,
        on_move_aborted=srv.on_move_aborted,
    )
    b.POLL_INTERVAL = 0.001
    srv.bridge = b

    # Record LED requests instead of running them.
    srv.led_calls = []
    monkeypatch.setattr(srv.led, 'set_status',
                        lambda status, recalculate=None: srv.led_calls.append(status))
    # Record background tasks instead of spawning threads.
    srv.tasks = []
    monkeypatch.setattr(srv.socketio, 'start_background_task',
                        lambda fn, *a, **kw: srv.tasks.append((fn, a)))
    monkeypatch.setattr(srv.socketio, 'sleep', lambda *_: None)
    return srv
