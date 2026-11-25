# On-Pi test plan

Everything here can be done **with the stepper driver dead**. The machine is open-loop
(no position feedback in `turntable.hal`) and `[JOINT_0]` has no home switch, so homing
and moves complete in software whether or not anything turns. That makes the state flow
(booting, ready, moving, estop, error) fully testable. Physical rotation is **not** tested.

Run as the `turntable` user unless noted. Watch the journal in a second terminal:
`journalctl -fu turntable-server -u linuxcnc`. Fill in the Result column as you go
(PASS / FAIL / date / notes). Keep the e-stop and a second client (phone or laptop on the LAN) handy.

## 0. Pre-flight
| # | Check | Expect | Result |
|---|-------|--------|--------|
| 0.1 | `sudo visudo -cf /etc/sudoers.d/turntable-shutdown`, then `ls -l` it | parsed OK; `-r--r----- root root` | pass|
| 0.2 | `sudo -n -l` | lists `/usr/sbin/shutdown -h now` NOPASSWD | pass|
| 0.3 | `systemctl show turntable-server -p User -p WatchdogUSec -p Restart` | `turntable`, `1min`, `always` | pass|
| 0.4 | `ls -l /run/turntable/control.sock` | `srw-------` owned by `turntable` | pass|
| 0.5 | journal has `Watchdog heartbeat every 20.0s` | present | pass|

## 1. Startup and e-stop (no motor needed)
| # | Steps | Expect | Result |
|---|-------|--------|--------|
| 1.1 | Reboot, e-stop released | LED red while booting, then green; page `ready` | pass|
| 1.2 | Reboot with e-stop **pressed** | `estop` (red/yellow); release it: back to `ready`. This is the joint-mode homing re-test | pass|
| 1.3 | E-stop press and release while idle, three times | `estop` then `ready` each time | pass|
| 1.4 | Two clients open; press e-stop | both show `estop` at once | pass|

## 2. Moves (software flow only)
| # | Steps | Expect | Result |
|---|-------|--------|--------|
| 2.1 | 10 deg CW, then 10 deg CCW | `moving` (blue), then `ready`; `move_complete` fires | awating logging|
| 2.2 | From client A send 360 deg; from client B send another immediately | B rejected ("busy"), yellow flash | |
| 2.3 | Send 0, 361, -5, direction `cw`/`X` (browser console `socket.emit`) | each rejected, no state change | |
| 2.4 | Send 360 deg, press e-stop mid-move | `estop` right away, no `move_complete`; release: `ready` | |
| 2.5 | During `estop`, spam a bad command | LED stays red/yellow (flash must not replace it) | |

## 3. Faults
| # | Steps | Expect | Result |
|---|-------|--------|--------|
| 3.1 | Drives off with no e-stop: `python3 -c "import linuxcnc as l; l.command().state(l.STATE_OFF)"` | error "Drives disabled without an e-stop", recoverable; e-stop press and release returns to `ready` | |
| 3.2 | Same as 3.1, but during a 360 deg move | same error, busy cleared, no stuck `moving` | |
| 3.3 | `sudo systemctl restart linuxcnc` while idle | `booting` within about 2 s, then `ready` on its own | |
| 3.4 | `sudo pkill -9 -f linuxcnc` (idle) | systemd restarts it; page recovers to `ready` | |
| 3.5 | Repeat 3.4 mid-move | page leaves `moving`, recovers | |
| 3.6 | `sudo systemctl restart turntable-server` | Pi does **not** power off (the SIGTERM fix); clients reconnect; socket recreated, mode 0600 | |
| 3.7 | `sudo kill -STOP $(systemctl show -p MainPID --value turntable-server)` | no heartbeats; systemd restarts it after about 60 s | |

## 4. Shutdown and button
| # | Steps | Expect | Result |
|---|-------|--------|--------|
| 4.1 | `echo hi \| socat - UNIX-CONNECT:/run/turntable/control.sock` | replies `unknown command`, nothing else happens | |
| 4.2 | `sudo mv /etc/sudoers.d/turntable-shutdown /root/`, press button | red/blue for 3 s, then red blinking error "shutdown command failed"; page not stuck on shutdown | |
| 4.3 | Same as 4.2 but with an error already showing (do 3.1 first, leave it) | the page leaves "Shutting down". **Expected to FAIL today**: known bug, `on_error` returns early without publishing | |
| 4.4 | Restore the sudoers file. Stop the server, press button | direct fallback shutdown works | |
| 4.5 | Restart the server. Press button | red/blue LED, page shows shutdown, Pi halts (do this last) | |

## 5. Network
| # | Steps | Expect | Result |
|---|-------|--------|--------|
| 5.1 | Load the page from a LAN client | loads | |
| 5.2 | Load it from outside the subnet (other VLAN/VPN), if available | 403 | |
| 5.3 | Boot with the Ethernet cable out for more than 2 min, then plug it in | **Expected to FAIL today**: red blinking, never recovers without a restart. Confirms open item #6 | |

## 6. Platform
| # | Check | Expect | Result |
|---|-------|--------|--------|
| 6.1 | `ls /dev/watchdog*`; `grep -i watchdog /etc/systemd/system.conf` | device exists; `RuntimeWatchdogSec` set (currently not) | |
| 6.2 | `journalctl --disk-usage`; `grep -i storage /etc/systemd/journald.conf` | note size and mode (SD wear) | |
| 6.3 | Box **closed**, an hour idle plus a few moves: `vcgencmd measure_temp`; `vcgencmd get_throttled` | under 70 C; `0x0` (steel box, no airflow) | |
| 6.4 | Stop `linuxcnc`, run `latency-test` for 10+ min under `stress-ng --cpu 4 --io 2` | max base-thread latency well under the 50 us period | |
| 6.5 | After the Mean Well buck converter is installed, repeat 6.3 | `get_throttled` still `0x0` (no under-voltage) | |

## Results log
| Date | Commit | Tester | Sections run | Notes |
|------|--------|--------|--------------|-------|
| | | | | |

## Not testable until the driver is repaired
Actual rotation; the audible ready chime (disabled anyway); step loss after a mid-move
e-stop; whether a move really finishes (open-loop, so software can't tell).
