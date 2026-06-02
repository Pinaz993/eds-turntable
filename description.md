# CNC Turntable Web Controller — Project Description
## For continuation of development in a new chat session

---

## Project Overview
A Raspberry Pi 4B-based CNC turntable controller for a machinist named Ed. The Pi runs
LinuxCNC to control a single stepper motor via a web interface served at `turntable.local`.
Ed scans a QR code, opens the control panel on his device, and commands the turntable to
rotate by a specified number of degrees in a specified direction.

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
- Flask-SocketIO 5.6.1
- eventlet 0.40.4
- gpiozero 2.0.1
- linuxcnc Python module (system package)

---

## Project Directory
`/home/turntable/linuxcnc/configs/turntable/`

```
turntable/
├── app.py                  # Flask-SocketIO server (NOT YET WRITTEN)
├── lcnc_bridge.py          # LinuxCNC Python API bridge (COMPLETE)
├── led.py                  # RGB LED status manager (COMPLETE)
├── shutdown_button.py      # Power button monitor (COMPLETE)
├── templates/
│   ├── control.html        # Control panel (NOT YET WRITTEN)
│   └── docs.html           # Documentation page (NOT YET WRITTEN)
├── static/
│   ├── control.js          # WebSocket logic (NOT YET WRITTEN)
│   └── style.css           # Styles (NOT YET WRITTEN)
├── turntable.ini           # LinuxCNC configuration (COMPLETE)
└── turntable.hal           # HAL configuration (COMPLETE)
```

## Systemd Services

### /etc/systemd/system/linuxcnc.service (complete)
```ini
[Unit]
Description=LinuxCNC Machine Controller
After=network.target

[Service]
Type=simple
User=turntable
WorkingDirectory=/home/turntable/linuxcnc/configs/turntable
ExecStart=/bin/bash -c 'echo "" | /usr/bin/linuxcnc /home/turntable/linuxcnc/configs/turntable/turntable.ini'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/turntable.service (NOT YET CREATED)
```ini
[Unit]
Description=Turntable Web Controller
After=network.target linuxcnc.service
Requires=linuxcnc.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/turntable/linuxcnc/configs/turntable
ExecStart=/usr/bin/python3 /home/turntable/linuxcnc/configs/turntable/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/shutdown-button.service (complete)
```ini
[Unit]
Description=Power Button Monitor
After=multi-user.target

[Service]
Type=simple
User=turntable
WorkingDirectory=/home/turntable/linuxcnc/configs/turntable
ExecStart=/usr/bin/python3 /home/turntable/linuxcnc/configs/turntable/shutdown_button.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/wifi-powersave-off.service (complete)
```ini
[Unit]
Description=Disable WiFi power saving
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw wlan0 set power_save off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

---

## Firewall
Port 5007 (linuxcncrsh) restricted to loopback only:
```
-A INPUT -i lo -p tcp -m tcp --dport 5007 -j ACCEPT
-A INPUT ! -i lo -p tcp -m tcp --dport 5007 -j DROP
```
Managed by `iptables-persistent`.

---

## What Needs To Be Written Next

### app.py
The main Flask-SocketIO application. Requirements:

**Startup sequence:**
1. Instantiate `StatusLED` and set to `booting`
2. Instantiate `LinuxCNCBridge` with callbacks
3. Start `bridge.startup()` in a background thread via `socketio.start_background_task()`
4. `on_ready` callback: set LED to `ready`
5. `on_error` callback: set LED to `system_error`, emit `machine_status` error to all clients

**Routes:**
- `GET /` → serve `control.html`
- `GET /docs` → serve `docs.html`

**Subnet filter:**
- Configurable at top of file (currently `172.17.1.` — will change at installation)
- Reject requests from outside local subnet with 403

**WebSocket events:**

Incoming `move_command`:
```json
{"degrees": 210, "direction": "CCW"}
```
- Sanitize: degrees must be numeric, 0 < x ≤ 360; direction must be "CW" or "CCW"
- If invalid: emit `move_error` to sender only, flash LED `command_rejected`
- If busy: emit `move_error` to sender only, flash LED `command_rejected`
- If valid: set busy, set LED `moving`, emit `move_started` to all,
  start `bridge.send_move()` in background thread

Outgoing events:
- `move_error` → requesting client only: `{"message": "..."}`
- `move_started` → all clients: `{"degrees": 210, "direction": "CCW"}`
- `move_complete` → all clients: `{}`
- `machine_status` → all clients: `{"state": "ready"|"moving"|"error"|"estop"}`

**E-stop monitoring:**
- Background thread polls `linuxcnc.stat()` for estop state changes
- On estop detected: set LED `estop_active`, emit `machine_status` estop to all clients
- On estop cleared: run `bridge._startup_sequence()` again to re-enable machine,
  set LED `ready`, emit `machine_status` ready to all clients
- Important: when E-stop is pressed, `s.task_state` goes to `STATE_OFF`.
  Recovery requires the full startup sequence including the ESTOP cycle quirk.

**Move completion callback:**
- Set LED to `ready`
- Emit `move_complete` to all clients

**Flask config:**
- `SECRET_KEY`: set to a random string
    - not to be distributed via Git when released
- `async_mode`: `eventlet`
- Host: `0.0.0.0`, port `80`
- Run as root (required for port 80)

### control.html / control.js
Single page control panel:
- Direction toggle switch (CW/CCW)
- Numeric input for degrees (positive only, 0 < x ≤ 360)
- Execute button
- Status indicator showing machine state
- Error toast for rejected commands (sender only)
- UI disabled while move is in progress or machine is in estop

### docs.html
Static documentation page explaining:
- How to use the control panel
- What the LED colors mean
- E-stop procedure

---

## Known Issues / Notes
- E-stop recovery requires full startup sequence rerun
- `linuxcnc` Python module loses its NML connection if LinuxCNC is restarted
  while the Python process is running — requires app restart too
- `FERROR = 50.0` is intentionally generous for open-loop stepper operation
- Level shifter (Adafruit 757) works correctly once wiring was fixed —
  no replacement needed
- Buck converter (Mean Well, 15W) on order for permanent Pi power from 24V DIN rail supply
- E-stop HAL line currently uses software workaround (`sets estop-in true`) —
  physical button and pull-up resistor now wired and working, workaround should be removed
