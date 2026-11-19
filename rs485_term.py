#!/usr/bin/env python3
import argparse
import sys
import time

import serial

ap = argparse.ArgumentParser()
ap.add_argument("--port", required=True)
ap.add_argument("--baud", type=int, default=9600)
ap.add_argument("--parity", default="N", choices="NEO")
ap.add_argument("--bytesize", type=int, default=8)
ap.add_argument("--stopbits", type=int, default=1)
ap.add_argument("--eol", default="\\r")
ap.add_argument("--wait", type=float, default=0.5)
ap.add_argument("--listen", type=float, default=0, help="only listen N s, send nothing")
ap.add_argument("cmd", nargs="*")
a = ap.parse_args()

par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[a.parity]
s = serial.Serial(a.port, a.baud, bytesize=a.bytesize, parity=par, stopbits=a.stopbits, timeout=0.05)
eol = a.eol.encode().decode("unicode_escape").encode("latin-1")


def show(tag, b):
    print(f"{tag} {b!r}  [{b.hex(' ')}]")


def rx(t):
    buf, end = b"", time.monotonic() + t
    while time.monotonic() < end:
        buf += s.read(256)
    return buf


if a.listen:
    show("<<", rx(a.listen))
    sys.exit()

cmds = a.cmd or iter(lambda: input("> "), "")
for c in cmds:
    f = c.encode().decode("unicode_escape").encode("latin-1") + eol
    s.reset_input_buffer()
    s.write(f)
    s.flush()
    show(">>", f)
    show("<<", rx(a.wait))
