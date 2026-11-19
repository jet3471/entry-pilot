"""Pure helpers for manual upload items (rendering and in-process actions).

The HTTP server (`serve` / `Handler`) and CLI entry (`ep owner-page`) have been removed because the app window
provides the authenticated to-do tab. Pure rendering and upload helpers remain for library/test use.
"""
import datetime
import html
import json
import os
import re
import threading
from urllib.parse import urlsplit

from ep import cli, config, model, store, translate

ID_RE = re.compile(r"^E\d{1,5}$")
EXT_OK = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD = 30 * 1024 * 1024
NEED_KO = {"login": "로그인", "payment": "결제", "upload": "사진/파일", "identity": "본인인증", "sns": "SNS", "app": "앱",
           "kakao": "카카오", "other": "기타", "residency": "거주지", "local_phone": "현지 전화", None: ""}
WD = "월화수목금토일"
_tr_lock = threading.Lock()
_tr_busy = False


def safe_url(u: str | None) -> str:
    s = str(u or "").strip()
    if not s:
        return ""
    p = urlsplit(s)
    return s if p.scheme.lower() in ("http", "https") and p.netloc else ""


def captures_dir() -> str:
    d = os.path.join(config.data_dir(), "captures")
    os.makedirs(d, exist_ok=True)
    return d


def owner_files(item_id: str) -> list:
    d = captures_dir()
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.startswith(item_id + "_owner_"))


def candidates(db) -> list:
    today = model.today_kst().isoformat()
    rows = db.execute(
        "SELECT id,title,url,deadline,deadline_time,state,lane,need,reason,question,verify_notes,prize,flags "
        "FROM items WHERE deadline>=? AND deadline!='' AND "
        "state='manual' AND need='upload' "
        "ORDER BY deadline, COALESCE(NULLIF(deadline_time,''),'23:59'), id", (today,)).fetchall()
    return [dict(r) for r in rows]


def what_text(it: dict) -> str:
    s = (it.get("verify_notes") or "").strip() or (it.get("reason") or "").strip() or (it.get("question") or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:320]


TODO_PROMPT = (
    "아래 JSON 배열의 각 항목은 온라인 이벤트 응모용 작업 메모입니다(영어, 한국어 또는 섞인 글). "
    "사람이 직접 하도록 줄 한국어 지시로 바꿔서, 입력과 같은 길이의 JSON 문자열 배열로만 답하세요. "
    "한 항목은 한 줄, 180자 이내. 포함할 것: 팔로우할 계정, 좋아요나 댓글 여부, 댓글을 달아야 하면 '댓글: ' 뒤에 메모에 있는 문구를 "
    "그대로, 폼 제출이나 캡처 같은 나머지 할 일. 메모에 없는 내용은 지어내지 마세요. 설명이나 코드블록 없이 배열만 출력하세요.\n")


def _key(t: str) -> str:
    return translate.text_hash("owner-todo:" + t)


def todo_cached(texts: list) -> dict:
    """{text: Korean instruction} for the texts already in the i18n cache (key = hash of 'owner-todo:' + text)."""
    db = store.connect()
    try:
        out = {}
        for t in set(texts):
            r = db.execute("SELECT ko FROM i18n WHERE hash=?", (_key(t),)).fetchone()
            if r and r[0]:
                out[t] = r[0]
        return out
    finally:
        db.close()


def todo_fill(texts: list, chunk: int = 20) -> dict:
    """Call the model for the texts that are not cached yet; returns counts. Raises translate.ModelError."""
    have = todo_cached(texts)
    miss = [t for t in dict.fromkeys(texts) if t and t not in have]
    done = 0
    for a in range(0, len(miss), chunk):
        part = miss[a:a + chunk]
        out = translate.run_model(TODO_PROMPT + json.dumps(part, ensure_ascii=False))
        arr = translate.extract_json_array(out)
        if len(arr) != len(part):
            raise translate.ModelError(f"model returned {len(arr)} items for {len(part)}")
        with store.writer() as db:
            for t, k in zip(part, arr):
                if isinstance(k, str) and k.strip():
                    db.execute("INSERT OR REPLACE INTO i18n(hash,en,ko,at) VALUES(?,?,?,?)",
                               (_key(t), t, k.strip(), store.now_iso()))
                    done += 1
    return {"missing": len(miss), "filled": done}


