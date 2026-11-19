# labdaq-iv

I-V sweeps on a Keysight B2901B with sample temperature from a Keithley DMM6500 (K-type TC), optionally stepping the substrate-heater controller through a list of temperatures.

The controller backend is chosen in `config.yaml` → `temperature_controller.backend`:

| Backend | Link | Status |
|---|---|---|
| `modbus` | Modbus RTU/ASCII over RS-485 | done |
| `rs485_ascii` | RS-485, maker's text protocol | done |
| `manual` | no comms, set by hand | next |
| `none` | sweep at current temperature | done |

## Files

| File | Purpose |
|---|---|
| `run_iv.py` | main script |
| `plot_iv.py` | plot `iv.csv` |
| `labdaq/tempctrl.py` | controller backends |
| `modbus_probe.py` | read-only Modbus register scan |
| `rs485_term.py` | raw RS-485 terminal |
| `config_additions.yaml` | `sweep` + `temperature_controller` blocks for `config.yaml` |
| `patches/instruments_additions.py` | paste into `labdaq/instruments.py` |
| `requirements_additions.txt` | extra packages |

Needs the base `labdaq` project (`run.py`, `labdaq/instruments.py`, `labdaq/thermocouple.py`).

```
pip install -r requirements_additions.txt
python run_iv.py --sim
python run_iv.py --sample-id S07
python plot_iv.py data/S07/IV_.../iv.csv
```

## Case 1: Modbus

```
python modbus_probe.py --port COM5 --find-slave
python modbus_probe.py --port COM5 --slave 1 --start 4096 --count 20
```

PV register = matches display. SP register = changes when you change the setpoint on the panel. Fill `pv_register`, `sp_register`. Manual address 40001 = protocol address 0 (fc 3). 4096/4097 are Delta DTB placeholders.

## Case 2: RS-485 text protocol

```
python rs485_term.py --port COM5 --listen 3
python rs485_term.py --port COM5 "01RPV" "01RSP"
python rs485_term.py --port COM5
```

Fill `read_pv`, `read_sp`, `write_sp`, `checksum`, `eol` from the manual or what works. Send only read commands until you know which one writes. No reply: swap A/B, check baud/parity/address, try `echo: true`.

## Bring-up (every backend)

1. `python run_iv.py --sim` (`stable_window_s: 10` for speed)
2. Read-only real run: `temperature_steps_c: null`, `end_setpoint_c: null`
3. Write test: `temperature_steps_c: [40]`, watch the panel
4. Full plan

## Output

`data/<sample>/IV_<time>/`: `iv.csv`, `temperature_log.csv`, `metadata.json`, `run.log`, configs.
