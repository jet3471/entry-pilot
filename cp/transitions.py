"""Item state commands: status, list, get, next-batch, claim, done, fail, release, exclude, owner, manual, requeue,
set, owner-done, error, resolve."""
import json

from cp import config, model, policy, store, workflow
from cp.cmdbase import CmdError, LIST_FIELDS, WORK_FIELDS, _agent, _append, _check, _need_item, expire_due, now
from cp.intake import DupIndex, _work_from


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
            "active_runs": active, "wins": db.execute("SELECT COUNT(*) FROM wins").fetchone()[0], "expired_now": exp}


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
    out = {"ok": True, "item": it}
    frozen = workflow.get_frozen_submission(db, it)
    if frozen is not None:
        out["frozen_submission"] = frozen
    return out


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
        if workflow.needs_refreeze_review(db, it):
            workflow.send_back_for_reapproval(db, it, agent="system", note_text="다시 확인해 주세요")
            return {"ok": False, "id": it["id"], "state": "review", "item": None,
                    "reason": "다시 확인해 주세요", "error": "다시 확인해 주세요"}
        if policy.prize_changed_after_accept(it.get("work")):
            workflow.send_back_for_reapproval(db, it, agent="system", note_text="다시 확인해 주세요")
            return {"ok": False, "id": it["id"], "state": "review", "item": None,
                    "reason": "다시 확인해 주세요", "error": f"{it['id']} prize changed after acceptance; 다시 확인해 주세요"}
        workflow.check_approved_draft(it, db=db)
        cands = [it]
    else:
        lanes = [a.lane] if a.lane else sorted(caps)
        marks = ",".join("?" * len(lanes))
        raw_ready = [it for it in store.items(db, f"state='ready' AND lane IN ({marks})", lanes)
                     if not (it.get("flags") or {}).get("no_resubmit")]
        cands = []
        for it in raw_ready:
            if workflow.needs_refreeze_review(db, it) or policy.prize_changed_after_accept(it.get("work")):
                workflow.send_back_for_reapproval(db, it, agent="system", note_text="다시 확인해 주세요")
                continue
            if workflow.approved_draft_ok(it, db=db)[0]:
                cands.append(it)
        cands.sort(key=model.order_key)
    if not cands:
        return {"ok": True, "item": None, "reason": "no ready item for " + agent + (f" lane {a.lane}" if a.lane else ""),
                "expired_now": exp}
    it = cands[0]
    store.update_item(db, it["id"], state="submitting", claimed_by=agent, claimed_at=now())
    store.add_history(db, it["id"], agent, "claim", f"lane={it.get('lane')}")
    claimed = store.get_item(db, it["id"])
    res = {"ok": True, "item": claimed, "expired_now": exp}
    frozen = workflow.get_frozen_submission(db, claimed)
    if frozen is not None:
        res["frozen_submission"] = frozen
    return res


def cmd_done(db, a):
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("done", it, agent)
    workflow.check_approved_draft(it, db=db)
    if not (a.evidence or "").strip():
        raise CmdError("--evidence must not be empty")
    workflow.check_sent_on_done(db, it, a)
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
    if it["state"] == "drafting":  # a draft run that ended without a result: back to the draft queue
        _check("draft-release", it, agent)
        store.update_item(db, it["id"], state="draft", claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "draft-release", "")
        return {"ok": True, "id": it["id"], "state": "draft"}
    _check("release", it, agent)
    if not it.get("lane"):
        raise CmdError(f"{it['id']} has no lane; requeue it for verification instead")
    if (it.get("flags") or {}).get("no_resubmit") and not a.force:
        raise CmdError(f"{it['id']} has flags.no_resubmit (completion unknown); use --force after checking")
    work = dict(it.get("work") or {})
    if it["state"] == "owner" and work.get("draft_dir"):
        # The final sending happens only after the user has seen the finished work and said to send it
        # (page or Telegram button). A work that came back from `owner` may have changed, so it waits in review again.
        workflow.clear_approval(db, it["id"], work)
        work["notified_review"] = None
        store.update_item(db, it["id"], state="review", work=work, question=None, claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "release", "to review: the user approves the finished work before it is sent")
        return {"ok": True, "id": it["id"], "state": "review", "lane": it["lane"]}
    if (work.get("draft_dir") or work.get("draft_files")) and not work.get("approved_at"):
        work["notified_review"] = None
        store.update_item(db, it["id"], state="review", work=work, question=None, claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "release", "to review: draft has no active approval")
        return {"ok": True, "id": it["id"], "state": "review", "lane": it["lane"]}
    if work.get("accepted_at") and not work.get("approved_at"):
        # an accepted contest item without an owner-approved work went from owner straight to ready (the submit
        # queue) on release. Such an item has nothing approved to send; it goes back to the draft queue instead.
        store.update_item(db, it["id"], state="draft", question=None, claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "release", "to draft: no owner-approved work to send")
        return {"ok": True, "id": it["id"], "state": "draft", "lane": it["lane"]}
    store.update_item(db, it["id"], state="ready", claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "release", "forced" if a.force else "")
    return {"ok": True, "id": it["id"], "state": "ready", "lane": it["lane"]}


