# Host-side tools

Python, run on the rover's NUC. Nothing here is flashed to the board.

| script | what it is for |
|---|---|
| `esp32_console.py` | interactive terminal for a board — use this instead of picocom |
| `esp32_scan.py` | bench tool: what is attached, and which board is which |
| `freya_esp32.py` | `resolve(name) -> device` for other programs to import |
| `freya_esp32_probe.py` | the shared probe. Everything above builds on it |

Needs `pyserial` (already on the NUC).

## Why any of this exists

The three Freya ESP32 boards — this one, the drill and the AS7265x science
module — are all ESP32-DevKitC behind a Silicon Labs CP2102, and their USB
descriptors are **identical**: same `10c4:ea60`, same factory serial `0001`,
same product string, same `bcdDevice`. `cp210x-cfg` cannot write the EEPROM,
so this cannot be fixed in hardware.

They also collide in `/dev/serial/by-id`, where all three claim the same
filename — so with two attached, that link resolves to an arbitrary board
while looking perfectly unambiguous.

This is not theoretical. The udev rule `/dev/asgard/drill_uart` matched *any*
CP2102 and was observed on 2026-08-28 pointing at **this** board, with the
drill bridge configured to open it and speak drill protocol at 921600 baud to
a stepper pump and a water line. The rule has been removed.

So identity comes from asking the board, never from the port:

```
->  id
<-  {"firmware":"freya-astro-bio-module","version":"1.0.0","protocol":1,"state":0}
```

`ttyUSBn` numbering is then irrelevant. Hold the open file descriptor, never a
remembered path, and re-resolve on every reconnect — the numbers get recycled.

## Everyday use

```bash
./esp32_console.py                        # find this board and attach
./esp32_console.py --port /dev/ttyUSB1    # skip the ~5 s probe
./esp32_scan.py                           # what is plugged in right now?
./esp32_scan.py --no-probe                # list only, open nothing
./esp32_scan.py --raw                     # every byte, for when it misbehaves
```

In the console, `/help` lists the firmware's commands and the state each works
in. `/quit` or Ctrl-D leaves.

From another program:

```python
from freya_esp32 import resolve, ASTRO_BIO
device, baud, identity = resolve(ASTRO_BIO)
```

`resolve` raises rather than guessing: zero matches and two matches are both
errors.

## Two things that will bite you

**Opening the port resets this board.** DTR/RTS are driven low before `open()`,
which was measured to leave the science module running — it does *not* hold
here, and every open produces a `POWERON_RESET`. So probe once at startup and
keep the handle. Never probe a board that is mid-run: it reboots mid-pump.

**One owner at a time.** Quit the console before running the scan, and vice
versa. `picocom` takes an flock; these scripts do not, so they will not
reliably refuse — they will just interfere.

## Not uniform across the fleet

The boards do not agree on how to be asked, so the probe sends three forms
every cycle and accepts three answer shapes:

| board | request | answer |
|---|---|---|
| astro-bio | `id\n` | `{"firmware":...}` |
| science | `{"request_id":...,"cmd":"ping"}\n` | nested under `"data"` |
| drilling | `02 01 06 FA 03` | binary frame, no newline |

The drill uses a protocol command rather than a text line because a text
interceptor in front of a binary parser could misread a legitimate frame whose
payload happened to contain those bytes.

Order matters and is not arbitrary — see the comments in
`freya_esp32_probe.py` before changing `PROBES`.
