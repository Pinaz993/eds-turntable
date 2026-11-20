# turntable_server.py
# Runs in Flask-SocketIO's plain `threading` mode: every connection, background task and
# timer is a real OS thread, so ordinary blocking calls (time.sleep, LinuxCNC polls) only
# block their own thread. State shared between threads is guarded by the locks below.
import fcntl
import ipaddress
import math
import os
import secrets
import signal
import socket
import struct
import subprocess
import threading
import traceback
import linuxcnc
from flask import Flask, render_template, request, abort
from flask_socketio import SocketIO, emit
from lcnc_bridge import LinuxCNCBridge
from led import StatusLED


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How many times to retry getting the interface network before giving up.
NETWORK_RETRY_ATTEMPTS = 10
# How long to wait between retries, in seconds.
NETWORK_RETRY_DELAY_SEC = 3

SERVER_HOST = '0.0.0.0'
SERVER_PORT = 80

# The largest rotation a single command may request, in degrees.
MAX_MOVE_DEGREES = 360.0

# Control socket. Sending the word "shutdown" here powers off the Pi; nothing else does.
# The directory is created by systemd (RuntimeDirectory=turntable in the service file), owned
# by the user this server runs as. The socket is mode 0600: only that user can use it, which
# is also the user the shutdown button script runs as.
CONTROL_SOCKET = '/run/turntable/control.sock'

def _get_interface_network(iface: str = 'eth0') -> ipaddress.IPv4Network | None:
    """Return the IPv4Network for *iface* by querying the kernel directly, for
    portability reasons.

    Fetches both the interface address (SIOCGIFADDR) and its netmask
    (SIOCGIFNETMASK) via ioctl, then lets ipaddress compute the correct
    network — works for any prefix length, no assumptions made.

    Returns None if the interface is absent or has no address yet, so the
    caller can decide how to handle the failure.
    """
    op_code_get_address: int     = 0x8915  # SIOCGIFADDR
    op_code_get_subnet_mask: int = 0x891B  # SIOCGIFNETMASK
    try:
        # Make a new socket, so we can use it as our line of communication to the kernel
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Format the name of the interface in question in such a way as the kernel can use it
            packed_iface_name = struct.pack('256s', iface[:15].encode())
            # Ask the kernel for the IP address of the interface
            ip   = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), op_code_get_address,     packed_iface_name)[20:24])
            # Ask the kernel for the subnet mask of the interface
            mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), op_code_get_subnet_mask, packed_iface_name)[20:24])
        # Use the built-in IP library to evaluate to the network
        return ipaddress.IPv4Network(f'{ip}/{mask}', strict=False)
    except OSError: # If we don't get anything from the kernel,
        return None # Return nothing, so we can know whether to try again.
# End _get_interface_network()

# Initialised to None — the subnet filter fails closed until _init_network()
# succeeds and sets this to a real network.
ALLOWED_NETWORK: ipaddress.IPv4Network | None = None

def _init_network():
    """
    Attempt to get the interface network, retrying on failure.
    Intended to run in the startup background thread alongside bridge.startup().
    Sets ALLOWED_NETWORK on success, calls on_error() and returns on failure.
    """
    global ALLOWED_NETWORK
    # Log the stage we're in:
    print("Initializing network...", flush=True)
    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        # Try to get the network address for the Ethernet port.
        network = _get_interface_network('eth0')
        # Do we have a network address to work with?
        if network is not None:
            # Set the LAN identifier
            ALLOWED_NETWORK = network
            # Log our address and prefix length
            print(f"Network Ready: {network.with_prefixlen}", flush=True)
            return
        # If not, then the network stack isn't usable yet.
        # Have we already tried the maximum number of times?
        if attempt < NETWORK_RETRY_ATTEMPTS:
            # Log it.
            print(f"eth0 not yet ready, attempt {attempt}/{NETWORK_RETRY_ATTEMPTS}. retrying in {NETWORK_RETRY_DELAY_SEC}s...", flush=True)
            # Wait, and try again.
            socketio.sleep(NETWORK_RETRY_DELAY_SEC)
    # We've exhausted all retries without getting a real address. Error out.
    on_error(
        f"Could not get network address for eth0 after "
        f"{NETWORK_RETRY_ATTEMPTS} attempts. "
        f"The machine cannot accept commands until this is resolved. "
        f"Please check the network connection and restart.",
        recoverable=False,
    )
