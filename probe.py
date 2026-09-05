#!/usr/bin/env python3
# Read-only probe for the GL.iNet GL-ATXPC board (KVM-ATX-V1.0).
# Queries power state and serial number. NEVER sends POWER_SW/POWER_RESET,
# so it is safe to run while the board is wired to a live machine.
# Usage: python3 probe.py [/dev/ttyACM0]

import sys
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"

def exchange(cmd: str) -> list:
    with serial.Serial(PORT, timeout=1, write_timeout=1) as tty:
        tty.reset_input_buffer()
        tty.write((cmd + "\n").encode("ascii"))
        tty.flush()
        lines = []
        while True:
            line = tty.readline()
            if not line:
                break
            lines.append(line.decode("ascii", "ignore").strip())
            if len(lines) >= 2:
                break
        return lines

STATE = {"0": "off", "1": "on", "2": "sleep"}

for cmd in ("GET_POWER_STATE", "GET_SN"):
    lines = exchange(cmd)
    if cmd == "GET_POWER_STATE" and len(lines) >= 2:
        print(f"power_state: {STATE.get(lines[1], lines[1] if len(lines) > 1 else '?')} (raw {lines})")
    else:
        print(f"{cmd}: {lines}")
