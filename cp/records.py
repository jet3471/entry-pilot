"""Wins, rewards and sign-up site records (`cp win`, `cp wins`, `cp rewards`, `cp signups`)."""
from cp import model, store
from cp.cmdbase import CmdError, _agent, _need_item, now


def cmd_win(db, a):
    agent = _agent(a)
    iid = None if a.id == "-" else a.id
    if iid:
        _need_item(db, iid)
    won = a.date or model.today_kst().isoformat()
    d, _ = model.parse_deadline(won)
    if not d:
        raise CmdError(f"cannot parse --date {a.date!r}")
    cur = db.execute("INSERT INTO wins(item_id, won_at, prize, note, recorded_by, at) VALUES(?,?,?,?,?,?)",
                     (iid, d, a.prize, a.note, agent, now()))
    if iid:
        store.add_history(db, iid, agent, "win", f"{d} {a.prize}")
    return {"ok": True, "win_id": cur.lastrowid, "item_id": iid, "won_at": d}


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
            continue  # (assumption) "deadline >= today" excludes items without a known deadline
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
