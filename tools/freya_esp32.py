#!/usr/bin/env python3
"""
Find a Freya ESP32 board by asking it which firmware it runs.

    from freya_esp32 import resolve, ASTRO_BIO
    device, baud, identity = resolve(ASTRO_BIO)

    ./freya_esp32.py                        list every board attached
    ./freya_esp32.py freya-science-module   print just that board's device path

The boards cannot be told apart by USB -- see freya_esp32_probe for why, and
for the three request forms this sends. Ambiguity is an error here, never a
choice: a caller that gets a path back has PROOF the board answered to that
name, which is the entire point of resolving by handshake instead of by
descriptor.

Probe once at startup and hold the open handle. Opening resets at least the
astro-bio board, so re-probing something mid-run reboots it; and ttyUSBn
numbers are recycled, so a remembered path can point at a different board
after a replug. The fd is safe once open -- the path is not.
"""

import sys

from serial.tools import list_ports

from freya_esp32_probe import (          # noqa: F401  (re-exported for callers)
    ASTRO_BIO,
    BAUDS,
    CP2102,
    DRILLING,
    PROBE_TIMEOUT,
    SCIENCE,
    probe,
)


class ResolveError(Exception):
    """No board runs that firmware, or more than one claims to."""


def discover(bauds=BAUDS, timeout=PROBE_TIMEOUT):
    """{firmware_name: [(device, baud, identity), ...]} for what is attached now."""
    found = {}

    for port in list_ports.comports():
        if (port.vid, port.pid) != CP2102:
            continue

        identity, baud, _heard, _reset = probe(port.device, bauds, timeout)

        if identity:
            found.setdefault(identity["firmware"], []).append(
                (port.device, baud, identity))

    return found


def resolve(firmware_name, bauds=BAUDS, timeout=PROBE_TIMEOUT):
    """(device, baud, identity) for the one board running firmware_name."""
    attached = discover(bauds, timeout)
    hits = attached.get(firmware_name, [])

    if len(hits) == 1:
        return hits[0]

    raise ResolveError(
        "expected exactly one {}, found {}. Attached: {}".format(
            firmware_name, len(hits),
            ", ".join("{} @{}".format(name, len(v))
                      for name, v in sorted(attached.items()))
            or "nothing answered any probe"))


def main():
    if len(sys.argv) > 1:
        device, _baud, _identity = resolve(sys.argv[1])
        print(device)

        return

    attached = discover()

    if not attached:
        print("no Freya ESP32 boards answered")

        return

    for name, hits in sorted(attached.items()):
        for device, baud, identity in hits:
            print("{:28} {:14} {:>7} baud  v{}".format(
                name, device, baud, identity.get("version", "?")))


if __name__ == "__main__":
    main()
