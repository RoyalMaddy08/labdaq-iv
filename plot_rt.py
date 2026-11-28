"""Usage: python plot_rt.py data/S07/RT_20261128_100000/rt.csv [--celsius] [--source controller] [--save rt.png]"""
import argparse

import matplotlib.pyplot as plt
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--celsius", action="store_true", help="x axis in C (default K)")
ap.add_argument("--source", choices=["dmm", "controller"], default="dmm", help="temperature column")
ap.add_argument("--save", help="write PNG instead of showing")
args = ap.parse_args()

df = pd.read_csv(args.csv)
T = df.temp_C if args.source == "dmm" else df.ctrl_pv_C
df["x"] = T if args.celsius else T + 273.15
df = df[(df.R_ohm > 0) & df.x.notna()]

colors = {"heating": "black", "cooling": "red", "hold": "gray", "log": "tab:blue"}
fig, ax = plt.subplots(figsize=(10, 6))
for phase, g in df.groupby("phase", sort=False):
    ax.semilogy(g.x, g.R_ohm, "s-", ms=3, lw=1.2, color=colors.get(phase), label=phase.capitalize())
comp = df[df.compliance == 1]
if not comp.empty:
    ax.semilogy(comp.x, comp.R_ohm, "x", color="orange", label="compliance")
ax.set_xlabel("Temperature (C)" if args.celsius else "Temperature (K)")
ax.set_ylabel("Resistance (Ω)")
ax.set_title("Resistance–Temperature")
ax.grid(True, which="both", ls="--", alpha=0.4)
ax.legend()
fig.tight_layout()
if args.save:
    fig.savefig(args.save, dpi=300)
else:
    plt.show()
