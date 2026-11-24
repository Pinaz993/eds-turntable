"""
Type stub for the parts of the `linuxcnc` Python module that this project uses.

The real module is a C extension that ships with LinuxCNC on the Pi and cannot be installed
anywhere else. This file exists only so IDEs and type checkers can resolve `import linuxcnc`
on a development machine. Python ignores `.pyi` files at runtime, so it has no effect on the
Pi, and the tests replace the module with a fake (tests/conftest.py).

Add to this file as the code starts using more of the API.
"""

# Raised for NML communication problems, such as LinuxCNC not running.
# The class names below are lowercase because that is what the real module calls them.
# noinspection PyPep8Naming
class error(Exception): ...

# Values of `stat.task_state` and arguments to `command.state()`.
STATE_ESTOP: int
STATE_ESTOP_RESET: int
STATE_ON: int

# Values of `stat.task_mode` and arguments to `command.mode()`.
MODE_MANUAL: int
MODE_AUTO: int
MODE_MDI: int

# Value of `stat.interp_state` when the interpreter has nothing to do.
INTERP_IDLE: int

# Value of `stat.motion_mode` when joints move independently (not teleoperated).
TRAJ_MODE_FREE: int

# Message types returned by `error_channel.poll()`.
NML_ERROR: int
OPERATOR_ERROR: int

# noinspection PyPep8Naming
class stat:
    """A snapshot of machine status. Call `poll()` to refresh it."""
    task_state: int
    task_mode: int
    motion_mode: int
    interp_state: int
    estop: int
    enabled: int
    inpos: bool
    queue: int
    # One entry per joint, nonzero once that joint is homed.
    homed: tuple[int, ...]

    def __init__(self) -> None: ...
    def poll(self) -> None: ...

# noinspection PyPep8Naming
class command:
    def __init__(self) -> None: ...
    def state(self, state: int) -> None: ...
    def mode(self, mode: int) -> None: ...
    def home(self, joint: int) -> None: ...
    def teleop_enable(self, enable: bool) -> None: ...
    def mdi(self, text: str) -> None: ...
    def abort(self) -> None: ...

# noinspection PyPep8Naming
class error_channel:
    def __init__(self) -> None: ...
    # The next pending message as (type, text), or None when there is none.
    def poll(self) -> tuple[int, str] | None: ...
