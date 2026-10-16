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
    @IMG <seq> <length> <width> <height>\\n
    @CHK <offset> <n>\\n <n raw bytes>          until <length> bytes are sent
    @END <seq> <crc32 hex>\\n
or  @ERR <seq> <reason>\\n

Ordinary text lines ("Touch", a servo reply) can arrive between chunks. They
are whole lines and are passed back to the caller rather than dropped, so the
serial log stays complete.

Pure Python over anything with read/readline/write/reset_input_buffer, so the
parser is tested without hardware.
"""

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


def _readline(port, deadline):
    """One line, or CameraError at the deadline. Partial reads are joined: the
    port's own timeout is short, and a line can straddle several reads."""
    buf = b""
    while True:
        if time.time() > deadline:
            raise CameraError("timed out waiting for the camera")
        part = port.readline()
        if part:
            buf += part
            if buf.endswith(b"\n"):
                return buf.decode("ascii", "replace").strip()


def _read_exact(port, n, deadline):
    buf = bytearray()
    while len(buf) < n:
        if time.time() > deadline:
            raise CameraError("timed out reading image data")
        part = port.read(n - len(buf))
        if part:
            buf += part
    return bytes(buf)


def identify(port, timeout_s=IDENTIFY_TIMEOUT_S):
    """True when the board runs the camera firmware and has a working camera,
    False when it runs the old probe-only firmware (which ignores 'I')."""
    port.write(IDENTIFY)
    deadline = time.time() + timeout_s
    try:
        while True:
            line = _readline(port, deadline)
            if line.startswith(IDENTIFY_REPLY):
                return line.split()[-1] == "camera"
    except CameraError:
        return False


def read_capture(port, timeout_s=CAPTURE_TIMEOUT_S):
    """Parse one framed capture from the port. The 'C' must already be sent."""
    deadline = time.time() + timeout_s
    other = []
    header = None
    while header is None:
        line = _readline(port, deadline)
        if line.startswith("@ERR"):
            raise CameraError(line.split(None, 2)[-1] if len(line.split()) > 2 else line)
        if line.startswith("@IMG"):
            header = line
        elif line:
            other.append(line)

    try:
        _tag, seq, length, width, height = header.split()
        seq, length, width, height = int(seq), int(length), int(width), int(height)
    except ValueError:
        raise CameraError("bad image header: %r" % header)
    if not 0 < length <= MAX_IMAGE_BYTES:
        raise CameraError("implausible image size: %d bytes" % length)

    data = bytearray(length)
    received = 0
    while True:
        line = _readline(port, deadline)
        if line.startswith("@CHK"):
            try:
                _tag, off, n = line.split()
                off, n = int(off), int(n)
            except ValueError:
                raise CameraError("bad chunk header: %r" % line)
            if off != received or n <= 0 or off + n > length:
                raise CameraError("chunk out of order at byte %d" % off)
            data[off:off + n] = _read_exact(port, n, deadline)
            received += n
        elif line.startswith("@END"):
            parts = line.split()
            if received != length:
                raise CameraError("image ended after %d of %d bytes" % (received, length))
            if len(parts) >= 3:
                want = int(parts[2], 16)
                got = zlib.crc32(bytes(data)) & 0xFFFFFFFF
                if want != got:
                    raise CameraError("image checksum mismatch")
            return Capture(bytes(data), width, height, seq, other)
        elif line.startswith("@ERR"):
            raise CameraError(line)
        elif line:
            other.append(line)


def capture(port, first=False):
    """Ask for a photo and read it. The caller holds the port's lock."""
    port.write(CAPTURE)
    return read_capture(port, FIRST_CAPTURE_TIMEOUT_S if first else CAPTURE_TIMEOUT_S)
