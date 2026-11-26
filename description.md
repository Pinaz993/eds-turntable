# CNC Turntable Web Controller — Project Description
## For continuation of development in a new chat session

---

## Project Overview
A Raspberry Pi 4B-based CNC turntable controller for a machinist named Ed. The Pi runs
LinuxCNC to control a single stepper motor via a web interface served at `turntable.local`.
Ed scans a QR code, opens the control panel on his device, and commands the turntable to
rotate by a specified number of degrees in a specified direction.

**Status:** hardware, LinuxCNC config, LED, shutdown button, the server
(`turntable_server.py` + `lcnc_bridge.py`), and the control panel (`control.html`,
`control.js`, `style.css`, `presets.js`) and the documentation page (`docs.html`, linked
from the control panel as `/docs`) are complete. The server and client logic were tested
against stubbed hardware, not yet on the Pi or in a real browser. The exception is the
power button and shutdown sequence, which have been tested many times on the Pi: the LED
alternates red/blue for a few seconds, then the Pi powers off.

---

## Hardware

### Raspberry Pi 4B
- OS: Debian GNU/Linux 13 (Trixie), aarch64
- Kernel: Linux 6.12.34+rpt-rpi-v8-rt (RT kernel)
- User: `turntable`
- Hostname: `turntable`
- IP: TBD (eth0)
- Booted to multi-user (console) mode — no desktop environment

### Pin Assignments
| GPIO | Physical Pin | Function                                                    |
|------|--------------|-------------------------------------------------------------|
| 3    | 5            | Power/shutdown button (active low, internal pull-up)        |
| 17   | 11           | Stepper enable (active low, inverted in HAL)                |
| 19   | 35           | RGB LED — Red                                               |
| 20   | 38           | RGB LED — Green                                             |
| 21   | 40           | RGB LED — Blue                                              |
| 22   | 15           | Stepper direction                                           |
| 24   | 18           | E-stop input (normally open contacts, 10kΩ pull-up to 3.3V) |
| 27   | 13           | Stepper step pulses                                         |

### Stepper System
- Driver: M335 (DIP switch configured for full stepping, 3A, 200 steps/rev)
- Gearbox: 40:1 reduction
- Scale: 22.2222 steps/degree
- Power: 24V Siemens DIN rail supply (10A)
- Level shifter: Adafruit 757 (3.3V → 5V) between Pi GPIO and M335 signal inputs
- E-stop: Physical NC button wired through stepper driver power supply,
  NO contacts wired to GPIO 24 with 10kΩ pull-up resistor

### RGB Status LED
- Common cathode RGB LED
- 470Ω current limiting resistors on each channel
- Connected to GPIO 19 (R), 20 (G), 21 (B)

### Power Button
- Momentary button on GPIO 3 (physical pin 5)
- Wired between GPIO 3 and ground
- GPIO 3 supports wake-from-halt (hardware feature of BCM2711)
- `dtoverlay=gpio-shutdown` NOT used — handled by `shutdown_button.py` instead for LED feedback during shutdown

---

## Software Stack
- LinuxCNC 2.9.8
- Python 3.13
- Flask 3.1.3
- Flask-SocketIO 5.6.1 (python-socketio 5.16.2, python-engineio 4.13.2)
- simple-websocket 1.1.0 (WebSocket support for Flask-SocketIO's threading mode; minimum
  versions declared in `pyproject.toml` and pinned in `uv.lock`). **Not eventlet**: it is deprecated upstream and was dropped.
- gpiozero 2.0.1
- linuxcnc Python module (system package; not on PyPI, so `linuxcnc.pyi` stands in for it in IDEs)
- Socket.IO **client** 4.8.1, vendored at `static/socket.io.min.js` (MIT license in
  `static/socket.io.LICENSE`). Served locally rather than from a CDN so the unit works
  without internet and the library version is fixed. Flask-SocketIO does not serve the
  client itself. Reference it as `<script src="/static/socket.io.min.js"></script>`.

---

## Project Directory
`/home/turntable/linuxcnc/configs/turntable/`