# End _init_network()

# Make a new Flask app.
app = Flask(__name__)
# We need a secret key for web-socket stuff. Will be invalidated on machine restart.
# Might need to switch to a static solution, based on user feedback.
app.config['SECRET_KEY'] = secrets.token_hex(32)
# Make a new socket IO controller/interface.
socketio = SocketIO(app, async_mode='threading')
# The Status LED on the machine, for communicating to users that don't have a client open
led    = StatusLED()
# The bridge for communicating with LinuxCNC.
# Initialised in main so the LED is set first
bridge: LinuxCNCBridge

# ---------------------------------------------------------------------------
# Machine state
#
# The server is the single source of truth for what state the machine is in.
# Everything the outside world sees (the LED, `machine_status` broadcasts, and replies
# to `get_state`) is derived from these flags by _current_state(), so they can never
# disagree with each other.
# ---------------------------------------------------------------------------

# The error currently stopping the machine.
# In an ideal world, this is always an empty variable.
# Unfortunately, we don't live in an ideal world.
_system_error: str = ''
# Whether pressing and releasing the e-stop can clear that error. True for faults in the machine
# (LinuxCNC refused a command, a move failed). False for faults the e-stop can't touch (no network).
_error_recoverable: bool = True
# True once the machine has completed its startup sequence. Cleared while recovering from an e-stop.
_booted: bool = False
# True while the e-stop is engaged.
_in_estop: bool = False
# True once a shutdown has begun.
_shutting_down: bool = False

# Several threads (web requests, the e-stop monitor, move threads) change and announce state.
# Announcements are serialised so a stale state can never be broadcast after a newer one.
_publish_lock = threading.Lock()
# Makes "is the machine ready?" and "mark it busy" a single step, so two clients sending a
# move at the same instant can't both be accepted.
_move_lock = threading.Lock()
# Makes "has a shutdown already begun?" and "begin one" a single step, so two button presses
# at the same instant can't both start a shutdown.
_shutdown_lock = threading.Lock()

# Maps each machine state to the LED status that communicates it.
_LED_FOR_STATE = {
    'booting':  'booting',
    'ready':    'ready',
    'moving':   'moving',
    'estop':    'estop_active',
    'error':    'system_error',
    'shutdown': 'shutdown',
}

def _current_state() -> dict:
    """
    Work out the machine's state from the flags above. Returns the exact payload
    for a `machine_status` event. Priority order matters: a shutdown outranks an
    error, which outranks an e-stop, and so on down to ready.
    """
    if _shutting_down:
        return {'state': 'shutdown'}
    if _system_error:
        return {'state': 'error', 'message': _system_error, 'recoverable': _error_recoverable}
    if _in_estop:
        return {'state': 'estop'}
    if not _booted:
        return {'state': 'booting'}
    if bridge.is_busy:
        return {'state': 'moving'}
    return {'state': 'ready'}
# End _current_state()

def _publish_state() -> None:
    """Set the LED to match the current state, and tell every connected client about it."""
    with _publish_lock:
        state = _current_state()
        led.set_status(_LED_FOR_STATE[state['state']])
        socketio.emit('machine_status', state)
# End _publish_state()

def _recalculate_led_status() -> None:
    """Called by StatusLED after a transient LED status expires."""
    led.set_status(_LED_FOR_STATE[_current_state()['state']])
# End _recalculate_led_status()

# ---------------------------------------------------------------------------
# Subnet filter — applied to every HTTP request and every WebSocket connection
# ---------------------------------------------------------------------------

