"""`ep mailwatch`: not part of Entry Pilot (the command reports that it is not available)."""
import json
import sys


def main(argv=None) -> int:
    print(json.dumps({"ok": False, "error": "mailwatch is not part of Entry Pilot"}))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
