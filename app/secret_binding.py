"""App-only in-process binding of existing unbound site secrets.

Agents cannot bind existing secrets from the CLI (`secret bind` refuses); only the user can bind an existing
unbound key to an exact ASCII https login host via the settings screen (`POST /api/secrets/bind`).
`GET /api/secrets/unbound` returns key names only — never secret values, lengths, or ciphertexts.
"""
import os

from app import engine, settings
from cp import secrets as cp_sec
from ep import secrets as ep_sec


def _configure_stores() -> bool:
    if ep_sec.SECRETS == ep_sec._INIT_SECRETS and ep_sec.AUTO == ep_sec._INIT_AUTO:
        engine.configure()
    cp_custom = cp_sec.SECRETS != cp_sec._INIT_SECRETS or cp_sec.AUTO != cp_sec._INIT_AUTO
    if not cp_custom:
        engine.configure_contest()
    modes = (settings.load().get("modes") or {})
    return bool(
        cp_custom
        or os.path.exists(cp_sec._secrets_path())
        or os.path.exists(cp_sec._auto_path())
        or modes.get("contests")
    )


def unbound_secrets() -> dict:
    """Return unbound custom secret key names only (never values)."""
    cp_active = _configure_stores()
    ep_keys = ep_sec.unbound_keys()
    cp_keys = cp_sec.unbound_keys() if cp_active else []
    merged = sorted(set(ep_keys) | set(cp_keys))
    return {
        "ok": True,
        "keys": merged,
        "events": ep_keys,
        "contests": cp_keys,
    }


def bind_secret(key: str, domain: str, which: str = "") -> dict:
    """Bind an existing unbound secret to an exact ASCII https login host (in-process app call only)."""
    if not isinstance(key, str) or not key.strip():
        return {"ok": False, "error": "key is required"}
    if not isinstance(domain, str) or not domain.strip():
        return {"ok": False, "error": "domain is required"}
    k = key.strip()
    dom_raw = domain.strip()
    which_norm = (which or "").strip().lower()
    if which_norm not in ("", "events", "contests"):
        return {"ok": False, "error": "invalid engine"}

    cp_active = _configure_stores()
    if which_norm == "events":
        return ep_sec.owner_bind_domain(k, dom_raw)
    if which_norm == "contests":
        return cp_sec.owner_bind_domain(k, dom_raw)

    if os.path.exists(ep_sec._secrets_path()) or os.path.exists(ep_sec._auto_path()):
        ep_sec.migrate()
    if cp_active and (os.path.exists(cp_sec._secrets_path()) or os.path.exists(cp_sec._auto_path())):
        cp_sec.migrate()

    ep_has = bool((ep_sec.load().get(k) or "").strip())
    cp_has = bool(cp_active and (cp_sec.load().get(k) or "").strip())
    if ep_has and cp_has:
        r_ep = ep_sec.owner_bind_domain(k, dom_raw)
        if not r_ep.get("ok"):
            return r_ep
        return cp_sec.owner_bind_domain(k, dom_raw)
    if cp_has:
        return cp_sec.owner_bind_domain(k, dom_raw)
    return ep_sec.owner_bind_domain(k, dom_raw)
