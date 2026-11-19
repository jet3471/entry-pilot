"""One KR contest scout run without browsing: a Python crawler collects candidates, then ONE agent call judges
them from the collected text and writes the inbox file. Started by the scheduler in place of the agent CLI:
    python -X utf8 -m cp.scout_run --agent gemini --out <inbox.jsonl> --known <known file>
The agent's own environment (account, CODEX_HOME, token) is inherited from the scheduler's launch. Exit code = the agent's
exit code; 0 candidates -> an empty inbox file and `scout done: 0 lines`."""
import argparse
import os
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run as a script

from cp import config, model, prompts
from cp.agents.base import _task_file_with_path, cleanup_task_file


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--known", required=True)
    ap.add_argument("--max", type=int, default=60)
    a = ap.parse_args(argv)
    cand = os.path.join(config.data_dir(), "work", os.path.basename(a.out).replace(".jsonl", "-cand.json"))
    os.makedirs(os.path.dirname(cand), exist_ok=True)
    # candidates judged before (accepted or not) are remembered, so the next scout never judges them again
    seen_path = os.path.join(config.data_dir(), "crawler-seen.tsv")
    known_all = os.path.join(config.data_dir(), "work", os.path.basename(a.out).replace(".jsonl", "-known.txt"))
    with open(known_all, "w", encoding="utf-8") as kf:
        for src in (a.known, seen_path):
            if os.path.exists(src):
                with open(src, encoding="utf-8", errors="replace") as f:
                    kf.write(f.read().rstrip("\n") + "\n")
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "cp.crawler", "--known", known_all, "--out", cand,
                        "--max", str(a.max)], cwd=config.PROJECT_DIR, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, timeout=600)
    sys.stdout.write("crawler exit %s\n%s\n" % (r.returncode, (r.stderr or "")[-1500:]))
    if r.returncode != 0 or not os.path.exists(cand):
        print("scout crawler failed: all sources failed")
        return 3
    import json
    with open(cand, encoding="utf-8") as f:
        cands = json.load(f)
    n = len(cands)
    if n == 0:
        open(a.out, "w", encoding="utf-8").close()
        print("scout done: 0 lines -> %s" % a.out)
        return 0
    today = model.today_kst()
    start = today + timedelta(days=3)  # time to write the work (as prompts/scout.md)
    until = today + timedelta(days=45)  # same window as prompts.py
    text = prompts._fill(prompts._read("scout-judge.md"),
                         {"CAND": cand, "OUT": a.out, "TODAY": start.isoformat(), "UNTIL": until.isoformat()})
    from cp import agents
    ad = agents.get(a.agent)
    instr, task_path = _task_file_with_path(a.agent, "scout", os.path.basename(a.out), text)
    cmd = ad.command(instr, "scout")
    sys.stdout.write("candidates: %d\n" % n)
    sys.stdout.flush()
    try:
        p = subprocess.Popen(cmd, cwd=config.AGENT_CWD, stdin=subprocess.PIPE)  # closed pipe, not NUL (NUL looks like a TTY to some CLIs)
        p.stdin.close()
        code = p.wait()
    finally:
        cleanup_task_file(task_path)
    if not os.path.exists(a.out):
        # five Gemini runs ended with a 429 and exit code 0; the candidates had been written to the seen
        # file before judging and were lost. Now they are remembered only when the judge wrote its file, and a run
        # without a file is a failure (the next scout collects the same candidates again).
        print("scout judge failed: no output file, candidates not marked as seen (exit %s)" % code)
        return code or 4
    with open(seen_path, "a", encoding="utf-8") as sf:
        for c in cands:
            sf.write("%s\t%s\n" % (c.get("source_url") or "", (c.get("title") or "").replace("\t", " ").replace("\n", " ")))
    return code


if __name__ == "__main__":
    sys.exit(main())
