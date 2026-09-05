# glatx — GL.iNet GL-ATXPC ATX power board driver for PiKVM


> **NOTE: This entire project — code, protocol documentation, and this README — was
> written by AI.**
>
>it do work tho

Lets a [PiKVM](https://pikvm.org) control a PC's power button / reset / power LED
using the [GL.iNet GL-ATXPC "ATX Board"](https://docs.gl-inet.com/kvm/en/user_guide/gl-atx-board/)
(silk-screened `KVM-ATX-V1.0`) — the accessory sold for the GL.iNet Comet/GLKVM
family — instead of PiKVM's native GPIO ATX board.

The board is a self-contained USB device (CH55x microcontroller, CDC-ACM,
USB ID `1209:c550`) that wires to the target PC's front-panel header and speaks
a simple ASCII line protocol over serial. Any Linux host can drive it; this
repo packages it as a native `kvmd` ATX plugin so it appears in the PiKVM web UI
(POWER panel, LED indicators) and the `/api/atx` HTTP API.

## Protocol (reverse-engineered from GL.iNet's `/sbin/atxpower`)

Line-based ASCII over CDC-ACM. Baud rate is irrelevant (USB CDC). Every command
is echoed by the board as `RCV: <COMMAND>`:

| Host sends              | Board replies                                | Meaning |
|-------------------------|----------------------------------------------|---------|
| `GET_POWER_STATE\n`     | `RCV: GET_POWER_STATE` then `0`/`1`/`2`      | 0=off, 1=on, 2=sleep (PWR-LED sense) |
| `POWER_SW\n`            | `RCV: POWER_SW`                              | short power press (~0.5 s) |
| `POWER_SW_FORCE <ms>\n` | `RCV: POWER_SW_FORCE`                        | long press, e.g. `6500` = 6.5 s |
| `POWER_RESET\n`         | `RCV: POWER_RESET`                           | reset press |
| `GET_SN\n`              | `RCV: GET_SN` then `<SN>`                    | board serial number |
| `UPDATE\n`              | `RCV: UPDATE`                                | reboot into bootloader (**never send casually**) |

`probe.py` in this repo exercises the read-only commands (`GET_POWER_STATE`,
`GET_SN`) and never presses anything — safe while the board is wired to a live
machine.

## Wiring

Per [GL.iNet's guide](https://docs.gl-inet.com/kvm/en/user_guide/gl-atx-board/):
connect the board's 9-pin harness to the target motherboard's front-panel
header — power switch, reset switch, and **power LED** pairs. The PWR-LED pair
is what powers state readback (`GET_POWER_STATE`); without it the board can
still press buttons but can't report on/off. The board has no HDD-LED input;
the HDD LED in the PiKVM UI will always show off.

Plug the board's USB-C into any USB port of the PiKVM Pi. It enumerates as
`/dev/ttyACM0`; a udev rule in this repo gives it the stable name `/dev/glatx`.

## Install

Copy the repo to the PiKVM box and, as root:

```bash
./install.sh
systemctl restart kvmd
```

The installer is idempotent — safe to re-run any time, and should be re-run
after major kvmd/Python upgrades (it re-resolves the kvmd package path).
Read-only sanity check (presses nothing):

```bash
curl -k -u admin:<password> https://pikvm/api/atx
```

## Files

| File                  | Purpose |
|-----------------------|---------|
| `glatx.py`            | `kvmd` ATX plugin — implements the full `BaseAtx` interface (power on/off/off-hard/reset-hard, raw clicks, LED state polling) |
| `glatx.override.yaml` | kvmd config fragment: `kvmd.atx.type: glatx`, `device: /dev/glatx` |
| `99-glatx.rules`      | udev rule → `/dev/glatx` symlink for USB ID `1209:c550` |
| `install.sh`          | Idempotent installer (deploy, symlink, udev, override, `kvmd -m` validation) |
| `probe.py`            | Read-only board diagnostic (state + serial number) |

## Configuration options (`/etc/kvmd/override.d/glatx.yaml`)

| Option              | Default         | Meaning |
|---------------------|-----------------|---------|
| `device`            | `/dev/ttyACM0`  | Serial device |
| `request_timeout`   | `1.0`           | Seconds to wait for the board's ack |
| `force_click_delay` | `6.5`           | Long-press duration in seconds (sent as `POWER_SW_FORCE <ms>`) |
| `poll_interval`     | `2.0`           | Power-state polling interval in seconds (`0` disables background polling) |

Semantics of the smart actions mirror GL.iNet's own `atxpower` tool: `power_on`
short-presses only if the PC is off, `power_off` short-presses if it's on,
`power_off_hard` long-presses if it's on, `power_reset_hard` presses reset if
it's on. The board's bootloader command (`UPDATE`) is intentionally not exposed.

## Switching between the GL-ATXPC and PiKVM's native GPIO ATX board

kvmd runs exactly one ATX driver at a time (`kvmd.atx.type`); simultaneous use
of both boards is not possible upstream. Switching is a one-file flip — with
the override removed, kvmd falls back to its stock `gpio` driver and default
pins (power 23 / reset 27 / power LED 24 / HDD LED 22):

```bash
rw
mv /etc/kvmd/override.d/glatx.yaml /etc/kvmd-glatx/glatx.yaml.disabled   # -> gpio
# mv /etc/kvmd-glatx/glatx.yaml.disabled /etc/kvmd/override.d/glatx.yaml # -> glatx
ro
systemctl restart kvmd
```

The `rw`/`ro` wrapper is needed because the PiKVM root filesystem is
read-only. The boards may both stay wired: the inactive one is simply never
opened.

## LED state in the UI

The POWER LED indicator in the PiKVM web UI is driven by the board's PWR-LED
sense wire (`GET_POWER_STATE`), refreshed every `poll_interval` seconds and
immediately after any click. Sleep (state `2`) is reported as off — kvmd's LED
model is a boolean. HDD LED is not wired on this board and always reads off.

If the board is unplugged or erroring, three consecutive failed polls set
`enabled: false` — the web UI greys out the POWER panel — and the power LED
drops to gray instead of showing a stale state. The transition is logged once
(and once again on recovery), not on every poll. Click attempts while offline
return a clean API error.

## Persistence across updates

- `/etc/kvmd/override.d/glatx.yaml`, `/etc/udev/rules.d/99-glatx.rules` and the
  canonical driver copy `/etc/kvmd-glatx/glatx.py` live in `/etc`-style
  locations that package updates never touch.
- `install.sh` symlinks the canonical copy into the kvmd Python package
  (`kvmd/plugins/atx/glatx.py`), because kvmd's plugin loader only imports
  modules from inside its own package. The symlink is unowned by pacman, so
  regular `pikvm-update` runs leave it alone.
- A Python **major** version bump moves `site-packages`; after such an update
  re-run `install.sh` (it re-resolves the path and re-validates the config).
- If kvmd ever refuses to start after an update, recover with:
  `rm -f /etc/kvmd/override.d/glatx.yaml && systemctl restart kvmd`.

## kvmd version compatibility

kvmd changed its plugin constructor convention between releases: older builds
(e.g. 4.167) pass options as `**kwargs`, newer builds (≥ ~4.19x, incl. 4.213)
pass a single `yamlconf.Section`. `glatx.py` supports both. Verified in
production against kvmd 4.213-1 / Python 3.14 on Arch Linux ARM (armv7l).

## Uninstall

```bash
rm -f /etc/kvmd/override.d/glatx.yaml /etc/udev/rules.d/99-glatx.rules
KVMD_PKG="$(python3 -c 'import kvmd, os; print(os.path.dirname(kvmd.__file__))')"
rm -f "$KVMD_PKG/plugins/atx/glatx.py" /etc/kvmd-glatx/glatx.py
udevadm control --reload && udevadm trigger --subsystem-match=tty
systemctl restart kvmd
```

## License

GPL-3.0-or-later (the plugin links kvmd, which is GPL-3.0).
