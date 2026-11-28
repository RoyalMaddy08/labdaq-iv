#!/usr/bin/env python3
"""
Resistance vs temperature (R-T): constant bias on the Keysight B2901B, R = V/I,
sample temperature from the DMM6500 and controller PV, logged every rt.interval_s
while the heater follows the rt.profile (heat / hold / cool).

  python run_rt.py --sim
  python run_rt.py --sample-id S07

Works with every temperature_controller backend:
  modbus, rs485_ascii  the script ramps the setpoint at rate_c_per_min
  manual               you are asked to set each segment target on the panel
  none                 no control; logs until Ctrl+C or rt.max_duration_s

Ctrl+C stops logging. The SMU goes to 0 V and off, and the heater setpoint goes to end_setpoint_c.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import random
import re
import shutil
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from labdaq import __version__
from labdaq import thermocouple as tc
from labdaq.tempctrl import OperatorAbort, make_controller
from run import load_config, open_instruments, setup_logging, sha256, write_json
from run_iv import Csv, _try, read_temp, utc

log = logging.getLogger("labdaq.rt")

RT_FIELDS = ["point", "segment", "phase", "timestamp_utc", "elapsed_s", "ctrl_sp_C", "ctrl_pv_C",
             "temp_C", "temp_K", "bias_V", "smu_V", "smu_I_A", "smu_V_neg", "smu_I_neg_A",
             "R_ohm", "compliance", "error"]


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def parse_profile(rc: dict, tcfg: dict) -> list[dict]:
    segs = []
    for i, s in enumerate(rc.get("profile") or []):
        if "hold_s" in s:
            segs.append({"kind": "hold", "hold_s": float(s["hold_s"])})
        elif "to_c" in s:
            r = s.get("rate_c_per_min")
            segs.append({"kind": "ramp", "to_c": float(s["to_c"]), "rate": float(r) if r else None})
        else:
            raise ValueError(f"rt.profile[{i}] needs to_c or hold_s: {s}")
    if segs and tcfg.get("backend", "none") != "none":
        lo, hi = tcfg["setpoint_limits_c"]
        bad = [s["to_c"] for s in segs if s["kind"] == "ramp" and not lo <= s["to_c"] <= hi]
        if bad:
            raise ValueError(f"rt.profile targets {bad} outside setpoint_limits_c {lo}..{hi}")
    return segs


class Profile:
    """Runs the heat/hold/cool segments. tick() is called once per logged point."""

    def __init__(self, segs: list[dict], ctrl, rc: dict):
        self.segs, self.ctrl = segs, ctrl
        self.tol = float(rc.get("arrival_tolerance_c", 5.0))
        self.timeout = float(rc.get("segment_timeout_s", 7200))
        self.upd = float(rc.get("sp_update_s", 30))
        self.src = rc.get("done_source", "dmm")
        self.can_ramp = getattr(ctrl, "has_pv", True)      # manual: one prompt per segment, no ramp
        self.i, self.phase, self.sp, self.fails = -1, "start", math.nan, 0
        self.flags: list[str] = []

    def _write(self, now: float, v: float) -> None:
        self.last_w = now
        try:
            self.ctrl.set_sp(v)
            self.sp, self.fails = v, 0
        except OperatorAbort:
            raise
        except Exception as e:
            self.fails += 1
            log.error("Setpoint write %.1f C failed (%d in a row): %r", v, self.fails, e)
            if self.fails >= 5:
                raise RuntimeError("setpoint writes keep failing") from e

    def _begin(self, now: float, T: float, pv: float) -> None:
        self.i += 1
        if self.i >= len(self.segs):
            return
        s = self.seg = self.segs[self.i]
        self.t0 = now
        if s["kind"] == "hold":
            self.phase = "hold"
            log.info("Segment %d: hold %.0f s", self.i, s["hold_s"])
            return
        start = pv if finite(pv) else T if finite(T) else s["to_c"]
        self.dir = 1 if s["to_c"] >= start else -1
        self.phase = "heating" if self.dir > 0 else "cooling"
        self.sp0, self.last_w, self.sp_done = start, -math.inf, False
        self.ramp = self.can_ramp and s["rate"] is not None
        log.info("Segment %d: %s to %.1f C%s", self.i, self.phase, s["to_c"],
                 f" at {s['rate']:g} C/min from {start:.1f} C" if self.ramp else " (setpoint jump)")
        if not self.ramp:
            self._write(now, s["to_c"])
            self.sp_done = True

    def _done(self, now: float, T: float, pv: float) -> bool:
        s, el = self.seg, now - self.t0
        if s["kind"] == "hold":
            return el >= s["hold_s"]
        if not self.sp_done and now - self.last_w >= self.upd:
            sp = self.sp0 + self.dir * s["rate"] / 60.0 * el
            if (sp - s["to_c"]) * self.dir >= 0:
                sp, self.sp_done = s["to_c"], True
            self._write(now, round(sp, 1))
        if el > self.timeout:
            msg = f"segment {self.i} ({self.phase} to {s['to_c']} C) timed out after {el:.0f} s"
            log.warning(msg)
            self.flags.append(msg)
            return True
        x = T if (self.src == "dmm" and finite(T)) or not finite(pv) else pv
        if not self.sp_done or not finite(x):
            return False
        return x >= s["to_c"] - self.tol if self.dir > 0 else x <= s["to_c"] + self.tol

    def tick(self, now: float, T: float, pv: float) -> bool:
        if self.i < 0:
            self._begin(now, T, pv)
        while self.i < len(self.segs):
            if not self._done(now, T, pv):
                return True
            self._begin(now, T, pv)
        return False


class SimGST:
    """--sim only: GST-like film. Amorphous R falls slowly with T, crystallises near 160 C,
    and stays low on cooling (like a real first heating run)."""

    def __init__(self, smu):
        self.smu, self.level, self.x, self.T = smu, 0.0, 0.0, 25.0

    def __getattr__(self, n):
        return getattr(self.smu, n)

    def set_level(self, v):
        self.level = float(v)
        self.smu.set_level(v)

    def measure(self):
        if finite(self.T):
            self.x = max(self.x, 1 / (1 + math.exp(-(self.T - 160) / 4)))
        ra = 9e8 * math.exp(-(self.T - 35) / 450) if finite(self.T) else 9e8
        r = math.exp(self.x * math.log(2.05e8) + (1 - self.x) * math.log(ra)) * random.gauss(1, 0.003)
        return self.level, self.level / r, False


def measure_point(smu, dmm, ctrl, pool, cfg, bias: float, alt: bool, settle: float) -> dict:
    jobs = {}
    if dmm:
        jobs["dmm"] = pool.submit(read_temp, dmm, cfg["dmm"])
    if ctrl and getattr(ctrl, "has_pv", True):
        jobs["ctrl"] = pool.submit(ctrl.read_pv)
    row, errs = {"bias_V": bias, "temp_C": math.nan, "ctrl_pv_C": math.nan}, {}
    try:
        if alt:
            smu.set_level(bias)
            time.sleep(settle)
            vp, ip, tp = smu.measure()
            smu.set_level(-bias)
            time.sleep(settle)
            vn, i_n, tn = smu.measure()
            row.update(smu_V=vp, smu_I_A=ip, smu_V_neg=vn, smu_I_neg_A=i_n, compliance=int(tp or tn))
            dv, di = vp - vn, ip - i_n
        else:
            vp, ip, tp = smu.measure()
            row.update(smu_V=vp, smu_I_A=ip, compliance=int(tp))
            dv, di = vp, ip
        row["R_ohm"] = dv / di if finite(di) and di != 0 else math.nan
    except Exception as e:
        errs["smu"] = repr(e)
        row["R_ohm"] = math.nan
    for name, fut in jobs.items():
        try:
            row["temp_C" if name == "dmm" else "ctrl_pv_C"] = fut.result()
        except Exception as e:
            errs[name] = repr(e)
    if finite(row["temp_C"]):
        row["temp_K"] = row["temp_C"] + 273.15
    row["error"] = "; ".join(f"{n}: {e}" for n, e in errs.items()) or None
    row["_smu_failed"] = "smu" in errs
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--sample-id")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    exp = cfg["experiment"]
    rc = cfg["rt"]
    tcfg = cfg.get("temperature_controller") or {"backend": "none"}
    backend = tcfg.get("backend", "none")
    if args.sample_id:
        exp["sample_id"] = args.sample_id

    bias = float(rc["bias_v"])
    lim = float(rc.get("max_bias_v", 10.0))
    if not 0 < abs(bias) <= lim:
        raise ValueError(f"rt.bias_v {bias} must be non-zero and within rt.max_bias_v {lim}")
    alt = bool(rc.get("alternate_polarity", False))
    interval = float(rc.get("interval_s", 1.0))
    segs = parse_profile(rc, tcfg) if backend != "none" else []
    if backend != "none" and not segs:
        raise ValueError("rt.profile is empty")
    if tcfg.get("end_setpoint_c") is not None and backend != "none":
        lo, hi = tcfg["setpoint_limits_c"]
        if not lo <= tcfg["end_setpoint_c"] <= hi:
            raise ValueError("end_setpoint_c outside setpoint_limits_c")
    max_dur = rc.get("max_duration_s")
    tc.self_test()

    start = dt.datetime.now().astimezone()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(exp["sample_id"]))
    run_dir = Path(exp["output_dir"]) / safe_id / ("RT_" + start.strftime("%Y%m%d_%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=False)
    setup_logging(run_dir, args.verbose)
    shutil.copy(args.config, run_dir / "config_original.yaml")
    (run_dir / "config_used.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    meta_path = run_dir / "metadata.json"
    meta = {"software": f"labdaq {__version__} (run_rt)", "simulated": args.sim,
            "start_local": start.isoformat(), "operator": exp.get("operator"),
            "sample_id": exp["sample_id"], "description": exp.get("description"),
            "config": cfg, "profile": segs}
    write_json(meta_path, meta)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    rt_path = run_dir / "rt.csv"
    rtlog = Csv(rt_path, RT_FIELDS)
    pool = ThreadPoolExecutor(max_workers=3)
    smu = dmm = ctrl = prof = None
    exit_reason, n, bad_in_row = "completed", 0, 0
    Rs: list[float] = []
    try:
        smu, dmm = open_instruments(cfg, args.sim)
        ctrl = make_controller(cfg, args.sim)
        if args.sim:
            smu = SimGST(smu)
            if dmm is not None and ctrl is not None:
                dmm.follow = getattr(ctrl, "plant", None) or ctrl
        if getattr(smu, "mode", "voltage") != "voltage":
            raise ValueError("R-T needs the SMU in voltage-source mode (smu.mode: voltage)")
        smu.configure(dict(cfg["smu"], source_level=0.0))
        if dmm:
            dmm.configure(cfg["dmm"])
        smu.fix_source_range(abs(bias))
        meta["instruments"] = {"smu": smu.idn, "dmm": dmm.idn if dmm else None}
        meta["controller"] = ctrl.describe() if ctrl else None
        write_json(meta_path, meta)

        prof = Profile(segs, ctrl, rc) if ctrl else None
        smu.output_on()
        smu.set_level(bias)
        stop.wait(float(cfg["smu"].get("settle_s", 1.0)))
        t0 = nxt = time.monotonic()
        log.info("Logging R-T at %g V every %g s%s", bias, interval,
                 " (alternating polarity)" if alt else "")

        while not stop.is_set():
            now = time.monotonic()
            row = measure_point(smu, dmm, ctrl, pool, cfg, bias, alt, float(rc.get("settle_s", 0.3)))
            if args.sim:
                smu.T = row["temp_C"]
            running = prof.tick(now, row["temp_C"], row["ctrl_pv_C"]) if prof else \
                (max_dur is None or now - t0 < float(max_dur))
            row.update(point=n, timestamp_utc=utc(), elapsed_s=round(now - t0, 3),
                       segment=prof.i if prof else None, phase=prof.phase if prof else "log",
                       ctrl_sp_C=prof.sp if prof else math.nan)
            rtlog.write(row)
            n += 1
            if finite(row["R_ohm"]):
                Rs.append(row["R_ohm"])
            if row["error"]:
                log.error("Point %d: %s", n, row["error"])
            bad_in_row = bad_in_row + 1 if row["_smu_failed"] else 0
            if bad_in_row >= int(rc.get("max_consecutive_errors", 5)):
                exit_reason = "SMU errors"
                break
            if row.get("compliance"):
                log.warning("Compliance at point %d: R is below bias/compliance, R_ohm not valid", n)
                if rc.get("abort_on_compliance", False):
                    exit_reason = "compliance"
                    break
            if n % int(rc.get("log_every", 10)) == 0:
                log.info("  %5.0f s  %-7s  T %.2f C  PV %.1f  SP %.1f  R %.4g ohm", now - t0,
                         row["phase"], row["temp_C"], row["ctrl_pv_C"], row["ctrl_sp_C"], row["R_ohm"])
            if not running:
                break
            nxt += interval
            stop.wait(max(0.0, nxt - time.monotonic()))
        if stop.is_set():
            exit_reason = "user stop"
    except OperatorAbort:
        exit_reason = "user stop"
    except Exception as e:
        exit_reason = f"crash: {e!r}"
        log.exception("Fatal error")
    finally:
        if smu:
            smu.safe_off()
        if ctrl is not None and tcfg.get("end_setpoint_c") is not None:
            try:
                getattr(ctrl, "finish", ctrl.set_sp)(float(tcfg["end_setpoint_c"]))
            except Exception as e:
                log.critical("Could not set end setpoint (%r): SET THE HEATER CONTROLLER MANUALLY", e)
        for dev in (smu, dmm, ctrl):
            if dev:
                dev.close()
        pool.shutdown(wait=False)
        rtlog.close()
        meta.update(points=n, R_min_ohm=min(Rs) if Rs else None, R_max_ohm=max(Rs) if Rs else None,
                    profile_flags=prof.flags if prof else [],
                    controller_events=getattr(ctrl, "events", None),
                    end_local=dt.datetime.now().astimezone().isoformat(), exit_reason=exit_reason,
                    rt_sha256=sha256(rt_path))
        write_json(meta_path, meta)
        log.info("Finished (%s) -> %s", exit_reason, run_dir)
    return 0 if exit_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
