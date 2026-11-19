"""`cp register`: scout inbox JSON lines -> items (or the pool when the pipeline is full). No LLM.

Inbox files are `<scout_inbox>\\<yyyymmdd-hhmmss>-<agent>-<REGION>.jsonl` (one JSON object per line, fields as in
prompts/scout.md). A file is registered once: its name goes into kv `register.done`. A file whose scout run is still
running (kv `scout.out.<run id>` of an active run) is left for the next call.

  cp register [--file PATH]... [--dry-run]

Rules per line (rows are never dropped silently: every skip is counted by reason, bad lines get one errors row per file):
  - not a JSON object / missing title, org or url          -> invalid
  - region: the line's `region`, else the file name's region, else KR
  - no parseable deadline, or deadline before today (KST)  -> expired / no_deadline  (assumption: undated entries skipped)
  - same normalized URL or org+title as an item or pool row -> duplicate
  - pipeline (new + verifying + ready) >= PIPELINE_MAX     -> pool row instead of an item
  - sns_account / post_url are not item fields: they are appended to `notes`; an empty `url` falls back to `post_url`
    (assumption)
Output: one JSON line {"ok": true, "files": N, "added": A, "pooled": P, "duplicate": D, "invalid": I, "skipped": S, ...}.
"""
import argparse
import contextlib
import glob
import json
import os
import re
import sys
from types import SimpleNamespace

from cp import cmdbase, config, intake, model, store

DONE_KEY = "register.done"
DONE_KEEP = 2000  # newest file names kept in kv register.done
ADD_KEYS = set(cmdbase.ADD_FIELDS)


def inbox_files() -> list:
    return sorted(glob.glob(os.path.join(config.scout_inbox_dir(), "*.jsonl")))


def _busy_files(db) -> set:
    """Base names of inbox files that an active scout run is still writing."""
    busy = set()
    for r in db.execute("SELECT id FROM runs WHERE (ended IS NULL OR ended='') AND kind='scout'"):
        out = store.kv_get(db, f"scout.out.{r['id']}")
        if isinstance(out, str):
            busy.add(os.path.basename(out))
    return busy


def pending_files(db) -> list:
    """Inbox files not yet registered and not being written."""
    done = store.kv_get(db, DONE_KEY, []) or []
    busy = _busy_files(db)
    return [f for f in inbox_files() if os.path.basename(f) not in done and os.path.basename(f) not in busy]


def _file_meta(path: str):
    """(agent, region) from `<stamp>-<agent>-<REGION>.jsonl`; agent falls back to system, region to None."""
    parts = re.sub(r"\.jsonl$", "", os.path.basename(path)).split("-")
    agent = parts[-2] if len(parts) >= 2 and parts[-2] in config.ACTORS else "system"
    region = parts[-1].upper() if parts and parts[-1].upper() in config.REGIONS else None
    return agent, region


def _row_from_line(obj: dict, default_region) -> dict:
    d = {k: v for k, v in obj.items() if k in ADD_KEYS}
    if not str(d.get("url") or "").strip() and str(obj.get("post_url") or "").strip():
        d["url"] = obj["post_url"]
    extra = []
    if obj.get("sns_account"):
        extra.append(f"SNS accounts to follow: {obj['sns_account']}")
    if obj.get("post_url") and obj.get("post_url") != d.get("url"):
        extra.append(f"original post: {obj['post_url']}")
    if extra:
        d["notes"] = " | ".join(x for x in [str(d.get("notes") or "").strip()] + extra if x)
    d["region"] = str(obj.get("region") or default_region or config.DEFAULT_REGION).upper()
    return d


def pipeline_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM items WHERE state IN ('new','verifying','ready')").fetchone()[0]