def _client_allowed() -> bool:
    """
    True if the current request came from inside the local network.
    Fails closed: if the network hasn't been resolved yet, or the address can't be read,
    the answer is no.
    """
    # If ALLOWED_NETWORK is None, the network hasn't been resolved yet — fail closed.
    if ALLOWED_NETWORK is None:
        return False
    try:
        # Grab the IP address from the request, make it a proper IP address object.
        client_ip_address = ipaddress.ip_address(request.remote_addr or '')
    except (ValueError, TypeError):  # If we can't pull an IP address out of the request
        return False
    return client_ip_address in ALLOWED_NETWORK
# End _client_allowed()

@app.before_request
# Before we even start processing the request, let's see if it's in the local network. If not, return error: forbidden
def restrict_to_subnet():
    if not _client_allowed():
        # Request denied as forbidden.
        abort(403)
# Flask's before_request hook never sees Socket.IO traffic (the Socket.IO layer answers it
# before Flask is involved), so the WebSocket connect handler below repeats this check.

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
# If the client doesn't provide a path, route them to the default path: the control interface.
def index():
    return render_template('control.html')

@app.route('/docs')
# If the client provides the `/docs` path, route them to the documentation page.
def docs():
    return render_template('docs.html')

# ---------------------------------------------------------------------------
# LinuxCNCBridge callbacks
# ---------------------------------------------------------------------------

# Called when the machine is ready to execute a command.
def on_ready():
    global _booted, _in_estop
    # The startup sequence exits early, without raising, if the hardware e-stop is engaged.
    # Check, so we don't announce "ready" for a machine that isn't.
    try:
        bridge.status.poll()
        _in_estop = bool(bridge.status.estop)
    except (linuxcnc.error, SystemError):
        # If we can't tell, leave the flag alone. The e-stop monitor will correct it.
        pass
    _booted = True
    # Update the LED and let all clients know.
    _publish_state()
# End on_ready()

# Called when the machine finishes a move command and is ready to execute another.
def on_move_complete():
    # Update the LED and tell all clients the machine's state.
    _publish_state()
    # Inform all clients that the machine has finished moving.
    socketio.emit('move_complete', {})
# End on_move_complete()

# Called when a move is cut short by an e-stop. The bridge has already cleared its busy flag.
def on_move_aborted():
    print("Move aborted (e-stop or drive power lost).", flush=True)
    # No `move_complete`: the move didn't complete. Clients learn what happened from the e-stop state.
    _publish_state()
# End on_move_aborted()

# Called when the machine encounters an error that stops it accepting commands.
# `recoverable` says whether an e-stop press-and-release can clear it (see _monitor_estop).
def on_error(message: str, recoverable: bool = True):
    # Log right away, in case anything else goes wrong
    print(f"[ERROR] {message} ({'clearable with e-stop' if recoverable else 'needs a restart'})", flush=True)
    # We have global variables for these.
    global _system_error, _error_recoverable
    # Already stuck in an error? Keep the first (most likely root-cause) message,
    # and don't spam the clients with repeats.
    if _system_error:
        return
    # Let anyone who cares know.
    _system_error = message
    _error_recoverable = recoverable
    # Set the LED and inform all clients that the machine cannot accept further commands without a restart.
    _publish_state()
# End on_error()

# Safety net for every thread started through `threading` (socketio's background tasks included).
# Without it, an uncaught exception prints a traceback to stderr at best, and the thread just stops.
# If that thread was a move, the UI would sit on "Moving" forever. If it was the e-stop monitor or the
# shutdown-button listener, a safety function would quietly stop working.
# Installed in the entry point below. It doesn't cover the main thread, or exceptions that a framework
# catches and handles itself (e.g. Flask error handlers).
def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    # A thread that raises SystemExit is just leaving. The default hook ignores it too.
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else 'unknown'
    print(f"[ERROR] Uncaught exception in thread '{name}':", flush=True)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    try:
        # Not recoverable: a thread that died unexpectedly may be the e-stop monitor itself,
        # in which case nothing is left to notice an e-stop press, so "clearable with e-stop" would be a lie.
        on_error(f"Thread '{name}' crashed: {args.exc_value}", recoverable=False)
    except Exception:
        # A hook that raises is no safety net. Log it and carry on.
        traceback.print_exc()
