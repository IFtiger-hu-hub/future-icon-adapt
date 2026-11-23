#!/usr/bin/env python3
"""Convenience launcher: python3 app.py  [same as python3 -m iconadapt gui]"""

from iconadapt.__main__ import main
import sys

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["gui"]
    raise SystemExit(main(argv))
