"""Notices about contest items that wait for the user (analyses and finished works).

  cp notify status            what waits and what would be sent now
  cp notify run [--dry-run]   queue what is due now (the scheduler runs this about once a minute)

When a notice is due (local time, policy notify_report_hours):
  * 5 (policy notify_batch_size) analyses wait that the user has not been told about            -> one notice, all of them
  * an analysis or finished work whose deadline is <= notify_deadline_days days away             -> at once, that item
  * a finished work of a big-prize contest (policy.is_big)                                      -> at once, one per work
  * 5 finished works of smaller contests wait                                                   -> one notice, all of them
  * 09:00 and 21:00 (notify_report_hours)                                                       -> report of everything waiting
The engine does not send anything itself: it writes each notice as a small JSON file into the "notices" folder of the data folder (the outbox).
The app sends it on the channel the user chose (Telegram, e-mail or none) and deletes the file once the send is
confirmed. An item counts as told about when its notice is in the outbox. The user answers in the app (contest tab).
Notices carry contest facts only: no personal data, no secrets.
"""
import argparse
import json
import os
from datetime import datetime, timedelta

from cp import config, model, policy, store

KEY_RETRY = "notify.retry_after"
KEY_REPORT = "notify.report."  # + YYYY-MM-DD.HH, set once the scheduled report of that hour was handled


def _now_kst() -> datetime:
    return datetime.now(model.tzinfo(config.KST_TZ))


def _days_left(it: dict, now: datetime):
    dl = (it.get("deadline") or "")[:10]
    if not dl:
        return None
    try:
        return (datetime.fromisoformat(dl).date() - now.date()).days
    except ValueError:
        return None


def waiting(db) -> dict:
    """Items waiting for the user. proposals: analysis ready (audited, or no audit needed, or audit skipped);
    held: analysis whose audit is still running; reviews: finished works."""
    out = {"proposals": [], "held": [], "reviews": []}
    for it in store.items(db, "state IN ('proposal','review')"):
        w = it.get("work") or {}
        if it["state"] == "review":
            out["reviews"].append(it)
        elif w.get("audit_required") and not (w.get("audit") or {}).get("verdict"):
            out["held"].append(it)
        else:
            out["proposals"].append(it)
    for k in out:
        out[k].sort(key=model.order_key)
    return out


def _told(it: dict) -> bool:
    w = it.get("work") or {}
    return bool(w.get("notified_review" if it["state"] == "review" else "notified_proposal"))


def plan(db, now: datetime | None = None, pol: dict | None = None) -> list:
    """Notices due now: [{"kind", "items": [item], "report_key": str|None}]. Pure: sends and marks nothing."""
    now = now or _now_kst()
    pol = pol or policy.POLICY
    batch, near = int(pol["notify_batch_size"]), int(pol["notify_deadline_days"])
    w = waiting(db)
    notices, used = [], set()

    def take(kind, items, report_key=None):
        items = [i for i in items if i["id"] not in used]
        if items:
            used.update(i["id"] for i in items)
            notices.append({"kind": kind, "items": items, "report_key": report_key})

    def urgent(i):
        d = _days_left(i, now)
        return d is not None and d <= near

    new_p = [i for i in w["proposals"] if not _told(i)]
    new_r = [i for i in w["reviews"] if not _told(i)]
    for i in new_r:  # finished works of big contests, one by one
        if policy.is_big(i, pol):
            take("review_big", [i])
    take("urgent", [i for i in new_p + new_r if urgent(i)])
    small = [i for i in new_r if i["id"] not in used]
    if len(small) >= batch:
        take("review_batch", small)
    rest_p = [i for i in new_p if i["id"] not in used]
    if len(rest_p) >= batch:
        take("proposal_batch", rest_p)
    for h in sorted(pol.get("notify_report_hours") or []):
        key = f"{KEY_REPORT}{now.date().isoformat()}.{h:02d}"
        if now.hour == h and store.kv_get(db, key) is None:  # only inside its own hour; a missed hour is not made up
            # the report lists everything waiting (also what was told before) and replaces the other notices of this tick
            return [{"kind": "report", "items": w["proposals"] + w["reviews"], "report_key": key}]
    return notices


# ---------- message text ----------
STAGE_KO = {"proposal": "분석서", "review": "완성작"}
KIND_KO = {"review_big": "큰 상금 공모전 완성작", "urgent": "마감이 가까운 항목", "review_batch": "완성작 모음",
           "proposal_batch": "분석서 모음", "report": "정기 보고"}


def _line(it: dict, now: datetime) -> str:
    w = it.get("work") or {}
    top = w.get("top_prize") or (f"{w['top_prize_krw']:,}원" if w.get("top_prize_krw") else "상금 미상")
    d = _days_left(it, now)
    dl = (it.get("deadline") or "")[:10] or "마감 미상"
    when = f"{dl}" + (f" (D-{d})" if d is not None and d >= 0 else "")
    head = f"{it['id']} [{STAGE_KO.get(it['state'], it['state'])}] {it.get('title') or it.get('title_en') or ''}"
    meta = f"   {it.get('org') or ''} | 상금 {top} | 마감 {when}"
    audit = ""
    if it["state"] == "proposal" and w.get("audit_required"):
        v = (w.get("audit") or {}).get("verdict")
        audit = {"pass": " | 적대적 검증 통과", "skipped": " | 적대적 검증 실행 못 함", "fail": " | 적대적 검증 실패"}.get(v, "")
    how = (w.get("submit_method") or "").strip()
    return head + "\n" + meta + (f" | 제출 {how}" if how else "") + audit


