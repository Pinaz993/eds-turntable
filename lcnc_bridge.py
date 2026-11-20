# lcnc_bridge.py
# Manages the connection to LinuxCNC via the Python linuxcnc module and exposes a clean
# interface for sending movement commands and polling machine status.

import linuxcnc
import time
from typing import Callable, cast
import threading


class SafeStatus:
    """An explicit, thread-safe wrapper for linuxcnc.stat."""
    def __init__(self, lock):
        self._stat = linuxcnc.stat()
        self._lock = lock

    def poll(self) -> None:
        with self._lock:
            self._stat.poll()

    # Explicit properties give PyCharm perfect type visibility
    @property
    def inpos(self) -> bool:
        return self._stat.inpos

    @property
    def queue(self) -> int:
        return self._stat.queue

    @property
    def interp_state(self) -> int:
        return self._stat.interp_state

    @property
    def estop(self) -> bool:
        return cast(bool, self._stat.estop)

    @property
    def task_state(self) -> int:
        return self._stat.task_state

    @property
    def enabled(self) -> bool:
        return self._stat.enabled

    @property
    def task_mode(self) -> int:
        return self._stat.task_mode

    @property
    def homed(self):
        return self._stat.homed

    @property
    def motion_mode(self) -> int:
        return self._stat.motion_mode


class SafeCommand:
    """An explicit, thread-safe wrapper for linuxcnc.command."""
    def __init__(self, lock):
        self._cmd = linuxcnc.command()
        self._lock = lock

    def state(self, state_type: int) -> None:
        with self._lock:
            self._cmd.state(state_type)

    def mode(self, mode_type: int) -> None:
        with self._lock:
            self._cmd.mode(mode_type)

    def home(self, joint: int) -> None:
        with self._lock:
            self._cmd.home(joint)

    def mdi(self, command_string: str) -> None:
        with self._lock:
            self._cmd.mdi(command_string)

    def teleop_enable(self, enabled: bool) -> None:
        with self._lock:
            self._cmd.teleop_enable(enabled)


class SafeErrors:
    """An explicit, thread-safe wrapper for linuxcnc.error_channel."""
    def __init__(self, lock):
        self._errors = linuxcnc.error_channel()
        self._lock = lock

    def poll(self):
        with self._lock:
            return self._errors.poll()

