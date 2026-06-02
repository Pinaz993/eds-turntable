# led.py
# Parameters and methods for colors and flashing timers of the status LED.

from gpiozero import RGBLED
from threading import Timer
from typing import Callable

class StatusLED:
    """
    A class to represent a status LED for the turntable.
    The turntable system has several states that need to be communicated to the user
    even if there is no client connected to the web server. This LED communicates this
    information.

    This class is responsible for communicating the status, not for determining what
    status to display.

    Attributes
    ---------
    status_settings : dict
        A list of all statuses with their associated on colors, off colors, on times, 
        off times, and expiration times. If an LED is supposed to be solid, not flashing,
        on_time will be None, and set_state will call self.led.color() instead of 
        self.led.blink(). If an LED state is not supposed to expire, the expiration_time will
        be None, and set_status will skip setting a callback.

    led : gpiozero.RGBLED
        The interface for controlling an RGB LED through GPIO pins. Uses pin 19 for red,
        pin 20 for green, and pin 21 for blue. Chosen, because these were far away from other
        signaling pins, to reduce chance of PWM-borne cross-talk.

    """
    
    status_settings = {
            'booting': { # The system is starting up.
                'on_color': (1, 0, 0), # Red
                'off_color': None, # No blink 
                'on_time': None,
                'off_time': None,
                'expr_time': None, # Doesn't expire automatically.
                },
            'ready': { # The system is ready to execute a command.
                'on_color': (0, 1, 0), # Green
                'off_color': None, # No blink
                'on_time': None,
                'off_time': None,
                'expr_time': None, # Doesn't expire automatically.
                },
            'moving': { # The system is currently executing a command.
                'on_color': (0, 0, 1), # Blue
                'off_color': None, # No blink
                'on_time': None,
                'off_time': None,
                'expr_time': None, # Doesn't expire automatically
                },
            'system_error': { # There is a problem with the system. Unrecoverable, needs restart.
                'on_color': (1, 0, 0), # Red
                'off_color': (0, 0, 0), # Off
                'on_time': 0.25, # On and off,
                'off_time': 0.25, # twice a second
                'expr_time': None, # Doesn't expire automatically.
                },
            'command_rejected': {
                'on_color': (1, 0.5, 0), # Yellow
                'off_color': (0, 0, 0), # Off
                'on_time': 0.25, # On and off,
                'off_time': 0.25, # twice a second
                'expr_time': 2, # Expires after two seconds
                },
            'estop_active': {
                'on_color': (1, 0, 0), # Red
                'off_color': (1, 0.5, 0),# Yellow
                'on_time': 0.5, # Back and forth,
                'off_time': 0.5, # once per second
                'expr_time': None # Doesn't expire automatically.
                },
            'shutdown': {
                'on_color': (1, 0, 0), # Red
                'off_color': (0, 0, 1), # Blue
                'on_time': 0.5, # Back and forth,
                'off_time': 0.5, # twice per second
                'expr_time': None, # doesn't expire automatically.
                },
            }
    
    led: RGBLED = RGBLED(red=19, green=20, blue=21)


    def set_status(self, status: str, recalculate: Callable | None = None) -> None:
        """
        Sets the LED state based on the states defined in self.status_states.
        
        Will error if state is not one of the states described above.

        Parameters
        ----------
        status : str
            A named status found as an entry in the list above, representing a status
            the machine can have that needs to be displayed to the user.
        recalculate : Callable
            A callback for recalculating the machine's status after a status expires.
        """
        
        # Grab the setting for the status, or None if it doesn't exist in the dict.
        setting = self.status_settings.get(status)
        if not setting: # Is `status` a valid status?
            raise KeyError(f"Invalid status: {status}")
        # `setting` is valid.
        if setting['on_time']: # Is the LED supposed to blink?
            # Yes. Get the LED to use all four LED settings from the dict.
            self.led.blink(on_color=setting['on_color'], off_color=setting['off_color'],
                            on_time=setting['on_time'], off_time=setting['off_time'])
        else: # No, the LED is not supposed to blink.
            self.led.color = setting['on_color']
        if setting['expr_time']: # Is the status supposed to expire?
            if recalculate: # Is recalculate a valid Callable?
                # Yes. Set a timer to recalculate machine status, with the proper time.
                Timer(setting['expr_time'], recalculate).start()
            else: # Tried to set an expiring status without a callback. Raise an exception.
                raise ValueError(f"Invalid callback for recalculating machine status: {recalculate}")

