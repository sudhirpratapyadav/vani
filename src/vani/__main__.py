"""Allows `python -m vani ...` alongside the installed `vani` script."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
