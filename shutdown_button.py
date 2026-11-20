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

# GPIO 3, physical pin 5
# Button is wired between GPIO 3 and ground (active low)
# Internal pull-up keeps the pin HIGH when button is not pressed
button = Button(3, pull_up=True)


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
        subprocess.run(['sudo', 'shutdown', '-h', 'now'])

# Register the shutdown function to be called when the button is pressed
button.when_pressed = shutdown

# Keep the script running indefinitely, waiting for button presses
pause()