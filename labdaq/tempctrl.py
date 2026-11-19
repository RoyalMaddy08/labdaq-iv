from __future__ import annotations

import logging
import math
import random
import re
import threading
import time

log = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    pass


class OperatorAbort(ControllerError):
    pass


def _parity(p):
    import serial
    return {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[str(p).upper()]


def open_serial(c):
    import serial
    return serial.Serial(c["port"], int(c["baudrate"]), bytesize=int(c["bytesize"]),
                         parity=_parity(c["parity"]), stopbits=int(c["stopbits"]),
                         timeout=float(c["timeout_s"]))


class TemperatureController:
    name = "base"
    can_set = False
    has_pv = True

    def __init__(self, limits):
        self.lo, self.hi = (float(x) for x in limits)
        self._lock = threading.Lock()

    def read_pv(self) -> float:
        raise NotImplementedError

    def read_sp(self):
        return None

    def set_sp(self, t: float) -> None:
        raise ControllerError(f"{self.name} cannot set setpoint")

    def check_limits(self, t: float) -> None:
        if not self.lo <= t <= self.hi:
            raise ControllerError(f"setpoint {t} outside {self.lo}..{self.hi}")

    def _verify(self, t, tol):
        back = self.read_sp()
        if back is None or not math.isfinite(back) or abs(back - t) > tol:
            raise ControllerError(f"setpoint readback {back} != {t}")
        log.info("setpoint -> %.1f (verified)", back)

    def describe(self) -> dict:
        return {"backend": self.name, "setpoint_limits_c": [self.lo, self.hi]}

    def close(self) -> None:
        pass


class ModbusController(TemperatureController):
    name = "modbus"
    can_set = True

    def __init__(self, c: dict, limits):
        super().__init__(limits)
        import minimalmodbus
        self.c = c
        self.scale, self.signed = float(c["scale"]), bool(c["signed"])
        self.invalid = {int(x) for x in c.get("invalid_raw") or []}
        mode = minimalmodbus.MODE_ASCII if c.get("mode", "rtu") == "ascii" else minimalmodbus.MODE_RTU
        self.name = f"modbus_{c.get('mode', 'rtu')}"
        i = minimalmodbus.Instrument(c["port"], int(c["slave_address"]), mode=mode)
        i.serial.baudrate = int(c["baudrate"])
        i.serial.parity = _parity(c["parity"])
        i.serial.stopbits, i.serial.bytesize = int(c["stopbits"]), int(c["bytesize"])
        i.serial.timeout = float(c["timeout_s"])
        i.clear_buffers_before_each_transaction = True
        self.inst = i
        log.info("%s PV=%.1f SP=%.1f", self.name, self.read_pv(), self.read_sp())

    def _raw(self, reg, fc):
        err = None
        for _ in range(3):
            try:
                with self._lock:
                    r = self.inst.read_register(int(reg), 0, functioncode=int(fc), signed=self.signed)
                return math.nan if r in self.invalid else r * self.scale
            except OSError as e:
                err = e
                time.sleep(0.05)
        raise ControllerError(f"read reg {reg}: {err!r}")

    def read_pv(self):
        return self._raw(self.c["pv_register"], self.c["pv_function"])

    def read_sp(self):
        return self._raw(self.c["sp_register"], self.c.get("sp_read_function", 3))

    def set_sp(self, t):
        t = float(t)
        self.check_limits(t)
        try:
            with self._lock:
                self.inst.write_register(int(self.c["sp_register"]), int(round(t / self.scale)), 0,
                                         functioncode=int(self.c["write_function"]), signed=self.signed)
        except OSError as e:
            raise ControllerError(f"setpoint write: {e!r}") from e
        self._verify(t, self.scale / 2 + 1e-9)

    def describe(self):
        keys = ("port", "slave_address", "baudrate", "parity", "pv_register", "pv_function",
                "sp_register", "write_function", "scale")
        return {**super().describe(), **{k: self.c[k] for k in keys}}

    def close(self):
        try:
            self.inst.serial.close()
        except Exception:
            pass


def _checksum(kind, data: bytes) -> bytes:
    if kind in (None, "none"):
        return b""
    if kind == "sum8":
        return f"{sum(data) & 0xFF:02X}".encode()
    if kind == "xor8":
        x = 0
        for b in data:
            x ^= b
        return f"{x:02X}".encode()
    if kind == "lrc":
        return f"{(-sum(data)) & 0xFF:02X}".encode()
    raise ValueError(f"checksum '{kind}'")


def _unescape(s):
    return s.encode().decode("unicode_escape").encode("latin-1")


class AsciiController(TemperatureController):
    name = "rs485_ascii"

    def __init__(self, c: dict, limits, port=None):
        super().__init__(limits)
        self.c = c
        self.addr = int(c["address"])
        self.eol = _unescape(c.get("eol", "\\r"))
        self.rx_eol = _unescape(c.get("reply_eol", c.get("eol", "\\r")))
        self.prefix = _unescape(c.get("prefix", ""))
        self.cks = c.get("checksum", "none")
        self.echo = bool(c.get("echo", False))
        self.gap = float(c.get("turnaround_s", 0.02))
        self.scale = float(c.get("scale", 1.0))
        self.value_fmt = c.get("value_format", "{value:.1f}")
        self.rx = re.compile(c["reply_regex"])
        self.ack = re.compile(c["write_ack_regex"]) if c.get("write_ack_regex") else None
        self.err = re.compile(c["error_regex"]) if c.get("error_regex") else None
        self.invalid = [re.compile(p) for p in c.get("invalid_regex") or []]
        self.can_set = bool(c.get("write_sp"))
        self.ser = port if port is not None else open_serial(c)
        log.info("rs485_ascii %s addr %d PV=%.1f", c.get("port"), self.addr, self.read_pv())

    def _frame(self, tmpl: str, **kw) -> bytes:
        body = self.prefix + tmpl.format(addr=self.addr, **kw).encode("ascii")
        return body + _checksum(self.cks, body) + self.eol

    def _readline(self) -> bytes:
        buf, end = b"", time.monotonic() + float(self.c["timeout_s"])
        while time.monotonic() < end:
            b = self.ser.read(1)
            if not b:
                continue
            buf += b
            if buf.endswith(self.rx_eol):
                return buf[:-len(self.rx_eol)]
        raise ControllerError(f"timeout, got {buf!r}")

    def transact(self, tmpl: str, **kw) -> str:
        frame = self._frame(tmpl, **kw)
        err = None
        for _ in range(3):
            try:
                with self._lock:
                    self.ser.reset_input_buffer()
                    self.ser.write(frame)
                    self.ser.flush()
                    if self.echo:
                        got = self.ser.read(len(frame))
                        if got != frame:
                            raise ControllerError(f"echo mismatch {got!r}")
                    raw = self._readline()
                    time.sleep(self.gap)
                txt = raw.decode("ascii", "replace")
                if self.err and self.err.search(txt):
                    raise ControllerError(f"device error reply {txt!r}")
                return txt
            except (OSError, ControllerError) as e:
                err = e
                time.sleep(0.05)
        raise ControllerError(f"{frame!r}: {err!r}")

    def _num(self, txt: str) -> float:
        if any(p.search(txt) for p in self.invalid):
            return math.nan
        m = self.rx.search(txt)
        if not m:
            raise ControllerError(f"cannot parse {txt!r}")
        return float(m.group(1)) * self.scale

    def read_pv(self):
        return self._num(self.transact(self.c["read_pv"]))

    def read_sp(self):
        return self._num(self.transact(self.c["read_sp"])) if self.c.get("read_sp") else None

    def set_sp(self, t):
        if not self.can_set:
            raise ControllerError("write_sp not configured")
        t = float(t)
        self.check_limits(t)
        v = t / self.scale
        reply = self.transact(self.c["write_sp"], value=v, raw=int(round(v)))
        if self.ack and not self.ack.search(reply):
            raise ControllerError(f"no ack: {reply!r}")
        if self.c.get("read_sp"):
            self._verify(t, self.scale / 2 + 0.05)

    def describe(self):
        keys = ("port", "address", "baudrate", "parity", "read_pv", "read_sp", "write_sp",
                "checksum", "scale")
        return {**super().describe(), **{k: self.c.get(k) for k in keys}}

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class SimController(TemperatureController):
    name = "simulated"
    can_set = True

    def __init__(self, limits, tau_s: float = 5.0):
        super().__init__(limits)
        self.sp = self.pv = 25.0
        self.tau, self._t = tau_s, time.monotonic()

    def read_pv(self):
        with self._lock:
            now = time.monotonic()
            self.pv += (self.sp - self.pv) * (1 - math.exp(-(now - self._t) / self.tau))
            self._t = now
            return self.pv + random.gauss(0, 0.05)

    def read_sp(self):
        return self.sp

    def set_sp(self, t):
        self.check_limits(float(t))
        self.sp = float(t)


class ManualController(TemperatureController):
    name = "manual"
    can_set = True
    has_pv = False

    def __init__(self, c: dict, limits, plant=None):
        super().__init__(limits)
        self.auto = bool(c.get("auto_confirm", False))
        self.beep = bool(c.get("beep", True))
        self.plant = plant
        self.sp = math.nan
        self.events = []

    def read_pv(self):
        return math.nan

    def read_sp(self):
        return self.sp

    def _done(self, req, got):
        self.sp = got
        if self.plant:
            self.plant.set_sp(got)
        self.events.append({"t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "requested_c": req, "confirmed_c": got})
        log.info("operator setpoint %.1f C%s", got, "" if got == req else f" (requested {req})")

    def set_sp(self, t):
        t = float(t)
        self.check_limits(t)
        if self.auto:
            return self._done(t, t)
        print(f"\n{'=' * 60}\n  SET HEATER CONTROLLER TO {t:.1f} C\n{'=' * 60}"
              + ("\a" if self.beep else ""), flush=True)
        while True:
            try:
                r = input("  Enter = done | number = value actually set | q = abort: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise OperatorAbort("operator abort") from None
            if r.lower() == "q":
                raise OperatorAbort("operator abort")
            if not r:
                return self._done(t, t)
            try:
                v = float(r)
                self.check_limits(v)
                return self._done(t, v)
            except (ValueError, ControllerError) as e:
                print(f"  invalid: {e}")

    def finish(self, t):
        msg = f"SET HEATER CONTROLLER TO {float(t):.1f} C NOW"
        print(f"\n{'!' * 60}\n  {msg}\n{'!' * 60}" + ("\a" if self.beep else ""), flush=True)
        log.critical(msg)
        if self.plant:
            self.plant.set_sp(float(t))

    def describe(self):
        return {**super().describe(), "auto_confirm": self.auto}


ModbusRTUController = ModbusController


def make_controller(cfg: dict, sim: bool = False):
    t = cfg.get("temperature_controller") or {}
    b = t.get("backend", "none")
    if b == "none":
        return None
    limits = t["setpoint_limits_c"]
    if b == "manual":
        return ManualController(t.get("manual") or {}, limits, SimController(limits) if sim else None)
    if sim:
        return SimController(limits)
    if b == "modbus":
        return ModbusController(t["modbus"], limits)
    if b == "rs485_ascii":
        return AsciiController(t["rs485_ascii"], limits)
    raise NotImplementedError(f"backend '{b}'")