# End _thread_excepthook()

# ---------------------------------------------------------------------------
# E-stop monitor
# ---------------------------------------------------------------------------

_estop_monitor_running = False

def _monitor_estop():
    """
    Continuously poll LinuxCNC stat for e-stop state changes.
    Designed for use in a background thread.
    """
    global _estop_monitor_running, _in_estop, _booted, _system_error
    _estop_monitor_running = True
    # Loop for as long as we are supposed to monitor.
    while _estop_monitor_running:
        # If `linuxcnc.stat()` isn't available, we need to be prepared
        try:
            # Grab the latest information
            bridge.status.poll()
            # stat.estop is a direct boolean — True when e-stop is active
            currently_estopped = bool(bridge.status.estop)
            if currently_estopped and not _in_estop:
                # E-stop just became active
                # Update our records
                _in_estop = True
                # Whatever move was running is dead. Don't let a stuck busy flag outlive it.
                bridge.clear_busy()
                # An e-stop acts as a fault reset, like on a real machine: it clears a recoverable
                # error, and releasing it runs the full startup sequence again. If the fault is
                # still there, startup fails and the error comes straight back.
                if _system_error and _error_recoverable:
                    print(f"E-stop engaged: clearing error ({_system_error})", flush=True)
                    _system_error = ''
                # Set the LED to inform the user, and inform all connected clients
                _publish_state()
            # End if currently_estopped and not _in_estop

            elif not currently_estopped and _in_estop:
                # E-stop just cleared — full re-init required
                # Update our records. Until the machine is back up it is "booting", not "ready".
                _in_estop = False
                _booted = False
                _publish_state()
                # Prepare for LinuxCNC to throw an error.
                try:
                    # Send commands to bring the machine to a known state ready for commands
                    bridge.startup_sequence()
                    # If it hasn't errored out, then we're ready.
                    on_ready()
                # If LinuxCNC does throw an error,
                except Exception as exc:
                    # Error out the machine.
                    on_error(str(exc))

        except (linuxcnc.error, SystemError) as exc:
            # Grab the error text.
            error_msg = str(exc)
            # "nml" or "not ready" would indicate that the error is because the system is still starting up.
            if isinstance(exc, SystemError) or "nml" in error_msg.lower() or "not ready" in error_msg.lower():
                # It's safe to silently ignore this and keep polling.
                pass
            else:
                # This is an error we _don't_ expect. Stop the machine.
                on_error(f"Unexpected LinuxCNC Engine Error: {error_msg}")
        # End except
        # Wait for a quarter of a second.
        # Excuse me, socketio people. Please stop requiring an int when you can easily take a float.
        # noinspection PyTypeChecker
        socketio.sleep(0.25)
# End _monitor_estop()

# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------

@socketio.on('connect')
def handle_connect(_auth=None):
    """
    Runs for every new WebSocket connection. Returning False refuses the connection.
    """
    # The HTTP subnet filter doesn't cover WebSockets, so check here too.
    if not _client_allowed():
        return False
# End handle_connect()

@socketio.on('get_state')
def handle_get_state() -> None:
    """
    Tells the sender (and only the sender) the machine's current state, as a `machine_status` event.
    Clients ask for this when they connect or reconnect, because they can't know what happened
    while they weren't listening.
    """
    emit('machine_status', _current_state())
# End handle_get_state()

