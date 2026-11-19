"""The user's review of contest items inside the app (no web page, no remote address).

The app server (app/server.py) calls these functions; they return plain dicts, written for the contest tab:

  overview(db)            items grouped by what the user has to do: answer, answered/running, manual, done
  detail(db, iid)         one item: the one-screen summary rows, what is sent (form answers, mail, pages, files), status
  decide(iid, action, note)  the user's answer: "yes" (통과), "revise" (수정, memo needed), "skip" (안 함)
  file_path / preview_png    read access to the files of one finished work (only inside its own draft folder)

Answers go through the same workflow functions as `cp accept|approve|reanalyze|revise|exclude`, attributed to `owner`
(the engine's internal name for the user) and written to owner_answers. Nothing is submitted without a yes: a
finished work waits in state `review` until then (policy review_policy 'all' is the default).
"""
import mimetypes
import os
import re
from datetime import date

from cp import analysis, cmdbase, config, dossier_ko, model, notify, preview, store, workflow

StaleDraftError = workflow.StaleDraftError
InvalidSendSpecError = workflow.InvalidSendSpecError

STATE_KO = {"new": "분석 대기", "verifying": "분석 중", "proposal": "분석서 확인", "draft": "작성 대기", "drafting": "작성 중",
            "review": "완성작 확인", "ready": "제출 대기", "submitting": "제출 중", "submitted": "제출 완료",
            "failed": "제출 실패", "owner": "AI가 물음", "manual": "직접 해야 함", "excluded": "제외"}
NEED_KO = {"login": "로그인 필요", "identity": "본인인증 필요", "payment": "결제 필요", "sns": "SNS 활동 필요",
           "upload": "직접 만든 파일 필요", "app": "앱에서만 가능", "kakao": "카카오톡 필요", "residency": "거주 조건",
           "local_phone": "현지 전화번호 필요", "unsupported": "이 앱이 지원하지 않는 형식", "other": "직접 확인 필요"}
ANSWER_STATES = ("proposal", "review")
RUNNING_STATES = ("new", "verifying", "draft", "drafting", "ready", "submitting")
CLIP = {"prize": 76, "send": 90, "format": 46, "rights": 46, "ai": 46, "stated": 70, "hidden": 44, "risk": 60, "one": 96}
HIDDEN_MAX, RISK_MAX = 4, 2
EVIDENCE = re.compile(r"\s*[:：]\s*추정|\s*\(추정\)|\s*(?:evidence|근거)\s*[:：는은]", re.I)
STALE = re.compile(r"오늘|남았|기준으로|days? left|as of", re.I)
STATED = ("stated", "명시", "밝힌", "표명", "공개된")
HIDDEN = ("hidden", "숨은")


# ---------- small text helpers ----------
def _plain(s) -> str:
    s = re.sub(r"\*\*|`", "", str(s or ""))
    s = re.sub(r"^\s*(?:#+|>+)\s*", "", s).strip()
    return re.sub(r"^\s*(?:[-•]|\d+[.)])\s*", "", s).strip()


