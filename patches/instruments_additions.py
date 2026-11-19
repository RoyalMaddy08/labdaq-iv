# Paste these into labdaq/instruments.py (from the earlier labdaq framework).

# ---- inside class KeysightB2901B ----
    def set_level(self, x: float) -> None:
        self.write(f":SOUR:{'VOLT' if self.mode == 'voltage' else 'CURR'} {x:.9g}")

    def fix_source_range(self, max_abs: float) -> None:
        f = "VOLT" if self.mode == "voltage" else "CURR"
        self.write(f":SOUR:{f}:RANG:AUTO OFF")
        self.write(f":SOUR:{f}:RANG {max_abs:.9g}")   # picks smallest range covering max_abs
        self.check_errors()

# ---- inside class SimSMU ----
    def set_level(self, x: float) -> None:
        self.level = float(x)

    def fix_source_range(self, max_abs: float) -> None:
        pass

# ---- in class SimDMM: replace read() ----
    def read(self) -> float:
        time.sleep(0.03)
        follow = getattr(self, "follow", None)
        if follow is not None:
            temp = follow.read_pv() - 1.5 + random.gauss(0, 0.05)   # sample a bit cooler than heater TC
        else:
            t = time.monotonic() - self._t0
            temp = 150 + 50 * math.sin(t / 90) + random.gauss(0, 0.05)
        if self.mode == "dcv_software":
            from .thermocouple import emf_mV
            return (emf_mV(temp) - emf_mV(self.cj)) * 1e-3
        return temp
