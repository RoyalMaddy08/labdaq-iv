"""Substrate-heater temperature controller backends.

All backends share one interface, so run_iv.py does not care how the controller is connected:
    read_pv()  -> deg C   controller's own measured temperature
    read_sp()  -> deg C   current setpoint (None if not available)
    set_sp(t)             change setpoint (only when can_set is True)
    describe() -> dict    recorded in metadata.json
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time

log = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    pass


class TemperatureController:
    name = "base"
    can_set = False

    def __init__(self, limits):
        self.lo, self.hi = (float(x) for x in limits)

    def read_pv(self) -> float:
        raise NotImplementedError

    def read_sp(self):
        return None

    def set_sp(self, t: float) -> None:
        raise ControllerError(f"{self.name} cannot change the setpoint")

    def check_limits(self, t: float) -> None:
        if not self.lo <= t <= self.hi:
            raise ControllerError(f"Setpoint {t} C outside allowed {self.lo}..{self.hi} C")

    def describe(self) -> dict:
        return {"backend": self.name, "setpoint_limits_c": [self.lo, self.hi]}

    def close(self) -> None:
        pass


class ModbusRTUController(TemperatureController):
    """Modbus RTU over RS-485 (USB adapter)."""
    name = "modbus_rtu"
    can_set = True

    def __init__(self, c: dict, limits):
        super().__init__(limits)
        import minimalmodbus
        import serial

        self.c = c
        self.scale = float(c["scale"])
        self.signed = bool(c["signed"])
        self.invalid = {int(x) for x in (c.get("invalid_raw") or [])}
        self._lock = threading.Lock()

        i = minimalmodbus.Instrument(c["port"], int(c["slave_address"]), mode=minimalmodbus.MODE_RTU)
        i.serial.baudrate = int(c["baudrate"])
        i.serial.parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
                           "O": serial.PARITY_ODD}[str(c["parity"]).upper()]
        i.serial.stopbits = int(c["stopbits"])
        i.serial.bytesize = int(c["bytesize"])
        i.serial.timeout = float(c["timeout_s"])
        i.clear_buffers_before_each_transaction = True
        i.close_port_after_each_call = False
        self.inst = i

        pv, sp = self.read_pv(), self.read_sp()          # fails here if wiring/settings are wrong
        log.info("Modbus controller %s addr %s: PV=%.1f C SP=%.1f C",
                 c["port"], c["slave_address"], pv, sp)
        if not math.isfinite(pv):
            log.warning("Controller PV is invalid: sensor fault, or wrong pv_register?")

    def _read_raw(self, reg, fc) -> int:
        last = None
        for _ in range(3):
            try:
                with self._lock:
                    return self.inst.read_register(int(reg), 0, functioncode=int(fc), signed=self.signed)
            except OSError as e:          # all minimalmodbus errors subclass IOError
                last = e
                time.sleep(0.05)
        raise ControllerError(f"Modbus read of register {reg} failed: {last!r}")

    def _to_c(self, raw: int) -> float:
        return math.nan if raw in self.invalid else raw * self.scale

    def read_pv(self) -> float:
        return self._to_c(self._read_raw(self.c["pv_register"], self.c["pv_function"]))

    def read_sp(self) -> float:
        return self._to_c(self._read_raw(self.c["sp_register"], self.c.get("sp_read_function", 3)))

    def set_sp(self, t: float) -> None:
        t = float(t)
        self.check_limits(t)
        raw = int(round(t / self.scale))
        try:
            with self._lock:
                self.inst.write_register(int(self.c["sp_register"]), raw, 0,
                                         functioncode=int(self.c["write_function"]), signed=self.signed)
        except OSError as e:
            raise ControllerError(f"Setpoint write failed: {e!r}") from e
        back = self.read_sp()
        if not math.isfinite(back) or abs(back - t) > self.scale / 2 + 1e-9:
            raise ControllerError(f"Setpoint readback {back} C does not match requested {t} C")
        log.info("Setpoint -> %.1f C (verified by readback)", back)

    def describe(self) -> dict:
        keep = ("port", "slave_address", "baudrate", "parity", "pv_register", "pv_function",
                "sp_register", "write_function", "scale")
        return {**super().describe(), **{k: self.c[k] for k in keep}}

    def close(self) -> None:
        try:
            self.inst.serial.close()
        except Exception:
            pass


class SimController(TemperatureController):
    """First-order heater model for --sim runs."""
    name = "simulated"
    can_set = True

    def __init__(self, limits, tau_s: float = 5.0):
        super().__init__(limits)
        self.sp = self.pv = 25.0
        self.tau = tau_s
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def read_pv(self) -> float:
        with self._lock:
            now = time.monotonic()
            self.pv += (self.sp - self.pv) * (1 - math.exp(-(now - self._t) / self.tau))
            self._t = now
            return self.pv + random.gauss(0, 0.05)

    def read_sp(self) -> float:
        return self.sp

    def set_sp(self, t: float) -> None:
        self.check_limits(float(t))
        self.sp = float(t)
        log.info("SIM setpoint -> %.1f C", self.sp)


def make_controller(cfg: dict, sim: bool = False):
    t = cfg.get("temperature_controller") or {}
    backend = t.get("backend", "none")
    if backend == "none":
        return None
    limits = t["setpoint_limits_c"]
    if sim:
        return SimController(limits)
    if backend == "modbus":
        return ModbusRTUController(t["modbus"], limits)
    raise NotImplementedError(f"Controller backend '{backend}' is not added yet")