def _clip(s, n: int) -> str:
    s = " ".join(_plain(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _claim(s: str) -> str:
    s = re.sub(r"^(?:guess|추정)\s*[:：]\s*", "", s, flags=re.I)
    return EVIDENCE.split(s, maxsplit=1)[0].strip(" .:：")


def _lines(body: str) -> list:
    return [x for x in (_plain(ln) for ln in str(body or "").splitlines()) if x]


def prize_text(it: dict) -> str:
    w = it.get("work") or {}
    return w.get("top_prize") or (f"{w['top_prize_krw']:,}원" if w.get("top_prize_krw") else "상금 미상")


def days_left(it: dict):
    try:
        return (date.fromisoformat((it.get("deadline") or "")[:10]) - model.today_kst()).days
    except ValueError:
        return None


def _intent(it) -> tuple:
    text, _ = dossier_ko.korean_text(it)
    stated, hidden = "", []
    for head, body in analysis.sections(text):
        h = head.lower()
        if any(k in h for k in HIDDEN):
            hidden = [c for c in (_claim(x) for x in _lines(body)) if c]
        elif any(k in h for k in STATED):
            stated = " ".join(_lines(body))
    return stated, hidden


def _core(sc, *words) -> str:
    for r in (sc or {}).get("핵심") or []:
        if isinstance(r, dict) and any(w in str(r.get("항목") or "") for w in words):
            return str(r.get("값") or "")
    return ""


def _format_to_list(raw) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [x.strip() for x in raw if isinstance(x, str) and x.strip()]
    return []


def _format_to(raw) -> str:
    return ", ".join(_format_to_list(raw))


# ---------- the one-screen summary ----------
def sheet_rows(it: dict, send: dict | None = None) -> tuple:
    """([(label, value)], note). value is a string or a list of strings. The cells come from the review table
    (dossier_ko.brief) when it is made; until then they are cut from the schematic and the dossier, and `note` says so."""
    w = it.get("work") or {}
    br = dossier_ko.brief(it)
    sc = dossier_ko.schema(it)
    send = send if isinstance(send, dict) else {}
    has_work = bool(w.get("snapshot_dir") or w.get("draft_dir"))
    how = how_from_send = send["how"].strip() if isinstance(send.get("how"), str) else ""
    to = _format_to(send.get("to"))
    dl = (it.get("deadline") or "미상")[:10] + (" " + it["deadline_time"] if it.get("deadline_time") else "")
    rows = []

    def row(k, v):
        if v:
            rows.append((k, v))

    def lst(xs, n=9, k=None):
        return [_clip(x, CLIP[k]) if k else str(x) for x in xs[:n]]
    note = ""
    if has_work:
        first = re.split(r"(?<=[.!?])\s+", str(w.get("draft_summary") or "").strip())[0]
        row("낸 것", "" if re.search(r"SEND|json|[.]md", first, re.I) else first)
    if br:
        row("구하는 것", br.get("한줄"))
        row("상금", lst(br.get("상금") or [], 3))
        out = "" if "없음" in str(br.get("발표")) else str(br.get("발표") or "")
        row("마감", dl + (f" (발표 {out})" if out else ""))
        row("대상", br.get("대상"))
        row("형식", br.get("형식"))
        row("보내는 곳", (f"{how} → {to}" if (how and to) else (to or how)) or br.get("제출"))
        row("권리", br.get("권리"))
        row("AI 규칙", br.get("AI"))
        row("밝힌 의도", br.get("밝힌 의도"))
        row("숨은 의도", lst(br.get("숨은 의도") or []))
        row("주의", lst(br.get("주의") or []))
    else:
        stated, hidden = _intent(it)
        risks = [str(x) for x in (sc or {}).get("주의") or [] if not STALE.search(str(x))]
        how = how or _core(sc, "제출") or w.get("submit_method") or ""
        if not has_work:
            row("구하는 것", _clip((sc or {}).get("한줄"), CLIP["one"]))
        row("상금", _clip(_core(sc, "상금") or prize_text(it), CLIP["prize"]))
        row("마감", dl)
        send_val = f"{how} → {to}" if (how and to) else (to or how)
        row("보내는 곳", send_val if (to or how_from_send) else _clip(send_val, CLIP["send"]))
        row("형식", _clip(_core(sc, "형식") or w.get("format"), CLIP["format"]))
        row("권리", _clip(_core(sc, "권리"), CLIP["rights"]))
        row("AI 규칙", _clip(_core(sc, "AI"), CLIP["ai"]))
        row("밝힌 의도", _clip(stated, CLIP["stated"]))
        row("숨은 의도", lst(hidden, HIDDEN_MAX, "hidden"))
        row("주의", lst(risks, RISK_MAX, "risk"))
        note = "요약표를 만드는 중입니다. 지금은 분석서에서 잘라 온 내용이라 문장이 끊길 수 있습니다."
    reapp = str(w.get("reapproval_note") or "").strip() if it.get("state") == "review" else ""
    if reapp:
        note = f"{reapp} ({note})" if note else reapp
    return rows, note


# ---------- what is sent ----------
def _read_text_file(path: str, limit: int = 20000) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def _active_work_dir(it: dict) -> tuple[str, list[str]]:
    w = (it or {}).get("work") or {}
    iid = (it or {}).get("id")
    if w.get("snapshot_dir"):
        d = preview.safe_snapshot_dir(w["snapshot_dir"], iid)
        return d, list(w.get("snapshot_files") or w.get("draft_files") or [])
    d = preview.safe_draft_dir(w.get("draft_dir") or "", iid)
    return d, list(w.get("draft_files") or [])


def units(it: dict, send: dict | None = None) -> list:
    """What is sent, as a list of dicts in the order the user reads it:
    target (method + full recipient/URL), fields (form answers), mail, image (PDF page or raster image),
    attachment (name + size + sha256 for non-PDF/non-image files), text (a text work)."""
    w = it.get("work") or {}
    iid = it["id"]
    d = w.get("snapshot_dir") or w.get("draft_dir") or ""
    files = list((w.get("snapshot_files") or w.get("draft_files") or []) if w.get("snapshot_dir") else w.get("draft_files") or [])
    send = (preview.load(d) if d else {}) if send is None else (send if isinstance(send, dict) else {})
    out = []
    how_s = send["how"].strip() if isinstance(send.get("how"), str) else ""
    to_list = _format_to_list(send.get("to"))
    if how_s or to_list:
        out.append({
            "type": "target",
            "label": "제출 방법 및 받는 곳",
            "how": how_s,
            "to": to_list,
            "text": f"{how_s} → {', '.join(to_list)}" if (how_s and to_list) else (", ".join(to_list) or how_s),
        })
    fields = [f for f in (send.get("fields") or [])
              if isinstance(f, (list, tuple)) and len(f) == 2 and isinstance(f[0], str) and isinstance(f[1], str)]
    if fields:
        out.append({"type": "fields", "label": f"입력할 내용 ({len(fields)}칸)", "rows": [[f[0], f[1]] for f in fields]})
    subj = send["subject"] if isinstance(send.get("subject"), str) else ""
    body = send["body"] if isinstance(send.get("body"), str) else ""
    if subj or body:
        out.append({"type": "mail", "label": "메일", "subject": subj, "body": body})
    from urllib.parse import quote
    shown = preview.pdfs(d, files, send)
    for title, pdf in shown:
        try:
            total = preview.page_count(d, pdf)
        except Exception as e:  # a broken PDF must be visible, not hidden
            out.append({"type": "warn", "label": title, "text": f"PDF를 열지 못했습니다 ({type(e).__name__}: {e})"})
            continue
        if total <= 0:
            out.append({"type": "warn", "label": title, "text": "PDF에 표시할 쪽이 없습니다."})
            continue
        shown_n = min(total, preview.MAX_PAGES)
        for i in range(shown_n):
            out.append({"type": "image", "label": f"{title} {i + 1}/{total}쪽",
                        "url": f"preview?id={quote(str(iid), safe='')}&pdf={quote(str(pdf), safe='')}&n={i}"})
        if total > preview.MAX_PAGES:
            out.append({"type": "warn", "label": f"{title} ({total}쪽)",
                        "text": f"전체 {total}쪽 가운데 앞 {preview.MAX_PAGES}쪽만 미리보기로 표시합니다. "
                                f"{preview.MAX_PAGES + 1}~{total}쪽({total - preview.MAX_PAGES}쪽)은 렌더링되지 않았으니 폴더 열기로 원본 전체를 확인하세요."})
    used = {p for _, p in shown} | {preview.NAME}
    sent = [x for x in (send.get("files") or []) if isinstance(x, str)]
    safe_d = preview.safe_work_dir(d, iid) if d else ""
    for name in (sent if send else files):
        if not isinstance(name, str) or name in used or name not in files:
            continue
        if name.lower().endswith(preview.IMAGES):
            out.append({"type": "image", "label": name,
                        "url": f"file?id={quote(str(iid), safe='')}&name={quote(str(name), safe='')}"})
            used.add(name)
        elif send:
            fp = preview.safe_work_file(safe_d, name, iid) if safe_d else ""
            if fp:
                try:
                    f_sha, f_sz = preview.sha256_file(fp)
                    out.append({
                        "type": "attachment",
                        "label": f"첨부 파일: {name}",
                        "name": name,
                        "size": f_sz,
                        "sha256": f_sha,
                        "text": f"{name} ({f_sz:,} 바이트, SHA-256: {f_sha})",
                    })
                except (OSError, ValueError) as e:
                    out.append({"type": "warn", "label": name, "text": f"첨부 파일을 읽지 못했습니다: {e}"})
            else:
                out.append({"type": "warn", "label": name, "text": f"첨부 파일 {name} 이(가) 없습니다."})
            used.add(name)
    if not send:
        for name in files:
            if not isinstance(name, str) or name in used or not name.lower().endswith((".md", ".txt")):
                continue
            fp = preview.safe_work_file(safe_d, name, iid) if safe_d else ""
            if fp and os.path.getsize(fp) <= preview.MAX_FILE_BYTES:
                out.append({"type": "text", "label": name, "text": _read_text_file(fp)})
    if safe_d and preview.safe_work_file(safe_d, preview.NAME, iid):
        bad = preview.problems(safe_d, files)
        if bad:
            out.append({"type": "warn", "label": "SEND.json 확인 필요",
                        "text": "보낼 명세(SEND.json)에 오류가 있습니다: " + "; ".join(bad)})
    if not out:
        out.append({"type": "warn", "label": "보낼 내용", "text": "보낼 파일과 내용을 찾지 못했습니다. 수정을 눌러 다시 만들게 하세요."})
    return out


def file_list(it: dict) -> list:
    d, files = _active_work_dir(it)
    out = []
    for name in files:
        p = preview.safe_work_file(d, name, it["id"]) if d else ""
        sz = None
        sha = ""
        if p:
            try:
                sha, sz = preview.sha256_file(p)
            except (OSError, ValueError):
                sz = os.path.getsize(p) if os.path.isfile(p) else None
        out.append({"name": name, "size": sz, "sha256": sha})
    return out


# ---------- lists ----------
def _row(it: dict) -> dict:
    w = it.get("work") or {}
    return {"id": it["id"], "title": it.get("title") or it.get("title_en") or it["id"], "org": it.get("org") or "",
            "state": it["state"], "state_ko": STATE_KO.get(it["state"], it["state"]),
            "kind": "work" if it["state"] == "review" else "analysis" if it["state"] == "proposal" else "",
            "deadline": (it.get("deadline") or "")[:10], "days_left": days_left(it), "prize": prize_text(it),
            "submit": w.get("submit_method") or "", "work_type": w.get("work_type") or "",
            "need": NEED_KO.get(it.get("need") or "", it.get("need") or ""),
            "note": it.get("question") or it.get("reason") or "", "url": it.get("url") or it.get("source_url") or ""}


def overview(db) -> dict:
    """{"answer": [...], "held": [...], "running": [...], "manual": [...], "owner": [...], "done": [...], "counts": {}}."""
    w = notify.waiting(db)
    out = {"answer": [_row(i) for i in w["reviews"] + w["proposals"]], "held": [_row(i) for i in w["held"]]}
    for key, states in (("running", RUNNING_STATES), ("manual", ("manual",)), ("owner", ("owner", "failed")),
                        ("done", ("submitted",))):
        marks = ",".join("'%s'" % s for s in states)
        rows = store.items(db, f"state IN ({marks})")
        rows.sort(key=model.order_key)
        out[key] = [_row(i) for i in rows]
    out["counts"] = {s: db.execute("SELECT COUNT(*) FROM items WHERE state=?", (s,)).fetchone()[0] for s in model.STATES}
    return out


def detail(db, iid: str) -> dict | None:
    it = store.get_item(db, iid)
    if not it:
        return None
    w = it.get("work") or {}
    d, _ = _active_work_dir(it)
    send = preview.load(d) if d else {}
    rows, note = sheet_rows(it, send)
    out = {"item": _row(it), "rows": [{"label": k, "value": v} for k, v in rows], "note": note,
           "answerable": it["state"] in ANSWER_STATES, "units": [], "files": [], "folder": bool(d),
           "draft_hash": "",
           "status": {"submitted_at": it.get("submitted_at") or "", "evidence": it.get("evidence") or "",
                      "question": it.get("question") or "", "reason": it.get("reason") or "",
                      "review_note": w.get("review_note") or "", "analysis_note": w.get("analysis_note") or ""},
           "history": [{"at": h.get("at"), "agent": h.get("agent"), "action": h.get("action"), "detail": (h.get("detail") or "")[:200]}
                       for h in store.history(db, iid)[-8:]]}
    if d and it["state"] not in ("proposal", "new", "verifying"):
        out["units"] = units(it, send)
        out["files"] = file_list(it)
        try:
            out["draft_hash"] = workflow.draft_hash(w, iid)
        except cmdbase.CmdError:
            out["draft_hash"] = ""
    if it["state"] == "proposal":
        text, ok = dossier_ko.korean_text(it)
        out["dossier"] = {"text": analysis.plain_headings(text), "translated": ok, "plan": analysis.plan_ko(it)}
        held = w.get("audit_required") and not (w.get("audit") or {}).get("verdict")
        out["answerable"] = not held
    return out


# ---------- the user's answer ----------
def decide(iid: str, action: str, note: str = "", draft_hash: str | None = None, *, require_hash: bool = False) -> str:
    """Apply the user's answer. Returns a Korean message. Raises cmdbase.CmdError when it cannot be applied."""
    note = (note or "").strip()
    deferred_err = None
    with store.writer() as db:
        it = store.get_item(db, iid)
        if not it:
            raise cmdbase.CmdError(f"항목 {iid} 이(가) 없습니다")
        st = it["state"]
        if st not in ANSWER_STATES:
            raise cmdbase.CmdError(f"{iid} 은(는) 지금 {STATE_KO.get(st, st)} 상태라 답할 수 없습니다")
        kind = {"yes": "approve", "revise": "revise", "skip": "skip"}.get(action)
        if not kind:
            raise cmdbase.CmdError("알 수 없는 답입니다")
        if kind == "revise" and not note:
            raise cmdbase.CmdError("수정은 고칠 내용을 메모에 적어야 합니다")
        if kind == "approve":
            if st == "proposal":
                workflow.accept(db, it, "owner", note or "answered in the app")
                msg = "통과했습니다. 작품 작성을 시작합니다."
            else:
                if require_hash and not (isinstance(draft_hash, str) and draft_hash.strip()):
                    raise StaleDraftError("화면에 표시된 초안 확인 해시가 없습니다. 화면을 새로고침해 다시 확인해 주세요.")
                try:
                    workflow.approve(db, it, "owner", note or "answered in the app", expected_hash=draft_hash)
                    msg = "통과했습니다. 제출 단계로 넘어갑니다."
                except InvalidSendSpecError as e:
                    # workflow.approve already sent the item back to `draft` with review_note; commit that revise
                    deferred_err = InvalidSendSpecError(
                        f"보낼 명세(SEND.json) 오류로 AI에게 수정을 요청했습니다: {e}"
                    )
                    kind = "revise"
                    msg = ""
        elif kind == "revise":
            (workflow.reanalyze if st == "proposal" else workflow.revise)(db, it, "owner", note)
            msg = "수정 요청을 보냈습니다. AI가 다시 합니다."
        else:
            cmdbase._check("exclude", it, "owner")
            store.update_item(db, iid, state="excluded", reason="the user declined in the app", claimed_by=None, claimed_at=None)
            store.add_history(db, iid, "owner", "exclude", "the user declined in the app" + (f": {note}" if note else ""))
            msg = "안 하기로 했습니다. 제외됩니다."
        db.execute("INSERT INTO owner_answers(item_id, kind, text, at, applied) VALUES(?,?,?,?,1)",
                   (iid, kind, str(deferred_err) if deferred_err else (note or action), store.now_iso()))
    if deferred_err is not None:
        raise deferred_err
    return msg


def next_waiting(db, after: str = "") -> str:
    """The next item to answer after `after` (finished works first, soonest deadline first); '' when none."""
    w = notify.waiting(db)
    ids = [i["id"] for i in w["reviews"] + w["proposals"] if i["id"] != after]
    return ids[0] if ids else ""


# ---------- files of a finished work ----------
def _item_files(db, iid: str):
    it = store.get_item(db, iid)
    if not it:
        return None, "", set()
    d, files = _active_work_dir(it)
    return it, d, set(files)


def file_path(db, iid: str, name: str) -> str:
    """Absolute path of one file of the item's snapshot/draft folder, '' when it is not one of its files."""
    _, d, names = _item_files(db, iid)
    if not d or name not in names:
        return ""
    return preview.safe_work_file(d, name, iid)


def preview_png(db, iid: str, pdf: str, n: int):
    _, d, names = _item_files(db, iid)
    if d and pdf in names and isinstance(pdf, str) and pdf.lower().endswith(".pdf") and preview.safe_work_file(d, pdf, iid):
        return preview.page_png(d, pdf, n)
    return None


def folder(db, iid: str) -> str:
    """The snapshot/draft folder of an item (for 'open folder'), '' when there is none."""
    _, d, _ = _item_files(db, iid)
    return preview.safe_work_dir(d, iid)
