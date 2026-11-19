"""CLI entry point of the contest engine: python cp.py <command> ... (the app exe also answers to this, see app/pyshim.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cp.cli  # noqa: E402

if __name__ == "__main__":
    cp.cli.main(sys.argv[1:])