def translate_pending(texts: list) -> None:
    """Background: fill the cache; the next page load shows Korean."""
    global _tr_busy
    with _tr_lock:
        if _tr_busy:
            return
        _tr_busy = True
    try:
        todo_fill(texts)
    except Exception as e:  # never swallow: record it
        try:
            with store.writer() as db:
                store.add_error(db, "system", f"owner-page translate failed: {type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        _tr_busy = False


def build_page() -> str:
    db = store.connect()
    try:
        items = candidates(db)
    finally:
        db.close()
    texts = [what_text(i) for i in items]
    ko = todo_cached(texts)
    miss = [t for t in texts if t and t not in ko]
    if miss:
        threading.Thread(target=translate_pending, args=(texts,), daemon=True).start()
    today = model.today_kst()
    rows, cur = [], None
    for i, t in zip(items, texts):
        d = datetime.date.fromisoformat(i["deadline"])
        left = (d - today).days
        if i["deadline"] != cur:
            cur = i["deadline"]
            n = sum(1 for x in items if x["deadline"] == cur)
            rows.append(f'<div class="g" data-left="{left}">{d.month}월 {d.day}일({WD[d.weekday()]}) 마감 {n}건</div>')
        kind = NEED_KO.get(i["need"], "")
        tm = (" " + i["deadline_time"]) if i.get("deadline_time") else ""
        n_files = len(owner_files(i["id"]))
        what = html.escape(ko.get(t, t))
        prize = html.escape((i.get("prize") or "")[:40])
        u = safe_url(i.get("url"))
        link_html = f'<a href="{html.escape(u, quote=True)}" target="_blank" rel="noopener noreferrer">링크 열기</a>' if u else ""
        rows.append(
            f'<div class="r" data-id="{html.escape(i["id"], quote=True)}" data-left="{left}">'
            f'<div class="h"><b>{html.escape(i["id"])}</b>{html.escape(tm)} <span class="n">{kind}</span> {html.escape((i["title"] or "")[:70])}</div>'
            f'<div class="w">{prize} / {what}</div>'
            f'<div class="a">{link_html}'
            f'<span class="s">{("사진 %d장 있음" % n_files) if n_files else ""}</span></div></div>')
    return PAGE.replace("@@COUNT@@", str(len(items))).replace("@@ROWS@@", "\n".join(rows))


PAGE = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>내가 할 일 @@COUNT@@건</title>
<style>
*{border-radius:0;transition:none;animation:none;box-sizing:border-box}
body{margin:0;font:15px/1.4 sans-serif;background:#fff;color:#111}
.top{padding:10px;background:#111;color:#fff;position:sticky;top:0;z-index:2}
.g{background:#ddd;padding:6px 10px;font-weight:bold}
.r{border-bottom:1px solid #999;padding:8px 10px}
.h b{font-size:16px}.n{border:1px solid #555;padding:0 4px;font-size:12px}
.w{margin:3px 0;color:#333;font-size:13px}
.a a{display:inline-block;padding:6px 10px;border:1px solid #111;color:#111;margin:2px 6px 2px 0;text-decoration:none;font-size:14px}
.s{font-size:13px;color:#333}
</style>
<div class="top"><b>내가 할 일 @@COUNT@@건</b></div>
@@ROWS@@
</html>'''


def apply_act(item_id: str, act: str) -> dict:
    if not ID_RE.match(item_id or ""):
        return {"ok": False, "error": "bad id"}
    stamp = model.now_kst().strftime("%m-%d %H:%M")
    if act == "done":
        r = cli.owner_act("done", item_id, f"user did it by hand (page {stamp})")
        msg = "완료로 기록"
    elif act == "skip":
        r = cli.owner_act("skip", item_id, f"user passed (page {stamp})")
        msg = "제외 처리"
    elif act == "submit":
        files = owner_files(item_id)
        note = (f"USER ACTION {stamp}: the user did the manual steps by hand. Use lane browser or form for what "
                f"remains (form, upload).")
        if files:
            note += " The user attached file(s) for the form/upload: " + "; ".join(files) + \
                    " (upload with page.set_input_files; they are the user's own screenshots/photos)."
        r = cli.owner_act("retry", item_id, note)
        msg = f"에이전트에게 맡김 (사진 {len(files)}장)"
    else:
        return {"ok": False, "error": "bad action"}
    if r.get("ok"):
        return {"ok": True, "msg": msg}
    return {"ok": False, "error": str(r.get("error"))[:200]}


def save_upload(item_id: str, name: str, body: bytes) -> dict:
    if not ID_RE.match(item_id or ""):
        return {"ok": False, "error": "bad id"}
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in EXT_OK:
        return {"ok": False, "error": f"사진 형식 아님 ({ext or '확장자 없음'})"}
    if not body or len(body) > MAX_UPLOAD:
        return {"ok": False, "error": "빈 파일이거나 30MB 초과"}
    n = len(owner_files(item_id)) + 1
    path = os.path.join(captures_dir(), f"{item_id}_owner_{n}{ext}")
    with open(path, "wb") as f:
        f.write(body)
    return {"ok": True, "path": path}