@socketio.on('move_command')
def handle_move_command(data=None) -> None:
    """
    Sanitizes user movement commands, then sends movement commands to LinuxCNC for implementation
    :param data: The data for the move. Contains a number of degrees to rotate, and a direction, 'CW' or 'CCW'.
    """
    def reject_move(rejection_message: str):
        """
        Reject a movement command, informing the sender, and setting the LED to flash accordingly.
        :param rejection_message: The message to send back, indicating why the move was rejected.
        """
        # Set the LED to inform the user that a movement command was rejected.
        led.set_status('command_rejected', recalculate=_recalculate_led_status)
        # Inform the sender of the move that the move was rejected, and why.
        emit('move_error', {'message': rejection_message})
    # End reject_move()

    # Start by sanitizing input
    # Is the data a `dict`, indicating that the payload was JSON?
    if not isinstance(data, dict):
        reject_move('Invalid command format.')
        # We're done with this movement command.
        return

    raw_degrees = data.get('degrees')
    # `True` and `False` would sneak through float() as 1.0 and 0.0. Not a number as far as we're concerned.
    if isinstance(raw_degrees, bool):
        reject_move('Invalid command data.')
        return

    try:  # Both of these must be present and of the proper types:
        # `degrees` must be a float (or castable as such)
        degrees   = float(raw_degrees)
        # `direction` must be a string (or castable as such)
        direction = str(data.get('direction', ''))
    except (TypeError, ValueError):
        reject_move('Invalid command data.')
        return

    # Degrees must be a real number greater than zero, and no more than one full turn.
    # `not (0 < x <= max)` is deliberate: it also catches NaN, which fails every comparison.
    # Infinity is caught by the upper bound.
    if not math.isfinite(degrees) or not (0 < degrees <= MAX_MOVE_DEGREES):
        reject_move(f'Degrees must be greater than 0 and no more than {MAX_MOVE_DEGREES:.0f}.')
        return

    # Direction must be either 'CW' or 'CCW'.
    if direction not in ('CW', 'CCW'):
        reject_move('Direction must be "CW" or "CCW".')
        return

    # Is the machine available for a new movement command?
    # Check and claim under one lock, so only one simultaneous command can win.
    rejection = None
    with _move_lock:
        current_state = _current_state()['state']
        if current_state == 'moving':
            rejection = 'Machine is busy — command rejected.'
        elif current_state != 'ready':
            # E-stop, error, starting up, shutting down. The UI disables itself in these states,
            # but the server is what actually decides.
            rejection = f'Machine is not ready (state: {current_state}) — command rejected.'
        else:
            # We're busy, can't take any more commands until we've finished this one
            bridge.set_busy()
    if rejection:
        reject_move(rejection)
        return

    # If we've reached this point, then implement the move.
    # Set the LED to tell the user the machine is moving, and let all clients know.
    _publish_state()
    # Let all clients know that the machine is moving this much, in this direction.
    socketio.emit('move_started', {'degrees': degrees, 'direction': direction})
    # Send the move to LinuxCNC through the bridge.
    socketio.start_background_task(bridge.send_move, degrees, direction)
# End handle_move_command()

# ---------------------------------------------------------------------------
# Shutdown
#
# Powering off the Pi is an explicit request: the word "shutdown" sent over the control
# socket (the physical button does this). It is NOT a side effect of any signal.
# SIGTERM and SIGINT only mean "exit the server", so `systemctl stop/restart`, Ctrl-C, and
# the operating system's own shutdown sequence can never start a second shutdown, and the
# server never makes systemd wait for it.
# ---------------------------------------------------------------------------

def _shutdown_sequence():
    """
    Set the LED, broadcast a shutdown notice to all connected clients, wait briefly for
    sockets to flush, then halt the system. Runs as a background thread.
    """
    global _shutting_down
    # Already on our way down? Don't do it twice (e.g. a second button press).
    with _shutdown_lock:
        if _shutting_down:
            return
        _shutting_down = True
    print("Shutdown requested over control socket.", flush=True)
    # Update the LED and let all clients know.
    _publish_state()
    socketio.sleep(3)
    # systemd will send this server SIGTERM as part of the shutdown. _handle_exit() makes
    # that exit immediately, so the Pi isn't held up waiting for us.
    subprocess.run(['sudo', 'shutdown', '-h', 'now'])
# End _shutdown_sequence()

