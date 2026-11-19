"""Telegram through the user's own bot (made with @BotFather). The token is stored encrypted (settings, DPAPI).

connect flow: verify(token) -> the user sends any message to the bot -> find_chat() reads it and saves the chat id ->
a test message goes out. After that the app sends items that need the user with buttons, and Poller turns button
presses into engine commands (app/notify.py)."""
import hashlib
import inspect
import json
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

from app import settings

API = "https://api.telegram.org/bot{token}/{method}"


class TgError(Exception):
    pass


def call(method: str, token: str | None = None, http_timeout=30, **params):
    token = token or settings.get_secret("telegram_token")
    if not token:
        raise TgError("텔레그램 봇이 연결되지 않았습니다.")
    data = urllib.parse.urlencode({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            j = json.load(e)
        except Exception:
            raise TgError(f"텔레그램 오류 HTTP {e.code}") from None
    except Exception as e:
        raise TgError(f"텔레그램 연결 실패 ({type(e).__name__})") from None
    if not j.get("ok"):
        raise TgError(j.get("description") or "텔레그램 오류")
    return j.get("result")


START_TTL = 600  # 10 minutes
MAX_FIND_PAGES = 50
_START_CODES: dict[str, float] = {}
_USED_CODES: set[str] = set()
_code_lock = threading.Lock()


def _persisted_used_codes() -> list[str]:
    raw = settings.load().get("telegram_used_codes")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, str) and x]


def _is_code_used(code: str) -> bool:
    return code in _USED_CODES or code in _persisted_used_codes()


def _mark_code_used(code: str):
    _USED_CODES.add(code)
    used = _persisted_used_codes()
    if code not in used:
        used.append(code)
        settings.update(telegram_used_codes=used)


def new_start_code() -> str:
    now = time.time()
    with _code_lock:
        for k, t in list(_START_CODES.items()):
            if now - t > START_TTL:
                _START_CODES.pop(k, None)
        while True:
            code = secrets.token_hex(6)
            if code not in _START_CODES and not _is_code_used(code):
                break
        _START_CODES[code] = now
        return code


def verify(token: str) -> dict:
    me = call("getMe", token=token)
    return {"ok": True, "id": str(me.get("id") or ""),
            "username": me.get("username") or "", "name": me.get("first_name") or ""}


def bot_identity(s: dict | None = None) -> str:
    """Stable identifier for the currently configured bot (empty when no bot token is configured)."""
    s = settings.load() if s is None else s
    tok = settings.get_secret("telegram_token")
    if not tok:
        raw_tok = s.get("telegram_token")
        if not raw_tok:
            return ""
        tok = raw_tok if isinstance(raw_tok, str) and not raw_tok.startswith("dpapi:") else ""
    bid = str(s.get("telegram_bot_id") or "").strip()
    if not bid and tok:
        prefix = tok.split(":", 1)[0] if ":" in tok else ""
        bid = prefix if prefix.isdigit() else hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]
    uname = str(s.get("telegram_bot") or "").strip().lower()
    if not bid and not uname:
        return ""
    return f"{bid}:{uname}" if (bid and uname) else (bid or uname)


def find_chat(token: str, start_code: str = "", started_at: float | None = None) -> dict:
    """The private chat that pressed Start on the link the app opened (t.me/<bot>?start=<code>)."""
    code = (start_code or "").strip()
    if not code:
        return {"ok": False, "error": "연결 코드가 없습니다. 봇 연결을 다시 시작하세요."}
    with _code_lock:
        if _is_code_used(code):
            return {"ok": False, "error": "이미 사용된 연결 코드입니다. 봇 연결을 다시 시작하세요."}
        now = time.time()
        issued = started_at if started_at is not None else _START_CODES.get(code)
        if issued is None:
            st = settings.load()
            if (st.get("telegram_start") or "").strip() == code:
                issued = st.get("telegram_start_at")
        if not isinstance(issued, (int, float)) or issued <= 0 or now - issued > START_TTL:
            _START_CODES.pop(code, None)
            return {"ok": False, "error": "연결 코드가 만료되었습니다. 봇 연결을 다시 시작하세요."}
    offset = None
    for _ in range(MAX_FIND_PAGES):
        kw = {"token": token, "http_timeout": 15}
        if offset is not None:
            kw["offset"] = offset
        ups = call("getUpdates", **kw) or []
        if not ups:
            break
        now = time.time()
        for u in reversed(ups):
            msg = u.get("message") or u.get("edited_message")
            if not msg or msg.get("chat", {}).get("type") != "private":
                continue
            if (msg.get("text") or "").strip() != f"/start {code}":
                continue
            msg_date = msg.get("date")
            if isinstance(msg_date, (int, float)) and now - msg_date > START_TTL:
                continue
            with _code_lock:
                if _is_code_used(code):
                    return {"ok": False, "error": "이미 사용된 연결 코드입니다. 봇 연결을 다시 시작하세요."}
                if now - issued > START_TTL:
                    _START_CODES.pop(code, None)
                    return {"ok": False, "error": "연결 코드가 만료되었습니다. 봇 연결을 다시 시작하세요."}
                _mark_code_used(code)
                _START_CODES.pop(code, None)
            return {"ok": True, "chat_id": str(msg["chat"]["id"]), "name": msg["chat"].get("first_name") or ""}
        ids = [u["update_id"] for u in ups if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
        if not ids:
            break
        next_offset = max(ids) + 1
        if offset is not None and next_offset <= offset:
            break
        offset = next_offset
    return {"ok": False, "error": "봇에게 온 메시지가 없습니다. 봇 대화창에서 시작(Start)을 누르세요."}


TOKEN_RE = re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{30,})\b")


