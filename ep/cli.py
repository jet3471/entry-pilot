"""argparse subcommands. Every command prints exactly one JSON line; exit code 1 on error.

Programmatic use: `ep.cli.call(["status"])` runs a core command and returns its result dict without printing.
"""
import argparse
import hashlib
import importlib
import json
import os
import secrets
import sys

from ep import config, model, store

# External command -> module (owned by other components). Lazily imported; see dispatch_external().
REST_MODULES = {"chrome": "chrome", "form-read": "form_read", "consent": "consent",
                "secret": "secrets", "sns": "sns_login", "mailwatch": "mailwatch"}
FULL_MODULES = {"run": "scheduler", "stop": "scheduler", "resume": "scheduler", "block": "scheduler",
                "accounts": "accounts", "render": "render", "translate": "translate",
                "register": "register", "backfill": "backfill"}

LIST_FIELDS = ("id", "region", "state", "lane", "site_kind", "title", "org", "url", "deadline", "deadline_time",
               "need", "reason", "question", "claimed_by", "eligibility", "flags", "submitted_by", "submitted_at")
ADD_FIELDS = ("title", "org", "url", "region", "form_type", "deadline", "deadline_time", "prize", "login_required",
              "source_url", "conditions", "result_date", "winners", "prize_min_value", "all_rewarded", "effort_min",
              "eligibility", "notes", "tz", "title_en")
ELIGIBILITY = ("open", "residency_required", "unknown")


class CmdError(Exception):
    pass


def now():
    return store.now_iso()


def _need_item(db, iid):
    it = store.get_item(db, iid)
    if not it:
        raise CmdError(f"no item {iid}")
    return it


def _check(action, it, agent):
    err = model.check_transition(action, it, agent)
    if err:
        raise CmdError(err)


def _agent(a, required=False):
    ag = getattr(a, "agent", None)
    if ag == "owner":
        raise CmdError("CLI cannot act as owner; use the app")
    if required and not ag:
        raise CmdError(f"{a.cmd} needs --agent ({'|'.join(config.AGENTS)})")
    return ag or "cli"


_int, _bool = model.to_int, model.to_bool


def _append(old, new):
    return ((old or "") + " | " + new).strip(" |") if new else (old or "")


