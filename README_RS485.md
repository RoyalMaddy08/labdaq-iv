# Case 2: RS-485, maker's text protocol

Copy into the repo root (overwrites `labdaq/tempctrl.py`; Modbus backend unchanged, now also `mode: ascii`).

| File | |
|---|---|
| `labdaq/tempctrl.py` | adds `rs485_ascii` backend |
| `rs485_term.py` | raw terminal to discover the protocol |
| `config_additions.yaml` | replace the `temperature_controller` block |

```
pip install pyserial
python rs485_term.py --port COM5 --listen 3          # does it stream on its own?
python rs485_term.py --port COM5 "01RPV" "01RSP"     # try manual's read commands
python rs485_term.py --port COM5                     # interactive
```

Fill `read_pv`, `read_sp`, `write_sp`, `checksum`, `eol` from what works, set `backend: rs485_ascii`, then:

1. `python run_iv.py --sim`
2. `temperature_steps_c: null`, `end_setpoint_c: null` → read-only real run
3. `temperature_steps_c: [40]` → write test, watch panel
4. full plan

Only send read commands with `rs485_term.py` until you know which command writes.
No reply: swap A/B, check baud/parity/address, try `echo: true`.
