"""The lid camera on the BLTouch board: asking for a photo and reading it back.

The camera shares the probe's serial port and the probe's XIAO ESP32S3 Sense
(firmware/bltouch_camera). Probing comes first, always:

  * The caller holds the port's lock for the whole capture, the same lock
    `Sender.blt_serial_send` takes for each servo command, so a command and a
    capture never read each other's replies.
  * The app refuses to capture while the machine is running or Align & Run is
    probing (EngravingPage), so in practice that lock is never contended. If a
    servo command does arrive mid-capture, the firmware moves the servo at once
    and only its reply line waits.

Frame format (one capture):
    @IMG <seq> <length> <width> <height> <token>\\n
    @CHK <offset> <n> <crc32 hex> <token>\\n <n raw bytes>   until done
    @END <seq> <crc32 hex> <token>\\n
or  @ERR <seq> <reason>\\n

The host acknowledges each piece by echoing its <token> (K and J alternate),
and the board waits for that before sending the next, re-sending a piece that
is not acknowledged. The ESP32's USB serial driver drops writes under load, so
delivery is checked here; the alternating token stops a late acknowledgement
of a re-sent piece from confirming the piece after it.

Ordinary text lines ("Touch", a servo reply) can arrive between chunks. They
are whole lines and are passed back to the caller rather than dropped, so the
serial log stays complete.

Exposure. Firmware 2 and later can fix exposure, gain and white balance
('E' + six letters), so every photo is taken the same way and a lid's
brightness depends only on the lid; 'A' goes back to automatic. The values come
from the exposure calibration (LidExposure) and are kept per machine in a small
JSON file (`load_exposure` / `save_exposure`).

Pure Python over anything with read/readline/write/reset_input_buffer, so the
parser is tested without hardware.
"""

import json
import os
import re
import time
import zlib

IDENTIFY = b"I"
CAPTURE = b"C"
IDENTIFY_REPLY = "BLTCAM"

# A UXGA JPEG is 100-250 kB; USB full speed moves that in well under a second.
CAPTURE_TIMEOUT_S = 8.0
# Powering the camera up on the first capture adds a second or two.
FIRST_CAPTURE_TIMEOUT_S = 15.0
IDENTIFY_TIMEOUT_S = 1.0
MAX_IMAGE_BYTES = 2 * 1024 * 1024


class CameraError(Exception):
    pass


class Capture:
    """One photo: the JPEG bytes plus what the board said about it."""

    def __init__(self, jpeg, width, height, seq, other_lines):
        self.jpeg = jpeg
        self.width = width
        self.height = height
        self.seq = seq
        # Text lines that arrived during the capture ("Touch", servo replies).
        self.other_lines = other_lines


class _Reader:
    """Buffered reads off the port. pyserial's readline() fetches one byte per
    call, which is too slow to keep up with a 2 kB chunk arriving every few
    ms: acknowledgements fall behind and the board starts re-sending."""

    def __init__(self, port):
        self.port = port
        self.buf = bytearray()

    def _fill(self, deadline, what):
        if time.time() > deadline:
            raise CameraError(what)
        waiting = getattr(self.port, "in_waiting", 0) or 1
        part = self.port.read(max(waiting, 1))
        if part:
            self.buf += part

    def readline(self, deadline):
        """One line, stripped, or CameraError at the deadline."""
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = bytes(self.buf[:i + 1])
                del self.buf[:i + 1]
                return line.decode("ascii", "replace").strip()
            self._fill(deadline, "timed out waiting for the camera")

    def read_exact(self, n, deadline):
        while len(self.buf) < n:
            self._fill(deadline, "timed out reading image data")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def identify_version(port, timeout_s=IDENTIFY_TIMEOUT_S):
    """(firmware version, has a working camera). (0, False) for the old
    probe-only firmware, which ignores 'I'."""
    port.write(IDENTIFY)
    deadline = time.time() + timeout_s
    reader = _Reader(port)
    try:
        while True:
            line = reader.readline(deadline)
            if line.startswith(IDENTIFY_REPLY + " "):
                parts = line.split()
                try:
                    version = int(parts[1])
                except (IndexError, ValueError):
                    version = 1
                return version, parts[-1] == "camera"
    except CameraError:
        return 0, False