```
turntable/
├── turntable_server.py     # Flask-SocketIO server (COMPLETE)
├── lcnc_bridge.py          # LinuxCNC Python API bridge (COMPLETE)
├── led.py                  # RGB LED status manager (COMPLETE)
├── shutdown_button.py      # Power button monitor (COMPLETE)
├── linuxcnc.pyi            # Type stub for the linuxcnc module, for IDEs only (ignored at runtime)
├── pyproject.toml          # Python version + dependency minimums (uv.lock pins them)
├── description.md          # This file
├── pi-test-plan.md         # On-Pi test checklist and results log (see "Testing")
├── LICENSE
├── tests/                  # pytest suite; fakes for linuxcnc and gpiozero (see "Testing")
├── .github/workflows/tests.yml   # runs the suite on every push
├── sudoers.d/
│   └── turntable-shutdown  # the one sudo rule the Pi power-off needs (install with visudo -cf first)
├── systemd-units/          # Copies of the unit files below (install to /etc/systemd/system/)
│   ├── linuxcnc.service
│   ├── turntable-server.service
│   ├── shutdown-button.service
│   └── wifi-powersave-off.service
├── templates/
│   ├── control.html        # Control panel (COMPLETE)
│   └── docs.html           # Documentation page (COMPLETE)
├── static/
│   ├── socket.io.min.js    # Socket.IO client 4.8.1 (COMPLETE, vendored)
│   ├── socket.io.LICENSE   # MIT license for the above
│   ├── control.js          # WebSocket + UI logic (COMPLETE)
│   ├── presets.js          # Quick-pick angles, empty for now (COMPLETE)
│   └── style.css           # Styles (COMPLETE)
├── turntable.ini           # LinuxCNC configuration (COMPLETE)
└── turntable.hal           # HAL configuration (COMPLETE)
```

## Systemd Services
Unit files are kept in `systemd-units/` in the repo and installed to `/etc/systemd/system/`.
All are complete:

| Unit                        | Runs                  | As       | Notes                                              |
|-----------------------------|-----------------------|----------|----------------------------------------------------|
| `linuxcnc.service`          | LinuxCNC + `turntable.ini` | turntable | `Restart=always`                              |
| `turntable-server.service`  | `turntable_server.py` | turntable | Port 80 via `AmbientCapabilities=CAP_NET_BIND_SERVICE` (not root). `RuntimeDirectory=turntable` creates `/run/turntable/` for the control socket. `TimeoutStopSec=10`. `Restart=always`, `PYTHONUNBUFFERED=1`, `WatchdogSec=30` (see "Stability measures"). `Wants=linuxcnc.service` (not `Requires=`): the server reconnects itself if LinuxCNC restarts |
| `shutdown-button.service`   | `shutdown_button.py`  | turntable | Sends `shutdown` over `/run/turntable/control.sock`, or shuts down directly if the server isn't reachable. `Restart=always` |
| `wifi-powersave-off.service`| `iw wlan0 set power_save off` | root | oneshot, `Restart=on-failure` (wlan0 may not exist yet at boot) |

## Firewall
Port 5007 (linuxcncrsh) restricted to loopback only:
```
-A INPUT -i lo -p tcp -m tcp --dport 5007 -j ACCEPT
-A INPUT ! -i lo -p tcp -m tcp --dport 5007 -j DROP
```
Managed by `iptables-persistent`.

---

## Server (`turntable_server.py`)

