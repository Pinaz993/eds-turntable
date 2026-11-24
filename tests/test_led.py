"""StatusLED: one expiry timer at a time, and a bad call must not touch the LED."""
import threading
import time
from typing import Protocol

import pytest


class _RecordingLED(Protocol):
    """What tests/conftest.py's FakeRGBLED records. The real RGBLED has neither attribute."""
    pins: tuple[int, int, int]
    history: list


def _fake_led(status_led) -> _RecordingLED:
    return status_led.led


@pytest.fixture
def status_led(monkeypatch):
    from led import StatusLED
    # A TypedDict is a plain dict at runtime, but the IDE's type checker doesn't see it as a Mapping.
    # noinspection PyTypeChecker
    monkeypatch.setitem(StatusLED.status_settings['command_rejected'], 'expr_time', 0.2)
    led = StatusLED()
    _fake_led(led).history.clear()
    return led


def test_led_is_wired_to_the_documented_pins(status_led):
    # GPIO 19 red, 20 green, 21 blue (see description.md).
    assert _fake_led(status_led).pins == (19, 20, 21)


def test_blinking_status_passes_all_four_blink_settings(status_led):
    status_led.set_status('estop_active')
    assert _fake_led(status_led).history[-1] == ('blink', (1, 0, 0), (1, 0.5, 0), 0.5, 0.5)


def test_newer_status_cancels_pending_expiry(status_led):
    fired = threading.Event()
    count = []

    def recalc():
        count.append(1)
        fired.set()

    status_led.set_status('command_rejected', recalculate=recalc)
    time.sleep(0.1)
    status_led.set_status('command_rejected', recalculate=recalc)  # a second rejected command
    time.sleep(0.15)
    # 0.25 s after the first: its timer would have fired by now if it had not been canceled.
    assert count == []
    assert fired.wait(1.0)
    time.sleep(0.1)
    assert count == [1]


def test_replacing_with_a_steady_status_cancels_expiry(status_led):
    count = []
    status_led.set_status('command_rejected', recalculate=lambda: count.append(1))
    status_led.set_status('ready')
    time.sleep(0.35)
    assert count == []


def test_expiring_status_without_callback_leaves_led_alone(status_led):
    before = list(_fake_led(status_led).history)
    with pytest.raises(ValueError):
        status_led.set_status('command_rejected')
    assert _fake_led(status_led).history == before


def test_unknown_status_is_rejected(status_led):
    with pytest.raises(KeyError):
        status_led.set_status('nonsense')
