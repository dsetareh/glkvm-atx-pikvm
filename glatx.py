# ========================================================================== #
#                                                                            #
#    kvmd/plugins/atx/glatx.py                                               #
#    PiKVM ATX driver for the GL.iNet GL-ATXPC power control board           #
#    ("KVM-ATX-V1.0", CH55x USB CDC device, USB ID 1209:c550).               #
#                                                                            #
#    Copyright (C) 2026                                                      #
#    License: GPL-3.0-or-later (links kvmd, which is GPL-3.0).               #
#                                                                            #
# ========================================================================== #
#
# Protocol (line-based ASCII over CDC-ACM; baud is irrelevant on USB CDC):
#
#   host -> board                 board -> host
#   ----------------------------  --------------------------------
#   "GET_POWER_STATE\n"           "RCV: GET_POWER_STATE", then
#                                 "0" (off) | "1" (on) | "2" (sleep)
#   "POWER_SW\n"                  "RCV: POWER_SW"       (short press, ~0.5 s)
#   "POWER_SW_FORCE <ms>\n"       "RCV: POWER_SW_FORCE" (long press, ms)
#   "POWER_RESET\n"               "RCV: POWER_RESET"
#   "GET_SN\n"                    "RCV: GET_SN", then "<SN>\n"  (not used here)
#   "UPDATE\n"                    reboot into bootloader -- intentionally
#                                 NOT exposed by this driver.
#
# State readback uses the board's PWR-LED sense wire; mapping: 1 -> on,
# 2 -> sleep (reported as led off), 0 -> off. HDD LED is not wired: always off.
#
# Install: see install.sh (symlinks this file into the kvmd package and sets
# kvmd.atx.type: glatx). Works with current kvmd v4.x (verified against the
# v4.213 BaseAtx interface).

import asyncio
import copy
import re
import time

import serial

from typing import Final
from typing import Optional
from typing import AsyncGenerator
from typing import Any

from ... import aiotools

from ...logging import get_logger

from ...yamlconf import Section
from ...yamlconf import Option

from ...validators.basic import valid_float_f0
from ...validators.basic import valid_float_f01
from ...validators.os import valid_abs_path

from . import AtxIsBusyError
from . import AtxOperationError
from . import BaseAtx