def _control_listener():
    """
    Accepts one-word commands on a Unix socket that only this server's user can open.
    Currently the only command is "shutdown". Runs as a background thread.
    """
    try:
        # Remove a stale socket file left by a previous run.
        if os.path.exists(CONTROL_SOCKET):
            os.unlink(CONTROL_SOCKET)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Create the socket file as mode 0600 (umask 0177 clears everything but rw for the owner).
        old_umask = os.umask(0o177)
        try:
            server.bind(CONTROL_SOCKET)
        finally:
            os.umask(old_umask)
        server.listen(1)
        print(f"Control socket ready: {CONTROL_SOCKET}", flush=True)
    except OSError as exc:
        # The machine still works without this. Only the button's graceful path is lost: it
        # falls back to a direct shutdown. Log it loudly and carry on.
        print(f"[WARNING] Could not create control socket ({CONTROL_SOCKET}): {exc}", flush=True)
        return

    while True:
        try:
            conn, _ = server.accept()
        except OSError as exc:
            print(f"[WARNING] Control socket accept error: {exc}", flush=True)
            socketio.sleep(1)
            continue
        # A misbehaving client must never kill the listener.
        try:
            with conn:
                conn.settimeout(2)
                command = conn.recv(64).strip()
                if command == b'shutdown':
                    conn.sendall(b'ok\n')
                    socketio.start_background_task(_shutdown_sequence)
                else:
                    conn.sendall(b'unknown command\n')
        except OSError as exc:
            print(f"[WARNING] Control socket client error: {exc}", flush=True)
# End _control_listener()

def _handle_exit(_signum, _frame):
    """
    Called on SIGTERM (systemd stop or restart, or the operating system shutting down) and
    SIGINT (Ctrl-C). Exits the server promptly. Does NOT power off the Pi.
    """
    try:
        os.unlink(CONTROL_SOCKET)
    except OSError:
        pass
    # Raised in the main thread, which is sitting in the web server's loop. That unwinds the
    # loop and ends the process. Background threads are daemon threads and go with it.
    raise SystemExit(0)
# End _handle_exit()

signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _startup():
    """
    Runs the network initialization and LinuxCNC startup sequence in sequence.
    Both must succeed before the machine is considered ready.
    Designed for use in the startup background thread.
    """
    # Resolve the allowed network first — the subnet filter is closed until this succeeds.
    _init_network()
    # If _init_network() hit an unrecoverable error, _system_error will be set.
    # Don't proceed to LinuxCNC startup in that case.
    if _system_error:
        return
    # Now bring LinuxCNC up.
    bridge.startup()
    # A failure the e-stop can't clear leaves nothing to monitor.
    if _system_error and not _error_recoverable:
        return
    # E-stop monitor runs independently for its entire lifetime. It also runs after a recoverable
    # startup failure, because pressing the e-stop is how the user retries.
    socketio.start_background_task(_monitor_estop)
#END _startup()

if __name__ == '__main__':
    # No thread should be able to die silently from here on.
    threading.excepthook = _thread_excepthook
    # Set the LED to communicate that the machine is setting up
    led.set_status('booting')
    # Set up the bridge
    bridge = LinuxCNCBridge(
        on_ready=on_ready,
        on_move_complete=on_move_complete,
        on_error=on_error,
        on_move_aborted=on_move_aborted,
    )
    # Listen for the shutdown button right away, independent of the network or LinuxCNC coming up.
    socketio.start_background_task(_control_listener)
    # Network init and LinuxCNC startup run sequentially in a single background
    # thread so the web server is already accepting connections while the
    # machine initializes. The subnet filter stays closed until network init succeeds.
    socketio.start_background_task(_startup)
    # `allow_unsafe_werkzeug`: threading mode uses Werkzeug's built-in server, which refuses to run
    # under systemd without this. Acceptable here: a handful of clients on a private LAN, behind the
    # subnet filter. (If that ever changes, gunicorn with one worker and several threads is the step up.)
    socketio.run(app, host=SERVER_HOST, port=SERVER_PORT, allow_unsafe_werkzeug=True)