def clipboard_text() -> str:
    """Text on the Windows clipboard ("" when none). Read only while the Telegram step is open."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = wintypes.HANDLE
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [wintypes.HANDLE]
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not u.OpenClipboard(None):
        return ""
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)[:2000]
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def token_in_clipboard() -> str:
    m = TOKEN_RE.search(clipboard_text() or "")
    return m.group(1) if m else ""


def send(text: str, buttons: list | None = None, chat_id: str | None = None) -> dict:
    chat_id = chat_id or settings.load().get("telegram_chat_id")
    if not chat_id:
        raise TgError("텔레그램 대화가 연결되지 않았습니다.")
    markup = {"inline_keyboard": buttons} if buttons else None
    return call("sendMessage", chat_id=chat_id, text=text[:4000], reply_markup=markup,
                disable_web_page_preview="true")


def is_active(s: dict | None = None) -> bool:
    s = settings.load() if s is None else s
    ch = s.get("notify") or ("telegram" if s.get("telegram_chat_id") else "none")
    return ch == "telegram" and bool(s.get("telegram_token") and s.get("telegram_chat_id"))


def _accepts_kw(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


class Poller(threading.Thread):
    """Long-polls getUpdates and hands button presses (callback_query) to on_button(data) -> reply text."""

    def __init__(self, on_button, log, on_reply=None):
        super().__init__(daemon=True, name="telegram-poller")
        self.on_button, self.log, self.on_reply = on_button, log, on_reply
        self.offset = None
        self._bot_key = ""
        self.stop = threading.Event()

    def _handle_update(self, u: dict, s: dict):
        linked_id = str(s.get("telegram_chat_id") or "")
        if not linked_id:
            return
        msg_in = u.get("message") or {}
        rt = msg_in.get("reply_to_message") or {}
        if (rt and self.on_reply
                and str(msg_in.get("chat", {}).get("id")) == linked_id
                and str(msg_in.get("from", {}).get("id")) == linked_id):
            try:
                kw = {"chat_id": linked_id} if _accepts_kw(self.on_reply, "chat_id") else {}
                reply = self.on_reply(rt.get("message_id"), msg_in.get("text") or "", **kw)
            except Exception as e:
                reply = f"처리 실패: {e}"
                self.log(f"telegram reply: {e}")
            if reply:
                try:
                    send(reply, chat_id=linked_id)
                except TgError as e:
                    self.log(f"telegram send: {e}")
            return
        cq = u.get("callback_query")
        if not cq:
            return
        if str(cq.get("from", {}).get("id")) != linked_id:
            return  # only the connected user may press buttons
        msg = cq.get("message") or {}
        cq_chat = str(msg.get("chat", {}).get("id") or "")
        if cq_chat and cq_chat != linked_id:
            return
        ok_btn = True
        try:
            kw = {}
            if _accepts_kw(self.on_button, "message_id"):
                kw["message_id"] = msg.get("message_id")
            if _accepts_kw(self.on_button, "chat_id"):
                kw["chat_id"] = linked_id
            reply = self.on_button(cq.get("data") or "", **kw)
        except Exception as e:  # visible, never swallowed
            ok_btn = False
            reply = f"처리 실패: {e}"
            self.log(f"telegram button {cq.get('data')}: {e}")
        try:
            call("answerCallbackQuery", callback_query_id=cq["id"], text=(reply or "")[:190])
            if msg.get("message_id"):
                call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                     reply_markup={"inline_keyboard": []})
                send(f"{'✔' if ok_btn else '⚠'} {reply}", chat_id=str(msg["chat"]["id"]))
        except TgError as e:
            self.log(f"telegram answer: {e}")

    def poll_once(self) -> bool:
        s = settings.load()
        if not s.get("telegram_token") and not settings.get_secret("telegram_token"):
            return False
        if not s.get("telegram_chat_id"):
            return False
        bkey = bot_identity(s)
        if bkey != self._bot_key:
            self._bot_key = bkey
            self.offset = None
        active = is_active(s)
        ups = call("getUpdates", http_timeout=40 if active else 15, offset=self.offset,
                   allowed_updates=["callback_query", "message"], timeout=25 if active else 0) or []
        for u in ups:
            if isinstance(u.get("update_id"), int):
                self.offset = u["update_id"] + 1
            s = settings.load()
            if not is_active(s):
                continue
            self._handle_update(u, s)
        return active

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.poll_once():
                    self.stop.wait(10)
                    continue
            except TgError as e:
                self.log(f"telegram poll: {e}")
                self.stop.wait(15)
                continue
            time.sleep(0.2)
