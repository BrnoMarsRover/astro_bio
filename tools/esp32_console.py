#!/usr/bin/env python3
"""
An interactive console for the astro-bio board.

    ./esp32_console.py                     find the board and attach
    ./esp32_console.py --port /dev/ttyUSB1 skip the ~3 s probe

Self-contained: needs only pyserial. It deliberately does not import the
fleet-wide scan tooling, so it works on a fresh clone of this repo rather than
only on the machine where that tooling happens to be installed.

WHY NOT picocom. picocom shows the wire, which on this firmware is unpleasant
for three reasons that have nothing to do with the hardware:

    the firmware uses println(), so every line ends \\r\\n and a raw terminal
      stair-steps down the screen
    it echoes "UART CMD: <cmd>" back, so everything you type appears twice
    during PUMPING it emits flow and pH lines several times a second, which
      land in the middle of whatever you are half-way through typing

So this strips the CRs, hides the echo, colours the traffic by kind, and
redraws your input line under anything that arrives while you type.

WHY IT HAS TO ASK THE BOARD ITS NAME. Every Freya ESP32 -- this one, the drill
and the AS7265x science module -- is an ESP32-DevKitC behind a Silicon Labs
CP2102, and their USB descriptors are identical: same 10c4:ea60, same factory
serial "0001", same product string. They also collide in /dev/serial/by-id,
where all three claim the same filename. So ttyUSBn and by-id are both unable
to say which board they point at, and the only honest answer comes from the
firmware. See the `id` command in main.cpp.

ONE OWNER. Nothing else may hold the port while this is attached.
"""

import argparse
import json
import readline  # noqa: F401  -- importing it gives input() history and editing
import sys
import threading
import time

import serial
from serial.tools import list_ports

FIRMWARE = "freya-astro-bio-module"
CP2102 = (0x10C4, 0xEA60)
BAUD = 115200
PROBE_TIMEOUT = 3.0
RESEND_INTERVAL = 0.4
PROMPT = "> "

# The firmware's own vocabulary, for /help. The board is the authority on what
# it accepts; this is a reminder, not a contract. See the state machine in
# main.cpp -- a command sent in the wrong state is silently discarded, because
# takeCommand() clears commandReady whether or not the word matched.
COMMANDS = [
    ("id", "identity JSON -- answers in any state"),
    ("init", "INIT: configure the TMC2209 driver"),
    ("meas", "MEASURE: 30 s of temperature and pH"),
    ("prepare", "PREPARE: spin the pump up, no flow measurement yet"),
    ("pump", "PREPARE -> PUMPING: pump to 80 mL"),
    ("purg", "PURGE: 5 s line clear -> DONE"),
    ("reset", "DONE/ERROR: back to INIT"),
    ("back", "DONE/ERROR: reverse the pump until 'stop'"),
    ("stop", "any state: EMERGENCY STOP -> ERROR (errorCode 7)"),
]


def open_quietly(device, baudrate=BAUD):
    """
    Open without asking the board to reset -- though here it resets anyway.

    DTR and RTS are driven low BEFORE open, which pySerial otherwise asserts.
    On the science module that was measured to leave the firmware running; on
    THIS board it does not hold, and every open produces a POWERON_RESET. The
    baud rate is likewise set before open: pySerial defaults to 9600, and
    opening at the wrong rate then switching leaves framing garbage in the
    driver buffer.
    """
    handle = serial.Serial()
    handle.port = device
    handle.baudrate = baudrate
    handle.timeout = 0.2
    handle.dtr = False
    handle.rts = False
    handle.open()
    handle.dtr = False
    handle.rts = False
    handle.reset_input_buffer()
    handle.reset_output_buffer()

    return handle