class LinuxCNCBridge:
    """
    A class to manage communication with LinuxCNC via the linuxcnc Python library.

    Handles the connection lifecycle, startup sequence, move commands, and
    status polling. Exposes callbacks for the Flask-SocketIO layer to hook
    into for WebSocket event emission.

    All LinuxCNC objects are wrapped in the Safe* classes above to serialize NML bus access
    across all threads. The semaphore is exposed as bridge.lock so
    external code (e.g. the e-stop monitor in turntable_server.py) can wrap its own
    linuxcnc objects with the same lock, ensuring system-wide NML serialization.

    Attributes
    ----------
    POLL_INTERVAL : float
        How many seconds to wait between status polls.
    STARTUP_TIMEOUT : float
        How many seconds to wait for each startup step to complete.
    MOVE_TIMEOUT : float
        How many seconds to wait for a move to complete.
    on_ready : Callable
        Called when the machine has completed startup and is ready for commands.
    on_move_complete : Callable
        Called when a move command has finished executing.
    on_error : Callable
        Called when an error occurs that stops the machine accepting commands.
    on_move_aborted : Callable, optional
        Called when a move is cut short by an e-stop or loss of drive power. NOT followed
        by on_move_complete.
    _lock : threading.Lock
        The shared NML access semaphore. Expose to external code that needs to
        wrap its own linuxcnc objects with the same serialization guarantee.
    """

    POLL_INTERVAL   = 0.1
    STARTUP_TIMEOUT = 30.0
    MOVE_TIMEOUT    = 120.0

    # C-major scale, C4 to C5. Each entry is (note_name, target_hz, feed_rate_deg_per_min).
    #
    # The stepper motor vibrates at a frequency equal to its step rate. Since the axis
    # scale is 22.2222 steps/degree, the audible frequency at a given feed rate F (deg/min) is:
    #
    #   freq = (F / 60) * scale  =>  F = (freq / scale) * 60
    #
    # Each note is played by issuing a G1 (feed-rate-controlled) incremental move of
    # NOTE_DEGREES degrees at the corresponding feed rate. 12 degrees was chosen because
    # it is the minimum distance at which every note in the scale — including the highest,
    # C5 — completes its acceleration ramp and reaches constant velocity before decelerating.
    # This produces a clean, discernible pitch rather than just a frequency sweep.
    # (Worst case: C5 needs ~5.54° to ramp up and ~5.54° to ramp down, leaving ~0.91° flat.)
    #
    # After the arpeggio the table has advanced 96° (8 × 12°) CW; a final G90 G0 A0
    # returns it to the home position at maximum speed.
    NOTE_DEGREES = 12.0
    _READY_CHIME = (
        # (name,   Hz,     deg/min)
        ('C4', 261.63,  706.40),
        ('D4', 293.66,  792.88),
        ('E4', 329.63,  890.00),
        ('F4', 349.23,  942.92),
        ('G4', 392.00, 1058.40),
        ('A4', 440.00, 1188.00),
        ('B4', 493.88, 1333.48),
        ('C5', 523.25, 1412.78),
    )

    def __init__(
        self,
        on_ready:         Callable,
        on_move_complete: Callable,
        on_error:         Callable,
        on_move_aborted:  Callable | None = None,
    ) -> None:
        self.on_ready         = on_ready
        self.on_move_complete = on_move_complete
        self.on_error         = on_error
        # Called when a move is cut short by an e-stop or loss of drive power.
        # Optional: with none set, an aborted move simply ends quietly.
        self.on_move_aborted  = on_move_aborted
        # Single semaphore shared across all NML objects in this bridge, and exposed
        # as bridge.lock for external callers (e.g. the e-stop monitor thread).
        self._lock     = threading.Lock()
        self.command = SafeCommand(self._lock)
        self.status = SafeStatus(self._lock)
        self.errors = SafeErrors(self._lock)
        self._busy    = False
    # End __init__()

    ### Utilities ###

    def poll_until(self, condition: Callable, timeout: float=STARTUP_TIMEOUT) -> bool:
        """
        Polls LinuxCNC until a condition is met or a timeout expires.

        Each call to self.status.poll() acquires and releases the NML semaphore
        automatically via the Safe* wrappers. The sleep between polls happens outside the
        lock so other threads can access the NML bus while we wait.

        Parameters
        ----------
        condition : Callable
            A zero-argument callable that returns True when the desired state is reached.
            Reads stat attributes directly — no lock needed, they are already cached
            by the preceding poll().
        timeout : float, optional
            Seconds to wait before giving up. Defaults to STARTUP_TIMEOUT.

        Returns
        -------
        bool
            True if the condition was met within the timeout. False otherwise.
        """
        # Record the current time
        start = time.time()
        # While we still have time
        while time.time() - start < timeout:
            # The poll might throw an error, be prepared.
            try:
                # Try to grab the current state of the machine
                self.status.poll()
            # If it does error out
            except (SystemError, linuxcnc.error) as exc:
                # With proper NML serialization these should not occur, but log them
                # as warnings rather than crashing if they somehow do.
                print(f"[WARNING] poll_until: unexpected poll error ({type(exc).__name__}: {exc}), retrying...", flush=True)
                # Wait for a little bit
                time.sleep(self.POLL_INTERVAL)
                # Back to the top of the loop.
                continue
            # Evaluate the condition, and see if the loop should end
            if condition():
                return True
            # Wait a little bit
            time.sleep(self.POLL_INTERVAL)
            # Back to the top of the loop
        # If we made it here, we ran out of time.
        return False
    # End poll_until()

    # Possible outcomes of _wait_for_move().
    OUTCOME_COMPLETE  = 'complete'
    OUTCOME_ABORTED   = 'aborted'
    OUTCOME_TIMED_OUT = 'timeout'

    def _machine_stopped(self) -> bool:
        """True if the e-stop is engaged or the drives are off (which kills any move in progress)."""
        return bool(self.status.estop) or not self.status.enabled
    # End _machine_stopped()

    def _wait_for_move(self) -> str:
        """
        Waits for the currently executing move to finish.

        Polls first for the move to start (so we do not race past it on a fast machine),
        then for it to end. An e-stop or loss of drive power ends the wait immediately:
        the move is dead, and what LinuxCNC reports afterwards says nothing about whether
        it finished.

        Returns
        -------
        str
            OUTCOME_COMPLETE if the move finished cleanly, OUTCOME_ABORTED if the machine was
            e-stopped or powered off, OUTCOME_TIMED_OUT if the wait ran out.
        """
        # How to ask if we are currently executing a move (or if it has been cut off)
        move_started = lambda: (not self.status.inpos or self.status.queue > 0
                                or self._machine_stopped())
        # Wait until the move starts before polling for completion, or up to 5 seconds.
        # Running out of time here is not an error: a short move may already be over.
        self.poll_until(move_started, timeout=5.0)

        # How to ask if the move is over, one way or another.
        move_over = lambda: (
                self._machine_stopped() or (
                    self.status.inpos and
                    self.status.interp_state == linuxcnc.INTERP_IDLE and
                    self.status.queue == 0
                )
            )
        if not self.poll_until(move_over, timeout=self.MOVE_TIMEOUT):
            return self.OUTCOME_TIMED_OUT
        return self.OUTCOME_ABORTED if self._machine_stopped() else self.OUTCOME_COMPLETE
    # End _wait_for_move()

    ### Startup ###

    def startup_sequence(self) -> None:
        """
        Runs the full startup sequence to bring the machine to an MDI-ready state.

        Waits for the LinuxCNC NML bus to be fully ready before issuing any commands,
        then steps through: ESTOP reset → machine enable → manual mode → home → MDI mode.

        The former ESTOP cycle (force STATE_ESTOP before STATE_ESTOP_RESET) has been
        removed. It was a workaround for torn-read corruption caused by concurrent NML
        access. With the Safe* wrappers serializing all NML traffic, torn reads no longer occur
        and the state machine stays consistent without the brute-force cycle.

        Raises
        ------
        RuntimeError
            If any startup step fails to complete within STARTUP_TIMEOUT seconds.
        """
        print("Starting LinuxCNC startup sequence", flush=True)

        # Wait for the NML bus to be fully ready before touching it.
        # LinuxCNC's task module may still be initializing after the process starts —
        # task_state > 0 confirms the task controller is running and NML is live.
        print("Waiting for LinuxCNC NML bus...", flush=True)
        start = time.time()
        while True:
            try:
                self.status.poll()
                if self.status.task_state > 0: break
            except (SystemError, linuxcnc.error):
                pass
            if time.time() - start > self.STARTUP_TIMEOUT:
                raise RuntimeError('LinuxCNC NML bus did not become ready before timeout')
            time.sleep(self.POLL_INTERVAL)
        print(f"NML bus ready (task_state={self.status.task_state})", flush=True)

        # Drain any stale errors left over from a previous session.
        print("Draining stale errors...", flush=True)
        while self.errors.poll():
            pass

        # Clear ESTOP. LinuxCNC starts in STATE_ESTOP; this moves it to STATE_ESTOP_RESET.
        print("Clearing ESTOP...", flush=True)
        self.command.state(linuxcnc.STATE_ESTOP_RESET)
        if not self.poll_until(lambda: self.status.estop == 0, timeout=5.0):
            # Instead of crashing, check if we are still hard-estopped by hardware
            if self.status.estop:
                print("[INFO] Hardware ESTOP is currently active. Pausing startup sequence until cleared.", flush=True)
                # Exit early but cleanly. We don't turn on drives or go to MDI yet.
                return
            else:
                raise RuntimeError('Failed to clear ESTOP due to an unexpected system error')
        print("ESTOP cleared")

        # Enable the machine (power on drives/amplifiers).
        print("Enabling machine...", flush=True)
        self.command.state(linuxcnc.STATE_ON)
        if not self.poll_until(lambda: self.status.enabled):
            raise RuntimeError('Failed to enable machine before timeout')
        print("Machine enabled", flush=True)

        # Switch to manual mode so we can home the axis.
        print("Switching to manual mode...", flush=True)
        self.command.mode(linuxcnc.MODE_MANUAL)
        if not self.poll_until(lambda: self.status.task_mode == linuxcnc.MODE_MANUAL):
            raise RuntimeError('Failed to set machine to manual mode before timeout')
        print("Manual mode confirmed", flush=True)

        # LinuxCNC only accepts a home command in joint mode. A joint that is still homed from
        # before an e-stop leaves motion in teleop mode, and `home` is then refused ("must be in
        # joint mode to home") with nothing but a message in the error channel. Ask for joint
        # mode first. Not fatal if it doesn't take: the home below reports the real problem.
        print("Switching to joint mode...", flush=True)
        self.command.teleop_enable(False)
        if not self.poll_until(lambda: self.status.motion_mode == linuxcnc.TRAJ_MODE_FREE, timeout=3.0):
            print("[WARNING] Motion did not report joint mode; trying to home anyway.", flush=True)

        # Home joint 0 (the A axis).
        print("Homing joint 0...", flush=True)
        self.command.home(0)
        if not self.poll_until(lambda: self.status.homed[0], timeout=15.0):
            raise RuntimeError('Failed to home A axis (joint 0) before timeout')
        print("Joint 0 homed", flush=True)

        # Switch to MDI mode — the machine is now ready to accept G-code commands.
        print("Switching to MDI mode...", flush=True)
        self.command.mode(linuxcnc.MODE_MDI)
        if not self.poll_until(lambda: self.status.task_mode == linuxcnc.MODE_MDI):
            raise RuntimeError('Failed to set machine to MDI mode before timeout')
        print("MDI mode confirmed, machine ready for commands", flush=True)
    # End startup_sequence()

    def _play_ready_chime(self) -> None:
        """
        Plays an ascending C-major arpeggio (C4→C5) by driving the stepper motor at
        step rates that fall within the human-audible range.

        Each note is a G91 G1 (incremental, feed-rate-controlled) move of NOTE_DEGREES
        degrees at the feed rate that produces the target step frequency. After all eight
        notes the table has advanced 96° CW; a final G90 G0 A0 returns it home.

        Silently skips any note whose move fails to complete, so a stuck or slow note
        does not abort the whole chime. Does not raise; errors here are non-fatal.
        """
        print("Playing ready chime", flush=True)
        for _name, _hz, feed in self._READY_CHIME:
            # G91 = incremental mode, G1 = feed-rate move, F in deg/min
            self.command.mdi(f'G91 G1 A{self.NOTE_DEGREES:.1f} F{feed:.2f}')
            # Wait for this note to finish before sending the next one.
            # If it times out we still continue — a clipped chime beats a hung startup.
            self._wait_for_move()

        # Return the table to its home position at maximum speed (G90 = absolute, G0 = rapid).
        self.command.mdi('G90 G0 A0')
        self._wait_for_move()
    # End _play_ready_chime()

    def startup(self) -> None:
        """
        Runs the full startup sequence to bring the machine to a state where it can accept
        MDI commands. Intended to be called once from a background thread at startup.
        Calls on_ready() on success, or on_error() if an unrecoverable error occurs.
        """
        try:
            self.startup_sequence()
            # self._play_ready_chime()
            self.on_ready()
        except Exception as exc:
            self.on_error(str(exc))
    # End startup()

    ### Movement ###

    def send_move(self, degrees: float, direction: str) -> None:
        """
        Sends a relative MDI move command to LinuxCNC and polls for completion.
        Intended to be called from a background thread via start_background_task().

        Parameters
        ----------
        degrees : float
            The number of degrees to move. Must be positive.
        direction : str
            The direction of movement. Must be 'CW' or 'CCW'.
            CCW moves are negated before sending.
        """
        signed = degrees if direction == 'CW' else -degrees
        # Fixed-point on purpose: Python would write tiny values as e.g. 1e-05, which G-code can't parse.
        self.command.mdi(f'G91 G0 A{signed:.4f}')

        result = self._wait_for_move()
        if result == self.OUTCOME_TIMED_OUT:
            self.on_error('Move timed out')
            return
        if result == self.OUTCOME_ABORTED:
            # Cut short by an e-stop. Don't read the error channel: whatever is in it is e-stop
            # noise, and the recovery sequence drains it. Don't report completion either.
            self._busy = False
            if self.on_move_aborted:
                self.on_move_aborted()
            return

        error = self.errors.poll()
        if error:
            error_type, error_msg = error
            if error_type in (linuxcnc.NML_ERROR, linuxcnc.OPERATOR_ERROR):
                self.on_error(error_msg)
                return

        self._busy = False
        self.on_move_complete()
    # End send_move()

    ### State ###

    @property
    def is_busy(self) -> bool:
        """Returns True if a move command is currently being executed."""
        return self._busy
    # End is_busy

    def clear_busy(self) -> None:
        """
        Marks the bridge as not busy. Called by turntable_server.py when an e-stop kills
        whatever move was running, so a stuck flag can't outlive the move.
        """
        self._busy = False
    # End clear_busy()

    def set_busy(self) -> None:
        """
        Marks the bridge as busy. Called by turntable_server.py before starting a move
        in a background thread.
        """
        self._busy = True
    # End set_busy()