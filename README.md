# labdaq I-V add-on + Modbus temperature controller (case 1)

Drop these into your existing `labdaq` project (the one with `run.py`,
`labdaq/instruments.py`, `labdaq/thermocouple.py`).

| File | Action |
|---|---|
| `run_iv.py` | new, project root |
| `plot_iv.py` | new, project root |
| `modbus_probe.py` | new, project root (read-only register scan) |
| `labdaq/tempctrl.py` | new |
| `patches/instruments_additions.py` | paste into `labdaq/instruments.py` |
| `config_additions.yaml` | add both blocks to `config.yaml` |
| `requirements_additions.txt` | `pip install -r requirements_additions.txt` |

## Bring-up order
1. `python run_iv.py --sim` (set `stable_window_s: 10` for speed)
2. `python modbus_probe.py --port COM5 --find-slave`
3. Scan registers, fill `pv_register` / `sp_register` in config
4. Read-only run: `temperature_steps_c: null`, `end_setpoint_c: null`
5. Write test with `[40]`, then the full plan

Registers 4096/4097 are Delta DTB placeholders. Manual address 40001 = protocol address 0 (fc 3).
