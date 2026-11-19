"""Project test command: python run_tests.py  (engine tests + app tests, each module in its own process)."""
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, EP_APP_DIR=tempfile.mkdtemp(prefix="ep-tests-"), PYTHONIOENCODING="utf-8")
for k in [k for k in env if k.upper().startswith(("CLAUDE", "GEMINI_", "GOOGLE_API", "LCH_"))]:
    env.pop(k)
bad = []
for t in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "-q", os.path.relpath(t, ROOT)], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = r.stderr.strip().splitlines()
    last = next((ln for ln in reversed(lines) if ln.startswith(("OK", "FAILED"))), lines[-1] if lines else "?")
    print(f"{os.path.basename(t):28} {last}")
    if r.returncode:
        bad.append(t)
        print(r.stderr[-3000:])
sys.exit(1 if bad else 0)
