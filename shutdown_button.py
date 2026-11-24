#!/usr/bin/env python3
# shutdown_button.py
# Monitors a physical button on GPIO 3 and asks the turntable server to shut the Pi down.
# The request is the word "shutdown" sent over a Unix socket, not a signal, so nothing else
# (systemd, Ctrl-C, a restart) can be mistaken for it. The server then sets the status LED,
# tells connected clients, and halts the system.

import socket
import subprocess
from signal import pause
from gpiozero import Button

CONTROL_SOCKET = '/run/turntable/control.sock'

# Used only when the server can't be reached. Must match sudoers.d/turntable-shutdown, and
# `-n` makes it fail at once instead of waiting for a password nobody can type.
SHUTDOWN_COMMAND = ['sudo', '-n', '/usr/sbin/shutdown', '-h', 'now']

# GPIO 3, physical pin 5
# Button is wired between GPIO 3 and ground (active low)
# Internal pull-up keeps the pin HIGH when button is not pressed
# bounce_time ignores anything shorter than 50 ms: contact bounce, and the sort of electrical
# noise a stepper driver and its long wires can put on an input, must not power the Pi off.
button = Button(3, pull_up=True, bounce_time=0.05)


def shutdown() -> None:
    """
    Asks turntable_server.py to shut the Pi down by sending "shutdown" over the control
    socket. Falls back to a direct shutdown if the server can't be reached (not running,
    or the socket doesn't exist), so the button always works.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(CONTROL_SOCKET)
            s.sendall(b'shutdown\n')
            s.recv(32)  # wait for the server's acknowledgement
    except OSError:
        # Server isn't running — shut down directly.
        try:
            result = subprocess.run(SHUTDOWN_COMMAND, timeout=30)
            if result.returncode:
                print(f"[ERROR] Shutdown command failed (exit status {result.returncode})", flush=True)
        except (OSError, subprocess.SubprocessError) as exc:
            # This runs on the gpiozero event thread. An exception escaping it would end that
            # thread, and the button would silently stop working until the service restarts.
            print(f"[ERROR] Shutdown command failed ({type(exc).__name__}: {exc})", flush=True)

# Register the shutdown function to be called when the button is pressed
button.when_pressed = shutdown

# Keep the script running indefinitely, waiting for button presses
pause()