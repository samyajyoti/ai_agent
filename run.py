"""Thin wrapper so you can run `python run.py ...` without `python -m src.main`."""

from src.main import entry

if __name__ == "__main__":
    entry()
