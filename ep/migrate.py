"""`ep migrate`: legacy board.json / runs.json -> platform.db, and profile.json defaults.

Idempotent: rows already present (same id / same error text) are skipped; a second run changes nothing.
`--dry-run` only reads: it migrates into an in-memory copy of platform.db and reports the counts it would write.
Legacy Korean text (titles, notes, history details) is copied as-is because it is legacy data.
"""
import json
import os
import re
import sqlite3
import urllib.parse

from ep import config, model, store

NO_RESUBMIT_MARK = "다시 제출 금지"  # legacy "do not submit again" note
EXPIRED_REASON = "expired before platform start"
EMAIL = ""  # the user types the e-mail in the app; migrate never overwrites it

to_bool, to_int = model.to_bool, model.to_int


def _lane(old_state, old_lane, vn):
    if old_state == "ready_gemini" or (old_lane == "gemini" and old_state != "ready_codex"):
        return "form"
    if old_state == "ready_codex" or old_lane == "codex":
        return "browser"
    return None


def map_item(it: dict, runs: dict, at: str) -> tuple[dict, list]:
    """Legacy item -> (new item dict, history rows). Pure function (no DB access)."""
    old_state = it.get("state") or "new"
    state = "ready" if old_state in ("ready_gemini", "ready_codex") else old_state
    if state not in model.STATES:
        state = "manual"  # an unknown legacy state is left to the user
    vn = it.get("verify_notes") or ""
    lane = _lane(old_state, it.get("lane"), vn)
    raw_dl = it.get("deadline")
    dl, tm = model.parse_deadline(raw_dl, "KR") if raw_dl else ("", "")
    reason = {"excluded": it.get("exclude_reason"), "manual": it.get("manual_reason"),
              "failed": it.get("fail_reason")}.get(state)
    hist = it.get("history") or []
    flags = {}
    if it.get("possible_dup_of"):
        flags["possible_dup_of"] = it["possible_dup_of"]
    blob = " ".join(str(it.get(k) or "") for k in ("notes", "owner_question", "fail_reason", "verify_notes"))
    if NO_RESUBMIT_MARK in blob and state in ("failed", "owner", "manual"):
        flags["no_resubmit"] = 1  # legacy "do not submit again" notes keep blocking resubmission
    tries = {"verify": int((runs.get("verify_tries") or {}).get(it["id"], 0) or 0),
             "submit": sum(1 for h in hist if h.get("action") == "fail")}  # legacy fail count
    notes = it.get("notes") or ""
    if it.get("share_note"):
        notes = (notes + " | " + it["share_note"]).strip(" |")
    new = {
        "id": it["id"], "region": "KR", "title": it.get("title"), "org": it.get("org"), "url": it.get("url"),
        "source_url": it.get("source_url"), "alt_urls": it.get("alt_urls") or [],
        "form_type": it.get("form_type") or "other", "site_kind": model.site_kind(it.get("url"), it.get("form_type")),
        "site_host": model.url_host(it.get("url")), "deadline": dl, "deadline_time": tm,
        "deadline_raw": str(raw_dl) if raw_dl and not dl else None, "tz": None, "prize": it.get("prize"),
        "prize_min_value": to_int(it.get("prize_min_value", it.get("min_prize_won"))),
        "winners": to_int(it.get("winners")), "all_rewarded": to_bool(it.get("all_rewarded")),
        "effort_min": to_int(it.get("effort_min", it.get("effort"))),
        "login_required": to_bool(it.get("login_required")), "conditions": None,
        "result_date": it.get("result_date"), "eligibility": "unknown", "state": state,
        "lane": lane if state not in ("new", "verifying") else None, "need": it.get("manual_need"),
        "reason": reason, "question": it.get("owner_question"), "verify_notes": None, "legacy_notes": vn or None,
        "notes": notes or None, "answers": it.get("answers"),
        "claimed_by": it.get("claimed_by") if state in ("submitting", "verifying") else None,
        "claimed_at": None, "added_by": it.get("added_by"), "added_at": it.get("added_at"), "updated_at": at,
        "tries": tries, "flags": flags, "evidence": it.get("evidence"), "submitted_at": it.get("submitted_at"),
        "submitted_by": it.get("submitted_by"),
    }
    rows = [(it["id"], h.get("at"), h.get("agent"), h.get("action"), h.get("detail") or "") for h in hist]
    rows.append((it["id"], at, "system", "migrate", f"migrated from board.json (legacy state {old_state})"))
    if dl and dl < config.PLATFORM_START and state not in ("submitted", "excluded"):
        new.update(state="excluded", reason=EXPIRED_REASON, claimed_by=None)
        rows.append((it["id"], at, "system", "expired", f"{EXPIRED_REASON} (deadline {dl}, legacy state {old_state})"))
    return new, rows


def map_pool(p: dict) -> dict:
    return {"id": p["id"], "region": "KR", "title": p.get("title"), "org": p.get("org"), "url": p.get("url"),
            "source_url": p.get("source_url"), "form_type": p.get("form_type") or "other",
            "deadline": p.get("deadline"), "prize": p.get("prize"), "login_required": to_bool(p.get("login_required")),
            "conditions": p.get("conditions"), "result_date": p.get("result_date"), "winners": to_int(p.get("winners")),
            "prize_min_value": to_int(p.get("prize_min_value", p.get("min_prize_won"))),
            "all_rewarded": to_bool(p.get("all_rewarded")), "effort_min": to_int(p.get("effort_min", p.get("effort"))),
            "added_at": p.get("added_at"), "added_by": p.get("added_by"), "promoted_to": None}