def identify(port, timeout_s=IDENTIFY_TIMEOUT_S):
    """True when the board runs the camera firmware and has a working camera,
    False when it runs the old probe-only firmware (which ignores 'I')."""
    return identify_version(port, timeout_s)[1]


# ------------------------------------------------------------------ exposure
EXPOSURE_VERSION = 2            # firmware that understands 'E' and 'A'
AEC_MAX = 1200
AGC_MAX = 30
WB_PRESETS = {0: "automatic", 1: "sunny", 2: "cloudy", 3: "office", 4: "home"}
EXPOSURE_REPLY = "BLTCAM-EXPOSURE"
# Per machine, next to the bCNC ini like the other lid camera files.
EXPOSURE_FILE_SUFFIX = "-lidcam-exposure.json"


def default_exposure_path():
    """Utils.iniUser + EXPOSURE_FILE_SUFFIX, for tools that run without bCNC's
    Utils (which needs the whole interface)."""
    return os.path.expanduser("~/.bCNC") + EXPOSURE_FILE_SUFFIX


def exposure_command(aec, agc, wb):
    """b'E' + six letters a-p (hex digits). Letters, never digits: a stray
    '1'-'4' on this port moves the probe pin."""
    aec, agc, wb = int(aec), int(agc), int(wb)
    if not (0 <= aec <= AEC_MAX and 0 <= agc <= AGC_MAX and wb in WB_PRESETS):
        raise ValueError("exposure out of range: aec=%s agc=%s wb=%s" % (aec, agc, wb))
    v = (aec << 12) | (agc << 4) | wb
    return b"E" + bytes(ord("a") + ((v >> (4 * i)) & 15) for i in range(5, -1, -1))


def _exposure_reply(port, timeout_s):
    deadline = time.time() + timeout_s
    reader = _Reader(port)
    while True:
        line = reader.readline(deadline)
        if line.startswith(EXPOSURE_REPLY):
            if line.split()[1:2] == ["bad"]:
                raise CameraError("the camera refused the exposure settings")
            return line


def set_exposure(port, aec, agc, wb, timeout_s=IDENTIFY_TIMEOUT_S):
    """Fix exposure, gain and white balance (firmware 2+). Takes effect from
    the next capture. The caller holds the port's lock."""
    port.write(exposure_command(aec, agc, wb))
    return _exposure_reply(port, timeout_s)


def set_auto_exposure(port, timeout_s=IDENTIFY_TIMEOUT_S):
    """Back to automatic exposure, gain and white balance (firmware 2+)."""
    port.write(b"A")
    return _exposure_reply(port, timeout_s)


def load_exposure(path):
    """The saved calibration, {"aec", "agc", "wb", ...}, or None."""
    try:
        with open(path) as f:
            rec = json.load(f)
        exposure_command(rec["aec"], rec["agc"], rec["wb"])     # validates
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_exposure(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1)
    os.replace(tmp, path)


def apply_saved_exposure(port, version, path):
    """After identify: fix the camera's exposure to the saved calibration, if
    there is one and the firmware can. Returns the record applied, or None
    (the camera stays automatic)."""
    if version < EXPOSURE_VERSION or not path:
        return None
    rec = load_exposure(path)
    if rec is None:
        return None
    set_exposure(port, rec["aec"], rec["agc"], rec["wb"])
    return rec


TAGS = ("@IMG", "@CHK", "@END", "@ERR")
ACK = b"K"
NAK = b"N"     # the last chunk arrived damaged: the board re-sends it at once
_CHK_HEADER = re.compile(rb"@CHK \d{1,7} \d{1,5} [0-9a-f]{8}(?: [KJ])?\r?\n")


