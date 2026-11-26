"""
What the e-stop monitor does when LinuxCNC goes away or is restarted underneath the server.

Regression tests for on-Pi test 3.3 (`systemctl restart linuxcnc` while idle), which failed two ways:
  * stat.poll() raised "emcStatusBuffer invalid err=5", which was reported as an engine error that
    "clearable with e-stop" even though the e-stop could not be read, so it never cleared;
  * the old connection kept answering "ready" from stale memory, and nothing noticed the restart.
"""
import subprocess

import linuxcnc
import pytest


@pytest.fixture
def mon(server, monkeypatch):
    """The server, with the LinuxCNC-lost bookkeeping reset and its delays removed."""
    for name, value in (('_lcnc_unreachable_since', None), ('_lcnc_lost', False),
                        ('_lcnc_invocation', None), ('_lcnc_invocation_checked', 0.0),
                        ('_lcnc_restart_due', None), ('_restart_requested', False),
                        ('LCNC_LOST_AFTER_SEC', 0), ('LCNC_UNIT_CHECK_SEC', 0), ('LCNC_RESTART_DELAY_SEC', 0)):
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server, '_read_lcnc_invocation', lambda: None)  # default: no systemd to ask

    server.startups, server.reconnects, server.states, server.restarts = [], [], [], []
    monkeypatch.setattr(server.os, 'kill', lambda pid, sig: server.restarts.append((pid, sig)))
    monkeypatch.setattr(server.bridge, 'startup_sequence', lambda: server.startups.append(1))
    monkeypatch.setattr(server.bridge, 'reconnect', lambda min_interval=0.0: server.reconnects.append(1))

    real_publish = server._publish_state
    def recording_publish():
        server.states.append(server._current_state()['state'])
        real_publish()
    monkeypatch.setattr(server, '_publish_state', recording_publish)
    return server


def script_polls(srv, monkeypatch, outcomes):
    """Make stat.poll() fail or succeed as listed ('fail'/'ok'); it succeeds once the list runs out."""
    remaining = list(outcomes)
    def poll():
        if remaining and remaining.pop(0) == 'fail':
            raise linuxcnc.error('emcStatusBuffer invalid err=5')
    monkeypatch.setattr(srv.bridge.status, 'poll', poll)


def run_monitor(srv, monkeypatch, iterations):
    """Run the real monitor loop for a fixed number of passes."""
    passes = []
    def sleep(_):
        passes.append(1)
        if len(passes) >= iterations:
            srv._estop_monitor_running = False
    monkeypatch.setattr(srv.socketio, 'sleep', sleep)
    srv._monitor_estop()


def test_poll_failure_is_not_an_engine_error(mon, monkeypatch):
    script_polls(mon, monkeypatch, ['fail'] * 5)
    run_monitor(mon, monkeypatch, 5)
    assert mon._system_error == ''            # not blamed on the engine, so nothing "clearable with e-stop"
    assert mon._current_state()['state'] == 'booting'
    assert mon.reconnects                     # the dead connection was rebuilt


def test_machine_comes_back_by_itself(mon, monkeypatch):
    script_polls(mon, monkeypatch, ['fail'] * 3)
    run_monitor(mon, monkeypatch, 6)
    assert mon.states[:1] == ['booting']
    assert mon.startups == [1]                # startup re-run exactly once
    assert mon._current_state()['state'] == 'ready'
    assert mon._system_error == ''


def test_a_move_in_progress_is_abandoned(mon, monkeypatch):
    mon.bridge.set_busy()
    script_polls(mon, monkeypatch, ['fail'] * 2)
    run_monitor(mon, monkeypatch, 2)
    assert not mon.bridge.is_busy


def test_service_restart_restarts_the_server(mon, monkeypatch):
    ids = iter(['run-1', 'run-2'])
    monkeypatch.setattr(mon, '_read_lcnc_invocation', lambda: next(ids, 'run-2'))
    run_monitor(mon, monkeypatch, 6)          # every poll succeeds, as against stale shared memory
    assert 'booting' in mon.states            # no longer claims to be ready
    assert mon.restarts == [(mon.os.getpid(), mon.signal.SIGTERM)]   # asked once, via the clean exit
    assert not mon.startups and not mon.reconnects   # no in-process attempt on the old connection
    assert mon._system_error == ''


def test_server_restart_waits_for_the_delay(mon, monkeypatch):
    monkeypatch.setattr(mon, 'LCNC_RESTART_DELAY_SEC', 3600)
    ids = iter(['run-1', 'run-2'])
    monkeypatch.setattr(mon, '_read_lcnc_invocation', lambda: next(ids, 'run-2'))
    run_monitor(mon, monkeypatch, 6)
    assert mon._current_state()['state'] == 'booting'
    assert mon.restarts == []


def test_restart_is_requested_only_once(mon):
    mon._restart_server()
    mon._restart_server()
    assert len(mon.restarts) == 1


def test_no_restart_no_fuss(mon, monkeypatch):
    monkeypatch.setattr(mon, '_read_lcnc_invocation', lambda: 'run-1')
    run_monitor(mon, monkeypatch, 5)
    assert mon.states == [] and not mon.reconnects and not mon.startups


def test_unknown_invocation_is_ignored(mon, monkeypatch):
    run_monitor(mon, monkeypatch, 3)          # systemd can't say: behave as before
    assert mon.states == [] and not mon.startups


@pytest.mark.parametrize('result, expected', [
    (subprocess.CompletedProcess([], 0, stdout='abc123\n'), 'abc123'),
    (subprocess.CompletedProcess([], 0, stdout='\n'), None),     # unit never started
    (subprocess.CompletedProcess([], 1, stdout=''), None),
    (FileNotFoundError(), None),                                  # no systemctl at all
    (subprocess.TimeoutExpired('systemctl', 2), None),
])
def test_read_invocation(server, monkeypatch, result, expected):
    def fake_run(*_a, **_kw):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(server.subprocess, 'run', fake_run)
    assert server._read_lcnc_invocation() == expected
