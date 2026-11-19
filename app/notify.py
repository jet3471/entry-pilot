"""Tells the user, on Telegram, about items that need them, with buttons; button presses become engine actions.

owner  = the AI stopped and asks something (question in the item)   buttons: 다시 시도 / 제외
manual = only a person can do it (login, ID check, payment, app...)  buttons: 완료 / 제외
Item ids already sent are kept in data/notified.json so nothing is sent twice.

Contest items (ids C...) go through the same channel: owner and manual items like above, and the notices the contest
engine queues in data/contest/notices/ (analyses and finished works that wait for the user's 통과 / 수정 in the app).
"""
import json
import os
import threading

from app import engine, paths, settings, telegram

NEED_KO = {"unsupported": "이 앱이 지원하지 않는 형식", "login": "로그인 필요", "identity": "본인인증 필요", "payment": "결제 필요", "sns": "SNS 활동 필요",
           "upload": "파일 올리기 필요", "app": "앱에서만 가능", "kakao": "카카오톡 필요", "residency": "거주 조건",
           "local_phone": "현지 전화번호 필요", "other": "직접 확인 필요"}


def _seen_path():
    return os.path.join(paths.data_dir(), "notified.json")


def _load_seen() -> set:
    try:
        return set(json.load(open(_seen_path(), encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set):
    with open(_seen_path(), "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-2000:], f)


def _map_path():
    return os.path.join(paths.data_dir(), "telegram-messages.json")


def clear_map():
    try:
        os.remove(_map_path())
    except OSError:
        pass


def _current_binding(chat_id: str | None = None) -> tuple[str, str]:
    s = settings.load()
    cid = str(chat_id if chat_id is not None else (s.get("telegram_chat_id") or "")).strip()
    bid = telegram.bot_identity(s)
    return bid, cid


def _load_map(bot_id: str, chat_id: str) -> dict:
    if not bot_id or not chat_id:
        clear_map()
        return {}
    try:
        with open(_map_path(), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        clear_map()
        return {}
    if (not isinstance(d, dict)
            or d.get("bot_id") != bot_id
            or d.get("chat_id") != chat_id
            or not isinstance(d.get("messages"), dict)):
        clear_map()
        return {}
    return dict(d["messages"])


def _save_map(bot_id: str, chat_id: str, messages: dict):
    os.makedirs(paths.data_dir(), exist_ok=True)
    p = _map_path()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bot_id": bot_id, "chat_id": chat_id, "messages": messages}, f, ensure_ascii=False)
    os.replace(tmp, p)


def remember(message_id, item_id, state: str = "owner", bot_id: str | None = None, chat_id: str | None = None):
    bid, cid = (bot_id, str(chat_id or "").strip()) if bot_id is not None else _current_binding(chat_id)
    if not bid or not cid:
        clear_map()
        return
    msgs = _load_map(bid, cid)
    msgs[str(message_id)] = {"item_id": str(item_id), "state": str(state)}
    if len(msgs) > 3000:
        msgs = dict(list(msgs.items())[-3000:])
    _save_map(bid, cid, msgs)


def lookup_message(message_id, chat_id: str | None = None) -> dict | None:
    bid, cid = _current_binding(chat_id)
    msgs = _load_map(bid, cid)
    entry = msgs.get(str(message_id))
    if isinstance(entry, dict) and entry.get("item_id") and entry.get("state"):
        return entry
    return None


def is_contest(item_id: str) -> bool:
    """Contest engine ids start with C (cp.config.ITEM_PREFIX); event engine ids do not."""
    return str(item_id).startswith("C")


def current_item_state(item_id: str) -> str | None:
    try:
        if is_contest(item_id):
            engine.configure_contest()
            from cp import store as cp_store
            db = cp_store.connect()
            try:
                it = cp_store.get_item(db, item_id)
                return it.get("state") if it else None
            finally:
                db.close()
        engine.configure()
        from ep import store as ep_store
        db = ep_store.connect()
        try:
            it = ep_store.get_item(db, item_id)
            return it.get("state") if it else None
        finally:
            db.close()
    except Exception:
        return None


def act(action: str, item_id: str, note: str = "", expected_state: str | None = None) -> dict:
    if expected_state is not None:
        cur = current_item_state(item_id)
        if cur != expected_state:
            return {"ok": False, "error": f"항목 상태가 이미 바뀌었습니다 ({cur or '없음'})."}
    if is_contest(item_id):
        return engine.contest_act(action, item_id, note)
    engine.configure()
    import ep.cli
    return ep.cli.owner_act(action, item_id, note, expected_state=expected_state)


def on_reply(message_id, text: str, chat_id: str | None = None) -> str | None:
    """A Telegram reply to one of our item messages: the text is the user's answer (requeue with a note)."""
    entry = lookup_message(message_id, chat_id=chat_id)
    if not entry:
        return None
    item_id, expected_state = entry["item_id"], entry["state"]
    if expected_state != "owner":
        return None
    r = act("retry", item_id, text, expected_state=expected_state)
    if not r.get("ok", False):
        raise RuntimeError(r.get("error") or "엔진이 거절함")
    return "답을 전달했고 다시 시도합니다"


def describe(it: dict) -> str:
    title = it.get("title") or it.get("name") or it.get("id")
    lines = [f"[{ '확인 필요' if it.get('state') == 'owner' else '직접 해야 함' }] {title}"]
    if it.get("deadline"):
        lines.append(f"마감 {it['deadline']}")
    if it.get("state") == "manual" and it.get("need"):
        lines.append(NEED_KO.get(it["need"], it["need"]))
    q = it.get("question") or it.get("reason")
    if q:
        lines.append(f"질문: {q}")
    if it.get("url"):
        lines.append(it["url"])
    return "\n".join(lines)


def buttons(it: dict) -> list:
    i = it["id"]
    if it.get("state") == "owner":
        return [[{"text": "다시 시도", "callback_data": f"retry:{i}"}, {"text": "제외", "callback_data": f"skip:{i}"}]]
    return [[{"text": "완료", "callback_data": f"done:{i}"}, {"text": "제외", "callback_data": f"skip:{i}"}]]


def on_button(data: str, message_id=None, chat_id: str | None = None) -> str:
    action, _, item_id = data.partition(":")
    if action == "noop":
        return "확인함"
    if action not in ("retry", "skip", "done") or not item_id:
        return "알 수 없는 버튼"
    if message_id is not None:
        entry = lookup_message(message_id, chat_id=chat_id)
        if not entry or entry.get("item_id") != item_id:
            raise RuntimeError("유효하지 않은 메시지입니다.")
        expected_state = entry["state"]
        if (expected_state == "owner" and action == "done") or (expected_state == "manual" and action == "retry"):
            raise RuntimeError("이 메시지에 맞지 않는 동작입니다.")
    else:
        bid, cid = _current_binding(chat_id)
        msgs = _load_map(bid, cid) if (bid and cid) else {}
        mapped = [v["state"] for v in msgs.values() if isinstance(v, dict) and v.get("item_id") == item_id]
        if action == "retry":
            expected_state = "owner"
        elif action == "done":
            expected_state = "manual"
        elif mapped:
            expected_state = mapped[-1]
        else:
            cur = current_item_state(item_id)
            if cur not in ("owner", "manual"):
                raise RuntimeError(f"항목 상태가 이미 바뀌었습니다 ({cur or '없음'}).")
            expected_state = cur
    r = act(action, item_id, expected_state=expected_state)
    if not r.get("ok", False):
        raise RuntimeError(r.get("error") or "엔진이 거절함")
    return {"retry": "다시 시도 목록에 넣음", "skip": "제외함", "done": "완료로 표시함"}[action]


class Watcher(threading.Thread):
    def __init__(self, log, every=60):
        super().__init__(daemon=True, name="notify-watcher")
        self.log, self.every = log, every
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(self.every):
            try:
                self.once()
            except Exception as e:
                self.log(f"notify: {type(e).__name__}: {e}")

    def once(self) -> int:
        s = settings.load()
        ch = channel(s)
        if ch != "telegram":
            _load_map(*_current_binding())
        if ch == "none":
            return 0
        if ch == "telegram":
            _load_map(*_current_binding())
        seen, sent, mail = _load_seen(), 0, []
        pools = [(state, engine.items(state, 30)) for state in ("owner", "manual")]
        if (s.get("modes") or {}).get("contests"):
            pools += [(state, contest_items(state)) for state in ("owner", "manual")]
        for state, rows in pools:
            for it in rows:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                key = f"{it['id']}:{state}"
                if key in seen:
                    continue
                it.setdefault("state", state)
                text = describe(it)
                if ch == "email":
                    mail.append(text)
                    seen.add(key)
                    sent += 1
                    continue
                if state == "owner":
                    text += "\n\n이 메시지에 답장하면 그 내용이 AI에게 전달되고 다시 시도합니다."
                msg = telegram.send(text, buttons(it))
                if isinstance(msg, dict) and msg.get("message_id"):
                    remember(msg["message_id"], it["id"], state)
                seen.add(key)
                sent += 1
        if mail:
            from app import mailer
            mailer.send(f"Entry Pilot: 확인할 일 {len(mail)}건",
                        "\n\n".join(mail) + "\n\n답은 Entry Pilot 창의 '확인할 일' 탭에서 합니다.")
        if sent:
            _save_seen(seen)
        if (s.get("modes") or {}).get("contests"):
            sent += send_outbox(ch)
        return sent


def channel(s: dict) -> str:
    """telegram / email / none. Older settings without a choice: telegram when a chat is connected."""
    ch = s.get("notify") or ("telegram" if s.get("telegram_chat_id") else "none")
    if ch == "telegram" and not (s.get("telegram_token") and s.get("telegram_chat_id")):
        return "none"
    if ch == "email" and not (s.get("email_address") and s.get("email_password")):
        return "none"
    return ch


def contest_items(state: str, limit: int = 30) -> list:
    """Contest items in one state (owner: the AI asks, manual: only the user can do it)."""
    try:
        engine.configure_contest()
        from cp import store
        db = store.connect()
        try:
            return store.items(db, "state=?", (state,))[:limit]
        finally:
            db.close()
    except Exception:
        return []


def outbox_dir() -> str:
    return os.path.join(engine.contest_dir(), "notices")


def send_outbox(ch: str) -> int:
    """Send the notices the contest engine queued (cp/notify.py) and delete each file once the send is confirmed.
    With no channel (none) the files stay; only the newest 50 are kept so they cannot pile up."""
    d = outbox_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return 0
    for old in names[:-50]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    names = names[-50:]
    if ch == "none":
        return 0
    sent = 0
    for n in names:
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                m = json.load(f)
            text = str(m.get("text") or "")[:3900]
            if ch == "email":
                from app import mailer
                mailer.send(str(m.get("subject") or "공모전 알림"), text)
            else:
                telegram.send((str(m.get("subject") or "") + chr(10) * 2 + text).strip()[:4000])
            os.remove(path)
            sent += 1
        except Exception as e:  # kept in the outbox and tried again next minute; the failure is visible in the app log
            raise RuntimeError(f"contest notice {n}: {type(e).__name__}: {e}") from None
    return sent
