# One-click machine setup for the File tab: reconnect the CNC and the BLTouch
# on the right ports, check the probe moves, home, sanity-check the work origin
# and move to it.
#
# The sequence is a generator driven by Tk's after(), so the UI keeps
# repainting (and the Cancel button keeps working) between steps. A step
# yields the number of milliseconds to wait before it is resumed.

import re
import threading
import time
from tkinter import messagebox

from CNC import CNC, WCS
from CNCRibbon import Page
from Sender import CONNECTED, NOT_CONNECTED
import Utils

try:
    import serial
except ImportError:
    serial = None

# Machine Z of the work origin on a correctly set up machine, and how far below
# it an origin may sit before G0Z0 risks driving into the table. X/Y only need
# to be inside the machine travel. Overridable in [AutoSetup].
EXPECTED_ORIGIN_Z = -76.6
ORIGIN_TOLERANCE_Z = 10.0

PROBE_CYCLES = 3
HOMING_TIMEOUT_S = 180
PROBE_SETTLE_MS = 800
POLL_MS = 100

GRBL_STATUS = re.compile(rb"<(Idle|Alarm|Run|Jog|Home|Hold|Door|Check|Sleep)")


class SetupError(Exception):
    pass


# -----------------------------------------------------------------------------
def _is_alarm(state):
    return state.upper().startswith("ALARM")


def _usb_ports(ports):
    """Only USB serial devices: opening a Bluetooth COM port can hang."""
    return [p[0] for p in ports if "VID:PID" in str(p[2]).upper()]


def _is_grbl(device, baud):
    """Open device and listen for GRBL. Runs on a worker thread.

    Opening asserts DTR, which resets an Arduino-based controller into
    printing its "Grbl x.y" banner. Boards that do not reset are asked for a
    status report instead. "?" is harmless to the probe's board, which only
    acts on the digits 1-4.
    """
    try:
        port = serial.Serial(device, int(baud), timeout=0.1)
    except Exception as e:
        print(f"[AUTOSETUP] {device}: cannot open ({e})")
        return False
    try:
        buf = b""
        end = time.time() + 2.5
        while time.time() < end and b"Grbl" not in buf:
            buf += port.read(256)
        if b"Grbl" in buf:
            return True
        port.write(b"?")
        end = time.time() + 1.0
        while time.time() < end and not GRBL_STATUS.search(buf):
            buf += port.read(256)
        return bool(GRBL_STATUS.search(buf))
    except Exception as e:
        print(f"[AUTOSETUP] {device}: read failed ({e})")
        return False
    finally:
        print(f"[AUTOSETUP] {device} replied: {buf!r}")
        try:
            port.close()
        except Exception:
            pass


