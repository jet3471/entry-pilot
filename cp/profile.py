"""Profile views per region (kr/en/jp) for forms. Values are never printed to logs by this module.

view(region) returns only fields a form may ask for; passwords, notes and `_note` markers are never included.
"""
import json
import os

from cp import config

KR_FIELDS = ("name", "birth", "gender", "age", "adult", "youth_19_34", "phone", "email", "address", "postcode",
             "residence_region", "occupation", "job_role", "org_size", "student",
             "school", "major", "grade", "nationality")
COMMON_FIELDS = ("email", "birth", "gender", "age", "adult", "school", "major", "grade", "nationality")
NEVER = ("comment_password", "comment_password_note", "notes", "note", "_note", "password")


def load(path: str | None = None) -> dict:
    path = path or config.profile_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _clean(v):
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if k not in NEVER and "password" not in k and x not in (None, "")}
    return v


def _sns(p: dict) -> dict:
    out = {}
    for site, d in (p.get("sns_handles") or {}).items():
        if isinstance(d, dict):
            c = {k: v for k, v in _clean(d).items() if k in ("handle", "id", "name", "channel", "account_name", "profile")}
            if c:
                out[site] = c
    return out


def view(region: str = "KR", profile: dict | None = None) -> dict:
    p = profile if profile is not None else load()
    region = (region or "KR").upper()
    if region == "KR":
        v = {k: p[k] for k in KR_FIELDS if k in p}
    else:
        v = {k: p[k] for k in COMMON_FIELDS if k in p}
        en = _clean(p.get("en") or {})
        if region == "JP":
            jp = _clean(p.get("jp") or {})
            v.update(jp)
            if en.get("phone_intl"):
                v["phone_intl"] = en["phone_intl"]
        else:
            v.update(en)
    sns = _sns(p)
    if sns:
        v["sns"] = sns
    return _clean(v)


def summary_for_prompt(region: str = "KR", profile: dict | None = None) -> str:
    """Compact 'key: value' lines for prompts (nested dicts flattened with dots)."""
    lines = []

    def walk(prefix, d):
        for k, x in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(x, dict):
                walk(key, x)
            else:
                lines.append(f"{key}: {x}")

    walk("", view(region, profile))
    return "\n".join(lines)
