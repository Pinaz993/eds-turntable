"""turntable_server behaviour: validation, state, error handling, shutdown, watchdog."""
import ipaddress
import math
import socket
import subprocess
import threading

import pytest


def _client(server, address='127.0.0.1'):
    """A fake Socket.IO client connecting from *address* (the test client sends none by default)."""
    http = server.app.test_client()
    http.environ_base['REMOTE_ADDR'] = address
    return server.socketio.test_client(server.app, flask_test_client=http)


def _send(server, payload):
    """Send a move_command from a fake client and return everything it received."""
    client = _client(server)
    client.get_received()
    try:
        client.emit('move_command', payload)
    except Exception:
        pass  # a handler that raises is part of what some tests check
    received = client.get_received()
    client.disconnect()
    return received


def _names(received):
    return [r['name'] for r in received]


# --- subnet filter ------------------------------------------------------------

def test_client_inside_the_network_connects(server):
    assert _client(server, '127.0.0.5').is_connected()


def test_client_outside_the_network_is_refused(server):
    assert not _client(server, '203.0.113.9').is_connected()


def test_everything_is_refused_until_the_network_is_known(server):
    server.ALLOWED_NETWORK = None
    assert not _client(server).is_connected()


def test_unparseable_client_address_is_refused(server):
    assert not _client(server, 'not-an-ip').is_connected()


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize('payload', [
    None, [], 'text', 5,
    {}, {'degrees': 90}, {'direction': 'CW'},
    {'degrees': True, 'direction': 'CW'},
    {'degrees': 'abc', 'direction': 'CW'},
    {'degrees': 0, 'direction': 'CW'},
    {'degrees': -5, 'direction': 'CW'},
    {'degrees': 360.01, 'direction': 'CW'},
    {'degrees': math.inf, 'direction': 'CW'},
    {'degrees': math.nan, 'direction': 'CW'},
    {'degrees': 90, 'direction': 'cw'},
    {'degrees': 90, 'direction': 'LEFT'},
])
def test_bad_commands_are_rejected_and_nothing_moves(server, payload):
    received = _send(server, payload)
    assert 'move_error' in _names(received)
    assert 'move_started' not in _names(received)
    assert server.tasks == []
    assert not server.bridge.is_busy


@pytest.mark.parametrize('degrees', [360, 0.0001, '45'])
def test_good_command_is_accepted(server, degrees):
    received = _send(server, {'degrees': degrees, 'direction': 'CCW'})
    assert 'move_started' in _names(received)
    assert len(server.tasks) == 1
    assert server.bridge.is_busy


def test_second_command_while_moving_is_refused(server):
    _send(server, {'degrees': 90, 'direction': 'CW'})
    received = _send(server, {'degrees': 90, 'direction': 'CW'})
    assert 'move_error' in _names(received)
    assert len(server.tasks) == 1


def test_simultaneous_commands_only_one_wins(server):
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(_names(_send(server, {'degrees': 10, 'direction': 'CW'})))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(server.tasks) == 1
    assert sum('move_started' in r for r in results) == 1


@pytest.mark.parametrize('flag', ['_in_estop', '_shutting_down'])
def test_commands_refused_when_not_ready(server, flag):
    setattr(server, flag, True)
    assert 'move_error' in _names(_send(server, {'degrees': 90, 'direction': 'CW'}))
    assert server.tasks == []


def test_busy_flag_is_released_if_the_move_cannot_be_started(server, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError('cannot start thread')
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server.socketio, 'start_background_task', boom)
    _send(server, {'degrees': 90, 'direction': 'CW'})
    assert not server.bridge.is_busy
    assert server._current_state()['state'] == 'ready'


# --- LED --------------------------------------------------------------------

def test_rejected_command_flashes_led_when_ready(server):
    _send(server, {'degrees': -1, 'direction': 'CW'})
    assert server.led_calls == ['command_rejected']


@pytest.mark.parametrize('flag', ['_in_estop', '_shutting_down'])
def test_rejected_command_never_hides_a_safety_led(server, flag):
    setattr(server, flag, True)
    _send(server, {'degrees': 90, 'direction': 'CW'})
    assert 'command_rejected' not in server.led_calls


def test_rejected_command_never_hides_an_error_led(server):
    server.on_error('boom')
    server.led_calls.clear()
    _send(server, {'degrees': 90, 'direction': 'CW'})
    assert 'command_rejected' not in server.led_calls


# --- errors -----------------------------------------------------------------

