# lcnc_bridge.py
# Manages the connection to LinuxCNC via the Python linuxcnc module and exposes a clean 
# interface for sending movement commands and polling machine status.

import linuxcnc
import time
from typing import Callable


class LinuxCNCBridge:
    """
    A class to manage communication with LinuxCNC via the linuxcnc Python library.

    Handles the connection lifecycle, startup sequence, move commands, and
    status polling. Exposes callbacks for the Flask-SocketIO layer to hook
    into for WebSocket event emission.

    Attributes
    ----------
    POLL_INTERVAL : float
        How many seconds to wait between status polls
    STARTUP_TIMEOUT : float
        How many seconds to wait for the startup sequence to complete
    MOVE_TIMEOUT : float
        How many seconds to wait for a move to complete
    on_ready : Callable
        Called when the machine has completed startup and is ready for commands.
    on_move_complete : Callable
        Called when a move command has finished executing.
    on_error : Callable
        Called when an unrecoverable error occurs.
    """

    POLL_INTERVAL = 0.1
    STARTUP_TIMEOUT = 10.0
    MOVE_TIMEOUT = 120.0

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
        ('C4', 261.63, 706.40),
        ('D4', 293.66, 792.88),
        ('E4', 329.63, 890.00),
        ('F4', 349.23, 942.92),
        ('G4', 392.00, 1058.40),
        ('A4', 440.00, 1188.00),
        ('B4', 493.88, 1333.48),
        ('C5', 523.25, 1412.78),
    )

    def __init__(
            self,
            on_ready: Callable,
            on_move_complete: Callable,
            on_error: Callable,
    ) -> None:
        self.on_ready = on_ready
        self.on_move_complete = on_move_complete
        self.on_error = on_error
        self._command = linuxcnc.command()
        self._status = linuxcnc.stat()
        self._errors = linuxcnc.error_channel()
        self._busy = False

    # End __init__()

    ### Utilities ###

    def _poll_until(self, condition: Callable, timeout: float | None = None) -> bool:
        """
        Polls LinuxCNC until a condition is met or a timeout expires.

        Parameters
        ----------
        condition : Callable
            A condition that returns True when the desired state is reached.
        timeout : float, optional
            Seconds to wait before giving up. Defaults to STARTUP_TIMEOUT.

        Returns
        -------
        bool
            True if the condition was met. False if the timeout expired.
        """
        # Do we have a specified timeout?
        if timeout is None:
            # If not, use the default
            timeout = self.STARTUP_TIMEOUT
        # Grab the current time.
        start = time.time()
        # Loop. Every iteration, grab the current time and compare with the start time. If the time
        # elapsed since the start time is less than the timeout period, do the following:
        while time.time() - start < timeout:
            # Update status information from LinuxCNC
            self._status.poll()
            # Check the condition based on that information
            if condition():
                # If the condition comes up true, we're done. Return True to let the caller know.
                return True
            # Otherwise, wait for a small amount of time, then loop again.
            time.sleep(self.POLL_INTERVAL)
        # End while
        # Since the loop ended without returning, the timeout expired. Let the caller know
        return False

    # End _poll_until()

    def _wait_for_move(self) -> bool:
        """
        Waits for the currently executing move to finish.

        Polls first for the move to start (so we don't race past it on a fast machine),
        then for it to complete. Uses MOVE_TIMEOUT for the completion wait.

        Returns
        -------
        bool
            True if the move completed cleanly. False if either wait timed out.
        """
        # Wait until the move starts before polling for completion
        move_start = lambda : not self._status.inpos or self._status.queue > 0
        if not self._poll_until(move_start, timeout=5.0):
            # Move might have completed between polls — not necessarily an error, keep going.
            pass
        # End if

        # Wait until the move is fully finished
        return self._poll_until(
            lambda: (
                    # Has the system reached the intended position
                    self._status.inpos and
                    # Is the system in an idle state
                    self._status.interp_state == linuxcnc.INTERP_IDLE and
                    # Has the system's queue run dry of movements?
                    self._status.queue == 0
            ),
            timeout=self.MOVE_TIMEOUT,
        )
    # End _wait_for_move()

    ### Startup ###

    def startup_sequence(self) -> None:
        """
        Runs the full startup sequence to bring the machine to an MDI ready state.

        The ESTOP cycle is needed because of a quirk of LinuxCNC's state machine:
        it can get stuck in a quasi ESTOP state. To bring it out of said state, one must put the
        machine into ESTOP, and then bring it out via ESTOP_RESET before the machine state can be
        changed.

        Raises
        ------
        RuntimeError
            If any startup step fails to complete within STARTUP_TIMEOUT seconds.
        """
        # Drain any stale errors from previous sessions. While there are errors to be grabbed:
        while self._errors.poll():
            # Do nothing with the grabbed error.
            pass
        # End while

        # Cycle estop state, in case of strangeness
        # Set ESTOP on
        self._command.state(linuxcnc.STATE_ESTOP)
        # Wait a bit
        time.sleep(0.2)
        # Reset ESTOP
        self._command.state(linuxcnc.STATE_ESTOP_RESET)
        # Keep polling until the estop is reported off. If you run out of time...
        if not self._poll_until(lambda: self._status.estop == 0):
            # ... pass an error up the chain.
            raise RuntimeError('Failed to clear ESTOP before timeout')
        # End if
        # ESTOP is guaranteed to be off.

        # Try to enable the machine.
        self._command.state(linuxcnc.STATE_ON)
        # Poll until the machine is reported enabled. If you run out of time...
        if not self._poll_until(lambda: self._status.enabled):
            # ... pass an error up the chain.
            raise RuntimeError('Failed to enable machine before timeout')
        # End if
        # Machine is guaranteed to be enabled.

        # Switch the machine to manual mode.
        self._command.mode(linuxcnc.MODE_MANUAL)
        # I think you get the picture.
        if not self._poll_until(lambda: self._status.task_mode == linuxcnc.MODE_MANUAL):
            raise RuntimeError('Failed to set machine to manual mode before timeout')
        # End if
        # Home joint #0
        self._command.home(0)
        if not self._poll_until(lambda: self._status.homed[0], timeout=15.0):
            raise RuntimeError('Failed to home A axis (joint 0) before timeout')
        # End if
        # Axis A is guaranteed to be homed, and so can be moved.

        # Switch the machine to MDI mode
        self._command.mode(linuxcnc.MODE_MDI)
        if not self._poll_until(lambda: self._status.task_mode == linuxcnc.MODE_MDI):
            raise RuntimeError('Failed to set machine to MDI mode before timeout')
        # End if
        # Machine is now ready to accept MDI commands.

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
        for _name, _hz, feed in self._READY_CHIME:
            # G91 = incremental mode, G1 = feed-rate move, F in deg/min
            self._command.mdi(f'G91 G1 A{self.NOTE_DEGREES:.1f} F{feed:.2f}')
            # Wait for this note to finish before sending the next one.
            # If it times out we still continue — a clipped chime beats a hung startup.
            self._wait_for_move()
        # End for

        # Return the table to its home position at maximum speed (G90 = absolute, G0 = rapid).
        self._command.mdi('G90 G0 A0')
        self._wait_for_move()
    # End _play_ready_chime()

    def startup(self) -> None:
        """
        Runs the full startup sequence to bring the machine to a state where it can accept MDI
        commands. Intended to be called once from a background thread at app startup.
        Calls on_ready() on success, or on_error() if an unrecoverable error occurs.
        """
        try:
            # Get the machine ready for MDI commands
            self.startup_sequence()
            # Play the ready chime to give Ed an audible confirmation that the machine is up.
            self._play_ready_chime()
            # If that didn't throw an error, we're ready to move
            self.on_ready()
        except Exception as e:
            # Pass the error up the chain to be handled.
            self.on_error(str(e))

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
        # Add a sign to the degrees, if necessary.
        signed = degrees if direction == 'CW' else -degrees
        # Send the command
        self._command.mdi(f'G91 G0 A{signed}')

        # Wait until the move is finished
        if not self._wait_for_move():
            self.on_error('Move timed out')
            return
        # End if

        # Check for errors
        error = self._errors.poll()
        # Do we have an error?
        if error:
            # Separate out the error type and the error message
            error_type, error_msg = error
            # Is this an error we care to pass up the chain?
            if error_type in (linuxcnc.NML_ERROR, linuxcnc.OPERATOR_ERROR):
                self.on_error(error_msg)
                return
            # End if
        # End if

        # Move is done!
        self._busy = False
        self.on_move_complete()

    # End send_move()

    ### State ###

    @property
    def is_busy(self) -> bool:
        """
        Returns True if a move command is currently being executed.
        """
        return self._busy

    # End is_busy()

    def set_busy(self) -> None:
        """
        Marks the bridge as busy. Called by turntable_server.py before starting a move
        in a background thread.
        """
        self._busy = True
    # End set_busy()