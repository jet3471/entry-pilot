"""settings.json: plain values plus DPAPI-protected secrets (keys listed in SECRET_KEYS)."""
import contextlib
import json
import msvcrt
import os
import threading
import time

from app import paths, secure

SECRET_KEYS = {
    "telegram_token", "telegram_start", "gemini_api_key",
    "openai_api_key", "anthropic_api_key", "email_password",
}
_INMEM_PLAIN_SECRETS = ("telegram_start",)
# External tools the author uses privately; they will be published later. The keys are reserved so a later version can wire
# them; "enabled" cannot be turned on yet (the app and both engines have no code that talks to them).
INTEGRATIONS = ("lch_wiki", "llm_relay", "llm_console")
DEFAULTS = {
    "version": paths.VERSION,
    "modes": {"events": True, "contests": False},
    "agents": {"claude": False, "codex": False, "gemini": False},
    "accounts": {"claude": 1, "codex": 1},  # how many accounts of each AI the user logs in (rotated on limits)
    "telegram_chat_id": "",
    "notify": "",           # where notices go: telegram, email, none ("" = not chosen yet)
    "email_address": "",     # the Gmail that sends notices
    "email_to": "",          # the inbox that receives them (any address; empty: the sending Gmail)
    "always_approve": True,
    "max_agents": 3,
    "min_prize_krw": 0,
    "interests": "",
    "contest_chosen": False,  # the user saved the "공모전 고르기" step (values live in data\policy.json)
    "integrations": {name: {"enabled": False, "url": ""} for name in INTEGRATIONS},
    "setup_done": False,
    "chrome": "",
}
_lock = threading.Lock()


def _fresh_defaults() -> dict:
    out = {}
    for k, v in DEFAULTS.items():
        if k == "integrations":
            out[k] = {n: dict(cfg) for n, cfg in v.items()}
        elif isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    return out


@contextlib.contextmanager
def _file_lock(wait_sec: float = 30.0):
    """OS byte-range lock on settings.json.lock: automatically released by Windows if the process dies."""
    lp = paths.settings_path() + ".lock"
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    with _lock:
        t0 = time.monotonic()
        fd = -1
        locked = False
        try:
            while True:
                try:
                    if fd < 0:
                        fd = os.open(lp, os.O_CREAT | os.O_RDWR)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() - t0 > wait_sec:
                        raise OSError(f"settings lock timed out: {lp}")
                    time.sleep(0.02)
            yield
        finally:
            if fd >= 0:
                if locked:
                    with contextlib.suppress(OSError):
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                with contextlib.suppress(OSError):
                    os.close(fd)


def _protect_verified(plain: str) -> str:
    enc = secure.protect(plain)
    if secure.unprotect(enc) != plain:
        raise OSError("DPAPI verification failed")
    return enc


def _write_atomic(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        secure.restrict_acl(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _read_raw(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _normalize_loaded(s: dict) -> dict:
    ig = s.get("integrations") if isinstance(s.get("integrations"), dict) else {}
    s["integrations"] = {n: {"enabled": False, "url": str((ig.get(n) or {}).get("url") or "")} for n in INTEGRATIONS}
    for k in _INMEM_PLAIN_SECRETS:
        v = s.get(k)
        if isinstance(v, str) and v.startswith("dpapi:"):
            try:
                s[k] = secure.unprotect(v)
            except (OSError, ValueError):
                s[k] = ""
    return s


def load() -> dict:
    s = _fresh_defaults()
    path = paths.settings_path()
    raw = _read_raw(path)
    if raw is not None:
        plain_keys = [k for k in SECRET_KEYS if isinstance(raw.get(k), str) and raw[k] and not raw[k].startswith("dpapi:")]
        if plain_keys:
            with _file_lock():
                fresh = _read_raw(path)
                if fresh is not None:
                    raw = fresh
                changed = False
                for k in SECRET_KEYS:
                    v = raw.get(k)
                    if isinstance(v, str) and v and not v.startswith("dpapi:"):
                        raw[k] = _protect_verified(v)
                        changed = True
                if changed:
                    _write_atomic(path, raw)
        s.update(raw)
    return _normalize_loaded(s)


def save(s: dict):
    out = dict(s)
    for k in SECRET_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v and not v.startswith("dpapi:"):
            out[k] = _protect_verified(v)
            if k not in _INMEM_PLAIN_SECRETS:
                s[k] = out[k]
    with _file_lock():
        _write_atomic(paths.settings_path(), out)


def update(**kv) -> dict:
    with _file_lock():
        s = _fresh_defaults()
        raw = _read_raw(paths.settings_path())
        if raw is not None:
            s.update(raw)
        s.update(kv)
        for k in SECRET_KEYS:
            v = s.get(k)
            if isinstance(v, str) and v and not v.startswith("dpapi:"):
                s[k] = _protect_verified(v)
        ig = s.get("integrations") if isinstance(s.get("integrations"), dict) else {}
        s["integrations"] = {n: {"enabled": False, "url": str((ig.get(n) or {}).get("url") or "")} for n in INTEGRATIONS}
        _write_atomic(paths.settings_path(), s)
        return _normalize_loaded(dict(s))


NESTED_KEYS = ("modes", "agents", "accounts", "integrations", "interests")


def _merge_dict(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def merge(changes: dict) -> dict:
    """Merge only the provided changed keys (and sub-keys of nested dicts) into settings.json inside the OS lock."""
    with _file_lock():
        s = _fresh_defaults()
        raw = _read_raw(paths.settings_path())
        if raw is not None:
            for k, v in raw.items():
                if k in NESTED_KEYS and isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k] = _merge_dict(s[k], v)
                else:
                    s[k] = v
        for k, v in (changes or {}).items():
            if k in NESTED_KEYS and isinstance(v, dict):
                cur = s.get(k)
                s[k] = _merge_dict(cur if isinstance(cur, dict) else {}, v)
            else:
                s[k] = v
        for k in SECRET_KEYS:
            v = s.get(k)
            if isinstance(v, str) and v and not v.startswith("dpapi:"):
                s[k] = _protect_verified(v)
        ig = s.get("integrations") if isinstance(s.get("integrations"), dict) else {}
        s["integrations"] = {n: {"enabled": False, "url": str((ig.get(n) or {}).get("url") or "")} for n in INTEGRATIONS}
        _write_atomic(paths.settings_path(), s)
        return _normalize_loaded(dict(s))


def set_secret(key: str, value: str):
    assert key in SECRET_KEYS
    enc = _protect_verified(value) if value else ""
    update(**{key: enc})


def get_secret(key: str) -> str:
    v = load().get(key) or ""
    if not v or not isinstance(v, str):
        return ""
    if not v.startswith("dpapi:"):
        return v
    try:
        return secure.unprotect(v)
    except (OSError, ValueError):
        return ""


def public(s: dict | None = None) -> dict:
    """Settings for the UI: secrets replaced by a saved/not saved flag."""
    s = dict(s or load())
    for k in SECRET_KEYS:
        s[k] = bool(s.get(k))
    return s
