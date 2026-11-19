"""Backfill missing minimum prize values.

  ep backfill [--limit 20] [--dry-run] [--state submitted,ready,...]

Picks items with a non-empty `prize` and prize_min_value IS NULL (oldest first, default states: every state except
excluded), asks the cheap model (the same runner as ep/translate.py) ONE question for all of them, and
applies the answers with store.update_item + a history row "backfill" by agent "claude".

Only the fields prize_min_value, winners and all_rewarded are ever written, and only while they are still NULL at the
moment of writing. An item gets at most one attempt (a history row "backfill" is written even when the model had no
value, so it is not asked again and again). A failed model call writes an errors row and marks nothing.
"""
import argparse
import json
import re
import sys

from ep import model, prompts, store, translate

DEFAULT_LIMIT = 20
DEFAULT_STATES = ("submitted", "submitting", "ready", "failed", "owner", "manual", "verifying", "new")
FIELDS = ("prize_min_value", "winners", "all_rewarded")
MAX_VALUE_KRW = 1_000_000_000  # sanity cap: larger answers are rejected as implausible
AGENT = "claude"  # history agent for model-made values


def candidates(db, states, limit: int) -> list:
    marks = ",".join("?" * len(states))
    rows = store.items(
        db, f"state IN ({marks}) AND prize IS NOT NULL AND TRIM(prize) != '' AND prize_min_value IS NULL "
            "AND id NOT IN (SELECT item_id FROM history WHERE action='backfill')", list(states))
    rows.sort(key=lambda it: (it.get("added_at") or "9999", int(it["id"][1:]) if it["id"][1:].isdigit() else 0))
    return rows[:limit]


def parse_answer(text: str) -> list:
    arr = translate.extract_json_array(text)
    return [a for a in arr if isinstance(a, dict)]


def _num(v):
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return model.to_int(v)


def clean(ans: dict) -> tuple:
    """(values to write, problems) for one answer object."""
    out, bad = {}, []
    v = _num(ans.get("prize_min_value"))
    if v is not None:
        if 0 < v <= MAX_VALUE_KRW:
            out["prize_min_value"] = v
        else:
            bad.append(f"prize_min_value {v} out of range")
    w = _num(ans.get("winners"))
    if w is not None:
        if 0 < w <= 10_000_000:
            out["winners"] = w
        else:
            bad.append(f"winners {w} out of range")
    ar = ans.get("all_rewarded")
    if ar in (0, 1) and not isinstance(ar, bool):
        out["all_rewarded"] = int(ar)
    elif ar is not None and ar != "":
        bad.append(f"all_rewarded {ar!r} not 0/1")
    return out, bad


def apply(answers: list, asked: set) -> dict:
    """Write the answers. Returns {"updated": [ids], "skipped": [{id, reason}]}. Ids that were not asked are ignored."""
    updated, skipped, seen = [], [], set()
    with store.writer() as db:
        for ans in answers:
            iid = str(ans.get("id") or "").strip()
            if iid not in asked or iid in seen:
                skipped.append({"id": iid or "?", "reason": "id not in the request" if iid not in asked else "duplicate answer"})
                continue
            seen.add(iid)
            it = store.get_item(db, iid)
            vals, bad = clean(ans)
            note = re.sub(r"\s+", " ", str(ans.get("note") or "")).strip()[:120]
            upd = {k: v for k, v in vals.items() if it.get(k) is None}
            if upd:
                store.update_item(db, iid, **upd)
                detail = "backfill " + " ".join(f"{k}={v}" for k, v in upd.items()) + (f" | {note}" if note else "")
                store.add_history(db, iid, AGENT, "backfill", detail)
                updated.append(iid)
            else:
                why = "; ".join(bad) or ("no value from the model" if not vals else "fields already set")
                store.add_history(db, iid, AGENT, "backfill", f"backfill tried: {why}" + (f" | {note}" if note else ""))
                skipped.append({"id": iid, "reason": why})
        for iid in asked - seen:  # asked but not answered: mark the attempt, not an error
            store.add_history(db, iid, AGENT, "backfill", "backfill tried: no answer for this id")
            skipped.append({"id": iid, "reason": "no answer for this id"})
    return {"updated": updated, "skipped": skipped}


def run(limit: int = DEFAULT_LIMIT, states=DEFAULT_STATES, dry_run: bool = False) -> dict:
    db = store.connect()
    try:
        rows = candidates(db, states, limit)
    finally:
        db.close()
    prompt, ids = prompts.backfill_build(rows)
    base = {"considered": len(ids), "model": translate.MODEL}
    if dry_run:
        return {"ok": True, "dry_run": True, **base, "ids": ids, "prompt_chars": len(prompt),
                "prompt_budget": prompts.BUDGET["total"], "states": list(states)}
    if not ids:
        return {"ok": True, **base, "updated": 0, "skipped": []}
    try:
        answers = parse_answer(translate.run_model(prompt))
    except translate.ModelError as e:
        msg = f"backfill model call failed: {e}"
        try:
            with store.writer() as db:
                store.add_error(db, "system", msg)
        except Exception as e2:
            msg += f" (errors row not written: {type(e2).__name__}: {e2})"
        return {"ok": False, "error": msg, **base, "updated": 0, "skipped": [], "env": translate.LAST.get("env")}
    res = apply(answers, set(ids))
    return {"ok": True, **base, "updated": len(res["updated"]), "updated_ids": res["updated"],
            "skipped": res["skipped"], "env": translate.LAST.get("env")}


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ep backfill")
    ap.add_argument("cmd", nargs="?", default="backfill")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--state", default=",".join(DEFAULT_STATES), help="comma separated item states")
    a = ap.parse_args(argv)
    states = [s.strip() for s in a.state.split(",") if s.strip()]
    bad = [s for s in states if s not in model.STATES]
    if bad or not states:
        res = {"ok": False, "error": f"bad --state {bad or a.state}; allowed: {','.join(model.STATES)}"}
    else:
        try:
            res = run(max(1, a.limit), states, a.dry_run)
        except Exception as e:  # never swallow: JSON result + errors row
            msg = f"backfill failed: {type(e).__name__}: {e}"
            try:
                with store.writer() as db:
                    store.add_error(db, "system", msg)
            except Exception:
                pass
            res = {"ok": False, "error": msg}
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    return 0 if res.get("ok") else 1