# =============================================================================
class AutoSetup:
    def __init__(self, app, report):
        """report(msg, color) shows progress; color is None while running."""
        self.app = app
        self.report = report
        self._gen = None
        self._after_id = None
        self.warnings = []

    # ----------------------------------------------------------------------
    @property
    def active(self):
        return self._gen is not None

    def start(self):
        if self.active:
            return
        if self.app.running or self._engraving_busy():
            messagebox.showerror(
                _("Auto Setup"),
                _("Stop the running job before running Auto Setup."),
                parent=self.app)
            return
        if serial is None:
            self.app.showSerialError()
            return
        if not messagebox.askokcancel(
                _("Auto Setup"),
                _("Auto Setup will reconnect the CNC and probe, cycle the "
                  "probe, home the machine and move to the "
                  "work origin.\n\nMake sure the work area is clear."),
                parent=self.app):
            return
        self.warnings = []
        self._gen = self._steps()
        self._tick()

    def cancel(self):
        if not self.active:
            return
        self._finish()
        # Homing and rapids ignore feed hold; only a reset stops them. Homing
        # still reads "Alarm" (no status reports), so reset unless Idle.
        if self.app.serial is not None and CNC.vars["state"] != "Idle":
            self.app.mcontrol.softReset(False)
        self._retract()
        self.report(_("Auto Setup cancelled"), "Salmon")

    # ----------------------------------------------------------------------
    def _tick(self):
        self._after_id = None
        try:
            delay = next(self._gen)
        except StopIteration:
            self._finish()
            if self.warnings:
                messagebox.showwarning(
                    _("Auto Setup finished with warnings"),
                    "\n\n".join(self.warnings), parent=self.app)
            self.report(_("Auto Setup complete"), "LightGreen")
            return
        except SetupError as e:
            self._fail(str(e))
            return
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._fail(_("Unexpected error: {}").format(e))
            return
        self._after_id = self.app.after(delay, self._tick)

    def _finish(self):
        if self._after_id is not None:
            self.app.after_cancel(self._after_id)
            self._after_id = None
        if self._gen is not None:
            self._gen.close()
            self._gen = None

    def _fail(self, msg):
        self._finish()
        self._retract()
        self.report(_("Auto Setup failed"), "Salmon")
        messagebox.showerror(_("Auto Setup failed"), msg, parent=self.app)

    def _status(self, msg):
        print(f"[AUTOSETUP] {msg}")
        self.report(msg, None)
        self.app.setStatus(msg)

    def _wait_for(self, cond, timeout_s, error):
        end = time.time() + timeout_s
        while not cond():
            if time.time() > end:
                raise SetupError(error() if callable(error) else error)
            yield POLL_MS

    def _retract(self):
        if self.app.blt_serial is not None:
            try:
                self.app.blt_serial_send("2")
            except Exception as e:
                print(f"[AUTOSETUP] retract failed: {e}")

    def _engraving_busy(self):
        engravings = getattr(self.app, "lid_engravings", None)
        return engravings is not None and engravings.busy()

    # ----------------------------------------------------------------------
    def _steps(self):
        yield from self._disconnect()
        cnc_port, probe_port = yield from self._find_ports()
        yield from self._connect(cnc_port, probe_port)
        yield from self._cycle_probe()
        yield from self._home()
        origin = yield from self._read_origin()
        safe = self._check_origin(origin)
        yield from self._move_to_origin(safe)

    # ----------------------------------------------------------------------
    def _disconnect(self):
        self._status(_("Disconnecting..."))
        if self.app.serial is not None:
            self.app.openClose()
        if self.app.blt_serial is not None:
            self.app.openCloseBLTouch()
        # Windows can refuse to reopen a port for a moment after closing it.
        yield 500

    def _find_ports(self):
        self._status(_("Refreshing serial ports..."))
        cnc_frame = Page.frames["Serial"]
        probe_frame = Page.frames["BLTouch"]
        cnc_frame.comportRefresh()
        probe_frame.comportRefresh()
        candidates = _usb_ports(cnc_frame.comportsGet())
        print(f"[AUTOSETUP] USB serial ports: {candidates}")
        if len(candidates) < 2:
            raise SetupError(
                _("Expected at least 2 USB serial ports (CNC and probe), "
                  "found: {}.\n\nCheck that both USB cables are plugged in "
                  "and powered.").format(", ".join(candidates) or _("none")))

        self._status(_("Identifying the CNC controller..."))
        baud = cnc_frame.baudCombo.get() or "115200"
        grbl = {}

        def scan():
            for device in candidates:
                grbl[device] = _is_grbl(device, baud)

        worker = threading.Thread(target=scan, daemon=True)
        worker.start()
        yield from self._wait_for(
            lambda: not worker.is_alive(), 10 + 5 * len(candidates),
            _("Timed out identifying serial ports."))

        cnc_ports = [d for d in candidates if grbl.get(d)]
        others = [d for d in candidates if not grbl.get(d)]
        if len(cnc_ports) != 1:
            raise SetupError(
                _("Expected exactly one GRBL controller, found {} on: {}.\n\n"
                  "Check the CNC is powered and no other program has its "
                  "port open.").format(
                    len(cnc_ports), ", ".join(cnc_ports) or ", ".join(candidates)))
        cnc_port = cnc_ports[0]

        # The probe board is silent, so it can only be told apart as the port
        # that is not GRBL. With more than one left, trust the saved choice.
        saved = probe_frame.portCombo.get().split("\t")[0]
        if len(others) == 1:
            probe_port = others[0]
        elif saved in others:
            probe_port = saved
        else:
            raise SetupError(
                _("CNC found on {}, but cannot tell which of {} is the probe."
                  "\n\nSelect the probe port in the BLTouch panel, connect it "
                  "once, then run Auto Setup again.").format(
                    cnc_port, ", ".join(others)))
        print(f"[AUTOSETUP] CNC={cnc_port} probe={probe_port}")
        return cnc_port, probe_port

    def _connect(self, cnc_port, probe_port):
        self._status(_("Connecting CNC on {}...").format(cnc_port))
        Page.frames["Serial"].portCombo.set(cnc_port)
        self.app.openClose()
        if self.app.serial is None:
            raise SetupError(_("Could not open the CNC on {}.").format(cnc_port))
        yield from self._wait_for(
            lambda: CNC.vars["state"] not in (CONNECTED, NOT_CONNECTED),
            10, _("The CNC on {} is not reporting its status.").format(cnc_port))

        self._status(_("Connecting probe on {}...").format(probe_port))
        Page.frames["BLTouch"].portCombo.set(probe_port)
        self.app.openCloseBLTouch()
        if self.app.blt_serial is None:
            raise SetupError(_("Could not open the probe on {}.").format(probe_port))
        yield from self._wait_probe_boot()

    def _wait_probe_boot(self):
        """Opening the port reboots the probe's ESP32. Its boot log is read and
        discarded so each command's logged response is the firmware's reply,
        not a leftover boot line."""
        self._status(_("Waiting for the probe to start..."))
        port = self.app.blt_serial
        start = last = time.time()
        log = b""
        while time.time() - start < 10:
            try:
                data = port.read(port.in_waiting or 0)
            except Exception as e:
                raise SetupError(_("Lost the probe connection: {}").format(e))
            if data:
                log += data
                last = time.time()
            elif time.time() - last > 1.0 and time.time() - start > 2.0:
                break
            yield POLL_MS
        print(f"[AUTOSETUP] probe boot log: {log!r}")

    def _probe_send(self, cmd):
        try:
            ok = self.app.blt_serial_send(cmd)
        except Exception as e:
            raise SetupError(_("Probe did not accept a command: {}").format(e))
        if ok is False:
            raise SetupError(_("Probe did not accept a command."))

    def _cycle_probe(self):
        # Reset first: a BLTouch in alarm (blinking) ignores deploy.
        self._status(_("Resetting probe..."))
        self._probe_send("4")
        yield PROBE_SETTLE_MS
        triggered = 0
        for i in range(PROBE_CYCLES):
            self._status(_("Probe check {}/{}: deploy").format(i + 1, PROBE_CYCLES))
            self._probe_send("1")
            yield PROBE_SETTLE_MS
            # A deployed pin must read untriggered, or every G38 probe fails.
            if "P" in CNC.vars.get("pins", ""):
                triggered += 1
            self._status(_("Probe check {}/{}: retract").format(i + 1, PROBE_CYCLES))
            self._probe_send("2")
            yield PROBE_SETTLE_MS
        if triggered:
            self.warnings.append(
                _("The controller's probe input read triggered while the probe "
                  "was deployed ({}/{} times). Probing will fail until this is "
                  "fixed: check the probe wiring and that the pin moves freely."
                  ).format(triggered, PROBE_CYCLES))

    def _home(self):
        """Home and wait for $H to be answered.

        GRBL sends no status reports while homing, so the state stays at the
        "Alarm" of the homing lock until it finishes. Completion is instead the
        controller's reply to $H; a failure arrives as an ALARM:n or error:n line.
        """
        self._status(_("Homing ($H)..."))
        replies = self.app._gcount
        self.app.mcontrol.home()

        def done():
            state = CNC.vars["state"]
            if state.startswith(("ALARM:", "error:")):
                raise SetupError(_("Homing failed: {}").format(state))
            return self.app._gcount > replies and state == "Idle"

        yield from self._wait_for(
            done, Utils.getFloat("AutoSetup", "homingTimeout", HOMING_TIMEOUT_S),
            lambda: _("Timed out waiting for homing (controller state: {})."
                      ).format(CNC.vars["state"]))

    def _read_origin(self):
        """Machine position of the active work origin, cross-checked two ways.

        The status report's WCO is what the DRO uses, but it only arrives every
        few reports and may still be from before the reconnect. $# reports the
        stored offsets; WCO is trusted once it agrees with them.
        """
        self._status(_("Reading work origin..."))
        for wcs in WCS:
            CNC.vars.pop(wcs, None)
        self.app.sendGCode("$$")  # max travel ($130-$132) for the move check
        self.app.sendGCode("$G")
        self.app.sendGCode("$#")

        def num(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def consistent():
            offsets = CNC.vars.get(CNC.vars.get("WCS", "G54"))
            if not offsets or len(offsets) < 3:
                return False
            expected = (
                num(offsets[0]) + num(CNC.vars.get("G92X")),
                num(offsets[1]) + num(CNC.vars.get("G92Y")),
                num(offsets[2]) + num(CNC.vars.get("G92Z")) + num(CNC.vars.get("TLO")),
            )
            wco = (CNC.vars["wcox"], CNC.vars["wcoy"], CNC.vars["wcoz"])
            return all(abs(a - b) < 0.05 for a, b in zip(expected, wco))

        yield from self._wait_for(
            consistent, 8,
            _("The controller's work offsets ($#) do not match its reported "
              "work origin. Check the offsets in the Terminal tab."))
        return CNC.vars["wcox"], CNC.vars["wcoy"], CNC.vars["wcoz"]

    def _check_origin(self, origin):
        lowest = (Utils.getFloat("AutoSetup", "originZ", EXPECTED_ORIGIN_Z)
                  - Utils.getFloat("AutoSetup", "toleranceZ", ORIGIN_TOLERANCE_Z))
        print(f"[AUTOSETUP] origin MPos={origin} lowest Z={lowest}")
        if origin[2] < lowest:
            raise SetupError(
                _("The work origin Z is too low, so the machine was not "
                  "moved: machine Z {:.3f}, lowest allowed {:.1f}.\n\n"
                  "Moving to Z0 could hit the table. Re-zero Z, then run "
                  "Auto Setup again.").format(origin[2], lowest))

        # GRBL's homed machine space is negative, down to -$130..$132. The
        # move passes through safe Z above the origin, then the origin itself.
        safe = CNC.vars["safe"]
        points = (("X", origin[0]), ("Y", origin[1]),
                  ("Z", origin[2]), ("Z", origin[2] + safe))
        for axis, t in points:
            travel = CNC.vars.get(f"grbl_13{'XYZ'.index(axis)}")
            low = -float(travel) if travel else None
            if t > 0 or (low is not None and t < low):
                raise SetupError(
                    _("Moving to the origin would put {} at machine {:.3f}, "
                      "outside the machine travel.").format(axis, t))
        return safe

    def _move_to_origin(self, safe):
        self._status(_("Moving to the origin..."))
        self.app.sendGCode("G90")
        self.app.sendGCode(f"G0Z{safe:g}")
        self.app.sendGCode("G0X0Y0")
        self.app.sendGCode("G0Z0")

        def arrived():
            if _is_alarm(CNC.vars["state"]):
                raise SetupError(
                    _("Alarm while moving: {}").format(CNC.vars["state"]))
            return (CNC.vars["state"] == "Idle"
                    and self.app.queue.qsize() == 0
                    and all(abs(CNC.vars[w]) < 0.05 for w in ("wx", "wy", "wz")))

        yield from self._wait_for(
            arrived, 60, _("Timed out moving to the origin."))