Listens on `0.0.0.0:80`. Runs in Flask-SocketIO's plain `threading` mode on Werkzeug's
built-in server (`allow_unsafe_werkzeug=True`, required under systemd; acceptable for a few
clients on a private LAN behind the subnet filter; Gunicorn with one worker and several
threads is the upgrade path if that ever changes). Every connection, background task and timer
is a real OS thread, so blocking calls like `time.sleep` are fine and nothing is
monkey-patched. Shared state is protected by locks: the bridge's NML lock (one
`threading.Lock` for all LinuxCNC access), `_publish_lock` (state announcements go out in
order), and `_move_lock` (checking "ready" and claiming the machine is one atomic step, so
simultaneous commands from several clients can't both be accepted).
`SECRET_KEY` is random per boot (`secrets.token_hex`), so it is never in source control.

### Startup
1. A `threading.excepthook` is installed: a thread that dies unexpectedly raises a
   **non-recoverable** error (it might have been the e-stop monitor).
2. `StatusLED` created; LED set to `booting`.
3. `LinuxCNCBridge` created with `on_ready`, `on_move_complete`, `on_error`, `on_move_aborted` callbacks.
4. `_control_listener` thread started right away, so the shutdown button works before the
   network or LinuxCNC are up.
5. One background thread (`_startup`) runs, in order:
   1. `_init_network()` — reads eth0's address and netmask from the kernel and sets
      `ALLOWED_NETWORK`. Retries 40× at 3 s intervals (two minutes, so a slow DHCP server
      doesn't force a restart); on failure calls `on_error`. On success a `_watch_network`
      thread starts: every 30 s it re-reads eth0 and adopts a changed network (a failed read
      keeps the old one).
   2. `bridge.startup()` — retried up to 10 × 30 s if LinuxCNC isn't reachable yet, then
      ESTOP reset → enable → manual mode → **joint mode** → home joint 0
      → MDI mode, then `on_ready()`. (The old forced ESTOP-cycle workaround is gone; all NML
      access is now serialized by one lock in the bridge.) On failure → `on_error`.
   3. `_monitor_estop` thread started. Skipped only if the failure can't be cleared by an
      e-stop (see "Errors" below).
6. The web server accepts connections throughout. Until step 5.1 finishes, everything is
   refused (fails closed).

If the hardware e-stop is engaged at boot, `startup_sequence()` returns early without
raising; `on_ready` detects this and the machine reports `estop` instead of `ready`.
Tested on the Pi: with the e-stop pressed at power-up, the machine starts in `estop` and
reaches `ready` once the e-stop is released.

### Subnet filter
The allowed network is **detected at runtime from eth0** (address + netmask), not
configured. Any client outside it gets 403 on HTTP, and WebSocket connections from
outside are refused in the `connect` handler. Flask's `before_request` does not see
Socket.IO traffic, so both layers share one helper, `_client_allowed()`.
Everything fails closed until the network has been resolved.

Only **eth0** is trusted, on purpose. The Pi sits inside a stainless steel enclosure, which
shields Wi-Fi, so wireless is not a supported way to reach the page and `wlan0` is never
consulted. A client on any other interface is refused even if its address looks local.
(`wifi-powersave-off.service` is left over from before this was settled and has no effect
while `wlan0` is unused; it is harmless to disable.)

### Machine state
The server holds the truth and derives everything from it via `_current_state()`.
The LED, `machine_status` broadcasts, and `get_state` replies all come from the same
function, so they cannot disagree. Highest priority first:

| State      | Meaning                                              | LED                 | Accepts moves |
|------------|------------------------------------------------------|---------------------|---------------|
| `shutdown` | Shutdown in progress                                 | red/blue alternating | no           |
| `error`    | Machine can't take commands; `message` and `recoverable` fields included. See "Errors" | red flashing | no |
| `estop`    | E-stop engaged                                       | red/yellow alternating | no         |
| `booting`  | Starting up, recovering after an e-stop clear, or LinuxCNC unreachable for 2 s+ | solid red | no |
| `moving`   | A move is executing                                  | solid blue          | no            |
| `ready`    | Idle                                                 | solid green         | yes           |

The yellow-flashing `command_rejected` LED status is transient (2 s), then the LED returns
to whatever the current state calls for.

### WebSocket protocol

**Client → server**

| Event          | Payload                                  | Behavior |
|----------------|------------------------------------------|-----------|
| `get_state`    | none                                     | Server replies with `machine_status` to the sender only. |
| `move_command` | `{"degrees": 210, "direction": "CCW"}`   | Validated, then executed. See below. |

**Server → client**

| Event            | Audience     | Payload |
|------------------|--------------|---------|
| `machine_status` | all clients (state changes), or sender only (reply to `get_state`) | `{"state": "<state>"}`; `error` adds `"message"` and `"recoverable"` (bool) |
| `move_started`   | all          | `{"degrees": 210, "direction": "CCW"}` |
| `move_complete`  | all          | `{}` |
| `move_error`     | sender only  | `{"message": "..."}` |

On a successful move the order is `machine_status: moving`, `move_started`, then later
`machine_status: ready`, `move_complete`.

**`move_command` validation** (any failure → `move_error` to the sender, LED flashes
`command_rejected`, nothing is sent to LinuxCNC):
- Payload must be a JSON object (a missing payload is also rejected).
- `degrees` must be numeric (numeric strings are accepted; booleans are not), finite,
  and `0 < degrees ≤ 360`. NaN, ±infinity, zero, negatives, and >360 are rejected.
- `direction` must be exactly `"CW"` or `"CCW"`.
- Machine state must be `ready`. While `moving` the message is "Machine is busy";
  in any other state it is "Machine is not ready (state: …)". The UI disables itself
  in these states, but the server is what actually enforces it.

CCW moves are sent to LinuxCNC as negative degrees, formatted fixed-point
(`G91 G0 A-210.0000`) so tiny values never become exponent notation.

### E-stop handling
`_monitor_estop` polls LinuxCNC every 0.25 s.
- E-stop engaged → the bridge's busy flag is cleared (any move in progress is dead),
  recoverable errors are cleared (below), state `estop`, LED + broadcast.
- E-stop cleared → state `booting`, full `bridge.startup_sequence()` rerun, then `ready`
  (or `error` if that fails).
- A move cut short by an e-stop is detected by the bridge (`_wait_for_move` returns
  `aborted` as soon as the e-stop is engaged or the drives are off). It does **not** send
  `move_complete` and does not read the LinuxCNC error channel; `on_move_aborted` just
  re-announces state. There is no separate client event for it; clients learn from the
  `estop` state. (A `move_aborted` event would be an easy addition.)
- Homing after an e-stop: the joint stays homed, which leaves LinuxCNC's motion in teleop
  mode, and `home` is then refused ("must be in joint mode to home"). The startup sequence
  calls `teleop_enable(False)` and waits for joint mode first.

### Errors
`on_error(message, recoverable=True)`. Only the first error is kept; later ones are ignored.
- **Recoverable** (default): faults in the machine, such as a refused command or a failed
  move. Pressing the e-stop **clears** the error (like a fault reset on a real machine);
  releasing it reruns the full startup sequence. If the fault is still there, the error
  returns. The e-stop monitor therefore keeps running after a failed startup.
- Recoverable examples: a refused command, a failed or timed-out move, and **LinuxCNC
  disabling the machine with no e-stop engaged** (`status.enabled` goes false; detected by
  the e-stop monitor while idle, and by the bridge mid-move). This is LinuxCNC's own
  enable state, not a sensor on the driver: the hardware has no feedback, so the Pi cannot
  tell if power to the stepper driver is lost, except when the e-stop does it. The error
  text mentions a following error or drive fault, which is a guess at the cause.
- **Not recoverable**: no network address on eth0, LinuxCNC never reachable at startup, or a
  crashed server thread. An e-stop can't help; restart.
- The page says which applies: "Press and release the e-stop to try again" or "Restart the
  turntable".
- Errors where LinuxCNC itself is unreachable (NML never came up) can't be cleared this
  way, since the e-stop can't be read either. Restart.

### Shutdown
Only the word `shutdown` sent over the control socket (`/run/turntable/control.sock`, mode
0600, owned by the server's user, which is also the button script's user) powers off the Pi: state `shutdown` (LED + broadcast), wait 3 s for
sockets to flush, `sudo -n /usr/sbin/shutdown -h now` (`sudoers.d/turntable-shutdown` grants exactly that
command; install it with `visudo -cf` first, then `install -m 0440`). If the command fails
(missing sudoers entry, timeout), the shutdown state is withdrawn and the machine reports a
recoverable `error` instead of sitting on "Shutting down" while still powered. A second
request while one is in progress is ignored. SIGTERM/SIGINT
(systemd stop/restart, a deployment, Ctrl-C) only make the server exit.

### LinuxCNC restarts
`turntable-server.service` `Wants=` LinuxCNC rather than requiring it, so LinuxCNC crashing
or restarting does not take the server down. At startup, `bridge.startup()` retries up to
10 × 30 s for the NML bus. At runtime, the e-stop monitor treats 2 s of failed polls as
"lost LinuxCNC" (state `booting`), rebuilds the NML objects once a second, and re-runs the
startup sequence when polls succeed again.

---

## Stability measures
What keeps the unit running unattended, and what each piece does and does not cover.

- **Restart policy.** All three long-running units use `Restart=always`, so any exit (not
  just a non-zero one) brings them back. A deliberate `systemctl stop` is still respected.
- **systemd watchdog.** `turntable-server.service` has `WatchdogSec=30`; `_watchdog()` sends
  a heartbeat every 10 s. It stops sending if the e-stop monitor has neither completed a loop
  nor ticked in `MONITOR_STALL_SEC` (30 s). The bridge's polling loops tick (`_tick_monitor`), so
  a long startup sequence inside the monitor stays healthy while it makes progress, and only a
  call that never returns trips the limit. Ticks from other threads (a move) are ignored.
  If the whole process freezes (e.g. a `linuxcnc` call that blocks while holding the
  interpreter lock), no heartbeat is sent at all. Either way systemd restarts the server.
  This does not restart LinuxCNC itself, and the hardware e-stop never depends on it.
  *Recognizing it:* `journalctl -u turntable-server` shows `Watchdog timeout (limit 30s)!`
  (a frozen process), or `E-stop monitor has stopped; withholding watchdog heartbeat.` just
  before the restart (a hung monitor). Until then the page is unresponsive and the LED keeps
  whatever it last showed (so it can be green on a frozen server): nothing in a frozen process
  can change it. Expect about 30 s to detect, then the usual boot sequence.
- **Move bookkeeping.** The bridge is marked busy only for as long as something will clear it:
  if announcing a move fails, the flag is released. A timed-out move is sent `abort`.
  The LinuxCNC error channel is drained before each move, so messages LinuxCNC posted itself
  (e-stop reset, enabling, homing) can't be blamed on the next move.
- **Errors.** `on_error` records the first error atomically, so concurrent failures can't
  overwrite each other or double-announce.
- **LED.** One expiry timer at a time (a newer status cancels the old one; timers are
  daemons), all LED changes under a lock, and a rejected-command flash never replaces an
  e-stop, error or shutdown indication.
- **Network.** Longer initial wait, and the subnet filter follows DHCP changes while running.
- **Power button.** 50 ms `bounce_time` against contact bounce and electrical noise from the
  stepper wiring; an exception in the fallback shutdown can't kill the button's event thread.

## Testing
`pytest` runs the real server, bridge and LED code against fake `linuxcnc` and `gpiozero`
modules (`tests/conftest.py`): no Pi needed. Install the dependencies in `pyproject.toml`
(`uv sync`, or `pip install` them), then run `pytest` from the repository root. `gpiozero` is
installed on a development machine only so the IDE can resolve it; the tests still use the fake. GitHub Actions
does the same on every push. The fake machine is deliberately small; it checks the logic in
this repository, not LinuxCNC's behavior, so the Pi-only checks under "Known Issues" still apply.

---

## Web Client (`templates/control.html`, `static/control.js`, `static/style.css`, `static/presets.js`)

Mobile-first, no external resources (system fonts, vendored Socket.IO). Script order in
the page: `socket.io.min.js`, `presets.js`, `control.js`.

### Connection and state handling
The client never assumes a state it hasn't been told.
- Starts in **unknown**, all controls disabled.
- On every Socket.IO `connect` (including reconnects) it resets to unknown and emits
  `get_state`; it retries every 3 s while still unknown.
- `disconnect` / `connect_error` → client-only **disconnected** state.
- Only `ready` enables the controls. Any unrecognized state is treated as unknown.
- After sending a move the controls lock until the server answers (3 s safety timeout).
- A client that joins mid-move shows "A move is in progress"; it only learns the angle
  and direction for moves it sees start.

### Layout
- Status banner on top with a lamp that mirrors the physical LED (same colors and flash
  patterns; flashing stops under `prefers-reduced-motion`, color remains), a state title,
  and a detail line (last move, current move, error message, or what to do).
- Direction toggle (CW / CCW), angle input (number, `0 < x ≤ 360`, same rule as the server,
  inline error text), presets row, Rotate button, link to `/docs`.
- Error toast for `move_error` (sender only); tap to dismiss, auto-hides after 5 s.

### Presets
Edit `static/presets.js` — no other file needs touching. Each entry is
`{ degrees, direction?, label? }`. Tapping a preset fills the angle (and direction, if given)
but never starts a move. The row is hidden while the list is empty; invalid entries are
skipped. Buttons wrap onto extra rows as needed.

### docs.html
Static page served at `/docs`, reusing `style.css` (a documentation section at the end of
that file). Covers: using the control panel, what the LED colors mean (kept in step with
the state table above), the e-stop procedure, what to do on an error, and the power button.
If an LED pattern or state changes, update this page too.

---

## Known Issues / Notes
- **Untested on the Pi** (checklist in `pi-test-plan.md`; covered by the fake-hardware suite only): the systemd watchdog
  (`systemctl show turntable-server -p WatchdogUSec`, then confirm a heartbeat line in the
  journal and that `kill -STOP <pid>` leads to a restart after ~60 s), `bounce_time` on the
  power button (confirm a normal press still shuts down), the sudoers entry (`sudo -n -l`
  as `turntable`, then press the button with the server stopped), and `Restart=always` on
  `linuxcnc.service` (`kill` it and confirm it returns and the page recovers).
- `NoNewPrivileges=` and similar systemd sandboxing are intentionally **not** set on
  `turntable-server.service`: the server shuts the Pi down through `sudo`, which they would block.
- History: an earlier version used eventlet with `monkey_patch()`; on the Pi that stalled
  the server before it printed anything. Cause not pinned down; the move to threading mode
  made it moot.
- The `linuxcnc` Python objects stay bound to dead shared memory if LinuxCNC restarts.
  `LinuxCNCBridge.reconnect()` rebuilds them. **Untested on the Pi**: `kill` LinuxCNC
  mid-idle and mid-move and confirm the page returns to `ready`.
- E-stop during a move, tested on the Pi: the page goes to `estop` immediately and returns
  to `ready` on release. A later test failed with "must be in joint mode to home"; cause and
  fix are described under "E-stop handling" (joint mode before homing, and an interrupted move
  no longer reads stray LinuxCNC errors). Both fixes were tested against a stubbed LinuxCNC
  that reproduces the failure. Tested on the Pi since: an e-stop pressed while idle and one
  pressed mid-move behave identically. Any move in progress is abandoned, the LED flashes
  red/yellow, and clients are told and show the same. **Still to re-test on the Pi**:
  releasing the e-stop and returning to `ready` after each (the joint-mode homing fix).
- The Pi cannot detect loss of stepper driver power by any means other than the e-stop,
  which cuts it and is read on GPIO 24.
- Cutting stepper power mid-move can lose steps, so the table may sit slightly off from where
  the software thinks it is after an interrupted move. Harmless for relative moves.
- `LinuxCNCBridge._play_ready_chime()` (plays a C-major arpeggio through the stepper, then
  returns to A0) exists but its call in `startup()` is commented out, so it does not run.
- Ctrl-C (SIGINT) and SIGTERM just exit the server; they never power off the Pi.
- `FERROR = 50.0` is intentionally generous for open-loop stepper operation.
- Level shifter (Adafruit 757) works correctly once wiring was fixed — no replacement needed.
- Buck converter (Mean Well, 15W) on order for permanent Pi power from 24V DIN rail supply.
- `turntable.hal` currently wires `hal_gpio.GPIO24-in` straight to
  `iocontrol.0.emc-enable-in` (no `sets estop-in true` workaround). Confirm this is the
  intended final state now that the physical button and pull-up are wired.