#!/usr/bin/env python3
"""Read-only Modbus scan to find your controller's registers. Never writes anything.

  python modbus_probe.py --port COM5 --find-slave
  python modbus_probe.py --port COM5 --slave 1 --start 0 --count 40
  python modbus_probe.py --port COM5 --slave 1 --start 4096 --count 20 --fc 4

PV register = the one matching the temperature on the display.
SP register = the one that changes when you change the setpoint on the front panel.
"""
import argparse

import minimalmodbus
import serial

ap = argparse.ArgumentParser()
ap.add_argument("--port", required=True)
ap.add_argument("--slave", type=int, default=1)
ap.add_argument("--baud", type=int, default=9600)
ap.add_argument("--parity", default="N", choices=["N", "E", "O"])
ap.add_argument("--stopbits", type=int, default=1)
ap.add_argument("--start", type=int, default=0)
ap.add_argument("--count", type=int, default=40)
ap.add_argument("--fc", type=int, default=3, choices=[3, 4])
ap.add_argument("--find-slave", action="store_true")
a = ap.parse_args()

inst = minimalmodbus.Instrument(a.port, a.slave, mode=minimalmodbus.MODE_RTU)
inst.serial.baudrate = a.baud
inst.serial.parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[a.parity]
inst.serial.stopbits = a.stopbits
inst.serial.timeout = 0.3
inst.clear_buffers_before_each_transaction = True

if a.find_slave:
    found = False
    for s in range(1, 33):
        inst.address = s
        for fc in (3, 4):
            try:
                v = inst.read_register(a.start, 0, functioncode=fc)
                print(f"Slave {s} answered (fc {fc}, register {a.start} = {v})")
                found = True
                break
            except minimalmodbus.SlaveReportedException:
                print(f"Slave {s} exists (rejected register {a.start}, which is fine)")
                found = True
                break
            except Exception:
                pass
    if not found:
        print("No answer. Check A/B wiring (try swapping), baud rate and parity.")
else:
    print(f"{'reg':>6} {'hex':>7} {'raw':>7} {'signed':>7} {'x0.1':>8}")
    for r in range(a.start, a.start + a.count):
        try:
            raw = inst.read_register(r, 0, functioncode=a.fc)
            sg = raw - 65536 if raw >= 32768 else raw
            print(f"{r:6d} {r:#07x} {raw:7d} {sg:7d} {sg * 0.1:8.1f}")
        except Exception as e:
            print(f"{r:6d} {r:#07x}   -- {type(e).__name__}")
