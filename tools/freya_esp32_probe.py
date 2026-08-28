"""
Ask a Freya ESP32 board which firmware it runs.

The shared probe. Imported by freya_esp32.py (the resolver) and esp32_scan.py
(the bench tool) so there is one description of how these boards are asked and
one place to fix it when a fourth board appears.

WHY ASKING IS THE ONLY WAY. The three boards -- science, drilling, astro-bio --
are ESP32-DevKitC behind a CP2102 and are DESCRIPTOR-IDENTICAL: same 10c4:ea60,
same Silicon Labs factory serial "0001", same product string. cp210x-cfg cannot
write the EEPROM. They also collide in /dev/serial/by-id, where all three claim
the same filename -- so with two attached that link resolves to an arbitrary one
while looking perfectly unambiguous. Nothing about the port identifies the board.

THREE REQUEST FORMS, because the boards do not agree on how to be asked and a
host meeting an unknown board cannot know which it is:

    id\\n                                    astro-bio, and science once
                                            BrnoMarsRover/as7265x#? lands
    {"request_id":...,"cmd":"ping"}\\n        science today
    02 01 06 FA 03                          drilling, CMD_GET_ID

Sending the wrong form is harmless. The science module answers INVALID_JSON;
astro-bio truncates a long line into its 32-byte command buffer where
takeCommand() discards it, and ignores the binary frame the same way; the drill's
parser discards any byte that is not STX while it is waiting for one.

THREE ANSWER SHAPES, likewise:

    {"firmware":...}                        astro-bio: top level
    {"ok":true,"data":{"firmware":...}}     science: dispatch() nests under data
    02 LEN 06 "name version" CKSUM ETX      drilling: binary, no newline at all

The third is why reading here is byte-oriented rather than line-oriented: a
readline() would sit until the timeout with the drill's answer already in the
buffer.
"""

import json
import time

import serial

CP2102 = (0x10C4, 0xEA60)
BAUDS = (115200, 921600)
PROBE_TIMEOUT = 2.5
RESEND_INTERVAL = 0.4
MAX_BUFFER = 65536

SCIENCE = "freya-science-module"
DRILLING = "freya-drilling-module"
ASTRO_BIO = "freya-astro-bio-module"

# RoverComm framing, from Drilling/code/mainBoardProgram/src/RoverComm.
STX = 0x02
ETX = 0x03
CMD_GET_ID = 0x06

# The ESP32 boot ROM banner. Seeing this during a probe means opening the port
# reset the board despite DTR/RTS being held low -- worth reporting, because it
# means no probe on this machine is safe against a board that is mid-run.
RESET_MARKERS = ("rst:0x", "boot:0x", "ets ", "SPI_FAST_FLASH_BOOT")


def _drill_request():
    """CMD_GET_ID as a RoverComm frame: STX LEN PAYLOAD CKSUM ETX."""
    payload = bytes([CMD_GET_ID])
    checksum = (-sum(payload)) & 0xFF

    return bytes([STX, len(payload)]) + payload + bytes([checksum, ETX])


# ORDER AND TERMINATION MATTER. The binary frame goes FIRST and is followed by
# a newline, because it has no terminator of its own: astro-bio accumulates raw
# bytes in a 32-byte command buffer until a newline arrives, so an unterminated
# frame gets glued onto the front of whatever follows. Sending `id` after a bare
# frame produced "\x02\x01\x06\xfa\x03id", which matches no command and is
# silently discarded by takeCommand() -- the board answers nothing and looks
# dead. The trailing newline flushes that buffer (the junk is discarded) so the
# `id` behind it arrives clean. The drill ignores a stray 0x0A: its parser is in
# WAIT_START and discards every byte that is not STX.
PROBES = (
    _drill_request() + b"\n",
    b"id\n",
    b'{"request_id":"esp32-probe","cmd":"ping"}\n',
)


def firmware_of(frame):
    """The firmware name, wherever this board chose to put it."""
    if not isinstance(frame, dict):
        return None

    name = frame.get("firmware")

    if name:
        return name

    data = frame.get("data")

    if isinstance(data, dict):
        return data.get("firmware")

    return None


def normalize(frame):
    """One shape for callers, whichever shape the board sent."""
    if frame.get("firmware"):
        return frame

    return dict(frame.get("data") or {})


