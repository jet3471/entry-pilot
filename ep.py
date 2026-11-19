"""CLI entry point: python ep.py <command> ... (Entry Pilot: the app exe also answers to this, see app/pyshim.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ep.cli  # noqa: E402

if __name__ == "__main__":
    ep.cli.main(sys.argv[1:])