def _phone_intl(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("82"):
        d = d[2:]
    d = d.removeprefix("0")
    if len(d) == 10:
        return f"+82 {d[:2]}-{d[2:6]}-{d[6:]}"
    if len(d) == 9:
        return f"+82 {d[:2]}-{d[2:5]}-{d[5:]}"
    return f"+82 {d}" if d else ""


def profile_update(dry_run: bool) -> dict:
    """email -> EMAIL; add `en` / `jp` defaults only if missing. Reports what changed, never the values."""
    path = config.profile_path()
    if not os.path.exists(path):
        return {"found": False}
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    res = {"found": True, "email_changed": False, "en_added": False, "jp_added": False}
    if EMAIL and p.get("email") != EMAIL:
        p["email"] = EMAIL
        res["email_changed"] = True
    if "en" not in p:
        p["en"] = {"name": "", "country": "", "phone_intl": _phone_intl(p.get("phone", "")), "address_en": ""}
        res["en_added"] = True
    if "jp" not in p:
        p["jp"] = {"name_katakana": "", "name_romaji": "", "country": ""}
        res["jp_added"] = True
    if not dry_run and (res["email_changed"] or res["en_added"] or res["jp_added"]):
        from ep import secrets as ep_secrets
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
        ep_secrets.restrict_acl(tmp)
        os.replace(tmp, path)
    return res


def _memory_copy() -> sqlite3.Connection:
    """In-memory copy of platform.db (read-only open) for --dry-run; empty schema when the db does not exist."""
    mem = sqlite3.connect(":memory:", isolation_level=None)
    mem.row_factory = sqlite3.Row
    path = config.db_path()
    if os.path.exists(path):
        uri = "file:" + urllib.parse.quote(path.replace("\\", "/"))
        try:
            src = sqlite3.connect(uri + "?mode=ro", uri=True)
            src.backup(mem)
        except sqlite3.OperationalError:
            src = sqlite3.connect(uri + "?immutable=1", uri=True)
            src.backup(mem)
        src.close()
    mem.executescript(store.SCHEMA)
    return mem


def run(db, dry_run: bool = False) -> dict:
    board_path, runs_path = config.legacy_board_path(), config.legacy_runs_path()
    if not os.path.exists(board_path):
        return {"ok": False, "error": f"no legacy board at {board_path}"}
    with open(board_path, encoding="utf-8") as f:
        board = json.load(f)
    runs = {}
    if os.path.exists(runs_path):
        with open(runs_path, encoding="utf-8") as f:
            runs = json.load(f)
    own = db is None
    if dry_run:
        db = _memory_copy()
        db.execute("BEGIN")
    at = store.now_iso()
    c = {"items_added": 0, "items_present": 0, "history_rows": 0, "expired_at_migration": 0, "states": {},
         "lanes": {}, "unparsed_deadlines": 0, "pool_added": 0, "pool_present": 0, "errors_added": 0,
         "errors_present": 0, "kv_set": [], "kv_present": []}
    have = {r[0] for r in db.execute("SELECT id FROM items")}
    hist_rows = []
    for it in board.get("items", []):
        if it["id"] in have:
            c["items_present"] += 1
            continue
        new, rows = map_item(it, runs, at)
        store.insert_item(db, new)
        hist_rows += rows
        c["items_added"] += 1
        c["states"][new["state"]] = c["states"].get(new["state"], 0) + 1
        if new["lane"]:
            c["lanes"][new["lane"]] = c["lanes"].get(new["lane"], 0) + 1
        c["expired_at_migration"] += new["reason"] == EXPIRED_REASON
        c["unparsed_deadlines"] += bool(new["deadline_raw"])
    db.executemany("INSERT INTO history(item_id, at, agent, action, detail) VALUES(?,?,?,?,?)", hist_rows)
    c["history_rows"] = len(hist_rows)

    have_p = {r[0] for r in db.execute("SELECT id FROM pool")}
    for p in board.get("pool", []):
        if p["id"] in have_p:
            c["pool_present"] += 1
            continue
        store.insert_pool(db, map_pool(p))
        c["pool_added"] += 1

    for e in board.get("errors", []):
        msg = e.get("message") or ""
        if db.execute("SELECT 1 FROM errors WHERE at=? AND agent=? AND message=?",
                      (e.get("at"), e.get("agent"), msg)).fetchone():
            c["errors_present"] += 1
            continue
        db.execute("INSERT INTO errors(at, agent, message, resolved) VALUES(?,?,?,?)",
                   (e.get("at"), e.get("agent"), msg, 1 if e.get("resolved") else 0))
        c["errors_added"] += 1

    kv = {"next_id": int(board.get("next_id") or 1), "stop": False}
    for k in ("codex_home", "claude_account"):
        if runs.get(k):
            kv[k] = runs[k]
    for agent, why in (runs.get("blocked") or {}).items():
        kv[f"blocked.{agent}"] = why if isinstance(why, str) else json.dumps(why, ensure_ascii=False)
    for k, v in kv.items():
        if store.kv_get(db, k) is None:
            store.kv_set(db, k, v)
            c["kv_set"].append(k)
        else:
            c["kv_present"].append(k)
    if store.kv_get(db, "migrated_at") is None:
        store.kv_set(db, "migrated_at", at)

    c["profile"] = profile_update(dry_run)
    if dry_run:
        db.execute("ROLLBACK")
        db.close()
    elif own:
        db.close()
    return {"ok": True, "dry_run": dry_run, **c}
