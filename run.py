"""Точка входа: python run.py — запускает трекер и дашборд."""
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from app.main import run  # noqa: E402

if __name__ == "__main__":
    run()