def drill_payload(buf):
    """
    The payload of the first checksum-valid RoverComm frame in buf, or None.

    Resynchronizing on every byte rather than assuming the buffer starts at a
    frame: the probe writes arrive while the board may still be mid-boot, so
    there is usually noise in front of the answer.
    """
    start = 0

    while True:
        start = buf.find(bytes([STX]), start)

        if start < 0 or start + 2 >= len(buf):
            return None

        length = buf[start + 1]
        checksum_at = start + 2 + length
        end = checksum_at + 1

        if length == 0 or end >= len(buf) or buf[end] != ETX:
            start += 1
            continue

        payload = bytes(buf[start + 2:checksum_at])

        if (sum(payload) + buf[checksum_at]) & 0xFF != 0:
            start += 1
            continue

        return payload


def _identity_from_drill(payload):
    """{"firmware":..., "version":...} from a CMD_GET_ID payload."""
    text = payload[1:].decode("utf-8", "replace").strip()
    name, _, version = text.rpartition(" ")

    return {"firmware": name or text, "version": version or "?"}


def open_quietly(device, baudrate=None):
    """
    Open without asking the board to reset, and report whether that held.

    DTR and RTS are driven low BEFORE open, which pySerial otherwise asserts.
    On the science module that was measured to leave the firmware running. On
    astro-bio it does NOT hold -- every open produces a POWERON_RESET -- so the
    caller is told and probing stays a startup-only activity.
    """
    handle = serial.Serial()
    handle.port = device

    # BEFORE open, not after. pySerial defaults to 9600, and opening at the
    # wrong rate then switching leaves framing garbage in the driver buffer --
    # which surfaces as "device reports readiness to read but returned no
    # data", an error message that blames the cable for a software mistake.
    handle.baudrate = baudrate or BAUDS[0]
    handle.timeout = 0.2
    handle.dtr = False
    handle.rts = False
    handle.open()
    handle.dtr = False
    handle.rts = False
    handle.reset_input_buffer()
    handle.reset_output_buffer()

    return handle


def probe(device, bauds=BAUDS, timeout=PROBE_TIMEOUT):
    """
    (identity, baud, heard, reset_seen) for whoever is on this port.

    identity is a dict carrying at least "firmware", or None if nothing
    answered. `heard` is every line and frame seen, for the bench tool to show.

    Each baud is tried on the SAME open handle: pySerial reconfigures the line
    in place, so the port is opened once per board. Every open is a chance to
    reset the board, so doing it once rather than once per baud matters.
    """
    heard = []

    try:
        handle = open_quietly(device)
    except (serial.SerialException, OSError) as error:
        return None, None, ["<could not open: {}>".format(error)], False

    def reset_seen():
        return any(m in h for h in heard for m in RESET_MARKERS)

    try:
        for baud in bauds:
            handle.baudrate = baud
            handle.reset_input_buffer()
            handle.reset_output_buffer()

            buf = bytearray()
            deadline = time.monotonic() + timeout
            next_send = 0.0

            while time.monotonic() < deadline:
                now = time.monotonic()

                # Re-sent throughout the window, not once at the top. Opening
                # resets these boards, so a request written at t=0 lands in the
                # bootloader and is silently lost -- which on the wire looks
                # EXACTLY like a board that has no identity command at all.
                if now >= next_send:
                    for request in PROBES:
                        handle.write(request)

                    handle.flush()
                    next_send = now + RESEND_INTERVAL

                chunk = handle.read(handle.in_waiting or 1)

                if chunk:
                    buf.extend(chunk)

                if len(buf) > MAX_BUFFER:
                    del buf[:-MAX_BUFFER]

                payload = drill_payload(buf)

                # len > 1 because a CMD_GET_ID RESPONSE carries the name after
                # the command code, while a bare one-byte `06` payload is OUR
                # OWN REQUEST coming back -- astro-bio accumulates the binary
                # probe in its command buffer and prints it on the next
                # newline, which reproduces a byte-perfect, checksum-valid
                # frame. Without this check the scan identifies every astro-bio
                # board as a drill with an empty name.
                if payload and len(payload) > 1 and payload[0] == CMD_GET_ID:
                    identity = _identity_from_drill(payload)
                    heard.append("{:>6}  <frame> {} {}".format(
                        baud, identity["firmware"], identity["version"]))

                    return identity, baud, heard, reset_seen()

                while b"\n" in buf:
                    raw, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)

                    line = raw.decode("utf-8", "replace").strip()

                    if not line:
                        continue

                    heard.append("{:>6}  {}".format(baud, line))

                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue

                    if firmware_of(frame):
                        return normalize(frame), baud, heard, reset_seen()
    finally:
        handle.close()

    return None, None, heard, reset_seen()