def identify(handle, timeout=PROBE_TIMEOUT):
    """The board's identity frame, or None if nothing answered in time."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    next_send = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()

        # Re-sent throughout the window, not once at the top: opening resets
        # the board, so a request written at t=0 lands in the bootloader and
        # is lost -- which looks exactly like a board with no `id` command.
        if now >= next_send:
            handle.write(b"id\n")
            handle.flush()
            next_send = now + RESEND_INTERVAL

        chunk = handle.read(handle.in_waiting or 1)

        if chunk:
            buf.extend(chunk)

        while b"\n" in buf:
            raw, _, rest = bytes(buf).partition(b"\n")
            buf = bytearray(rest)

            try:
                frame = json.loads(raw.decode("utf-8", "replace").strip())
            except ValueError:
                continue

            if isinstance(frame, dict) and frame.get("firmware"):
                return frame

    return None


def find_board():
    """(device, identity) for the one attached board running this firmware."""
    hits = []

    for port in list_ports.comports():
        if (port.vid, port.pid) != CP2102:
            continue

        try:
            handle = open_quietly(port.device)
        except (serial.SerialException, OSError):
            continue                      # busy or gone; not our business

        try:
            frame = identify(handle)
        finally:
            handle.close()

        if frame and frame.get("firmware") == FIRMWARE:
            hits.append((port.device, frame))

    if len(hits) == 1:
        return hits[0]

    raise SystemExit(
        "expected exactly one {}, found {}.\n"
        "  Plug the board in, or name the port with --port.".format(
            FIRMWARE, len(hits)))


class Colour:
    def __init__(self, enabled):
        self.on = enabled

    def __call__(self, code, text):
        return "\x1b[{}m{}\x1b[0m".format(code, text) if self.on else text


def classify(line):
    """What kind of line this is, so it can be shown differently."""
    if line.startswith("ERROR"):
        return "error"

    if line.startswith("UART CMD:"):
        return "echo"

    if line.startswith("{"):
        return "json"

    if line.startswith(("rst:", "ets ", "boot:", "configsip", "clk_drv",
                        "mode:", "load:", "entry ")):
        return "boot"

    if "Posli" in line or line.startswith((
            "Pripraveno", "Zarizeni", "Mereni hotove", "Proces hotov",
            "Chyba", "Pumpovani aktivni", "Objem napumpovan")):
        return "state"

    if line.startswith(("Prutok", "[pH")) or "snimac" in line:
        return "telem"

    return "info"


class Console:
    def __init__(self, handle, colour, show_echo, show_boot):
        self.handle = handle
        self.c = colour
        self.show_echo = show_echo
        self.show_boot = show_boot
        self.running = True
        self.started = time.monotonic()
        self.state = "?"

    def emit(self, text):
        """Print above the prompt without eating what is being typed."""
        buffered = readline.get_line_buffer()

        sys.stdout.write("\r\x1b[2K" + text + "\n")
        sys.stdout.write(PROMPT + buffered)
        sys.stdout.flush()

    def show(self, line):
        kind = classify(line)

        if kind == "echo" and not self.show_echo:
            return

        if kind == "boot" and not self.show_boot:
            return

        stamp = self.c("2;37", "{:7.1f}".format(time.monotonic() - self.started))

        if kind == "json":
            try:
                frame = json.loads(line)
            except ValueError:
                pass
            else:
                if frame.get("firmware"):
                    line = "{} v{}  protocol {}  state {}".format(
                        frame["firmware"], frame.get("version", "?"),
                        frame.get("protocol", "?"), frame.get("state", "?"))

            self.emit("{} {}".format(stamp, self.c("36", line)))

            return

        if kind == "state":
            self.state = line
            self.emit("{} {}".format(stamp, self.c("1;33", line)))

            return

        painted = {
            "error": lambda t: self.c("1;31", t),
            "telem": lambda t: self.c("2", t),
            "echo": lambda t: self.c("2", t),
            "boot": lambda t: self.c("2", t),
        }.get(kind, lambda t: t)(line)

        self.emit("{} {}".format(stamp, painted))

    def reader(self):
        pending = bytearray()

        while self.running:
            try:
                chunk = self.handle.read(self.handle.in_waiting or 1)
            except (serial.SerialException, OSError) as error:
                self.emit(self.c("1;31", "!! port lost: {}".format(error)))
                self.running = False

                return

            if not chunk:
                continue

            pending.extend(chunk)

            while b"\n" in pending:
                raw, _, rest = bytes(pending).partition(b"\n")
                pending = bytearray(rest)

                line = raw.decode("utf-8", "replace").replace("\r", "").strip()

                if line:
                    self.show(line)

    def local(self, text):
        """Console commands, which must not reach the board."""
        word = text[1:].strip().lower()

        if word in ("q", "quit", "exit"):
            self.running = False

        elif word in ("h", "help", "?"):
            print("  firmware commands (sent to the board):")

            for name, description in COMMANDS:
                print("    {:9} {}".format(name, description))

            print("  console commands (handled here):")
            print("    /help      this")
            print("    /state     the last state line the board printed")
            print("    /echo      show or hide the board's UART CMD echo")
            print("    /boot      show or hide the ESP32 boot banner")
            print("    /quit      leave (Ctrl-D does too)")

        elif word == "state":
            print("  {}".format(self.state))

        elif word == "echo":
            self.show_echo = not self.show_echo
            print("  echo {}".format("shown" if self.show_echo else "hidden"))

        elif word == "boot":
            self.show_boot = not self.show_boot
            print("  boot banner {}".format(
                "shown" if self.show_boot else "hidden"))

        else:
            print("  unknown console command: /{}   (try /help)".format(word))

    def run(self):
        threading.Thread(target=self.reader, daemon=True).start()

        while self.running:
            try:
                text = input(PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                print()

                break

            if not text:
                continue

            if text.startswith("/"):
                self.local(text)

                continue

            try:
                self.handle.write((text + "\n").encode("utf-8"))
                self.handle.flush()
            except (serial.SerialException, OSError) as error:
                print("  !! write failed: {}".format(error))

                break

        self.running = False


def main():
    parser = argparse.ArgumentParser(
        description="Interactive console for the astro-bio board.")
    parser.add_argument("--port", help="skip the probe and attach to this device")
    parser.add_argument("-b", "--baud", type=int, default=BAUD)
    parser.add_argument("--echo", action="store_true",
                        help="show the board's own UART CMD echo")
    parser.add_argument("--boot", action="store_true",
                        help="show the ESP32 boot banner")
    parser.add_argument("--no-colour", action="store_true")
    args = parser.parse_args()

    if args.port:
        device = args.port
    else:
        print("looking for {} ...".format(FIRMWARE))
        device, identity = find_board()
        print("  found v{} on {}".format(identity.get("version", "?"), device))

    try:
        handle = open_quietly(device, args.baud)
    except (serial.SerialException, OSError) as error:
        raise SystemExit("could not open {}: {}".format(device, error))

    print("attached to {} at {} baud".format(device, args.baud))
    print("opening the port RESETS this board, so it is starting from INIT.")
    print("/help for commands, /quit or Ctrl-D to leave.")
    print()

    try:
        Console(handle,
                Colour(sys.stdout.isatty() and not args.no_colour),
                args.echo, args.boot).run()
    finally:
        handle.close()
        print("detached.")


if __name__ == "__main__":
    main()
