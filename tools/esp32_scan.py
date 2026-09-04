#!/usr/bin/env python3
"""
What ESP32 boards are attached, and which is which?

A bench tool. Lists every serial device on the machine, marks the ones that
could be a Freya ESP32, and asks each of those who it is.

    ./esp32_scan.py              list and probe everything
    ./esp32_scan.py --raw        also print every line and frame each board sent
    ./esp32_scan.py --no-probe   list only, open nothing
    ./esp32_scan.py -t 4.0       longer probe, for a slow or busy board

The three boards -- science, drilling, astro-bio -- are descriptor-identical
behind a CP2102 and cannot be told apart by USB. See freya_esp32_probe for why,
and for the three request forms this sends.

SAFE TO RUN, with one caveat it reports for itself: DTR and RTS are driven low
before the port is opened so probing should not reset a running board, and the
scan SHOUTS if a boot banner appears anyway -- which it does on astro-bio.
"""

import argparse
import os
import time
from pathlib import Path

try:
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("pyserial missing:  pip3 install pyserial")

from freya_esp32_probe import BAUDS, CP2102, PROBE_TIMEOUT, probe

KNOWN = {
    "freya-science-module": "AS7265x soil analysis module",
    "freya-drilling-module": "drill controller",
    "freya-astro-bio-module": "astro-bio pump / pH / flow controller",
}


def links_to(device, directory):
    """Every symlink in `directory` pointing at this device."""
    names = []
    base = Path(directory)

    if base.is_dir():
        for link in sorted(base.iterdir()):
            try:
                if os.path.realpath(link) == os.path.realpath(device):
                    names.append(link.name)
            except OSError:
                pass

    return names


def usb_port_of(device):
    """The physical USB path -- the only thing unique per attached board."""
    for name in links_to(device, "/dev/serial/by-path"):
        return name.replace("pci-0000:00:14.0-usb-0:", "")

    return "?"


def main():
    parser = argparse.ArgumentParser(
        description="Enumerate and identify the Freya ESP32 boards.")
    parser.add_argument("--raw", action="store_true",
                        help="print every line and frame each board sent")
    parser.add_argument("-t", "--timeout", type=float, default=PROBE_TIMEOUT,
                        help="seconds to wait per baud rate")
    parser.add_argument("-b", "--baud", type=int, action="append",
                        help="baud to try; repeatable (default 115200, 921600)")
    parser.add_argument("--no-probe", action="store_true",
                        help="list devices only, open nothing")
    args = parser.parse_args()

    bauds = tuple(args.baud) if args.baud else BAUDS
    ports = sorted(list_ports.comports(), key=lambda p: p.device)

    print()
    print("SERIAL DEVICES  --  {}  {}".format(
        os.uname().nodename, time.strftime("%Y-%m-%d %H:%M:%S")))
    print()

    if not ports:
        print("  none")

        return

    candidates = []

    for port in ports:
        is_esp = (port.vid, port.pid) == CP2102

        if is_esp:
            candidates.append(port)

        vidpid = ("{:04x}:{:04x}".format(port.vid, port.pid)
                  if port.vid is not None else "-")
        names = links_to(port.device, "/dev/asgard")

        print("  {:<14} {:<10} {:<22} {:<12} {}".format(
            port.device, vidpid,
            (port.product or port.description or "")[:22],
            port.serial_number or "-",
            ", ".join(names) if names else
            ("<< ESP32 CANDIDATE >>" if is_esp else "")))

    print()

    if args.no_probe:
        return

    if not candidates:
        print("No CP2102 boards attached -- nothing to probe.")
        print()

        return

    print("PROBING {} CP2102 CANDIDATE{} (bauds: {})".format(
        len(candidates), "" if len(candidates) == 1 else "S",
        ", ".join(str(b) for b in bauds)))
    print()

    seen = {}

    for port in candidates:
        print("  {}   usb port {}".format(port.device, usb_port_of(port.device)))

        identity, baud, heard, reset = probe(port.device, bauds, args.timeout)

        if identity:
            name = identity["firmware"]
            seen.setdefault(name, []).append(port.device)

            print("    -> {}  v{}  @{} baud".format(
                name, identity.get("version", "?"), baud))
            print("       {}".format(KNOWN.get(name, "UNKNOWN firmware name")))
        else:
            print("    -> UNIDENTIFIED (no answer to any probe, at any baud)")

            if heard:
                print("       but it said: {}".format(
                    heard[0].split("  ", 1)[-1][:70]))
                print("       so the board is alive and the wiring is fine;")
                print("       it has no identity command, or speaks another")
                print("       protocol this scan does not know.")
            else:
                print("       silent -- wrong baud, held by another process,")
                print("       or not running firmware.")

        if reset:
            print("    !! BOOT BANNER SEEN -- opening the port RESET this")
            print("       board. The DTR/RTS-low trick did not hold here, so")
            print("       do not probe this board while it is mid-run.")

        if args.raw and heard:
            print()

            for line in heard:
                print("       {}".format(line))

        print()

    for name, devices in sorted(seen.items()):
        if len(devices) > 1:
            print("  !! {} answered on {} ports: {}".format(
                name, len(devices), ", ".join(devices)))
            print("     Two boards cannot both be it. Check the firmware names.")
            print()


if __name__ == "__main__":
    main()
