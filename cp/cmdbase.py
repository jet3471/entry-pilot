"""Shared pieces of the item commands: the error type, argument helpers, the transition check, expiry."""
import json

from cp import config, model, store


LIST_FIELDS = ("id", "region", "state", "lane", "site_kind", "title", "org", "url", "deadline", "deadline_time",
               "need", "reason", "question", "claimed_by", "eligibility", "flags", "submitted_by", "submitted_at")
ADD_FIELDS = ("title", "org", "url", "region", "form_type", "deadline", "deadline_time", "prize", "login_required",
              "source_url", "conditions", "result_date", "winners", "prize_min_value", "all_rewarded", "effort_min",
              "eligibility", "notes", "tz", "title_en",
              # contest fields (stored in item.work)
              "work_type", "theme", "format", "submit_method", "top_prize", "top_prize_krw", "team_size")
WORK_FIELDS = ("work_type", "theme", "format", "submit_method", "top_prize", "top_prize_krw", "team_size")
WORK_TYPES = ("essay", "story", "slogan", "naming", "idea", "plan", "design", "photo", "video", "poster", "code", "other")
ELIGIBILITY = ("open", "residency_required", "unknown")


class CmdError(Exception):
    pass


def now():
    return store.now_iso()


def _need_item(db, iid):
    it = store.get_item(db, iid)
    if not it:
        raise CmdError(f"no item {iid}")
    return it


def _check(action, it, agent):
    err = model.check_transition(action, it, agent)
    if err:
        raise CmdError(err)


def _agent(a, required=False):
    ag = getattr(a, "agent", None)
    if ag == "owner":
        raise CmdError("CLI cannot act as owner; use the app")
    if required and not ag:
        raise CmdError(f"{a.cmd} needs --agent ({'|'.join(config.AGENTS)})")
    return ag or "cli"


def _append(old, new):
    return ((old or "") + " | " + new).strip(" |") if new else (old or "")


def _load_json_arg(a):
    if getattr(a, "json", None):
        data = json.loads(a.json)
    elif getattr(a, "file", None):
        with open(a.file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        raise CmdError("give --json or --file")
    return [data] if isinstance(data, dict) else data


def expire_due(db, agent="system") -> int:
    """Exclude items whose deadline already passed (item tz; deadline_time or end of day). Submitting is left alone."""
    n = 0
    marks = ",".join("?" * len(model.EXPIRABLE))
    for it in store.items(db, f"state IN ({marks}) AND deadline != ''", model.EXPIRABLE):
        if model.is_expired(it):
            store.update_item(db, it["id"], state="excluded", reason="expired", claimed_by=None, claimed_at=None)
            store.add_history(db, it["id"], agent, "expired",
                              f"deadline {it['deadline']} {it.get('deadline_time') or 'end of day'} {model.item_tz(it)}")
            n += 1
    return n