def _tagged(line):
    """The protocol line inside `line`, or None. A header can arrive glued to
    the tail of a damaged chunk, so the last tag anywhere in the line counts."""
    best = -1
    for tag in TAGS:
        best = max(best, line.rfind(tag))
    return line[best:] if best >= 0 else None


def _token(parts):
    """The acknowledgement a piece asks for: its last field when that is K or J."""
    last = parts[-1] if parts else ""
    return last.encode() if last in ("K", "J") else ACK


def _printable(line):
    return line and all(32 <= ord(ch) < 127 for ch in line)


def read_capture(port, timeout_s=CAPTURE_TIMEOUT_S):
    """Parse one framed capture from the port. The 'C' must already be sent.

    Every @IMG and good @CHK is acknowledged with 'K'; the board re-sends
    anything not acknowledged. A chunk whose checksum fails is not
    acknowledged, so it comes again, and a repeat of one already stored is
    acknowledged and dropped.
    """
    deadline = time.time() + timeout_s
    reader = _Reader(port)
    other = []
    seq = length = width = height = None
    data = None
    received = 0
    while True:
        line = reader.readline(deadline)
        tagged = _tagged(line)
        if tagged is None:
            # Bytes whose header was lost: the board re-sends that chunk when
            # its acknowledgement does not come. No NAK here: one lost header
            # can leave several such lines, and each NAK costs a re-send.
            if _printable(line):
                other.append(line)
            continue
        parts = tagged.split()
        tag = parts[0]

        # A late @END or @ERR re-sent for an earlier photo is not this one's.
        try:
            msg_seq = int(parts[1])
        except (IndexError, ValueError):
            msg_seq = None
        if tag in ("@END", "@ERR") and seq is not None and msg_seq != seq:
            continue

        if tag == "@ERR":
            raise CameraError(" ".join(parts[2:]) or tagged)

        if tag == "@IMG":
            try:
                new_seq, new_len, new_w, new_h = (int(v) for v in parts[1:5])
            except ValueError:
                continue                    # damaged header: it will come again
            if seq is not None and new_seq != seq:
                continue
            seq, length, width, height = new_seq, new_len, new_w, new_h
            if not 0 < length <= MAX_IMAGE_BYTES:
                raise CameraError("implausible image size: %d bytes" % length)
            if data is None:
                data = bytearray(length)
            port.write(_token(parts))
            continue

        if tag == "@CHK":
            try:
                off, n, crc = int(parts[1]), int(parts[2]), int(parts[3], 16)
            except (IndexError, ValueError):
                continue
            if data is None or n <= 0 or off + n > length:
                continue
            chunk = reader.read_exact(n, deadline)
            if zlib.crc32(chunk) & 0xFFFFFFFF != crc:
                # A short (partly dropped) chunk makes the fixed-length read
                # run on into whatever came next, usually its re-send. Put
                # back everything from the first header found in it, so the
                # parser picks up from there instead of staying out of step.
                # Only a complete chunk header counts: image bytes can contain
                # "@END" or "@CHK" by chance, but hardly this whole pattern.
                m = _CHK_HEADER.search(chunk)
                if m:
                    reader.buf[0:0] = chunk[m.start():]
                else:
                    port.write(NAK)
                continue
            if off == received:
                data[off:off + n] = chunk
                received += n
            elif off > received:
                continue                    # cannot happen unless lost sync
            port.write(_token(parts))       # a repeat is acknowledged again
            continue

        if tag == "@END":
            if data is None:
                continue                    # an earlier photo's, re-sent
            port.write(_token(parts))
            if received != length:
                raise CameraError("image ended after %d of %s bytes" % (received, length))
            if len(parts) >= 3:
                want = int(parts[2], 16)
                if zlib.crc32(bytes(data)) & 0xFFFFFFFF != want:
                    raise CameraError("image checksum mismatch")
            return Capture(bytes(data), width, height, seq, other)


def capture(port, first=False):
    """Ask for a photo and read it. The caller holds the port's lock."""
    port.write(CAPTURE)
    return read_capture(port, FIRST_CAPTURE_TIMEOUT_S if first else CAPTURE_TIMEOUT_S)
