"""LinuxCNCBridge behavior against the fake machine."""


def test_cw_move_completes(bridge, machine):
    bridge.set_busy()
    bridge.send_move(90, 'CW')
    assert machine.mdi_log == ['G91 G0 A90.0000']
    assert bridge.calls == [('complete',)]
    assert not bridge.is_busy


def test_ccw_is_sent_negative_and_fixed_point(bridge, machine):
    bridge.set_busy()
    bridge.send_move(0.00001, 'CCW')
    # Never exponent notation: G-code can't parse "1e-05".
    assert machine.mdi_log == ['G91 G0 A-0.0000']


def test_stale_error_is_not_blamed_on_a_good_move(bridge, machine):
    # Left behind by something earlier (an e-stop reset, say).
    machine.errors.append((11, 'left over from earlier'))
    bridge.set_busy()
    bridge.send_move(45, 'CW')
    assert bridge.calls == [('complete',)]
    assert machine.errors == []


def test_error_raised_by_the_move_itself_is_reported(bridge, machine):
    machine.on_mdi = lambda _text: machine.errors.append((11, 'joint 0 following error'))
    bridge.set_busy()
    bridge.send_move(45, 'CW')
    assert bridge.calls == [('error', 'joint 0 following error', True)]
    assert not bridge.is_busy


def test_timed_out_move_is_aborted_and_reported(bridge, machine):
    bridge.MOVE_TIMEOUT = 0.05
    machine.move_polls = 10**9  # never finishes
    bridge.set_busy()
    bridge.send_move(45, 'CW')
    assert machine.abort_count == 1
    assert bridge.calls == [('error', 'Move timed out', True)]
    assert not bridge.is_busy


def test_estop_during_move_is_aborted_not_complete(bridge, machine):
    machine.move_polls = 10**9
    machine.on_mdi = lambda _t: setattr(machine, 'estop', True)
    bridge.set_busy()
    bridge.send_move(45, 'CW')
    assert bridge.calls == [('aborted',)]
    assert not bridge.is_busy


def test_drives_dropping_out_without_estop_is_an_error(bridge, machine):
    machine.move_polls = 10**9
    machine.on_mdi = lambda _t: setattr(machine, 'enabled', False)
    bridge.set_busy()
    bridge.send_move(45, 'CW')
    assert len(bridge.calls) == 1 and bridge.calls[0][0] == 'error'
    assert not bridge.is_busy


def test_drain_errors_is_bounded(bridge, machine):
    machine.errors.extend([(11, 'x')] * 500)
    assert bridge.drain_errors() == bridge.MAX_ERROR_DRAIN