def _load_json_arg(a):
    if getattr(a, "json", None):
        data = json.loads(a.json)
    elif getattr(a, "file", None):
        with open(a.file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        raise CmdError("give --json or --file")
    return [data] if isinstance(data, dict) else data


# ---------- expiry ----------
def expire_due(db, agent="system") -> int:
    """Exclude items whose deadline already passed (item tz; deadline_time or end of day). Submitting is left alone."""
    n = 0
    marks = ",".join("?" * len(model.EXPIRABLE))
    for it in store.items(db, f"state IN ({marks}) AND deadline != ''", model.EXPIRABLE):
        if model.is_expired(it):
            store.update_item(db, it["id"], state="excluded", reason="expired", claimed_by=None, claimed_at=None)
            store.add_history(db, it["id"], agent, "expired",
                              f"deadline {it['deadline']} {it.get('deadline_time') or 'end of day'} {model.item_tz(it)}")
            n += 1
    return n


# ---------- duplicate detection ----------
class DupIndex:
    def __init__(self, db):
        self.rows = [dict(r) for r in db.execute("SELECT id, url, alt_urls, org, title, state FROM items")]
        self.by_url = {}
        self.by_title = {}
        for r in self.rows:
            self._index(r)

    def _index(self, r):
        urls = [r.get("url")]
        try:
            urls += json.loads(r.get("alt_urls") or "[]")
        except ValueError:
            pass
        for u in urls:
            nu = model.norm_url(u)
            if nu:
                self.by_url.setdefault(nu, r)
        nt = model.norm_title(r.get("org"), r.get("title"))
        if len(nt) >= 4:
            self.by_title.setdefault(nt, r)

    def find(self, url=None, org=None, title=None):
        nu = model.norm_url(url) if url else ""
        if nu and nu in self.by_url:
            return self.by_url[nu]
        nt = model.norm_title(org, title) if (org or title) else ""
        if len(nt) >= 4 and nt in self.by_title:
            return self.by_title[nt]
        return None

    def similar(self, org, title):
        for r in self.rows:
            if model.similar(org, title, r.get("org"), r.get("title")):
                return r
        return None

    def add(self, it):
        r = {k: it.get(k) for k in ("id", "url", "org", "title", "state")}
        r["alt_urls"] = json.dumps(it.get("alt_urls") or [])
        self.rows.append(r)
        self._index(r)


def build_item(d: dict, agent: str, state: str = "new") -> dict:
    """Validate and normalize one input row into an item dict (no id). Raises CmdError for invalid rows."""
    unknown = [k for k in d if k not in ADD_FIELDS]
    for k in ("title", "org", "url"):
        if not str(d.get(k) or "").strip():
            raise CmdError(f"missing {k}")
    region = str(d.get("region") or config.DEFAULT_REGION).upper()
    if region not in config.REGIONS:
        raise CmdError(f"bad region {region}")
    raw_dl = d.get("deadline")
    dl, tm = model.parse_deadline(raw_dl, region)
    if d.get("deadline_time"):
        tm = model.parse_deadline("2000-01-01 " + str(d["deadline_time"]), region)[1] or tm
    elig = d.get("eligibility")
    if elig not in ELIGIBILITY:
        elig = model.eligibility_from_text(" ".join(str(d.get(k) or "") for k in ("title", "conditions", "notes", "prize")),
                                           region)
    it = {
        "region": region, "title": str(d["title"]).strip(), "title_en": d.get("title_en"), "org": str(d["org"]).strip(),
        "url": str(d["url"]).strip(), "source_url": d.get("source_url"), "alt_urls": [],
        "form_type": d.get("form_type") or "other", "site_kind": model.site_kind(d["url"], d.get("form_type")),
        "site_host": model.url_host(d["url"]), "deadline": dl, "deadline_time": tm,
        "deadline_raw": (str(raw_dl) if raw_dl and not dl else None), "tz": d.get("tz") or None,
        "prize": d.get("prize"), "prize_min_value": _int(d.get("prize_min_value")), "winners": _int(d.get("winners")),
        "all_rewarded": _bool(d.get("all_rewarded")), "effort_min": _int(d.get("effort_min")),
        "login_required": _bool(d.get("login_required")), "conditions": d.get("conditions"),
        "result_date": d.get("result_date"), "eligibility": elig, "state": state, "notes": d.get("notes"),
        "added_by": agent, "added_at": now(), "updated_at": now(), "tries": {}, "flags": {},
    }
    if it["tz"]:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(it["tz"])
        except Exception:
            raise CmdError(f"bad tz {it['tz']}")
    if dl and model.is_expired(it):
        raise CmdError(f"deadline already passed ({dl})")
    if unknown:
        it["_ignored"] = unknown
    return it


def _insert_new(db, idx: DupIndex, d: dict, agent: str) -> dict:
    try:
        it = build_item(d, agent)
    except CmdError as e:
        return {"status": "invalid", "error": str(e), "title": d.get("title"), "url": d.get("url")}
    dup = idx.find(it["url"], it["org"], it["title"])
    if dup:
        return {"status": "duplicate", "duplicate_of": dup["id"], "state": dup["state"], "url": it["url"]}
    near = idx.similar(it["org"], it["title"])
    if near:
        it["flags"]["possible_dup_of"] = near["id"]
    ignored = it.pop("_ignored", None)
    it["id"] = store.next_item_id(db)
    store.insert_item(db, it)
    idx.add(it)
    store.add_history(db, it["id"], agent, "add", f"possible_dup_of={near['id']}" if near else "")
    res = {"id": it["id"], "status": "added", "region": it["region"], "site_kind": it["site_kind"],
           "deadline": it["deadline"], "eligibility": it["eligibility"]}
    if near:
        res["possible_dup_of"] = near["id"]
    if it.get("deadline_raw"):
        res["deadline_raw"] = it["deadline_raw"]
    if ignored:
        res["ignored_fields"] = ignored
    return res


# ---------- commands ----------
def cmd_status(db, a):
    exp = expire_due(db)
    by_state = {s: 0 for s in model.STATES}
    by_region, by_agent = {}, {}
    for r in db.execute("SELECT state, region, COUNT(*) n FROM items GROUP BY state, region"):
        by_state[r["state"]] = by_state.get(r["state"], 0) + r["n"]
        reg = by_region.setdefault(r["region"], {})
        reg[r["state"]] = reg.get(r["state"], 0) + r["n"]
    for r in db.execute("SELECT submitted_by, COUNT(*) n FROM items WHERE state='submitted' GROUP BY submitted_by"):
        by_agent[r["submitted_by"] or "?"] = r["n"]
    manual_by_need = {r[0] or "?": r[1] for r in
                      db.execute("SELECT need, COUNT(*) FROM items WHERE state='manual' GROUP BY need")}
    today = model.today_kst().isoformat()
    today_n = db.execute("SELECT COUNT(*) FROM items WHERE state='submitted' AND substr(submitted_at,1,10)=?",
                         (today,)).fetchone()[0]
    errs = [dict(r) for r in db.execute("SELECT id, at, agent, message FROM errors WHERE resolved=0 ORDER BY id")]
    pool_total = db.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
    pool_open = db.execute("SELECT COUNT(*) FROM pool WHERE promoted_to IS NULL OR promoted_to=''").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM runs WHERE ended IS NULL OR ended=''").fetchone()[0]
    return {"ok": True, "total": sum(by_state.values()), "states": by_state, "by_region": by_region,
            "manual_by_need": manual_by_need,
            "submitted": by_state["submitted"], "submitted_by": by_agent, "today": today, "today_submitted": today_n,
            "blocked": {k.removeprefix("blocked."): v for k, v in store.kv_prefix(db, "blocked.").items() if v},
            "stop": store.kv_get(db, "stop", False), "open_errors": errs, "pool": pool_total, "pool_open": pool_open,
            "active_runs": active, "wins": db.execute("SELECT COUNT(*) FROM wins").fetchone()[0], "expired_now": exp,
            "xlsx_pending": db.execute("SELECT COUNT(*) FROM owner_answers WHERE applied=0").fetchone()[0]}


def cmd_list(db, a):
    exp = expire_due(db)
    where, params = ["1"], []
    if a.state:
        where.append("state=?"); params.append(a.state)
    if a.region:
        where.append("region=?"); params.append(a.region.upper())
    if a.lane:
        where.append("lane=?"); params.append(a.lane)
    rows = store.items(db, " AND ".join(where), params)
    if a.flag:
        rows = [it for it in rows if (it.get("flags") or {}).get(a.flag)]
    rows.sort(key=model.order_key)
    if a.limit:
        rows = rows[: a.limit]
    if a.fields != "all":
        fields = [f.strip() for f in a.fields.split(",")] if a.fields else LIST_FIELDS
        rows = [{k: it.get(k) for k in fields if k in it} for it in rows]
    return {"ok": True, "count": len(rows), "items": rows, "expired_now": exp}


def cmd_get(db, a):
    it = _need_item(db, a.id)
    it["history"] = store.history(db, a.id)
    return {"ok": True, "item": it}


def cmd_check(db, a):
    idx = DupIndex(db)
    d = idx.find(a.url)
    nu = model.norm_url(a.url)
    pool = next((p for p in store.pool_rows(db) if p.get("url") and model.norm_url(p["url"]) == nu), None)
    return {"ok": True, "known": bool(d), "normalized": nu,
            "item": {"id": d["id"], "state": d["state"], "title": d["title"]} if d else None,
            "pool": pool["id"] if pool else None, "site_kind": model.site_kind(a.url)}


def cmd_add(db, a):
    agent = _agent(a)
    idx = DupIndex(db)
    res = [_insert_new(db, idx, d, agent) for d in _load_json_arg(a)]
    n_new = db.execute("SELECT COUNT(*) FROM items WHERE state='new'").fetchone()[0]
    return {"ok": True, "results": res, "added": sum(r["status"] == "added" for r in res), "new_waiting": n_new}


def cmd_next_batch(db, a):
    agent = _agent(a, required=True)
    exp = expire_due(db)
    key = f"verify.{agent}"
    cands = [it for it in store.items(db, "state='new'")
             if (it.get("tries") or {}).get(key, 0) < config.VERIFY_MAX_TRIES]
    cands.sort(key=model.order_key)
    picked = []
    for it in cands[: a.size]:
        tries = dict(it.get("tries") or {})
        tries["verify"] = tries.get("verify", 0) + 1
        tries[key] = tries.get(key, 0) + 1
        store.update_item(db, it["id"], state="verifying", claimed_by=agent, claimed_at=now(), tries=tries)
        store.add_history(db, it["id"], agent, "verify-start", "")
        picked.append(store.get_item(db, it["id"]))
    return {"ok": True, "items": picked, "expired_now": exp}


def cmd_verify(db, a):
    agent = _agent(a)
    it = _need_item(db, a.id)
    _check("verify", it, agent)
    upd = {}
    if a.url and model.norm_url(a.url) != model.norm_url(it.get("url")):
        dup = DupIndex(db).find(a.url)
        if dup and dup["id"] != it["id"]:
            reason = f"duplicate of {dup['id']}"
            store.update_item(db, it["id"], state="excluded", reason=reason, claimed_by=None, claimed_at=None)
            store.add_history(db, it["id"], agent, "exclude", reason)
            return {"ok": True, "id": it["id"], "state": "excluded", "duplicate_of": dup["id"]}
        alt = list(it.get("alt_urls") or [])
        if it.get("url"):
            alt.append(it["url"])
        upd.update(url=a.url, alt_urls=alt, site_kind=model.site_kind(a.url, it.get("form_type")),
                   site_host=model.url_host(a.url))
    if a.answers:
        try:
            upd["answers"] = json.loads(a.answers)
        except ValueError as e:
            raise CmdError(f"--answers is not JSON: {e}")
    if a.notes:
        upd["verify_notes"] = a.notes
    if a.result_date:
        upd["result_date"] = a.result_date
    if a.eligibility:
        upd["eligibility"] = a.eligibility
    if (a.eligibility or it.get("eligibility")) == "residency_required" and it.get("region") != "KR":
        # a JP/US residency restriction is treated as never met (the user is a Korean resident): exclude
        # instead of queueing a submit. KR items are kept: Korean residency ("국내 거주") is satisfied.
        upd.update(state="excluded", reason="residency required", lane=a.lane, claimed_by=None, claimed_at=None)
        store.update_item(db, it["id"], **upd)
        store.add_history(db, it["id"], agent, "exclude", "residency required (at verify)")
        return {"ok": True, "id": it["id"], "state": "excluded", "reason": "residency required"}
    upd.update(state="ready", lane=a.lane, claimed_by=None, claimed_at=None, need=None, question=None)
    store.update_item(db, it["id"], **upd)
    store.add_history(db, it["id"], agent, "verify", f"lane={a.lane}")
    return {"ok": True, "id": it["id"], "state": "ready", "lane": a.lane}


def _simple(db, a, action, state, **fields):
    agent = _agent(a)
    it = _need_item(db, a.id)
    _check(action, it, agent)
    store.update_item(db, it["id"], state=state, claimed_by=None, claimed_at=None, **fields)
    detail = "; ".join(f"{k}={v}" for k, v in fields.items() if v)
    store.add_history(db, it["id"], agent, action, detail)
    return {"ok": True, "id": it["id"], "state": state}


def cmd_exclude(db, a):
    return _simple(db, a, "exclude", "excluded", reason=a.reason)


def cmd_owner(db, a):
    return _simple(db, a, "owner", "owner", question=a.question)


def cmd_manual(db, a):
    return _simple(db, a, "manual", "manual", need=a.need, reason=a.reason)


def cmd_claim(db, a):
    agent = _agent(a, required=True)
    if agent not in config.CAPABILITIES:
        raise CmdError(f"{agent} cannot claim (claim agents: {', '.join(config.CAPABILITIES)})")
    caps = config.CAPABILITIES[agent]
    if a.lane and a.lane not in caps:
        raise CmdError(f"{agent} cannot claim lane {a.lane} (capabilities: {sorted(caps)})")
    exp = expire_due(db)
    if a.id:
        it = _need_item(db, a.id)
        _check("claim", it, agent)
        if not model.can_claim(agent, it.get("lane")):
            raise CmdError(f"{agent} cannot claim {it['id']}: lane {it.get('lane')} not in {sorted(caps)}")
        if a.lane and it.get("lane") != a.lane:
            raise CmdError(f"{it['id']} lane is {it.get('lane')}, not {a.lane}")
        cands = [it]
    else:
        lanes = [a.lane] if a.lane else sorted(caps)
        marks = ",".join("?" * len(lanes))
        cands = [it for it in store.items(db, f"state='ready' AND lane IN ({marks})", lanes)
                 if not (it.get("flags") or {}).get("no_resubmit")]
        cands.sort(key=model.order_key)
    if not cands:
        return {"ok": True, "item": None, "reason": "no ready item for " + agent + (f" lane {a.lane}" if a.lane else ""),
                "expired_now": exp}
    it = cands[0]
    store.update_item(db, it["id"], state="submitting", claimed_by=agent, claimed_at=now())
    store.add_history(db, it["id"], agent, "claim", f"lane={it.get('lane')}")
    return {"ok": True, "item": store.get_item(db, it["id"]), "expired_now": exp}


def cmd_done(db, a):
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("done", it, agent)
    if not (a.evidence or "").strip():
        raise CmdError("--evidence must not be empty")
    by = it.get("claimed_by") if agent == "system" and it.get("claimed_by") else agent
    store.update_item(db, it["id"], state="submitted", submitted_by=by, submitted_at=now(), evidence=a.evidence,
                      claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "done", a.evidence)
    n = db.execute("SELECT COUNT(*) FROM items WHERE state='submitted'").fetchone()[0]
    return {"ok": True, "id": it["id"], "state": "submitted", "submitted_total": n}


def cmd_fail(db, a):
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("fail", it, agent)
    tries = dict(it.get("tries") or {})
    tries["submit"] = tries.get("submit", 0) + 1
    flags = dict(it.get("flags") or {})
    if a.no_resubmit:
        flags["no_resubmit"] = 1
    if tries["submit"] >= config.SUBMIT_MAX_FAILS:
        reason = f"failed {tries['submit']} times: {a.reason}"
        store.update_item(db, it["id"], state="manual", need="other", reason=reason, tries=tries, flags=flags,
                          claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "fail", a.reason)
        store.add_history(db, it["id"], "system", "manual", f"other: {reason}")
        return {"ok": True, "id": it["id"], "state": "manual", "tries": tries["submit"]}
    store.update_item(db, it["id"], state="failed", reason=a.reason, tries=tries, flags=flags,
                      claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "fail", a.reason + (" [no_resubmit]" if a.no_resubmit else ""))
    return {"ok": True, "id": it["id"], "state": "failed", "tries": tries["submit"],
            "no_resubmit": bool(flags.get("no_resubmit"))}


def cmd_release(db, a):
    agent = _agent(a)
    it = _need_item(db, a.id)
    _check("release", it, agent)
    if not it.get("lane"):
        raise CmdError(f"{it['id']} has no lane; requeue it for verification instead")
    if (it.get("flags") or {}).get("no_resubmit") and not a.force:
        raise CmdError(f"{it['id']} has flags.no_resubmit (completion unknown); use --force after checking")
    store.update_item(db, it["id"], state="ready", claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "release", "forced" if a.force else "")
    return {"ok": True, "id": it["id"], "state": "ready", "lane": it["lane"]}


def requeue(db, it, agent, note=None):
    _check("requeue", it, agent)
    upd = dict(state="new", lane=None, need=None, reason=None, question=None, claimed_by=None, claimed_at=None)
    if note:
        upd["verify_notes"] = _append(it.get("verify_notes"), note)
    if agent in model.PRIVILEGED:
        # an owner/system requeue (e.g. after the user's answer) gives every agent fresh verify tries
        upd["tries"] = {k: v for k, v in (it.get("tries") or {}).items() if not k.startswith("verify")}
    store.update_item(db, it["id"], **upd)
    store.add_history(db, it["id"], agent, "requeue", note or "")


def cmd_requeue(db, a):
    it = _need_item(db, a.id)
    requeue(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "new"}


def cmd_set(db, a):
    agent = _agent(a)
    it = _need_item(db, a.id)
    upd, changed = {}, []
    if a.result_date is not None:
        upd["result_date"] = a.result_date
    if a.note is not None:
        upd["notes"] = _append(it.get("notes"), a.note)
    if a.verify_notes is not None:
        upd["verify_notes"] = _append(it.get("verify_notes"), a.verify_notes)
    if a.title_en is not None:
        upd["title_en"] = a.title_en
    if a.source_url is not None:
        upd["source_url"] = a.source_url
    if a.eligibility is not None:
        upd["eligibility"] = a.eligibility
    if a.region is not None:
        r = a.region.upper()
        if r not in config.REGIONS:
            raise CmdError(f"bad region {r}")
        upd["region"] = r
    if a.tz is not None:
        model.tzinfo(a.tz)
        upd["tz"] = a.tz or None
    if a.deadline is not None:
        dl, tm = model.parse_deadline(a.deadline, upd.get("region") or it.get("region"))
        if a.deadline and not dl:
            raise CmdError(f"cannot parse deadline {a.deadline!r}")
        upd.update(deadline=dl, deadline_time=tm, deadline_raw=None)
    if a.deadline_time is not None:
        upd["deadline_time"] = model.parse_deadline("2000-01-01 " + a.deadline_time)[1] if a.deadline_time else ""
    if a.url is not None:  # the entry URL is a dedupe key: only filled when empty (old rule)
        if it.get("url"):
            raise CmdError(f"{it['id']} already has url {it['url']}")
        dup = DupIndex(db).find(a.url)
        if dup and dup["id"] != it["id"]:
            raise CmdError(f"url belongs to {dup['id']}")
        upd.update(url=a.url, site_kind=model.site_kind(a.url, it.get("form_type")), site_host=model.url_host(a.url))
    if a.flag:
        flags = dict(it.get("flags") or {})
        for kv in a.flag:
            k, sep, v = kv.partition("=")
            if not sep or not k:
                raise CmdError(f"--flag needs k=v, got {kv!r}")
            if v == "":
                flags.pop(k, None)
            else:
                try:
                    flags[k] = json.loads(v)
                except ValueError:
                    flags[k] = v
        upd["flags"] = flags
    if not upd:
        raise CmdError("nothing to set")
    store.update_item(db, it["id"], **upd)
    for k, v in upd.items():
        changed.append(f"{k}={json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}")
    store.add_history(db, it["id"], agent, "set", "; ".join(changed))
    return {"ok": True, "id": it["id"], "changed": sorted(upd)}


def owner_done(db, it, agent, evidence):
    if agent != "owner":
        raise CmdError("only the user (in the app) can mark owner-done")
    _check("owner-done", it, agent)
    store.update_item(db, it["id"], state="submitted", submitted_by="owner", submitted_at=now(), evidence=evidence,
                      claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "owner-done", evidence)


def cmd_owner_done(db, a):
    it = _need_item(db, a.id)
    owner_done(db, it, _agent(a), a.evidence)
    return {"ok": True, "id": it["id"], "state": "submitted", "submitted_by": "owner"}


def owner_act(action: str, item_id: str, note: str = "", expected_state: str | None = None) -> dict:
    """In-process user action for the app server only: done | retry | skip."""
    try:
        with store.writer() as db:
            it = _need_item(db, item_id)
            if expected_state is not None and it["state"] != expected_state:
                return {"ok": False, "error": f"항목 상태가 이미 바뀌었습니다 ({it['state']})."}
            if action == "done":
                owner_done(db, it, "owner", note or "사용자가 앱에서 완료 표시")
                return {"ok": True, "id": it["id"], "state": "submitted", "submitted_by": "owner"}
            if action == "retry":
                requeue(db, it, "owner", f"사용자 답: {note}" if note else None)
                return {"ok": True, "id": it["id"], "state": "new"}
            if action == "skip":
                _check("exclude", it, "owner")
                reason = note or "사용자가 앱에서 제외"
                store.update_item(db, it["id"], state="excluded", reason=reason, claimed_by=None, claimed_at=None)
                store.add_history(db, it["id"], "owner", "exclude", f"reason={reason}")
                return {"ok": True, "id": it["id"], "state": "excluded"}
    except (CmdError, store.StoreError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown action {action}"}


def cmd_error(db, a):
    agent = _agent(a, required=True)
    eid = store.add_error(db, agent, a.message)
    store.add_history(db, None, agent, "error", a.message)
    return {"ok": True, "error_id": eid}


def cmd_resolve(db, a):
    if a.id:
        cur = db.execute("UPDATE errors SET resolved=1 WHERE id=? AND resolved=0", (a.id,))
    else:
        agent = _agent(a, required=True)
        cur = db.execute("UPDATE errors SET resolved=1 WHERE agent=? AND resolved=0", (agent,))
    return {"ok": True, "resolved": cur.rowcount}


# ---------- pool ----------
def cmd_pool_add(db, a):
    agent = _agent(a)
    idx = DupIndex(db)
    pool = store.pool_rows(db)
    res = []
    for d in _load_json_arg(a):
        if not d.get("title") or not d.get("org"):
            res.append({"status": "invalid", "error": "missing title or org", "title": d.get("title")})
            continue
        region = str(d.get("region") or config.DEFAULT_REGION).upper()
        if region not in config.REGIONS:
            res.append({"status": "invalid", "error": f"bad region {region}", "title": d.get("title")})
            continue
        dup = idx.find(d.get("url"), d.get("org"), d.get("title"))
        if dup:
            res.append({"status": "duplicate", "duplicate_of": dup["id"], "title": d.get("title")})
            continue
        nu = model.norm_url(d.get("url"))
        nt = model.norm_title(d.get("org"), d.get("title"))
        pdup = next((p for p in pool if (nu and p.get("url") and model.norm_url(p["url"]) == nu)
                     or (len(nt) >= 4 and nt == model.norm_title(p.get("org"), p.get("title")))), None)
        if pdup:
            res.append({"status": "duplicate", "duplicate_of": pdup["id"], "title": d.get("title")})
            continue
        p = {"id": store.next_pool_id(db), "region": region, "title": d["title"], "org": d["org"],
             "url": d.get("url"), "source_url": d.get("source_url"), "form_type": d.get("form_type") or "other",
             "deadline": d.get("deadline"), "prize": d.get("prize"), "login_required": _bool(d.get("login_required")),
             "conditions": d.get("conditions") or d.get("notes"), "result_date": d.get("result_date"),
             "winners": _int(d.get("winners")), "prize_min_value": _int(d.get("prize_min_value")),
             "all_rewarded": _bool(d.get("all_rewarded")), "effort_min": _int(d.get("effort_min")),
             "added_at": now(), "added_by": agent}
        store.insert_pool(db, p)
        pool.append(p)
        res.append({"status": "added", "id": p["id"]})
    return {"ok": True, "results": res, "pool": len(pool)}


def cmd_pool_list(db, a):
    where, params = "1", []
    if a.region:
        where, params = "region=?", [a.region.upper()]
    rows = store.pool_rows(db, where, params)
    if not a.all:
        rows = [p for p in rows if not p.get("promoted_to")]
    return {"ok": True, "count": len(rows), "pool": rows}


def cmd_pool_promote(db, a):
    agent = _agent(a)
    idx = DupIndex(db)
    res = []
    for pid in a.ids:
        rows = store.pool_rows(db, "id=?", [pid])
        if not rows:
            res.append({"pool_id": pid, "status": "invalid", "error": "no such pool row"})
            continue
        p = rows[0]
        if p.get("promoted_to"):
            res.append({"pool_id": pid, "status": "already", "id": p["promoted_to"]})
            continue
        d = {k: p.get(k) for k in ("title", "org", "url", "region", "form_type", "deadline", "prize", "login_required",
                                   "source_url", "conditions", "result_date", "winners", "prize_min_value",
                                   "all_rewarded", "effort_min")}
        r = _insert_new(db, idx, d, agent)
        r["pool_id"] = pid
        if r["status"] == "added":
            db.execute("UPDATE pool SET promoted_to=? WHERE id=?", (r["id"], pid))
        elif r["status"] == "duplicate":
            db.execute("UPDATE pool SET promoted_to=? WHERE id=?", (r["duplicate_of"], pid))
        res.append(r)
    return {"ok": True, "results": res}


# ---------- owner xlsx ----------
def _xlsx_locked(path) -> bool:
    d, name = os.path.split(path)
    if os.path.exists(os.path.join(d, "~$" + name)):
        return True
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True


def read_owner_xlsx(path) -> list[tuple[str, str, str]]:
    """Rows (item_id, kind, text) from the owner's columns. kind: done | skip | answer."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        for sheet, col in ((config.XLSX_SHEET_MANUAL, config.XLSX_COL_DONE),
                           (config.XLSX_SHEET_OWNER, config.XLSX_COL_ANSWER)):
            if sheet not in wb.sheetnames:
                continue
            rows = wb[sheet].iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in (next(rows, None) or ())]
            if col not in header:
                continue
            ci = header.index(col)
            ii = header.index(config.XLSX_COL_ID) if config.XLSX_COL_ID in header else 0
            for row in rows:
                if not row or ii >= len(row) or not row[ii]:
                    continue
                v = str(row[ci]).strip() if ci < len(row) and row[ci] is not None else ""
                if not v:
                    continue
                iid = str(row[ii]).strip()
                if col == config.XLSX_COL_ANSWER:
                    out.append((iid, "answer", v))
                elif v in config.XLSX_DONE_VALUES:
                    out.append((iid, "done", v))
                elif v in config.XLSX_SKIP_VALUES:
                    out.append((iid, "skip", v))
    finally:
        wb.close()
    return out


def _xlsx_action_error(it: dict | None, kind: str) -> str | None:
    if not it:
        return "no such item"
    if kind == "done":
        return None if it["state"] == "submitted" else model.check_transition("owner-done", it, "owner")
    if kind == "skip":
        return None if it["state"] == "excluded" else model.check_transition("exclude", it, "owner")
    if kind == "answer":
        return model.check_transition("requeue", it, "owner")
    return f"unknown kind {kind}"


def _ensure_owner_answers_cols(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(owner_answers)")}
    if "version" not in cols:
        db.execute("ALTER TABLE owner_answers ADD COLUMN version INTEGER DEFAULT 1")
    if "content_hash" not in cols:
        db.execute("ALTER TABLE owner_answers ADD COLUMN content_hash TEXT DEFAULT ''")


def _xlsx_content_hash(item_id: str, kind: str, text: str, it: dict | None = None) -> str:
    payload = {
        "id": str(item_id),
        "kind": str(kind),
        "text": str(text),
        "state": str((it or {}).get("state") or ""),
        "question": str((it or {}).get("question") or ""),
        "need": str((it or {}).get("need") or ""),
        "reason": str((it or {}).get("reason") or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _next_xlsx_version(db, item_id: str, prev_ver: int = 0) -> int:
    key = f"xlsx_ver.{item_id}"
    cur = store.kv_get(db, key, 0)
    base_ver = max(int(cur) if isinstance(cur, int) else 0, int(prev_ver or 0))
    nxt = base_ver + 1
    store.kv_set(db, key, nxt)
    return nxt


def _pending_xlsx_rows(db) -> list[dict]:
    cols = {r[1] for r in db.execute("PRAGMA table_info(owner_answers)")}
    ver_col = "oa.version" if "version" in cols else "1 AS version"
    hash_col = "oa.content_hash" if "content_hash" in cols else "'' AS content_hash"
    rows = db.execute(
        f"SELECT oa.id AS pending_id, oa.item_id, oa.kind, oa.text, oa.at, {ver_col}, {hash_col} "
        "FROM owner_answers oa WHERE oa.applied=0 ORDER BY oa.id"
    ).fetchall()
    out = []
    for r in rows:
        it = store.get_item(db, r["item_id"])
        if not it:
            continue
        if (r["kind"] == "done" and it["state"] == "submitted") or (r["kind"] == "skip" and it["state"] == "excluded"):
            continue
        if _xlsx_action_error(it, r["kind"]) is not None:
            continue
        ver = int(r["version"] or 1)
        chash = _xlsx_content_hash(r["item_id"], r["kind"], r["text"], it)
        out.append({
            "pending_id": r["pending_id"],
            "id": r["item_id"],
            "item_id": r["item_id"],
            "version": ver,
            "hash": chash,
            "content_hash": chash,
            "kind": r["kind"],
            "text": r["text"],
            "at": r["at"],
            "title": it.get("title") or r["item_id"],
            "org": it.get("org") or "",
            "url": it.get("url") or "",
            "deadline": it.get("deadline") or "",
            "state": it.get("state") or "",
            "question": it.get("question") or "",
            "reason": it.get("reason") or "",
            "need": it.get("need") or "",
        })
    return out


def cmd_sync_xlsx(db, a):
    """Record owner edits from the xlsx as pending (applied=0); never apply owner actions directly."""
    path = config.owner_xlsx_path()
    if not os.path.exists(path):
        return {"ok": True, "changed": 0, "missing": True}
    if _xlsx_locked(path):
        return {"ok": True, "changed": 0, "locked": True}
    try:
        rows = read_owner_xlsx(path)
    except PermissionError:
        return {"ok": True, "changed": 0, "locked": True}
    _ensure_owner_answers_cols(db)
    dedup: dict[str, tuple[str, str]] = {}
    for iid, kind, text in rows:
        dedup[iid] = (kind, text)
    active_ids = set(dedup)
    for (old_iid,) in db.execute("SELECT DISTINCT item_id FROM owner_answers WHERE applied=0").fetchall():
        if old_iid not in active_ids:
            db.execute("DELETE FROM owner_answers WHERE item_id=? AND applied=0", (old_iid,))
    queued, skipped = [], []
    for iid, (kind, text) in dedup.items():
        prev_applied = db.execute(
            "SELECT 1 FROM owner_answers WHERE item_id=? AND kind=? AND text=? AND applied=1",
            (iid, kind, text),
        ).fetchone()
        if prev_applied:
            continue
        it = store.get_item(db, iid)
        if it and ((kind == "done" and it["state"] == "submitted") or (kind == "skip" and it["state"] == "excluded")):
            db.execute("DELETE FROM owner_answers WHERE item_id=? AND applied=0", (iid,))
            continue
        err = _xlsx_action_error(it, kind)
        if err is not None:
            db.execute("DELETE FROM owner_answers WHERE item_id=? AND applied=0", (iid,))
            skipped.append({"id": iid, "kind": kind, "error": err})
            continue
        chash = _xlsx_content_hash(iid, kind, text, it)
        prev = db.execute(
            "SELECT id, kind, text, version, content_hash FROM owner_answers WHERE item_id=? AND applied=0",
            (iid,),
        ).fetchone()
        if prev:
            if (prev["kind"], prev["text"]) != (kind, text):
                ver = _next_xlsx_version(db, iid, prev["version"] or 1)
                db.execute(
                    "UPDATE owner_answers SET kind=?, text=?, version=?, content_hash=?, at=? WHERE id=?",
                    (kind, text, ver, chash, now(), prev["id"]),
                )
                queued.append({"id": iid, "kind": kind, "text": text, "version": ver, "hash": chash})
            elif (prev["content_hash"] or "") != chash or not prev["version"]:
                ver = (_next_xlsx_version(db, iid, prev["version"] or 1)
                       if (prev["content_hash"] or "") else int(prev["version"] or _next_xlsx_version(db, iid, 0)))
                db.execute("UPDATE owner_answers SET version=?, content_hash=? WHERE id=?", (ver, chash, prev["id"]))
        else:
            ver = _next_xlsx_version(db, iid, 0)
            db.execute(
                "INSERT INTO owner_answers(item_id, kind, text, at, applied, version, content_hash) VALUES(?,?,?,?,0,?,?)",
                (iid, kind, text, now(), ver, chash),
            )
            queued.append({"id": iid, "kind": kind, "text": text, "version": ver, "hash": chash})
    pending = _pending_xlsx_rows(db)
    return {"ok": True, "changed": 0, "queued": len(queued), "pending": len(pending),
            "items": pending, "skipped": skipped}


def pending_xlsx() -> list[dict]:
    """In-process read of pending xlsx changes for the app server."""
    db = store.connect()
    try:
        return _pending_xlsx_rows(db)
    finally:
        db.close()


def _parse_confirm_targets(items: list[dict] | None, item_id: str, version: int | None, content_hash: str) -> tuple[list[dict] | None, str]:
    if items is not None:
        if not isinstance(items, list) or not items:
            return None, "확인할 항목을 지정하세요."
        targets = []
        for entry in items:
            if not isinstance(entry, dict):
                return None, "확인할 항목 형식이 올바르지 않습니다."
            iid = str(entry.get("id") or entry.get("item_id") or "").strip()
            ver = entry.get("version")
            chash = str(entry.get("hash") or entry.get("content_hash") or "").strip()
            if not iid or not isinstance(ver, int) or isinstance(ver, bool) or ver < 1 or not chash:
                return None, "확인할 항목의 버전과 해시가 필요합니다."
            targets.append({"id": iid, "version": ver, "hash": chash})
        return targets, ""
    iid = str(item_id or "").strip()
    if not iid:
        return None, "확인할 항목을 지정하세요."
    chash = str(content_hash or "").strip()
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or not chash:
        return None, "확인할 항목의 버전과 해시가 필요합니다."
    return [{"id": iid, "version": version, "hash": chash}], ""


def confirm_xlsx(
    items: list[dict] | None = None,
    *,
    item_id: str = "",
    version: int | None = None,
    content_hash: str = "",
    pending_id: int | None = None,
    all_items: bool = False,
) -> dict:
    """In-process user confirmation of displayed pending xlsx changes (verified by id + version + hash)."""
    if all_items:
        return {"ok": False, "error": "화면에 표시된 항목 목록(id·version·hash)만 확인할 수 있습니다.", "refresh": True}
    targets, err = _parse_confirm_targets(items, item_id, version, content_hash)
    if targets is None:
        return {"ok": False, "error": err}
    try:
        with store.writer() as db:
            _ensure_owner_answers_cols(db)
            applied, refused, skipped = [], [], []
            for t in targets:
                want_id, want_ver, want_hash = t["id"], t["version"], t["hash"]
                if pending_id is not None and len(targets) == 1:
                    r = db.execute(
                        "SELECT id, item_id, kind, text, version, content_hash FROM owner_answers "
                        "WHERE id=? AND item_id=? AND applied=0",
                        (int(pending_id), want_id),
                    ).fetchone()
                else:
                    r = db.execute(
                        "SELECT id, item_id, kind, text, version, content_hash FROM owner_answers "
                        "WHERE item_id=? AND applied=0 ORDER BY id DESC LIMIT 1",
                        (want_id,),
                    ).fetchone()
                if not r:
                    refused.append({"id": want_id, "error": "missing or already handled", "stale": True})
                    continue
                rid, iid, kind, text = r["id"], r["item_id"], r["kind"], r["text"]
                it = store.get_item(db, iid)
                if not it:
                    db.execute("DELETE FROM owner_answers WHERE id=?", (rid,))
                    skipped.append({"id": iid, "kind": kind, "error": "no such item"})
                    continue
                cur_ver = int(r["version"] or 1)
                live_hash = _xlsx_content_hash(iid, kind, text, it)
                stored_hash = str(r["content_hash"] or live_hash)
                if (cur_ver != want_ver
                        or not secrets.compare_digest(live_hash, want_hash)
                        or not secrets.compare_digest(stored_hash, want_hash)):
                    if stored_hash != live_hash:
                        new_ver = _next_xlsx_version(db, iid, cur_ver)
                        db.execute("UPDATE owner_answers SET version=?, content_hash=? WHERE id=?",
                                   (new_ver, live_hash, rid))
                    refused.append({"id": iid, "error": "changed after display", "stale": True})
                    continue
                try:
                    if kind == "done":
                        if it["state"] != "submitted":
                            owner_done(db, it, "owner", "owner marked done in xlsx")
                    elif kind == "skip":
                        if it["state"] != "excluded":
                            _check("exclude", it, "owner")
                            store.update_item(db, iid, state="excluded", reason="owner declined in xlsx",
                                              claimed_by=None, claimed_at=None)
                            store.add_history(db, iid, "owner", "exclude", "owner declined in xlsx")
                    elif kind == "answer":
                        requeue(db, it, "owner", f"owner answer: {text}")
                    else:
                        raise CmdError(f"unknown kind {kind}")
                    db.execute("UPDATE owner_answers SET applied=1, at=? WHERE id=?", (now(), rid))
                    applied.append({"id": iid, "kind": kind, "state": store.get_item(db, iid)["state"]})
                except CmdError as e:
                    db.execute("DELETE FROM owner_answers WHERE id=?", (rid,))
                    skipped.append({"id": iid, "kind": kind, "error": str(e)})
            pending_now = _pending_xlsx_rows(db)
            if refused:
                return {"ok": False, "stale": True, "refresh": True,
                        "error": "화면 표시 뒤 바뀐 항목이 있어 반영하지 않았습니다. 목록을 다시 확인하세요.",
                        "count": len(applied), "applied": applied, "refused": refused, "skipped": skipped,
                        "items": pending_now}
            if not applied and skipped:
                return {"ok": False, "refresh": True, "error": skipped[0]["error"],
                        "skipped": skipped, "items": pending_now}
            return {"ok": True, "count": len(applied), "applied": applied, "skipped": skipped, "items": pending_now}
    except (CmdError, store.StoreError, ValueError) as e:
        return {"ok": False, "error": str(e)}


# ---------- wins, rewards, sign-up sites ----------
def cmd_win(db, a):
    agent = _agent(a)
    iid = None if a.id == "-" else a.id
    if iid:
        _need_item(db, iid)
    won = a.date or model.today_kst().isoformat()
    d, _ = model.parse_deadline(won)
    if not d:
        raise CmdError(f"cannot parse --date {a.date!r}")
    cur = db.execute("INSERT INTO wins(item_id, won_at, prize, note, recorded_by, at, value_krw, value_basis) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (iid, d, a.prize, a.note, agent, now(), a.value, a.value_basis))
    if iid:
        store.add_history(db, iid, agent, "win", f"{d} {a.prize}")
    return {"ok": True, "win_id": cur.lastrowid, "item_id": iid, "won_at": d}


def cmd_win_value(db, a):
    cur = db.execute("UPDATE wins SET value_krw=?, value_basis=? WHERE id=?", (a.value, a.value_basis, a.win_id))
    if not cur.rowcount:
        raise CmdError(f"no win with id {a.win_id}")
    return {"ok": True, "win_id": a.win_id, "value_krw": a.value}


def cmd_wins(db, a):
    rows = [dict(r) for r in db.execute(
        "SELECT w.*, i.title, i.org FROM wins w LEFT JOIN items i ON i.id = w.item_id ORDER BY w.won_at, w.id")]
    return {"ok": True, "count": len(rows), "wins": rows}


def cmd_rewards(db, a):
    rows = store.items(db, "state=?", [a.state])
    with_val = [it for it in rows if it.get("prize_min_value") is not None]
    rows.sort(key=lambda it: (0 if it.get("result_date") else 1, str(it.get("result_date") or ""), it["id"]))
    return {"ok": True, "state": a.state, "count": len(rows), "with_min_value": len(with_val),
            "sum_min_value_all_rewarded": sum(it["prize_min_value"] for it in with_val if it.get("all_rewarded") == 1),
            "sum_min_value": sum(it["prize_min_value"] for it in with_val),
            "missing_min_value_ids": [it["id"] for it in rows if it.get("prize_min_value") is None][:200],
            "items": [{k: it.get(k) for k in ("id", "deadline", "result_date", "prize", "prize_min_value", "winners",
                                              "all_rewarded")} for it in rows]}


def cmd_signups(db, a):
    """Manual items needing login/identity whose deadline is still ahead (or unknown), grouped by site host."""
    today = model.today_kst().isoformat()
    groups = {}
    for it in store.items(db, "state='manual' AND need IN ('login','identity')"):
        if it.get("deadline") and it["deadline"] < today:
            continue
        if not it.get("deadline"):
            continue  # "deadline >= today" excludes items without a known deadline
        h = model.signup_host(it.get("url")) or "?"
        g = groups.setdefault(h, {"host": h, "count": 0, "earliest_deadline": None, "example_title": None,
                                  "example_prize": None, "needs": set(), "ids": [], "reasons": []})
        g["count"] += 1
        g["ids"].append(it["id"])
        g["needs"].add(it["need"])
        if it.get("reason") and it["reason"] not in g["reasons"]:
            g["reasons"].append(it["reason"])
        if g["earliest_deadline"] is None or it["deadline"] < g["earliest_deadline"]:
            g["earliest_deadline"] = it["deadline"]
            g["example_title"], g["example_prize"] = it.get("title"), it.get("prize")
    out = sorted(groups.values(), key=lambda g: (-g["count"], g["earliest_deadline"], g["host"]))
    for g in out:
        g["needs"] = sorted(g["needs"])
    return {"ok": True, "sites": len(out), "items": sum(g["count"] for g in out), "groups": out}


def cmd_migrate(db, a):
    from ep import migrate
    return migrate.run(db, dry_run=a.dry_run)


# ---------- parser ----------
def build_parser(internal: bool = False) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a subparser default never overwrites an --agent given before the command name.
    # The CLI never accepts "owner"; "system" is accepted only for in-process scheduler calls (internal=True).
    actors = config.AGENTS + ("system",) if internal else config.AGENTS
    common.add_argument("--agent", choices=actors, default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="ep", parents=[common])
    sp = p.add_subparsers(dest="cmd", required=True)

    def sub(name):
        return sp.add_parser(name, parents=[common])

    sub("status")
    x = sub("list"); x.add_argument("--state", choices=model.STATES); x.add_argument("--region")
    x.add_argument("--lane", choices=config.LANES); x.add_argument("--limit", type=int); x.add_argument("--fields")
    x.add_argument("--flag")
    x = sub("get"); x.add_argument("id")
    x = sub("check"); x.add_argument("url")
    for name in ("add", "pool-add"):
        x = sub(name); x.add_argument("--json"); x.add_argument("--file")
    x = sub("pool-list"); x.add_argument("--region"); x.add_argument("--all", action="store_true")
    x = sub("pool-promote"); x.add_argument("ids", nargs="+")
    x = sub("next-batch"); x.add_argument("--size", type=int, default=10)
    x = sub("verify"); x.add_argument("id"); x.add_argument("--lane", required=True, choices=config.LANES)
    x.add_argument("--notes"); x.add_argument("--answers"); x.add_argument("--url")
    x.add_argument("--eligibility", choices=ELIGIBILITY); x.add_argument("--result-date", dest="result_date")
    x = sub("exclude"); x.add_argument("id"); x.add_argument("--reason", required=True)
    x = sub("owner"); x.add_argument("id"); x.add_argument("--question", required=True)
    x = sub("manual"); x.add_argument("id"); x.add_argument("--need", required=True, choices=config.MANUAL_NEEDS)
    x.add_argument("--reason", required=True)
    x = sub("claim"); x.add_argument("--lane", choices=config.LANES); x.add_argument("--id")
    x = sub("done"); x.add_argument("id"); x.add_argument("--evidence", required=True)
    x = sub("fail"); x.add_argument("id"); x.add_argument("--reason", required=True)
    x.add_argument("--no-resubmit", dest="no_resubmit", action="store_true")
    x = sub("release"); x.add_argument("id"); x.add_argument("--force", action="store_true")
    x = sub("requeue"); x.add_argument("id"); x.add_argument("--note")
    x = sub("set"); x.add_argument("id")
    for opt in ("--result-date", "--note", "--verify-notes", "--region", "--deadline", "--deadline-time",
                "--title-en", "--source-url", "--url", "--tz"):
        x.add_argument(opt, dest=opt[2:].replace("-", "_"))
    x.add_argument("--eligibility", choices=ELIGIBILITY); x.add_argument("--flag", action="append")
    x = sub("owner-done"); x.add_argument("id"); x.add_argument("--evidence", required=True)
    x = sub("error"); x.add_argument("--message", required=True)
    x = sub("resolve"); x.add_argument("--id", type=int)
    sub("sync-xlsx")
    x = sub("win"); x.add_argument("id", help="item id, or - when unknown"); x.add_argument("--prize", required=True)
    x.add_argument("--date"); x.add_argument("--note")
    x.add_argument("--value", type=int, help="market value in KRW"); x.add_argument("--value-basis")
    x = sub("win-value", ); x.add_argument("win_id", type=int); x.add_argument("--value", type=int, required=True)
    x.add_argument("--value-basis", required=True)
    sub("wins")
    x = sub("rewards"); x.add_argument("--state", default="submitted", choices=model.STATES)
    sub("signups")
    x = sub("migrate"); x.add_argument("--dry-run", dest="dry_run", action="store_true")
    return p


COMMANDS = {
    "status": cmd_status, "list": cmd_list, "get": cmd_get, "check": cmd_check, "add": cmd_add,
    "next-batch": cmd_next_batch, "verify": cmd_verify, "exclude": cmd_exclude, "owner": cmd_owner,
    "manual": cmd_manual, "claim": cmd_claim, "done": cmd_done, "fail": cmd_fail, "release": cmd_release,
    "requeue": cmd_requeue, "set": cmd_set, "owner-done": cmd_owner_done, "error": cmd_error,
    "resolve": cmd_resolve, "pool-add": cmd_pool_add, "pool-list": cmd_pool_list,
    "pool-promote": cmd_pool_promote, "sync-xlsx": cmd_sync_xlsx, "migrate": cmd_migrate,
    "win": cmd_win, "win-value": cmd_win_value, "wins": cmd_wins, "rewards": cmd_rewards, "signups": cmd_signups,
}
READ_ONLY = {"get", "check", "pool-list", "wins", "rewards", "signups"}


def call(argv: list[str], *, internal: bool = False) -> dict:
    """Run one core command and return its result dict (errors are returned as {"ok": False, "error": ...})."""
    parser = build_parser(internal=internal)
    try:
        a = parser.parse_args(argv)
    except SystemExit as e:
        if e.code == 0:
            return {"ok": True, "help": True}
        return {"ok": False, "error": f"bad arguments: {' '.join(argv)}"}
    fn = COMMANDS[a.cmd]
    try:
        if a.cmd in READ_ONLY:
            db = store.connect()
            try:
                return fn(db, a)
            finally:
                db.close()
        if a.cmd == "migrate" and a.dry_run:
            from ep import migrate
            return migrate.run(None, dry_run=True)
        with store.writer() as db:
            return fn(db, a)
    except CmdError as e:
        return {"ok": False, "error": str(e)}
    except store.StoreError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # never swallow: report type and message as the command result
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def dispatch_external(argv: list[str]):
    """Commands owned by other modules. Returns an exit code, or None when argv is a core command."""
    if not argv:
        return None
    cmd = argv[0]
    if cmd in REST_MODULES:
        name, args = REST_MODULES[cmd], argv[1:]
    elif cmd in FULL_MODULES:
        name, args = FULL_MODULES[cmd], argv
    else:
        return None
    try:
        mod = importlib.import_module(f"ep.{name}")
        fn = getattr(mod, "main")
    except (ImportError, AttributeError):
        print(json.dumps({"ok": False, "error": f"module not available: {name}"}, ensure_ascii=False))
        return 1
    rc = fn(args)
    return rc if isinstance(rc, int) else 0


def main(argv: list[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    rc = dispatch_external(argv)
    if rc is not None:
        sys.exit(rc)
    if argv and argv[0] in ("-h", "--help") or not argv:
        build_parser().print_help()
        sys.exit(0 if argv else 1)
    r = call(argv)
    print(json.dumps(r, ensure_ascii=False, default=str))
    sys.exit(0 if r.get("ok") else 1)
