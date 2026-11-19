"""Usage: python plot_iv.py data/S07/IV_20261119_213000/iv.csv"""
import sys

import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv(sys.argv[1])
fig, (a, b) = plt.subplots(1, 2, figsize=(11, 4.5))
for s, g in df.groupby("sweep"):
    T = g.temp_C.mean() if "temp_C" in g else float("nan")
    lab = f"sweep {s}" + (f" ({T:.1f} °C)" if pd.notna(T) else "")
    a.plot(g.smu_V, g.smu_I_A, ".-", ms=3, label=lab)
    b.semilogy(g.smu_V, g.smu_I_A.abs(), ".-", ms=3, label=lab)
comp = df[df.compliance == 1]
if not comp.empty:
    a.plot(comp.smu_V, comp.smu_I_A, "rx", label="compliance")
a.set_xlabel("V (V)"); a.set_ylabel("I (A)")
b.set_xlabel("V (V)"); b.set_ylabel("|I| (A)")
a.legend(fontsize=8)
fig.tight_layout()
plt.show()
