"""How items come in: duplicate detection, building a new item from a scout row, `cp add`, `cp check`, the pool."""
import json

from cp import config, model, store
from cp.cmdbase import ADD_FIELDS, CmdError, ELIGIBILITY, WORK_FIELDS, _agent, _load_json_arg, now


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


def _work_from(d: dict, base: dict | None = None) -> dict:
    """Contest fields of an input row -> item.work (unknown or empty values are left out; never guessed)."""
    w = dict(base or {})
    for k in WORK_FIELDS:
        v = d.get(k)
        if v in (None, ""):
            continue
        if k in ("top_prize_krw", "team_size"):
            v = model.to_int(v)
            if v is None:
                continue
        w[k] = v
    return w


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
        "prize": d.get("prize"), "prize_min_value": model.to_int(d.get("prize_min_value")), "winners": model.to_int(d.get("winners")),
        "all_rewarded": model.to_bool(d.get("all_rewarded")), "effort_min": model.to_int(d.get("effort_min")),
        "login_required": model.to_bool(d.get("login_required")), "conditions": d.get("conditions"),
        "result_date": d.get("result_date"), "eligibility": elig, "state": state, "notes": d.get("notes"),
        "added_by": agent, "added_at": now(), "updated_at": now(), "tries": {}, "flags": {},
        "work": _work_from(d),
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
             "deadline": d.get("deadline"), "prize": d.get("prize"), "login_required": model.to_bool(d.get("login_required")),
             "conditions": d.get("conditions") or d.get("notes"), "result_date": d.get("result_date"),
             "winners": model.to_int(d.get("winners")), "prize_min_value": model.to_int(d.get("prize_min_value")),
             "all_rewarded": model.to_bool(d.get("all_rewarded")), "effort_min": model.to_int(d.get("effort_min")),
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
