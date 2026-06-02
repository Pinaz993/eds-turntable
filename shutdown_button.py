#!/usr/bin/env python3
# shutdown_button.py
# Monitors a physical button on GPIO 3 and initiates a clean system shutdown
# when it is pressed. Sets the status LED to the shutdown state before
# initiating shutdown so the user knows the button press was registered.

import os
import signal
import subprocess
from gpiozero import Button


# GPIO 3, physical pin 5
# Button is wired between GPIO 3 and ground (active low)
# Internal pull-up keeps the pin HIGH when button is not pressed
button = Button(3, pull_up=True)


def shutdown() -> None:
    """
    Finds turntable_server.py's PID and sends it SIGTERM, which triggers its _handle_shutdown
    handler — that sets the LED, broadcasts to clients, and exits cleanly.
    Falls back to a direct shutdown if turntable_server.py isn't running.
    """
    # Prepare for the server to not be running
    try:
        # Look for the server, so we can send it a kill signal
        pid = int(subprocess.check_output(['pgrep', '-f', 'turntable_server.py']))
        os.kill(pid, signal.SIGTERM)
    except (subprocess.CalledProcessError, ValueError):
        # turntable_server.py isn't running — shut down directly.
        subprocess.run(['sudo', 'shutdown', '-h', 'now'])

# Register the shutdown function to be called when the button is pressed
button.when_pressed = shutdown

# Keep the script running indefinitely, waiting for button presses
from signal import pause
pause()