def render(notice: dict, now: datetime | None = None) -> tuple[str, str]:
    """(subject, text) in Korean. Kept under 4800 characters (Telegram limit)."""
    now = now or _now_kst()
    items = notice["items"]
    kind = notice["kind"]
    n_p = sum(1 for i in items if i["state"] == "proposal")
    n_r = sum(1 for i in items if i["state"] == "review")
    subject = f"[공모전] {KIND_KO.get(kind, kind)}: 분석서 {n_p}건, 완성작 {n_r}건 검토 요청"
    foot = "\n\nEntry Pilot 창의 '공모전' 탭에서 보고 답합니다(통과, 수정)."
    head = f"{KIND_KO.get(kind, kind)} ({now.strftime('%m-%d %H:%M')}). 답을 기다리는 항목입니다.\n\n"
    budget = 4600 - len(head) - len(foot)
    body, used = "", 0
    for it in items:
        ln = _line(it, now) + "\n"
        if len(body) + len(ln) > budget:
            break
        body += ln + "\n"
        used += 1
    if used < len(items):
        body += f"... 외 {len(items) - used}건은 앱에서 확인하세요."
    return subject, head + body.rstrip() + foot


# ---------- sending: the outbox ----------
def send(db, subject: str, text: str) -> dict:
    """Put one notice into the outbox. Returns {"sent": ["app"], "failed": {}} or the error. The app does the real send."""
    d = config.notices_dir()
    try:
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        tmp = os.path.join(d, stamp + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"subject": subject, "text": text, "at": store.now_iso()}, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, stamp + ".json"))
    except OSError as e:
        return {"sent": [], "failed": {"outbox": f"{type(e).__name__}: {e}"}}
    return {"sent": ["app"], "failed": {}}


def mark_told(db, items: list):
    for it in items:
        cur = store.get_item(db, it["id"])
        if not cur or cur["state"] != it["state"]:
            continue
        w = dict(cur.get("work") or {})
        w["notified_review" if cur["state"] == "review" else "notified_proposal"] = store.now_iso()
        store.update_item(db, cur["id"], work=w)


def run(dry_run: bool = False, now: datetime | None = None) -> dict:
    """Queue every due notice. Returns a result dict; with dry_run nothing is queued or marked."""
    now = now or _now_kst()
    db = store.connect()
    try:
        notices = plan(db, now)
        retry = store.kv_get(db, KEY_RETRY)
    finally:
        db.close()
    res = {"ok": True, "due": [{"kind": n["kind"], "ids": [i["id"] for i in n["items"]]} for n in notices], "sent": [],
           "failed": []}
    if dry_run or not notices:
        return res
    if retry and datetime.fromisoformat(retry) > now:
        res["waiting_retry_until"] = retry
        return res
    for n in notices:
        if not n["items"]:  # an empty report: nothing waits; the hour is recorded as handled
            with store.writer() as db:
                store.kv_set(db, n["report_key"], store.now_iso())
            continue
        subject, text = render(n, now)
        with store.writer() as db:
            r = send(db, subject, text)
            for ch, err in r["failed"].items():
                store.add_error(db, "system", f"notice ({n['kind']}) on {ch} failed: {err}")
            if r["sent"]:
                mark_told(db, n["items"])
                if n["report_key"]:
                    store.kv_set(db, n["report_key"], store.now_iso())
                store.kv_del(db, KEY_RETRY)
                res["sent"].append({"kind": n["kind"], "ids": [i["id"] for i in n["items"]], "channels": r["sent"]})
            else:
                store.kv_set(db, KEY_RETRY, (now + timedelta(seconds=config.NOTIFY_RETRY_SEC)).isoformat())
                res["failed"].append({"kind": n["kind"], "errors": r["failed"]})
                res["ok"] = False
                break  # the next loop retries after NOTIFY_RETRY_SEC
    return res


def status() -> dict:
    db = store.connect()
    try:
        w = waiting(db)
        return {"ok": True, "retry_after": store.kv_get(db, KEY_RETRY),
                "waiting": {k: [i["id"] for i in v] for k, v in w.items()},
                "untold": {"proposals": [i["id"] for i in w["proposals"] if not _told(i)],
                           "reviews": [i["id"] for i in w["reviews"] if not _told(i)]},
                "due_now": [{"kind": n["kind"], "ids": [i["id"] for i in n["items"]]} for n in plan(db)]}
    finally:
        db.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="cp notify")
    ap.add_argument("action", choices=("status", "run"))
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    a = ap.parse_args(argv)
    config.utf8_stdio()
    r = status() if a.action == "status" else run(a.dry_run)
    print(json.dumps(r, ensure_ascii=False, default=str))
    return 0 if r.get("ok") else 1
