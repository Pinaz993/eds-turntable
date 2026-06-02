# turntable_server.py
import fcntl
import ipaddress
import secrets
import signal
import socket
import struct
import subprocess

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
    Intended to run in the startup background task alongside bridge.startup().
    Sets ALLOWED_NETWORK on success, calls on_error() and returns on failure.
    """
    global ALLOWED_NETWORK
    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        network = _get_interface_network('eth0')
        if network is not None:
            ALLOWED_NETWORK = network
            return
        # Not ready yet — log and wait before retrying
        if attempt < NETWORK_RETRY_ATTEMPTS:
            socketio.sleep(NETWORK_RETRY_DELAY_SEC)
    # Exhausted all retries without getting a real address
    on_error(
        f"Could not get network address for eth0 after "
        f"{NETWORK_RETRY_ATTEMPTS} attempts. "
        f"The machine cannot accept commands until this is resolved. "
        f"Please check the network connection and restart."
    )
# End _init_network()

# Make a new Flask app.
app = Flask(__name__)
# We need a secret key for web-socket stuff. Will be invalidated on machine restart.
# Might need to switch to a static solution, based on user feedback.
app.config['SECRET_KEY'] = secrets.token_hex(32)
# Make a new socket IO controller/interface.
socketio = SocketIO(app, async_mode='eventlet')
# The Status LED on the machine, for communicating to users that don't have a client open
led    = StatusLED()
# The bridge for communicating with LinuxCNC.
# Initialised in main so the LED is set first
bridge: LinuxCNCBridge

# ---------------------------------------------------------------------------
# Subnet filter — applied to every request
# ---------------------------------------------------------------------------
@app.before_request
# Before we even start processing the request, let's see if it's in the local network. If not, return error: forbidden
def restrict_to_subnet():
    # If ALLOWED_NETWORK is None, the network hasn't been resolved yet — fail closed.
    if ALLOWED_NETWORK is None:
        abort(403)
    try:
        # Grab the IP address from the request, make it a proper IP address object.
        client_ip_address = ipaddress.ip_address(request.remote_addr or '')
    except (ValueError, TypeError):  # If we can't pull an IP address out of the request
        # Request denied as forbidden.
        abort(403)
    if client_ip_address not in ALLOWED_NETWORK:
        # Request denied as forbidden.
        abort(403)

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
    # Set the LED to communicate this change, glow a steady green.
    led.set_status('ready')
    # Let all clients know that the machine is ready to accept commands.
    socketio.emit('machine_status', {'state': 'ready'})
# End on_ready()

# Called when the machine finishes a move command and is ready to execute another.
def on_move_complete():
    # Set the LED to communicate this change, glow a steady green.
    led.set_status('ready')
    # Inform all clients that the machine is ready to accept commands
    socketio.emit('machine_status', {'state': 'ready'})
    # Inform all clients that the machine has finished moving.
    socketio.emit('move_complete', {})
# End on_move_complete()

# Called when the machine encounters an unrecoverable system error.
def on_error(message: str):
    # We have a global variable for this.
    global _system_error
    _system_error = message
    # Log, because it's the responsible thing to do.
    print(f"[UNRECOVERABLE ERROR] {message}", flush=True)
    # Set the LED to communicate that the machine needs to be restarted
    led.set_status('system_error')
    # Inform all clients that the machine cannot accept further commands without a restart.
    socketio.emit('machine_status', {'state': 'error', 'message': message})
# End on_error()

# ---------------------------------------------------------------------------
# E-stop monitor
# ---------------------------------------------------------------------------

_estop_monitor_running = False

def _monitor_estop():
    """
    Continuously poll LinuxCNC stat for e-stop state changes.
    Designed for use in a background green-thread.
    """
    global _estop_monitor_running
    _estop_monitor_running = True

    # Grab the stat monitor from LinuxCNC's Python library.
    stat = linuxcnc.stat()
    # By default, estop is off.
    in_estop = False
    # Loop for as long as we are supposed to monitor.
    while _estop_monitor_running:
        # If `linuxcnc.stat()` isn't available, we need to be prepared
        try:
            # Update to the current state
            stat.poll()
            # stat.estop is a direct boolean — True when e-stop is active
            currently_estopped = stat.estop
            if currently_estopped and not in_estop:
                # E-stop just became active
                # Update our records
                in_estop = True
                # Set the LED to inform the user
                led.set_status('estop_active')
                # Inform all connected clients
                socketio.emit('machine_status', {'state': 'estop'})
            # End if currently_estopped and not in_estop

            elif not currently_estopped and in_estop:
                # E-stop just cleared — full re-init required
                # Update our records
                in_estop = False
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

@socketio.on('move_command')
def handle_move_command(data) -> None:
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
        emit('move_error', {'message': rejection_message}, broadcast=False)
    # End reject_move()

    # Start by sanitizing input
    # Is the data a `dict`, indicating that the payload was JSON?
    if not isinstance(data, dict):
        reject_move('Invalid command format.')
        # We're done with this movement command.
        return

    try:  # Both of these must be present and of the proper types:
        # `degrees` must be a float (or castable as such)
        degrees   = float(data.get('degrees', 0))
        # `direction` must be a string (or castable as such)
        direction = str(data.get('direction', ''))
    except (TypeError, ValueError):
        reject_move('Invalid command data.')
        return

    # Zero degree movement can't be implemented, and is a sign that the user did something wrong.
    if degrees == 0:
        reject_move('Degrees must be non-zero.')
        return

    # Direction must be either 'CW' or 'CCW'.
    if direction not in ('CW', 'CCW'):
        reject_move('Direction must be "CW" or "CCW".')
        return

    # Is the machine available for a new movement command?
    if bridge.is_busy:
        reject_move('Machine is busy — command rejected.')
        return

    # If we've reached this point, then implement the move.
    # We're busy, can't take any more commands until we've finished this one
    bridge.set_busy()
    # Set the LED to tell the user the machine is moving
    led.set_status('moving')
    # Let all clients know that the machine is moving.
    socketio.emit('machine_status', {'state': 'moving'})
    # Let all clients know that the machine is moving this much, in this direction.
    socketio.emit('move_started', {'degrees': degrees, 'direction': direction})
    # Send the move to LinuxCNC through the bridge.
    socketio.start_background_task(bridge.send_move, degrees, direction)
# End handle_move_command()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The unrecoverable error currently being experienced.
# In an ideal world, this is always an empty variable.
# Unfortunately, we don't live in an ideal world.
_system_error: str = ''

def _recalculate_led_status() -> None:
    """Called by StatusLED after a transient LED status expires."""
    global _system_error
    # Are we stuck in an unrecoverable error?
    if _system_error:
        led.set_status('system_error')
        return
    # Grab a stats object from LinuxCNC and update to the latest machine stats.
    stat = linuxcnc.stat()
    stat.poll()
    # stat.estop is a direct boolean — True when e-stop is active
    if stat.estop:
        led.set_status('estop_active')
    # Are we busy with a move?
    elif bridge and bridge.is_busy:
        led.set_status('moving')
    # Otherwise, we're ready for a command.
    else:
        led.set_status('ready')
# End _recalculate_led_status()

# ---------------------------------------------------------------------------
# Shutdown handler
# ---------------------------------------------------------------------------

def _handle_shutdown(_signum, _frame):
    """
    Broadcast a shutdown notice to all connected clients, set the LED,
    wait briefly for sockets to flush, then halt the system.
    Called on SIGTERM (systemd stop, shutdown button) and SIGINT (Ctrl-C).
    """
    led.set_status('shutdown')
    socketio.emit('machine_status', {'state': 'shutdown'})
    socketio.sleep(3)
    subprocess.run(['sudo', 'shutdown', '-h', 'now'])
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _startup():
    """
    Runs the network initialization and LinuxCNC startup sequence in sequence.
    Both must succeed before the machine is considered ready.
    Designed for use in the startup background green-thread.
    """
    global _system_error
    # Resolve the allowed network first — the subnet filter is closed until this succeeds.
    _init_network()
    # If _init_network() hit an unrecoverable error, _system_error will be set.
    # Don't proceed to LinuxCNC startup in that case.
    if _system_error:
        return
    # Now bring LinuxCNC up.
    bridge.startup()
    # NML is confirmed live at this point — safe to start polling.
    # E-stop monitor runs independently for its entire lifetime.
    socketio.start_background_task(_monitor_estop)
# End _startup()

if __name__ == '__main__':
    # Set the LED to communicate that the machine is setting up
    led.set_status('booting')
    # Set up the bridge
    bridge = LinuxCNCBridge(
        on_ready=on_ready,
        on_move_complete=on_move_complete,
        on_error=on_error,
    )
    # Network init and LinuxCNC startup run sequentially in a single background
    # green-thread so the web server is already accepting connections while the
    # machine initializes. The subnet filter stays closed until network init succeeds.
    socketio.start_background_task(_startup)
    socketio.run(app, host=SERVER_HOST, port=SERVER_PORT)
# End if name is main