def test_first_error_wins_under_concurrency(server, monkeypatch):
    published = []
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server, '_publish_state', lambda: published.append(1))
    barrier = threading.Barrier(16)

    def fail(i):
        barrier.wait()
        server.on_error(f'error {i}')

    threads = [threading.Thread(target=fail, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert server._system_error.startswith('error ')
    assert len(published) == 1


def test_error_state_reports_recoverability(server):
    server.on_error('x', recoverable=False)
    assert server._current_state() == {'state': 'error', 'message': 'x', 'recoverable': False}


# --- shutdown ---------------------------------------------------------------

def _fake_run(returncode=0, stderr='', raises=None):
    def run(cmd, **kwargs):
        # noinspection PyUnresolvedReferences
        run.cmd = cmd
        # noinspection PyUnresolvedReferences
        run.kwargs = kwargs
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, '', stderr)
    return run


def test_successful_shutdown_stays_in_shutdown_state(server, monkeypatch):
    run = _fake_run()
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server.subprocess, 'run', run)
    server._shutdown_sequence()
    assert server._current_state() == {'state': 'shutdown'}
    # noinspection PyUnresolvedReferences
    assert run.cmd == server.SHUTDOWN_COMMAND
    # A hung sudo must not hold the shutdown thread forever.
    # noinspection PyUnresolvedReferences
    assert run.kwargs['timeout'] == server.SHUTDOWN_COMMAND_TIMEOUT_SEC


@pytest.mark.parametrize('run', [
    _fake_run(returncode=1, stderr='sudo: a password is required'),
    _fake_run(raises=FileNotFoundError('sudo')),
    _fake_run(raises=subprocess.TimeoutExpired('sudo', 30)),
])
def test_failed_shutdown_is_reported_not_left_stuck(server, monkeypatch, run):
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server.subprocess, 'run', run)
    server._shutdown_sequence()
    state = server._current_state()
    assert state['state'] == 'error'
    assert 'shutdown' in state['message'].lower()
    assert not server._shutting_down


def test_shutdown_command_matches_sudoers_entry():
    from pathlib import Path
    import turntable_server
    sudoers = (Path(__file__).parent.parent / 'sudoers.d' / 'turntable-shutdown').read_text()
    rule = [l for l in sudoers.splitlines() if l.startswith('turntable')][0]
    allowed = rule.split('NOPASSWD:')[1].strip()
    # SHUTDOWN_COMMAND is `sudo -n <allowed command...>`
    assert ' '.join(turntable_server.SHUTDOWN_COMMAND[2:]) == allowed


# --- network ----------------------------------------------------------------

def test_network_change_is_picked_up(server, monkeypatch):
    new = ipaddress.IPv4Network('192.168.7.0/24')
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server, '_get_interface_network', lambda _iface='eth0': new)
    assert server._refresh_network() is True
    assert server.ALLOWED_NETWORK == new
    assert server._refresh_network() is False  # unchanged


def test_failed_network_read_keeps_the_old_network(server, monkeypatch):
    before = server.ALLOWED_NETWORK
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(server, '_get_interface_network', lambda _iface='eth0': None)
    assert server._refresh_network() is False
    assert server.ALLOWED_NETWORK == before


# --- watchdog ---------------------------------------------------------------

def test_monitor_health(server):
    assert server._monitor_healthy(now=1e9)               # never started: nothing to judge
    server._monitor_heartbeat = 1000.0
    assert server._monitor_healthy(now=1000.0 + server.MONITOR_STALL_SEC - 1)
    assert not server._monitor_healthy(now=1000.0 + server.MONITOR_STALL_SEC + 1)


def test_sd_notify_without_systemd_is_a_noop(monkeypatch, server):
    monkeypatch.delenv('NOTIFY_SOCKET', raising=False)
    assert server._sd_notify('WATCHDOG=1') is False


def test_sd_notify_reaches_a_notify_socket(monkeypatch, server, tmp_path):
    path = str(tmp_path / 'notify')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(path)
    listener.settimeout(2)
    monkeypatch.setenv('NOTIFY_SOCKET', path)
    assert server._sd_notify('WATCHDOG=1') is True
    assert listener.recv(64) == b'WATCHDOG=1'
    listener.close()


def test_sd_notify_survives_a_dead_socket(monkeypatch, server, tmp_path):
    monkeypatch.setenv('NOTIFY_SOCKET', str(tmp_path / 'nobody-home'))
    assert server._sd_notify('WATCHDOG=1') is False