def register_file(db, path: str, idx, today: str, pipeline: int, dry_run=False) -> dict:
    """Register one file inside the caller's write transaction. Returns the per-file counts."""
    agent, file_region = _file_meta(path)
    res = {"file": os.path.basename(path), "lines": 0, "added": 0, "pooled": 0, "duplicate": 0, "invalid": 0,
           "expired": 0, "no_deadline": 0, "first_invalid": None, "ids": []}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        res["invalid"] += 1
        res["first_invalid"] = f"cannot read file: {e}"
        return res

    def bad(n, why):
        res["invalid"] += 1
        if res["first_invalid"] is None:
            res["first_invalid"] = f"line {n}: {why}"

    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        res["lines"] += 1
        try:
            obj = json.loads(line)
        except ValueError as e:
            bad(n, f"not JSON ({e})")
            continue
        if not isinstance(obj, dict):
            bad(n, "not a JSON object")
            continue
        d = _row_from_line(obj, file_region)
        missing = [k for k in ("title", "org", "url") if not str(d.get(k) or "").strip()]
        if missing:
            bad(n, "missing " + ",".join(missing))
            continue
        if d["region"] not in config.REGIONS:
            bad(n, f"bad region {d['region']}")
            continue
        dl, _ = model.parse_deadline(d.get("deadline"), d["region"])
        if not dl:
            res["no_deadline"] += 1
            continue
        if dl < today:
            res["expired"] += 1
            continue
        d["deadline"] = dl
        if dry_run:
            res["pooled" if pipeline >= config.PIPELINE_MAX else "added"] += 1
            continue
        if pipeline >= config.PIPELINE_MAX:
            r = intake.cmd_pool_add(db, SimpleNamespace(agent=agent, json=json.dumps([d], ensure_ascii=False), file=None))
            st = r["results"][0]
            if st["status"] == "added":
                res["pooled"] += 1
            elif st["status"] == "duplicate":
                res["duplicate"] += 1
            else:
                bad(n, st.get("error") or "pool row rejected")
            continue
        st = intake._insert_new(db, idx, d, agent)
        if st["status"] == "added":
            res["added"] += 1
            res["ids"].append(st["id"])
            pipeline += 1
        elif st["status"] == "duplicate":
            res["duplicate"] += 1
        else:
            bad(n, st.get("error") or "rejected")
    res["pipeline_after"] = pipeline
    return res


def run(files: list | None = None, dry_run=False) -> dict:
    """Register `files` (default: every pending inbox file). Returns the summary dict."""
    db = store.connect()
    try:
        files = list(files) if files else pending_files(db)
        total = {"ok": True, "files": 0, "added": 0, "pooled": 0, "duplicate": 0, "invalid": 0, "skipped": 0,
                 "per_file": [], "dry_run": dry_run}
        if not files:
            return total
        today = model.today_kst().isoformat()
        ctx = contextlib.nullcontext(db) if dry_run else store.writer(db)
        with ctx:
            idx = intake.DupIndex(db)
            pipeline = pipeline_count(db)
            done = list(store.kv_get(db, DONE_KEY, []) or [])
            for path in files:
                r = register_file(db, path, idx, today, pipeline, dry_run)
                pipeline = r.get("pipeline_after", pipeline)
                total["files"] += 1
                for k in ("added", "pooled", "duplicate", "invalid"):
                    total[k] += r[k]
                total["skipped"] += r["expired"] + r["no_deadline"]
                total["per_file"].append({k: v for k, v in r.items() if k not in ("pipeline_after",)})
                if not dry_run:
                    done.append(os.path.basename(path))
                    if r["invalid"]:
                        store.add_error(db, "system", f"register {r['file']}: {r['invalid']} invalid line(s); "
                                                      f"first: {r['first_invalid']}")
                    store.add_history(db, None, "system", "register",
                                      f"{r['file']}: added {r['added']}, pool {r['pooled']}, duplicate "
                                      f"{r['duplicate']}, invalid {r['invalid']}, expired {r['expired']}, "
                                      f"no deadline {r['no_deadline']}")
            if not dry_run:
                store.kv_set(db, DONE_KEY, done[-DONE_KEEP:])
        return total
    finally:
        db.close()


def write_known_events(db) -> str:
    """known-events.txt for scouts: `url<TAB>org title` of every item and pool row. Returns the path."""
    path = config.known_events_path()
    rows = [f"{r['url'] or ''}\t{(r['org'] or '').strip()} {(r['title'] or '').strip()}".replace("\r", " ")
            for r in db.execute("SELECT url, org, title FROM items UNION ALL SELECT url, org, title FROM pool")]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(r.replace("\n", " ") for r in rows))
    os.replace(tmp, path)
    return path


def main(argv) -> int:
    config.utf8_stdio()
    args = list(argv)
    if args and args[0] == "register":
        args = args[1:]
    ap = argparse.ArgumentParser(prog="cp register")
    ap.add_argument("--file", action="append", help="register this file (repeatable) instead of the pending inbox files")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    try:
        a = ap.parse_args(args)
    except SystemExit:
        print(json.dumps({"ok": False, "error": f"bad arguments: {' '.join(args)}"}))
        return 1
    try:
        r = run(a.file, a.dry_run)
    except Exception as e:  # never swallow: report as the command result
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(r, ensure_ascii=False, default=str))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