# =====
class Plugin(BaseAtx):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # kvmd < ~4.19x constructs plugins with keyword arguments
        # (BasePlugin.__init__(self, **_)); newer kvmd passes a single
        # yamlconf Section positionally. Support both.
        if args:
            super().__init__(args[0])
            opts = {name: getattr(args[0], name) for name in self.get_plugin_options()}
        else:
            super().__init__(**kwargs)
            opts = kwargs

        self.__device: Final[str] = opts["device"]
        self.__request_timeout: Final[float] = opts["request_timeout"]
        self.__force_click_delay: Final[float] = opts["force_click_delay"]
        self.__poll_interval: Final[float] = opts["poll_interval"]

        self.__notifier = aiotools.AioNotifier()
        self.__power_region = aiotools.AioExclusiveRegion(AtxIsBusyError, self.__notifier)
        self.__reset_region = aiotools.AioExclusiveRegion(AtxIsBusyError, self.__notifier)

        self.__lock = asyncio.Lock()
        self.__leds_power: bool = False
        self.__leds_ts: float = 0.0
        self.__poll_failures: int = 0
        self.__online: bool = True

    @classmethod
    def get_plugin_options(cls) -> dict:
        return {
            "device": Option("/dev/ttyACM0", type=valid_abs_path),
            "request_timeout": Option(1.0, type=valid_float_f01),
            "force_click_delay": Option(6.5, type=valid_float_f01),
            "poll_interval": Option(2.0, type=valid_float_f0),
        }

    # ===== Serial transport

    @staticmethod
    def __token(cmd: str) -> str:
        return cmd.split()[0]

    def __exchange_sync(self, cmd: str, with_payload: bool) -> Optional[str]:
        try:
            with serial.Serial(
                port=self.__device,
                timeout=self.__request_timeout,
                write_timeout=self.__request_timeout,
            ) as tty:
                tty.reset_input_buffer()
                tty.write((cmd + "\n").encode("ascii"))
                tty.flush()

                ack = "RCV: " + self.__token(cmd)
                while True:
                    line = tty.readline()
                    if not line:
                        raise AtxOperationError(
                            f"ATX board on {self.__device}: timeout waiting for ack {ack!r}")
                    if line.decode("ascii", "ignore").strip() == ack:
                        break

                if not with_payload:
                    return None

                value = tty.readline()
                if not value:
                    raise AtxOperationError(
                        f"ATX board on {self.__device}: timeout waiting for payload")
                return value.decode("ascii", "ignore").strip()
        except (serial.SerialException, OSError) as ex:
            raise AtxOperationError(f"ATX board on {self.__device}: {ex}") from ex

    async def __request(self, cmd: str, with_payload: bool = False) -> Optional[str]:
        async with self.__lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.__exchange_sync, cmd, with_payload)

    # ===== State

    async def __query_power(self) -> bool:
        raw = await self.__request("GET_POWER_STATE", with_payload=True)
        if raw is None or not re.match(r"^-?[0-9]+(\.[0-9]+)?$", raw):
            raise AtxOperationError(f"ATX board on {self.__device}: bad power state {raw!r}")
        state = int(float(raw))
        self.__leds_power = (state == 1)
        self.__leds_ts = time.monotonic()
        return self.__leds_power

    async def __get_power(self) -> bool:
        if (time.monotonic() - self.__leds_ts) <= max(self.__poll_interval, 1.0):
            return self.__leds_power
        return await self.__query_power()

    async def get_state(self) -> dict:
        power_busy = self.__power_region.is_busy()
        reset_busy = self.__reset_region.is_busy()
        try:
            power = await self.__get_power()
        except AtxOperationError:
            power = self.__leds_power  # Report last known state, don't spam errors
        return {
            "enabled": self.__online,
            "busy": (power_busy or reset_busy),
            "acts": {
                "power": power_busy,
                "reset": reset_busy,
            },
            "leds": {
                "power": power,
                "hdd": False,
            },
        }

    async def trigger_state(self) -> None:
        self.__notifier.notify(1)

    async def poll_state(self) -> AsyncGenerator[dict]:
        prev: dict = {}
        while True:
            if (await self.__notifier.wait()) > 0:
                prev = {}
            new = await self.get_state()
            if new != prev:
                prev = copy.deepcopy(new)
                yield new

    async def systask(self) -> None:
        if self.__poll_interval <= 0:
            return
        while True:
            try:
                await self.__query_power()
                self.__poll_failures = 0
                if not self.__online:
                    self.__online = True
                    get_logger(0).info("ATX board is back online")
                self.__notifier.notify(1)
            except AtxOperationError as ex:
                self.__poll_failures += 1
                if self.__online and self.__poll_failures >= 3:
                    # Board unreachable (unplugged): disable the ATX panel
                    # in the UI instead of showing stale state, and log
                    # the transition once rather than on every failure.
                    self.__online = False
                    self.__leds_power = False
                    self.__leds_ts = time.monotonic()
                    get_logger(0).warning("ATX board unreachable after %d polls: %s "
                                          "(most likely unplugged); marking ATX disabled",
                                          self.__poll_failures, ex)
                    self.__notifier.notify(1)
            await asyncio.sleep(self.__poll_interval)

    # ===== Clicks

    @aiotools.atomic_fg
    async def __click(self, name: str, region: aiotools.AioExclusiveRegion, cmd: str, wait: bool) -> None:
        if wait:
            with region:
                await self.__inner_click(name, cmd)
        else:
            await aiotools.run_region_task(
                f"Can't perform ATX {name} click or operation was not completed",
                region, self.__inner_click, cmd,
            )

    async def __inner_click(self, name: str, cmd: str) -> None:
        await self.__request(cmd)
        get_logger(0).info("Clicked ATX button %r", name)
        await asyncio.sleep(1.0)  # Let the target state settle, then refresh
        try:
            await self.__query_power()
        except AtxOperationError:
            pass
        self.__notifier.notify(1)

    # ===== API actions (semantics mirror the vendor atxpower script)

    async def power_on(self, wait: bool) -> None:
        if not (await self.__get_power()):
            await self.click_power(wait)

    async def power_off(self, wait: bool) -> None:
        if (await self.__get_power()):
            await self.click_power(wait)

    async def power_off_hard(self, wait: bool) -> None:
        if (await self.__get_power()):
            await self.click_power_long(wait)

    async def power_reset_hard(self, wait: bool) -> None:
        if (await self.__get_power()):
            await self.click_reset(wait)

    # ===== Raw buttons

    async def click_power(self, wait: bool) -> None:
        await self.__click("power", self.__power_region, "POWER_SW", wait)

    async def click_power_long(self, wait: bool) -> None:
        ms = int(round(self.__force_click_delay * 1000))
        await self.__click("power_long", self.__power_region, f"POWER_SW_FORCE {ms}", wait)

    async def click_reset(self, wait: bool) -> None:
        await self.__click("reset", self.__reset_region, "POWER_RESET", wait)
