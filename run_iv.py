#!/usr/bin/env python3
"""
I-V sweeps on the Keysight B2901B, optionally at a series of temperatures set through
the substrate-heater controller. Sample temperature (DMM6500) and controller PV are
recorded at every I-V point.

  python run_iv.py --sim
  python run_iv.py --sample-id S07

Ctrl+C stops after the current point. The SMU is set to 0 and switched off, and the
heater setpoint goes to end_setpoint_c.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import logging
import math
import os
import re
import shutil
import signal
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from labdaq import __version__
from labdaq import thermocouple as tc
from labdaq.tempctrl import OperatorAbort, make_controller
from run import fmt, load_config, open_instruments, setup_logging, sha256, write_json

log = logging.getLogger("labdaq.iv")

IV_FIELDS = ["sweep", "setpoint_C", "point", "direction", "timestamp_utc", "elapsed_s", "set_value",
             "smu_V", "smu_I_A", "compliance", "temp_C", "ctrl_pv_C", "error"]
T_FIELDS = ["timestamp_utc", "elapsed_s", "temp_C", "ctrl_pv_C", "ctrl_sp_C", "phase"]


class Csv:
    def __init__(self, path: Path, fields: list[str]):
        self.fields = fields
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fields, extrasaction="ignore")
        self.w.writeheader()
        self._sync()

    def write(self, row: dict) -> None:
        self.w.writerow({k: fmt(row.get(k)) for k in self.fields})
        self._sync()

    def _sync(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()


class Rig:
    def __init__(self, cfg, smu, dmm, ctrl, pool, stop):
        self.cfg, self.smu, self.dmm, self.ctrl, self.pool, self.stop = cfg, smu, dmm, ctrl, pool, stop
        self.sc = cfg["sweep"]
        self.tcfg = cfg.get("temperature_controller") or {}
        self.t0 = time.monotonic()


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sweep_points(c: dict) -> list[float]:
    def lin(a: float, b: float, step: float) -> list[float]:
        n = int(round(abs(b - a) / step))
        return [a + (b - a) * k / n for k in range(n + 1)] if n else [a]

    s, e, st = float(c["start"]), float(c["stop"]), abs(float(c["step"]))
    if st <= 0:
        raise ValueError("sweep.step must be > 0")
    if c["shape"] == "single":
        pts = lin(s, e, st)
    elif c["shape"] == "double":
        fwd = lin(s, e, st)
        pts = fwd + fwd[-2::-1]
    elif c["shape"] == "loop":
        m = abs(e)
        pts = lin(0.0, m, st) + lin(m, -m, st)[1:] + lin(-m, 0.0, st)[1:]
    else:
        raise ValueError(f"Unknown sweep.shape '{c['shape']}'")
    return [round(p, 12) for p in pts]


def read_temp(dmm, dcfg: dict) -> float:
    x = dmm.read()
    if not math.isfinite(x):
        return math.nan
    if dcfg["mode"] == "dcv_software":
        return tc.hot_junction_C(x, float(dcfg["cold_junction_c"]))
    return x


def _try(fn, *args):
    try:
        return fn(*args), None
    except Exception as e:
        return math.nan, repr(e)


def _stats(xs, prefix: str) -> dict:
    g = [x for x in xs if isinstance(x, float) and math.isfinite(x)]
    return {f"{prefix}_mean_C": statistics.fmean(g) if g else None,
            f"{prefix}_min_C": min(g) if g else None,
            f"{prefix}_max_C": max(g) if g else None}


def wait_stable(rig: Rig, tlog: Csv, label: str, target) -> bool | None:
    """Log T every second until stable. If a setpoint target exists, the controller PV
    must also be within setpoint_tolerance_c of it (prevents 'stable before heating started')."""
    sc, tcfg = rig.sc, rig.tcfg
    source = tcfg.get("stable_source", "dmm")
    if source == "dmm" and not rig.dmm:
        source = "controller"
    has_pv = bool(rig.ctrl) and getattr(rig.ctrl, "has_pv", True)
    if source == "controller" and not has_pv:
        log.warning("No temperature source available for the stability check; skipping")
        return None
    window, band = float(sc["stable_window_s"]), float(sc["stable_band_c"])
    tol = float(tcfg.get("setpoint_tolerance_c", 2.0))
    arr = tcfg.get("arrival_tolerance_c")
    hist: collections.deque = collections.deque()
    start = nxt = time.monotonic()
    n = 0
    log.info("Waiting for stable T (%s, %.2f C over %.0f s)%s", source, band, window,
             f", setpoint {target} C" if target is not None else "")
    while not rig.stop.is_set():
        now = time.monotonic()
        T, e1 = _try(read_temp, rig.dmm, rig.cfg["dmm"]) if rig.dmm else (math.nan, None)
        pv, e2 = _try(rig.ctrl.read_pv) if has_pv else (math.nan, None)
        sp, e3 = _try(rig.ctrl.read_sp) if rig.ctrl else (math.nan, None)
        for e in (e1, e2, e3):
            if e:
                log.error("Temperature read failed: %s", e)
        tlog.write({"timestamp_utc": utc(), "elapsed_s": round(now - rig.t0, 3),
                    "temp_C": T, "ctrl_pv_C": pv, "ctrl_sp_C": sp, "phase": label})
        x = T if source == "dmm" else pv
        hist.append((now, x))
        while hist and now - hist[0][0] > window:
            hist.popleft()
        arrived = True
        if has_pv and target is not None:
            arrived = math.isfinite(pv) and abs(pv - target) <= tol
        elif target is not None and arr is not None:
            arrived = math.isfinite(T) and abs(T - target) <= float(arr)
        vals = [v for _, v in hist]
        if now - start >= window and arrived and all(math.isfinite(v) for v in vals):
            span = max(vals) - min(vals)
            if span <= band:
                log.info("Stable: sample %.2f C, controller %.1f C (span %.2f C)", T, pv, span)
                return True
        n += 1
        if n % 10 == 0:
            log.info("  waiting %4.0f s: sample %.2f C, controller PV %.1f / SP %.1f C",
                     now - start, T, pv, sp)
        if now - start > float(sc["stable_timeout_s"]):
            log.warning("Not stable after %.0f s; sweeping anyway (flagged in metadata)", now - start)
            return False
        nxt += 1.0
        rig.stop.wait(max(0.0, nxt - time.monotonic()))
    return False


def run_one_sweep(rig: Rig, s: int, target, points: list[float], ivlog: Csv) -> dict:
    temps, pvs, n_comp, n_done, aborted, prev = [], [], 0, 0, None, None
    log.info("Sweep %d (setpoint %s C): %d points", s, target, len(points))
    for k, v in enumerate(points):
        if rig.stop.is_set():
            aborted = "user stop"
            break
        rig.smu.set_level(v)
        time.sleep(float(rig.sc["delay_s"]))
        now = time.monotonic()
        row = {"sweep": s, "setpoint_C": target, "point": k, "set_value": v,
               "timestamp_utc": utc(), "elapsed_s": round(now - rig.t0, 4),
               "direction": "start" if prev is None else ("up" if v > prev else "down")}
        jobs = {"smu": rig.pool.submit(rig.smu.measure)}
        if rig.dmm:
            jobs["dmm"] = rig.pool.submit(read_temp, rig.dmm, rig.cfg["dmm"])
        if rig.ctrl and getattr(rig.ctrl, "has_pv", True):
            jobs["ctrl"] = rig.pool.submit(rig.ctrl.read_pv)
        res, errs = {}, {}
        for name, fut in jobs.items():
            try:
                res[name] = fut.result()
            except Exception as e:
                errs[name] = repr(e)
        if "smu" in res:
            mv, mi, trip = res["smu"]
            row.update(smu_V=mv, smu_I_A=mi, compliance=int(trip))
        if "dmm" in res:
            row["temp_C"] = res["dmm"]
            temps.append(res["dmm"])
        if "ctrl" in res:
            row["ctrl_pv_C"] = res["ctrl"]
            pvs.append(res["ctrl"])
        row["error"] = "; ".join(f"{n}: {e}" for n, e in errs.items()) or None
        ivlog.write(row)
        n_done += 1
        prev = v
        if errs:
            log.error("Sweep %d point %d: %s", s, k, row["error"])
        if "smu" in errs:
            aborted = "SMU error"
            break
        if row.get("compliance"):
            n_comp += 1
            log.warning("Compliance at set value %g", v)
            if rig.sc["abort_on_compliance"]:
                aborted = f"compliance at {v:g}"
                break
    return {"sweep": s, "setpoint_C": target, "points_done": n_done, "points_planned": len(points),
            "compliance_points": n_comp, "aborted": aborted,
            **_stats(temps, "sample_T"), **_stats(pvs, "controller_T")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--sample-id")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    exp, sc = cfg["experiment"], cfg["sweep"]
    tcfg = cfg.get("temperature_controller") or {"backend": "none"}
    if args.sample_id:
        exp["sample_id"] = args.sample_id

    points = sweep_points(sc)
    vmax = max(abs(p) for p in points)
    if vmax > float(sc["abs_limit"]):
        raise ValueError(f"Sweep reaches {vmax:g}, above sweep.abs_limit {sc['abs_limit']}")
    steps = tcfg.get("temperature_steps_c") or [None]
    if steps != [None]:
        if tcfg.get("backend", "none") == "none":
            raise ValueError("temperature_steps_c needs a temperature_controller backend")
        lo, hi = tcfg["setpoint_limits_c"]
        wanted = list(steps) + ([tcfg["end_setpoint_c"]] if tcfg.get("end_setpoint_c") is not None else [])
        bad = [t for t in wanted if not lo <= t <= hi]
        if bad:
            raise ValueError(f"Setpoints {bad} outside setpoint_limits_c {lo}..{hi}")
    plan = [t for t in steps for _ in range(int(sc["n_sweeps"]))]
    tc.self_test()

    start = dt.datetime.now().astimezone()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(exp["sample_id"]))
    run_dir = Path(exp["output_dir"]) / safe_id / ("IV_" + start.strftime("%Y%m%d_%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=False)
    setup_logging(run_dir, args.verbose)
    shutil.copy(args.config, run_dir / "config_original.yaml")
    (run_dir / "config_used.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    meta_path = run_dir / "metadata.json"
    meta = {"software": f"labdaq {__version__} (run_iv)", "simulated": args.sim,
            "start_local": start.isoformat(), "operator": exp.get("operator"),
            "sample_id": exp["sample_id"], "description": exp.get("description"),
            "config": cfg, "set_points": points, "temperature_plan_C": steps, "sweeps": []}
    write_json(meta_path, meta)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    iv_path, t_path = run_dir / "iv.csv", run_dir / "temperature_log.csv"
    ivlog, tlog = Csv(iv_path, IV_FIELDS), Csv(t_path, T_FIELDS)
    pool = ThreadPoolExecutor(max_workers=3)
    smu = dmm = ctrl = None
    exit_reason = "completed"
    try:
        smu, dmm = open_instruments(cfg, args.sim)
        ctrl = make_controller(cfg, args.sim)
        if args.sim and dmm is not None and ctrl is not None:
            dmm.follow = getattr(ctrl, "plant", None) or ctrl
        smu.configure(dict(cfg["smu"], source_level=0.0))
        if dmm:
            dmm.configure(cfg["dmm"])
        smu.fix_source_range(vmax)
        meta["instruments"] = {"smu": smu.idn, "dmm": dmm.idn if dmm else None}
        meta["controller"] = ctrl.describe() if ctrl else None
        write_json(meta_path, meta)

        rig = Rig(cfg, smu, dmm, ctrl, pool, stop)
        smu.output_on()
        stop.wait(cfg["smu"]["settle_s"])
        rig.t0 = time.monotonic()

        for s, target in enumerate(plan):
            if stop.is_set():
                exit_reason = "user stop"
                break
            if target is not None and (s == 0 or plan[s - 1] != target):
                ctrl.set_sp(target)
            elif s > 0:
                stop.wait(float(sc["pause_between_s"]))
            stable = wait_stable(rig, tlog, f"before sweep {s}", target) if sc["wait_for_stable_T"] else None
            if stop.is_set():
                exit_reason = "user stop"
                break
            info = run_one_sweep(rig, s, target, points, ivlog)
            info["T_stable_before"] = stable
            smu.set_level(0.0)
            meta["sweeps"].append(info)
            write_json(meta_path, meta)
            log.info("Sweep %d done: %s", s, info)
            if info["aborted"]:
                exit_reason = info["aborted"]
                break
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
        ivlog.close()
        tlog.close()
        meta["controller_events"] = getattr(ctrl, "events", None)
        meta.update(end_local=dt.datetime.now().astimezone().isoformat(), exit_reason=exit_reason,
                    iv_sha256=sha256(iv_path), temperature_log_sha256=sha256(t_path))
        write_json(meta_path, meta)
        log.info("Finished (%s) -> %s", exit_reason, run_dir)
    return 0 if exit_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