def requeue(db, it, agent, note=None):
    _check("requeue", it, agent)
    upd = dict(state="new", lane=None, need=None, reason=None, question=None, claimed_by=None, claimed_at=None)
    work = dict(it.get("work") or {})
    if (work.get("approved_hash") or work.get("approved_at") or work.get("approval")
            or work.get("frozen_submission") or store.kv_get(db, workflow.FROZEN_KV_PREFIX + it["id"])
            or work.get("accepted_at") or "accepted_top_prize_krw" in work
            or work.get("prize_changed_after_accept")):
        workflow.clear_approval(db, it["id"], work)
        for k in ("accepted_at", "accepted_by", "accepted_top_prize_krw", "accepted_top_prize",
                  "prize_changed_after_accept"):
            work.pop(k, None)
        upd["work"] = work
    if note:
        upd["verify_notes"] = _append(it.get("verify_notes"), note)
    if agent in model.PRIVILEGED:
        # (assumption) an owner/system requeue (e.g. after an owner answer) gives every agent fresh verify tries
        upd["tries"] = {k: v for k, v in (it.get("tries") or {}).items() if not k.startswith("verify")}
    store.update_item(db, it["id"], **upd)
    store.add_history(db, it["id"], agent, "requeue", note or "")


def cmd_requeue(db, a):
    it = _need_item(db, a.id)
    requeue(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "new"}


SUBMISSION_SET_KEYS = frozenset({"work", "url", "source_url", "verify_notes", "eligibility", "region", "flags"})


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
    wd = {k: getattr(a, k, None) for k in WORK_FIELDS}
    if any(v is not None for v in wd.values()):
        upd["work"] = _work_from(wd, it.get("work"))
    if a.flag:
        flags = dict(it.get("flags") or {})
        for kv in a.flag:
            k, sep, v = kv.partition("=")
            if not sep or not k:
                raise CmdError(f"--flag needs k=v, got {kv!r}")
            if k == "no_resubmit" and v == "" and agent not in model.PRIVILEGED:
                raise CmdError("cannot clear no_resubmit flag via set")
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
    if upd.keys() & SUBMISSION_SET_KEYS:
        if it["state"] in ("submitting", "submitted"):
            raise CmdError(f"cannot change submission fields while {it['id']} is {it['state']}")
        cur_work = dict(it.get("work") or {})
        w_upd = dict(upd.get("work") or cur_work)
        was_accepted = bool(cur_work.get("accepted_at") or "accepted_top_prize_krw" in cur_work)
        prize_touched = a.top_prize_krw is not None or a.top_prize is not None
        prize_changed = False
        if was_accepted and prize_touched:
            if (policy._valid_positive_prize(w_upd.get("top_prize_krw"))
                    != policy._valid_positive_prize(cur_work.get("accepted_top_prize_krw"))
                    or w_upd.get("top_prize_krw") != cur_work.get("top_prize_krw")
                    or (a.top_prize is not None and w_upd.get("top_prize") != cur_work.get("top_prize"))):
                prize_changed = True
                w_upd["prize_changed_after_accept"] = True
        has_approval = bool(
            cur_work.get("approved_hash") or cur_work.get("approved_at") or cur_work.get("approval")
            or cur_work.get("frozen_submission") or store.kv_get(db, workflow.FROZEN_KV_PREFIX + it["id"])
        )
        if prize_changed:
            workflow.clear_approval(db, it["id"], w_upd)
            w_upd["notified_review"] = None
            upd["work"] = w_upd
            if it["state"] in ("ready", "failed"):
                upd.update(state="review", claimed_by=None, claimed_at=None)
        elif has_approval:
            workflow.clear_approval(db, it["id"], w_upd)
            w_upd["notified_review"] = None
            upd["work"] = w_upd
            if it["state"] in ("ready", "failed"):
                upd.update(state="review", claimed_by=None, claimed_at=None)
        elif it["state"] == "ready":
            raise CmdError(f"cannot change submission fields while {it['id']} is ready; requeue it instead")
    store.update_item(db, it["id"], **upd)
    for k, v in upd.items():
        changed.append(f"{k}={json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}")
    store.add_history(db, it["id"], agent, "set", "; ".join(changed))
    return {"ok": True, "id": it["id"], "state": upd.get("state", it["state"]), "changed": sorted(upd)}


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


def owner_act(action: str, item_id: str, note: str = "") -> dict:
    """In-process user action for the app server only: done | retry | skip."""
    try:
        with store.writer() as db:
            it = _need_item(db, item_id)
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
