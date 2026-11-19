"""The contest work flow of one item: verify with the analysis dossier, the user's accept or re-analysis, the audit
verdict, draft claim and draft-done, the user's approval or revision request."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

from cp import config, model, policy, preview, store
from cp.cmdbase import CmdError, WORK_TYPES, _agent, _append, _check, _need_item, expire_due, now
from cp.intake import DupIndex, _work_from

FROZEN_KV_PREFIX = "frozen_submission."
SENT_KV_PREFIX = "sent_report."
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


class StaleDraftError(CmdError):
    """The draft on disk changed since the user opened the review screen (or no hash was sent)."""


class InvalidSendSpecError(CmdError):
    """SEND.json failed validation when freezing at approval; the item was sent back for revision."""


def cmd_verify(db, a):
    agent = _agent(a)
    it = _need_item(db, a.id)
    _check("verify", it, agent)
    upd = {}
    if a.url and model.norm_url(a.url) != model.norm_url(it.get("url")):
        dup = DupIndex(db).find(a.url)
        if dup and dup["id"] != it["id"]:
            reason = f"duplicate of {dup['id']}"
            store.update_item(db, it["id"], state="excluded", reason=reason, claimed_by=None, claimed_at=None)
            store.add_history(db, it["id"], agent, "exclude", reason)
            return {"ok": True, "id": it["id"], "state": "excluded", "duplicate_of": dup["id"]}
        alt = list(it.get("alt_urls") or [])
        if it.get("url"):
            alt.append(it["url"])
        upd.update(url=a.url, alt_urls=alt, site_kind=model.site_kind(a.url, it.get("form_type")),
                   site_host=model.url_host(a.url))
    if a.answers:
        try:
            upd["answers"] = json.loads(a.answers)
        except ValueError as e:
            raise CmdError(f"--answers is not JSON: {e}")
    if a.notes:
        upd["verify_notes"] = a.notes
    if a.result_date:
        upd["result_date"] = a.result_date
    if a.eligibility:
        upd["eligibility"] = a.eligibility
    if (a.eligibility or it.get("eligibility")) == "residency_required" and it.get("region") != "KR":
        # (assumption) a JP/US residency restriction can never be met by the user (Korean resident): exclude
        # instead of queueing a submit. KR items are kept: Korean residency ("국내 거주") is satisfied.
        upd.update(state="excluded", reason="residency required", lane=a.lane, claimed_by=None, claimed_at=None)
        store.update_item(db, it["id"], **upd)
        store.add_history(db, it["id"], agent, "exclude", "residency required (at verify)")
        return {"ok": True, "id": it["id"], "state": "excluded", "reason": "residency required"}
    old_work = it.get("work") or {}
    if a.no_work and (old_work.get("accepted_at") or old_work.get("drafted_at")
                      or old_work.get("draft_dir") or old_work.get("draft_files")):
        raise CmdError(f"{it['id']} already has an accepted or drafted work; cannot mark --no-work")
    if not a.no_work and not (a.work_type or old_work.get("work_type")):
        raise CmdError("verify needs --work-type (" + "|".join(WORK_TYPES) + ") so the work gets drafted, "
                       "or --no-work when the entry needs no written work (quiz, survey, vote)")
    wd = {"work_type": a.work_type, "theme": a.theme, "format": a.format, "submit_method": a.submit_method,
          "top_prize": a.top_prize, "top_prize_krw": a.top_prize_krw, "team_size": a.team_size}
    work = _work_from(wd, old_work)
    if a.no_work:
        state = "ready"
        work["no_work"] = True
    else:
        state = "proposal"
        work.pop("no_work", None)
        clear_approval(db, it["id"], work)
        for k in ("accepted_at", "accepted_by", "accepted_top_prize_krw", "accepted_top_prize",
                  "prize_changed_after_accept", "reapproval_note", "reapproval_requested_at"):
            work.pop(k, None)
        pol = policy.active_policy()
        work["analysis_path"] = _store_analysis(it["id"], getattr(a, "analysis_file", None))
        work.update(analysis_by=agent, analysis_at=now(), analysis_note=None, audit=None, notified_proposal=None,
                    notified_review=None)
        work["audit_required"] = bool(pol.get("audit_enabled", False)
                                      and any(x in config.AUDIT_AGENTS for x in config.ENABLED_AGENTS)
                                      and policy.is_big({**it, "work": work}, pol))
    upd.update(state=state, lane=a.lane, claimed_by=None, claimed_at=None, need=None, question=None, work=work)
    store.update_item(db, it["id"], **upd)
    store.add_history(db, it["id"], agent, "verify", f"lane={a.lane}; " + (
        "no work to draft" if a.no_work else f"work_type={work.get('work_type')}; analysis written"
        + ("; audit required" if work.get("audit_required") else "")))
    return {"ok": True, "id": it["id"], "state": state, "lane": a.lane, "work": work}


ANALYSIS_SECTIONS = (  # (heading keyword that must appear in a "## " line, what goes there)
    ("contest", "what the contest is: theme, work kind, dates, prizes"),
    ("organizer", "who runs it and who funds it"),
    ("stated", "the organizer's stated purpose and what they say they want"),
    ("hidden", "the likely hidden intent (marketing, content harvesting, rights, data, judging bias); label every guess"),
    ("rules", "eligibility, required format, length, files, copyright and reuse terms, AI-use terms, limits per person"),
    ("approach", "the recommended approach and why, with alternatives rejected"),
    ("needs", "programs, accounts, facts or decisions needed from the user (write 'none' when none)"),
    ("summary", "Korean summary for the user, 3 to 6 lines"),
)


def _store_analysis(item_id: str, path) -> str:
    """Validate the analysis dossier file and keep a copy at <data>\\analysis\\<ID>.md. Returns the stored path."""
    if not path:
        raise CmdError("verify needs --analysis-file PATH (the analysis dossier, Markdown), or --no-work. Required "
                       "'## ' sections: " + "; ".join(f"{k} ({d})" for k, d in ANALYSIS_SECTIONS))
    if preview.is_reparse(path) or not os.path.isfile(path):
        raise CmdError(f"--analysis-file {path} does not exist")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    heads = [ln[3:].strip().lower() for ln in text.splitlines() if ln.startswith("## ")]
    missing = [k for k, _ in ANALYSIS_SECTIONS if not any(k in h for h in heads)]
    if missing:
        raise CmdError("analysis file lacks '## ' sections for: " + ", ".join(missing))
    if len(text.strip()) < 600:
        raise CmdError("analysis file is too short (under 600 characters); write each section")
    dest_dir = config.analysis_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{item_id}.md")
    if preview.is_reparse(dest_dir) or preview.is_reparse(dest):
        raise CmdError("analysis destination cannot be a link")
    if os.path.abspath(path) != os.path.abspath(dest):
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return dest


def cmd_draft_claim(db, a):
    """draft -> drafting for the soonest-deadline item (or --id). Counts tries.draft."""
    agent = _agent(a, required=True)
    exp = expire_due(db)
    if a.id:
        cands = [_need_item(db, a.id)]
        _check("draft-start", cands[0], agent)
    else:
        cands = [it for it in store.items(db, "state='draft'")
                 if (it.get("tries") or {}).get("draft", 0) < config.DRAFT_MAX_TRIES]
        cands.sort(key=model.order_key)
    if not cands:
        return {"ok": True, "item": None, "reason": "no draft item", "expired_now": exp}
    it = cands[0]
    tries = dict(it.get("tries") or {})
    tries["draft"] = tries.get("draft", 0) + 1
    store.update_item(db, it["id"], state="drafting", claimed_by=agent, claimed_at=now(), tries=tries)
    store.add_history(db, it["id"], agent, "draft-start", f"try {tries['draft']}")
    return {"ok": True, "item": store.get_item(db, it["id"]), "expired_now": exp}


def draft_dir_for(item_id: str) -> str:
    if not preview._valid_single_name(item_id):
        raise CmdError(f"invalid item id {item_id!r}")
    return os.path.join(config.drafts_dir(), item_id)


def _validated_draft_files(item_id: str | None, raw_dir: str) -> tuple[str, list[str]]:
    """Validate `raw_dir` is a real directory inside config.drafts_dir() with no symlinks/junctions anywhere.
    Real subfolders and dot entries are ignored; file count and per-file size limits are enforced."""
    return _validated_dir_files(
        "draft", preview.safe_draft_dir(raw_dir, item_id), raw_dir, "drafts root",
        lambda d, entry: preview.safe_draft_file(d, entry, item_id))


def _validated_dir_files(kind: str, d: str | None, raw_dir: str, root_name: str, file_checker) -> tuple[str, list[str]]:
    """Shared walk for draft and snapshot folders: no links, dot entries and subfolders skipped, size and count limits.
    `file_checker(d, entry)` returns the safe file path or a falsy value."""
    if not d:
        raise CmdError(f"{kind} folder {raw_dir} is missing, outside {root_name}, or a link")
    files = []
    for entry in sorted(os.listdir(d)):
        p = os.path.join(d, entry)
        if preview.is_reparse(p):
            raise CmdError(f"{kind} entry {entry} cannot be a link")
        if entry.startswith(".") or os.path.isdir(p):
            continue
        fp = file_checker(d, entry)
        if not fp:
            raise CmdError(f"{kind} entry {entry} is not a regular file inside the {kind} folder")
        sz = os.path.getsize(fp)
        if sz > preview.MAX_FILE_BYTES:
            raise CmdError(f"{kind} file {entry} exceeds {preview.MAX_FILE_BYTES // (1024 * 1024)} MB limit ({sz} bytes)")
        files.append(entry)
        if len(files) > preview.MAX_DRAFT_FILES:
            raise CmdError(f"{kind} folder has too many files (limit {preview.MAX_DRAFT_FILES})")
    return d, files


def _validated_snapshot_files(item_id: str | None, raw_dir: str,
                              expected_hash: str | None = None) -> tuple[str, list[str]]:
    """Validate `raw_dir` is an app-owned snapshot folder `<data>/snapshots/<ID>/<sha256>` with regular files only."""
    return _validated_dir_files(
        "snapshot", preview.safe_snapshot_dir(raw_dir, item_id, expected_hash), raw_dir, "snapshots root",
        lambda d, entry: preview.safe_snapshot_file(d, entry, item_id, expected_hash))


def _sanitized_send_from_file(sfp: str) -> dict:
    """Parse SEND.json at `sfp` with the same normalization as preview.load()."""
    return preview.lenient_send_from_file(sfp)


def _compute_folder_hash(dir_real: str, files: list[str]) -> str:
    """Deterministic SHA-256 over SEND.json metadata and every regular file's bytes inside `dir_real`."""
    if not files:
        raise CmdError("draft folder has no files")
    sfp = os.path.join(dir_real, preview.NAME) if preview.NAME in files else ""
    send = _sanitized_send_from_file(sfp) if sfp else {}
    to_val = send.get("to")
    to_meta = list(to_val) if isinstance(to_val, list) else str(to_val or "")
    h = hashlib.sha256()
    meta = {
        "how": str(send.get("how") or ""),
        "to": to_meta,
        "subject": str(send.get("subject") or ""),
        "body": str(send.get("body") or ""),
        "fields": send.get("fields") or [],
        "files": send.get("files") or [],
        "draft_files": files,
    }
    h.update(json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for name in files:
        fp = os.path.join(dir_real, name)
        if not preview._valid_single_name(name) or preview.is_reparse(fp) or not os.path.isfile(fp):
            raise CmdError(f"draft file {name} is invalid or a link")
        st_size = os.path.getsize(fp)
        if st_size > preview.MAX_FILE_BYTES:
            raise CmdError(f"draft file {name} exceeds {preview.MAX_FILE_BYTES // (1024 * 1024)} MB limit ({st_size} bytes)")
        h.update(b"\x00FILE:" + name.encode("utf-8") + b"\x00")
        h.update(str(st_size).encode("ascii") + b":")
        total = 0
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(preview.HASH_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > preview.MAX_FILE_BYTES:
                    raise CmdError(f"draft file {name} exceeds {preview.MAX_FILE_BYTES // (1024 * 1024)} MB limit")
                h.update(chunk)
        if total != st_size:
            raise CmdError(f"draft file {name} changed size while hashing")
    return h.hexdigest()


def create_snapshot(item_id: str, raw_draft_dir: str | None = None, *, require_valid_send: bool = True) -> tuple[str, list[str], str]:
    """Copy regular draft files into `<data>/snapshots/<ID>/<hash>/` outside the agent-writable draft folder,
    compute the hash from the copied snapshot, and validate SEND.json on that snapshot."""
    if not preview._valid_single_name(item_id):
        raise CmdError(f"invalid item id {item_id!r}")
    src_dir, src_files = _validated_draft_files(item_id, raw_draft_dir or draft_dir_for(item_id))
    if not src_files:
        raise CmdError("draft folder has no files")
    data_root = config.data_dir()
    snap_root = preview.snapshots_dir()
    item_root = os.path.join(snap_root, item_id)
    for folder in (data_root, snap_root, item_root):
        if os.path.lexists(folder) and preview.is_reparse(folder):
            raise CmdError(f"snapshot path cannot be a link: {folder}")
        os.makedirs(folder, exist_ok=True)
        if preview.is_reparse(folder):
            raise CmdError(f"snapshot path cannot be a link: {folder}")
    stage_dir = os.path.join(item_root, f".stage-{os.getpid()}-{os.urandom(8).hex()}")
    os.makedirs(stage_dir, exist_ok=False)
    try:
        if preview.is_reparse(stage_dir):
            raise CmdError("snapshot staging folder cannot be a link")
        for name in src_files:
            src_fp = preview.safe_draft_file(src_dir, name, item_id)
            if not src_fp:
                raise CmdError(f"draft file {name} is invalid or a link")
            dst_fp = os.path.join(stage_dir, name)
            total = 0
            with open(src_fp, "rb") as rf, open(dst_fp, "wb") as wf:
                while True:
                    chunk = rf.read(preview.HASH_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > preview.MAX_FILE_BYTES:
                        raise CmdError(f"draft file {name} exceeds {preview.MAX_FILE_BYTES // (1024 * 1024)} MB limit")
                    wf.write(chunk)
            if preview.is_reparse(src_fp) or preview.is_reparse(dst_fp):
                raise CmdError(f"draft file {name} cannot be a link")
        snap_hash = _compute_folder_hash(stage_dir, src_files)
        final_dir = os.path.join(item_root, snap_hash)
        if os.path.lexists(final_dir):
            if preview.is_reparse(final_dir):
                raise CmdError("snapshot destination cannot be a link")
            shutil.rmtree(final_dir)
        os.replace(stage_dir, final_dir)
    finally:
        if os.path.lexists(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
    safe_dir = preview.safe_snapshot_dir(final_dir, item_id, snap_hash)
    if not safe_dir:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise CmdError("snapshot folder validation failed")
    if require_valid_send:
        bad = preview.problems(safe_dir, src_files)
        if bad:
            shutil.rmtree(safe_dir, ignore_errors=True)
            raise CmdError("submission preview not ready: " + "; ".join(bad))
    if _compute_folder_hash(safe_dir, src_files) != snap_hash:
        shutil.rmtree(safe_dir, ignore_errors=True)
        raise CmdError("snapshot hash changed during creation")
    return safe_dir, list(src_files), snap_hash


def draft_hash(work: dict, item_id: str | None = None) -> str:
    """SHA-256 digest of everything approved: recipient/how/subject/fields/body/files from SEND.json and every file's bytes."""
    snap_raw = (work or {}).get("snapshot_dir")
    if snap_raw:
        d, files = _validated_snapshot_files(item_id, snap_raw)
        return _compute_folder_hash(d, files)
    raw_dir = (work or {}).get("draft_dir") or (draft_dir_for(item_id) if item_id else "")
    d, files = _validated_draft_files(item_id, raw_dir)
    return _compute_folder_hash(d, files)


def _canon_method(how: str, fallback: str = "") -> str:
    s = (how or "").strip().lower()
    if s in ("이메일", "email", "mail"):
        return "email"
    if s in ("누리집 파일 올리기", "upload"):
        return "upload"
    if s in ("누리집 양식 입력", "online_form", "web_form", "form"):
        return "form"
    fb = (fallback or "").strip().lower()
    if fb in ("email", "form", "upload"):
        return fb
    return s or fb


def freeze_submission(work: dict, item_id: str, approved_by: str, draft_digest: str | None = None,
                      at: str | None = None) -> dict:
    """Validate SEND.json in the app-owned snapshot and build the immutable frozen submission record."""
    snap_raw = (work or {}).get("snapshot_dir")
    try:
        if snap_raw:
            d, files = _validated_snapshot_files(item_id, snap_raw)
        else:
            d, files, _ = create_snapshot(item_id, (work or {}).get("draft_dir") or draft_dir_for(item_id))
    except CmdError as e:
        raise InvalidSendSpecError(str(e)) from e
    if not files:
        raise InvalidSendSpecError("draft folder has no files")
    send, bad = preview.validate_send(d, files)
    if bad or send is None:
        raise InvalidSendSpecError("; ".join(bad or ["invalid SEND.json"]))
    digest = draft_digest or _compute_folder_hash(d, files)
    attachments = []
    file_hashes = {}
    for name in send["files"]:
        fp = preview.safe_snapshot_file(d, name, item_id)
        if not fp:
            raise InvalidSendSpecError(f"`files` names {name}, which is not a valid file in the snapshot folder")
        try:
            f_sha, f_sz = preview.sha256_file(fp)
        except (OSError, ValueError) as e:
            raise InvalidSendSpecError(f"cannot hash attachment {name}: {e}") from e
        attachments.append({"name": name, "path": fp, "sha256": f_sha, "size": f_sz})
        file_hashes[name] = f_sha
    recip = list(send["to"]) if isinstance(send["to"], list) else send["to"]
    ts = at or now()
    return {
        "item_id": item_id,
        "method": _canon_method(send["how"], str((work or {}).get("submit_method") or "")),
        "how": send["how"],
        "recipient": recip,
        "to": list(recip) if isinstance(recip, list) else recip,
        "subject": send["subject"],
        "body": send["body"],
        "fields": [[k, v] for k, v in send["fields"]],
        "files": list(send["files"]),
        "snapshot_dir": d,
        "attachments": attachments,
        "file_hashes": file_hashes,
        "draft_hash": digest,
        "approved_by": approved_by,
        "frozen_at": ts,
    }


def record_approval(db, item_id: str, work: dict, *, by: str, policy_name: str | None = None,
                    expected_hash: str | None = None) -> dict:
    """Validate snapshot & SEND.json, record {by, policy, hash, at} and freeze the immutable submission record in DB."""
    if work.get("snapshot_dir"):
        try:
            d, files = _validated_snapshot_files(item_id, work["snapshot_dir"])
        except CmdError as e:
            raise InvalidSendSpecError(str(e)) from e
    else:
        try:
            d, files, _ = create_snapshot(item_id, work.get("draft_dir") or draft_dir_for(item_id))
        except CmdError as e:
            raise InvalidSendSpecError(str(e)) from e
    if not files:
        raise InvalidSendSpecError("draft folder has no files")
    cur_hash = _compute_folder_hash(d, files)
    if expected_hash is not None and expected_hash != cur_hash:
        raise StaleDraftError("화면에 표시된 초안이 변경되었습니다. 화면을 새로고침해 다시 확인해 주세요.")
    ts = now()
    frozen = freeze_submission({**work, "snapshot_dir": d, "snapshot_files": files}, item_id, approved_by=by,
                               draft_digest=cur_hash, at=ts)
    rec = {"by": by, "hash": cur_hash, "at": ts}
    if policy_name is not None:
        rec["policy"] = policy_name
    work["snapshot_dir"] = d
    work["snapshot_files"] = files
    work["snapshot_hash"] = cur_hash
    work["draft_files"] = files
    work["approval"] = rec
    work["approved_by"] = by
    if policy_name is not None:
        work["approved_policy"] = policy_name
    else:
        work.pop("approved_policy", None)
    work["approved_at"] = ts
    work["approved_hash"] = cur_hash
    work["frozen_submission"] = frozen
    work.pop("reapproval_note", None)
    if by == "owner":
        work["accepted_top_prize_krw"] = policy._valid_positive_prize(work.get("top_prize_krw"))
        work["accepted_top_prize"] = work.get("top_prize")
        work.pop("prize_changed_after_accept", None)
    if db is not None:
        store.kv_set(db, FROZEN_KV_PREFIX + item_id, frozen)
        store.kv_del(db, SENT_KV_PREFIX + item_id)
    return frozen


def clear_approval(db, item_id: str | None, work: dict) -> None:
    """Drop all approval and frozen-submission fields when a draft is revised, redrafted, edited, or invalidated."""
    for k in ("approval", "approved_at", "approved_by", "approved_policy", "approved_hash", "frozen_submission"):
        work.pop(k, None)
    if db is not None and item_id:
        store.kv_del(db, FROZEN_KV_PREFIX + item_id)
        store.kv_del(db, SENT_KV_PREFIX + item_id)


def _item_has_draft(it: dict) -> bool:
    """True when `it` has drafted files or approval metadata requiring hash verification; False for no-draft items."""
    work = (it or {}).get("work") or {}
    if work.get("no_work"):
        return False
    if (work.get("snapshot_dir") or work.get("snapshot_files") or work.get("draft_files") or work.get("drafted_at")
            or work.get("approval") or work.get("approved_hash") or work.get("approved_at")
            or work.get("frozen_submission")):
        return True
    raw_dir = work.get("draft_dir")
    if not raw_dir:
        return False
    iid = (it or {}).get("id")
    d = preview.safe_draft_dir(raw_dir, iid if isinstance(iid, str) and iid else None)
    if not d:
        return True
    files, errs = preview._list_regular_files(d)
    return bool(files or errs)


def get_frozen_submission(db, it: dict) -> dict | None:
    """Return the immutable frozen submission record for `it` (preferring the DB kv store), or None when none."""
    if not it:
        return None
    iid = it.get("id")
    work = it.get("work") or {}
    kv_rec = store.kv_get(db, FROZEN_KV_PREFIX + iid) if (db is not None and iid) else None
    wk_rec = work.get("frozen_submission")
    if isinstance(kv_rec, dict):
        return kv_rec
    if isinstance(wk_rec, dict):
        return wk_rec
    return None


def needs_refreeze_review(db, it: dict) -> bool:
    """True for pre-round-2 drafts that have an approval hash but no frozen submission record."""
    if not _item_has_draft(it):
        return False
    work = (it or {}).get("work") or {}
    approval = work.get("approval") if isinstance(work.get("approval"), dict) else {}
    has_hash = bool(work.get("approved_hash") or approval.get("hash"))
    return has_hash and get_frozen_submission(db, it) is None


def send_back_for_reapproval(db, it: dict, agent: str = "system", note_text: str = "다시 확인해 주세요") -> None:
    """Send a pre-round-2 approved draft back to `review` once with note '다시 확인해 주세요'."""
    work = dict(it.get("work") or {})
    if not work.get("snapshot_dir"):
        try:
            s_dir, s_files, s_hash = create_snapshot(it["id"], work.get("draft_dir") or draft_dir_for(it["id"]),
                                                     require_valid_send=False)
            work.update(snapshot_dir=s_dir, snapshot_files=s_files, snapshot_hash=s_hash)
        except CmdError:
            pass
    clear_approval(db, it["id"], work)
    work["notified_review"] = None
    work["reapproval_note"] = note_text
    work["reapproval_requested_at"] = now()
    old_notes = it.get("notes") or ""
    new_notes = old_notes if note_text in old_notes else _append(old_notes, note_text)
    store.update_item(db, it["id"], state="review", reason=note_text, notes=new_notes, work=work,
                      claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "review", note_text)


def check_approved_draft(it: dict, db=None):
    """Raise CmdError if a drafted contest item lacks user/policy approval, frozen record, or snapshot/prize integrity."""
    if not _item_has_draft(it):
        return
    iid = (it or {}).get("id") or "?"
    work = (it or {}).get("work") or {}
    approval = work.get("approval") if isinstance(work.get("approval"), dict) else None
    if approval:
        by = approval.get("by")
        want = approval.get("hash")
        at = approval.get("at") or work.get("approved_at")
        pol_name = approval.get("policy")
        if work.get("approved_hash") and work.get("approved_hash") != want:
            raise CmdError(f"{iid} approval hash mismatch; refusing to submit")
    else:
        by = work.get("approved_by") or "owner"
        want = work.get("approved_hash")
        at = work.get("approved_at")
        pol_name = work.get("approved_policy")
    if by not in ("owner", "policy") or not at or not want:
        raise CmdError(f"{iid} has no valid approval hash; refusing to submit")
    if by == "policy":
        if not (isinstance(pol_name, str) and pol_name.strip()):
            raise CmdError(f"{iid} policy approval lacks policy name; refusing to submit")
        if policy._valid_positive_prize(work.get("accepted_top_prize_krw")) is None:
            raise CmdError(f"{iid} unknown prize requires human review; refusing policy approval")
    if policy.prize_changed_after_accept(work):
        raise CmdError(f"{iid} prize changed after acceptance; refusing to submit")
    kv_rec = store.kv_get(db, FROZEN_KV_PREFIX + iid) if (db is not None and (it or {}).get("id")) else None
    wk_rec = work.get("frozen_submission")
    if isinstance(kv_rec, dict) and isinstance(wk_rec, dict) and kv_rec != wk_rec:
        raise CmdError(f"{iid} frozen submission record tampered; refusing to submit")
    frozen = kv_rec if isinstance(kv_rec, dict) else (wk_rec if isinstance(wk_rec, dict) else None)
    if frozen is None:
        raise CmdError(f"{iid} has no frozen submission record; 다시 확인해 주세요")
    if frozen.get("draft_hash") != want:
        raise CmdError(f"{iid} frozen submission hash mismatch; refusing to submit")
    snap_raw = frozen.get("snapshot_dir") or work.get("snapshot_dir")
    if snap_raw:
        if work.get("snapshot_dir") and os.path.normcase(os.path.abspath(snap_raw)) != os.path.normcase(os.path.abspath(work["snapshot_dir"])):
            raise CmdError(f"{iid} snapshot_dir mismatch; refusing to submit")
        d = preview.safe_snapshot_dir(snap_raw, (it or {}).get("id"), want)
        if not d:
            raise CmdError(f"{iid} snapshot folder missing or invalid; refusing to submit")
        got = draft_hash({**work, "snapshot_dir": d}, (it or {}).get("id"))
        if got != want:
            raise CmdError(f"{iid} draft changed after approval; refusing to submit")
        _check_frozen_attachments(
            iid, frozen, lambda name: preview.safe_snapshot_file(d, name, (it or {}).get("id"), want), True)
    else:
        got = draft_hash(work, (it or {}).get("id"))
        if got != want:
            raise CmdError(f"{iid} draft changed after approval; refusing to submit")
        raw_dir = work.get("draft_dir") or (draft_dir_for(iid) if (it or {}).get("id") else "")
        d = preview.safe_draft_dir(raw_dir, (it or {}).get("id"))
        _check_frozen_attachments(
            iid, frozen, lambda name: preview.safe_draft_file(d, name, (it or {}).get("id")) if d else "", False)


def _check_frozen_attachments(iid: str, frozen: dict, locate, check_path: bool) -> None:
    """Every frozen attachment must exist (via `locate(name)`), keep its recorded path (snapshots only) and its SHA-256."""
    for att in frozen.get("attachments") or []:
        fp = locate(att.get("name") or "")
        if not fp:
            raise CmdError(f"{iid} frozen attachment {att.get('name')} missing; refusing to submit")
        if check_path and att.get("path") and os.path.normcase(os.path.realpath(att["path"])) != os.path.normcase(fp):
            raise CmdError(f"{iid} frozen attachment path mismatch for {att.get('name')}; refusing to submit")
        try:
            sha, _ = preview.sha256_file(fp)
        except (OSError, ValueError) as e:
            raise CmdError(f"{iid} frozen attachment {att.get('name')} unreadable: {e}") from e
        if sha != att.get("sha256"):
            raise CmdError(f"{iid} attachment {att.get('name')} changed after approval; refusing to submit")


def approved_draft_ok(it: dict, db=None) -> tuple[bool, str]:
    try:
        check_approved_draft(it, db=db)
        return True, ""
    except CmdError as e:
        return False, str(e)


def _norm_recipients(v) -> list[str]:
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    if isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
        return [x.strip() for x in v]
    raise CmdError("sent report `to`/`recipient` must be a non-empty string or list of strings")


def _is_email_target(s: str) -> bool:
    return "@" in s and "://" not in s


def _norm_nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or ""))


def _norm_body(s) -> str:
    t = unicodedata.normalize("NFC", str(s or ""))
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _norm_field_str(v) -> str:
    s = "" if v is None else str(v)
    return unicodedata.normalize("NFC", s).replace("\r\n", "\n").replace("\r", "\n")


def _same_target(want: str, got: str) -> bool:
    if _is_email_target(want) or _is_email_target(got):
        return _is_email_target(want) and _is_email_target(got) and want.strip().lower() == got.strip().lower()
    if "://" in want or "://" in got:
        return model.same_submit_url(want, got)
    return _norm_nfc(want).strip() == _norm_nfc(got).strip()


def verify_sent_report(frozen: dict, report: dict) -> None:
    """Compare what the agent reports it sent against the frozen submission record; raise CmdError on any mismatch."""
    if not isinstance(frozen, dict):
        raise CmdError("missing frozen submission record")
    if not isinstance(report, dict):
        raise CmdError("sent report must be a JSON object")
    raw_to = report.get("recipient") if "recipient" in report else report.get("to")
    if raw_to is None:
        raise CmdError("sent report missing `recipient`/`to`")
    want_list = _norm_recipients(frozen.get("recipient") if "recipient" in frozen else frozen.get("to"))
    got_list = _norm_recipients(raw_to)
    if all(_is_email_target(x) for x in want_list) and all(_is_email_target(x) for x in got_list):
        if sorted(x.strip().lower() for x in want_list) != sorted(x.strip().lower() for x in got_list):
            raise CmdError(f"sent recipient {got_list!r} does not match approved recipient {want_list!r}")
    elif len(want_list) != len(got_list) or not all(_same_target(w, g) for w, g in zip(want_list, got_list)):
        raise CmdError(f"sent recipient {got_list!r} does not match approved recipient {want_list!r}")

    got_method = report.get("method") if "method" in report else report.get("how")
    if got_method is not None:
        if not isinstance(got_method, str) or _canon_method(got_method) != _canon_method(
                frozen.get("how") or "", frozen.get("method") or ""):
            raise CmdError(f"sent method {got_method!r} does not match approved method {frozen.get('method')!r}")

    want_subj = _norm_nfc(frozen.get("subject") or "").strip()
    if want_subj:
        got_subj = report.get("subject")
        if not isinstance(got_subj, str) or _norm_nfc(got_subj).strip() != want_subj:
            raise CmdError(f"sent subject {got_subj!r} does not match approved subject {want_subj!r}")
    elif "subject" in report and report["subject"] is not None:
        got_subj = report["subject"]
        if not isinstance(got_subj, str) or _norm_nfc(got_subj).strip() != "":
            raise CmdError(f"sent subject {got_subj!r} does not match empty approved subject")

    raw_want_body = str(frozen.get("body") or "")
    norm_want_body = _norm_body(raw_want_body)
    valid_body_shas = {
        hashlib.sha256(cand.encode("utf-8")).hexdigest()
        for cand in (
            raw_want_body,
            norm_want_body,
            _norm_nfc(raw_want_body).replace("\r\n", "\n").replace("\r", "\n"),
        )
    }
    if "body" in report and report["body"] is not None:
        if not isinstance(report["body"], str) or _norm_body(report["body"]) != norm_want_body:
            raise CmdError("sent body does not match approved body")
    elif "body_sha256" in report and report["body_sha256"] is not None:
        if not isinstance(report["body_sha256"], str):
            raise CmdError("sent body_sha256 must be a string")
        got_sha = report["body_sha256"].strip().lower()
        if norm_want_body:
            if got_sha not in valid_body_shas:
                raise CmdError("sent body_sha256 does not match approved body")
        elif got_sha and got_sha not in valid_body_shas:
            raise CmdError("sent body_sha256 does not match empty approved body")
    elif norm_want_body:
        raise CmdError("sent report missing `body` or `body_sha256` for approved body")

    want_fields = [[_norm_field_str(k), _norm_field_str(v)] for k, v in (frozen.get("fields") or [])]
    if ("fields" in report and report["fields"] is not None) or (want_fields and frozen.get("method") == "form"):
        got_fields = report.get("fields")
        if (not isinstance(got_fields, list)
                or any(not (isinstance(x, (list, tuple)) and len(x) == 2
                            and x[0] is not None and not isinstance(x[0], (list, tuple, dict, bool))
                            and not isinstance(x[1], (list, tuple, dict))) for x in got_fields)):
            raise CmdError("sent `fields` must be a list of [label, value] pairs")
        if [[_norm_field_str(x[0]), _norm_field_str(x[1])] for x in got_fields] != want_fields:
            raise CmdError("sent form fields do not match approved fields")

    want_atts = frozen.get("attachments") or []
    if "attachments" in report and report["attachments"] is not None:
        got_atts = report["attachments"]
        if not isinstance(got_atts, list) or len(got_atts) != len(want_atts):
            raise CmdError("sent attachments count does not match approved attachments")
        for w_att, g_att in zip(want_atts, got_atts):
            if not isinstance(g_att, dict):
                raise CmdError("sent attachments entries must be {name, sha256}")
            if (g_att.get("name") != w_att["name"]
                    or str(g_att.get("sha256") or "").strip().lower() != w_att["sha256"]):
                raise CmdError(f"sent attachment {g_att!r} does not match approved {w_att['name']}")
    elif "files" in report and report["files"] is not None:
        got_files = report["files"]
        want_names = [x["name"] for x in want_atts]
        if not isinstance(got_files, list) or got_files != want_names:
            raise CmdError(f"sent files {got_files!r} do not match approved files {want_names!r}")
        if "file_hashes" in report:
            want_map = {x["name"]: x["sha256"] for x in want_atts}
            if report["file_hashes"] != want_map:
                raise CmdError("sent file_hashes do not match approved attachment SHA-256 digests")
    elif want_atts:
        raise CmdError("sent report missing `attachments` for approved attachment files")


def _extract_sent_report(db, item_id: str, a) -> tuple[dict | None, str]:
    """Extract optional structured sent report from CLI args, SENT_JSON:/JSON in --evidence, or DB kv; return (report, plain_ev)."""
    raw_arg = getattr(a, "sent_json", None) or getattr(a, "sent", None)
    ev = (getattr(a, "evidence", None) or "").strip()
    if raw_arg is not None:
        try:
            rep = json.loads(raw_arg) if isinstance(raw_arg, str) else raw_arg
        except ValueError as e:
            raise CmdError(f"invalid --sent-json JSON: {e}") from e
        if not isinstance(rep, dict):
            raise CmdError("--sent-json must be a JSON object")
        return rep, ev
    if "SENT_JSON:" in ev:
        idx = ev.index("SENT_JSON:") + len("SENT_JSON:")
        try:
            rep, end = json.JSONDecoder().raw_decode(ev[idx:].lstrip())
        except ValueError as e:
            raise CmdError(f"malformed SENT_JSON in --evidence: {e}") from e
        if not isinstance(rep, dict):
            raise CmdError("SENT_JSON in --evidence must be a JSON object")
        rest = ev[:ev.index("SENT_JSON:")] + " " + ev[idx:].lstrip()[end:]
        return rep, rest.strip()
    if ev.startswith("{"):
        try:
            rep = json.loads(ev)
        except ValueError as e:
            raise CmdError(f"malformed JSON in --evidence: {e}") from e
        if not isinstance(rep, dict):
            raise CmdError("JSON --evidence must be an object")
        return rep, ""
    kv_rep = store.kv_get(db, SENT_KV_PREFIX + item_id) if db is not None else None
    if kv_rep is not None:
        if not isinstance(kv_rep, dict):
            raise CmdError("stored sent_report is not a JSON object")
        return kv_rep, ev
    return None, ev


def check_sent_on_done(db, it: dict, a) -> None:
    """Require --sent-json and verify it against the frozen submission record on `cp done`."""
    frozen = get_frozen_submission(db, it)
    if frozen is None:
        if not _item_has_draft(it):
            return
        raise CmdError(f"{it['id']} has no frozen submission record; refusing done")
    if not isinstance(frozen, dict):
        raise CmdError(f"{it['id']} has an invalid frozen submission record; refusing done")
    report, _ = _extract_sent_report(db, it["id"], a)
    if report is None:
        raise CmdError(
            f"{it['id']} requires --sent-json '<JSON of what was sent>' on cp done (matching the frozen submission record)"
        )
    verify_sent_report(frozen, report)
    if db is not None:
        store.kv_del(db, SENT_KV_PREFIX + it["id"])


def cmd_draft_done(db, a):
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("draft-done", it, agent)
    raw_dir = draft_dir_for(it["id"])
    if not os.path.exists(raw_dir):
        raise CmdError(f"no files in {raw_dir}; write the work there first")
    if not (a.summary or "").strip():
        raise CmdError("--summary must not be empty (what the work is, in one or two sentences)")
    d, _ = _validated_draft_files(it["id"], raw_dir)
    snap_dir, snap_files, snap_hash = create_snapshot(it["id"], d)
    work = dict(it.get("work") or {})
    work.update(draft_dir=d, draft_files=snap_files, snapshot_dir=snap_dir, snapshot_files=snap_files,
                snapshot_hash=snap_hash, draft_summary=a.summary.strip(),
                facts_used=[x.strip() for x in (a.facts or "").split("|") if x.strip()],
                drafted_by=agent, drafted_at=now(), notified_review=None)
    work.pop("reapproval_note", None)
    clear_approval(db, it["id"], work)
    pol = policy.active_policy()
    if policy.needs_review({**it, "work": work}, pol):
        nxt = "review"
    else:
        nxt = "ready"
        pol_name = str(pol.get("review_policy") or "none")
        record_approval(db, it["id"], work, by="policy", policy_name=pol_name, expected_hash=snap_hash)
    store.update_item(db, it["id"], state=nxt, work=work, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "draft-done", f"{len(snap_files)} file(s); next {nxt}")
    return {"ok": True, "id": it["id"], "state": nxt, "files": snap_files, "dir": snap_dir}


def approve(db, it, agent, note=None, *, expected_hash: str | None = None):
    if agent != "owner":  # "owner" is the user: an agent can never send a finished work on
        raise CmdError("only the user can approve a finished work (the 통과 button in the app)")
    _check("approve", it, agent)
    work = dict(it.get("work") or {})
    try:
        record_approval(db, it["id"], work, by=agent, expected_hash=expected_hash)
    except InvalidSendSpecError as e:
        revise(db, it, "owner", f"SEND.json validation failed: {e}")
        raise
    store.update_item(db, it["id"], state="ready", work=work, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "approve", note or "")


def revise(db, it, agent, note):
    if agent != "owner":
        raise CmdError("only the user can request a revision (in the app)")
    _check("revise", it, agent)
    work = dict(it.get("work") or {})
    work["review_note"] = note
    work["notified_review"] = None
    work.pop("reapproval_note", None)
    clear_approval(db, it["id"], work)
    work["revisions"] = int(work.get("revisions") or 0) + 1
    tries = {k: v for k, v in (it.get("tries") or {}).items() if k != "draft"}  # fresh draft tries after a revision
    store.update_item(db, it["id"], state="draft", work=work, tries=tries, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "revise", note)
    return work["revisions"]


def redraft(db, it, agent, note):
    """An accepted work goes back to drafting for a reason that is not the user's judgement of it: the tools failed
    (item went to manual) or the rules for what a finished work contains changed. Any approval is dropped, so the
    owner sees and approves the new version. Only for items whose analysis the user accepted."""
    _check("redraft", it, agent)
    work = dict(it.get("work") or {})
    if not work.get("accepted_at"):
        raise CmdError(f"{it['id']} has no accepted analysis; requeue it instead")
    work["review_note"] = "System note, not the user's criticism of the work: " + note
    work["notified_review"] = None
    work.pop("reapproval_note", None)
    clear_approval(db, it["id"], work)
    tries = {k: v for k, v in (it.get("tries") or {}).items() if k != "draft"}
    store.update_item(db, it["id"], state="draft", work=work, tries=tries, need=None, reason=None,
                      question=None, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "redraft", note)


def cmd_redraft(db, a):
    it = _need_item(db, a.id)
    redraft(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "draft"}


def accept(db, it, agent, note=None, *, _auto: bool = False):
    """The user accepts the analysis and the recommended approach: proposal -> draft (the work gets written)."""
    if agent != "owner" and not (_auto and agent == "system"):
        raise CmdError("only the user can accept an analysis (in the app)")
    _check("accept", it, agent)
    work = dict(it.get("work") or {})
    work["accepted_at"], work["accepted_by"] = now(), agent
    if agent == "owner":
        work["accepted_top_prize_krw"] = policy._valid_positive_prize(work.get("top_prize_krw"))
        work["accepted_top_prize"] = work.get("top_prize")
        work.pop("prize_changed_after_accept", None)
    else:
        work["accepted_top_prize_krw"] = None
        work.pop("accepted_top_prize", None)
    if note:
        work["accept_note"] = note
    tries = {k: v for k, v in (it.get("tries") or {}).items() if k != "draft"}
    store.update_item(db, it["id"], state="draft", work=work, tries=tries, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "accept", note or "")


def reanalyze(db, it, agent, note):
    """The user wants the analysis redone: proposal -> new, with the user's note for the analyst."""
    if agent != "owner":
        raise CmdError("only the user can request re-analysis (in the app)")
    _check("reanalyze", it, agent)
    work = dict(it.get("work") or {})
    clear_approval(db, it["id"], work)
    for k in ("accepted_at", "accepted_by", "accepted_top_prize_krw", "accepted_top_prize", "prize_changed_after_accept"):
        work.pop(k, None)
    work.update(analysis_note=note, audit=None, notified_proposal=None, notified_review=None)
    work["reanalyses"] = int(work.get("reanalyses") or 0) + 1
    tries = {k: v for k, v in (it.get("tries") or {}).items() if not (k == "no_result" or k.startswith("verify"))}
    store.update_item(db, it["id"], state="new", work=work, tries=tries, lane=None, claimed_by=None, claimed_at=None)
    store.add_history(db, it["id"], agent, "reanalyze", note)


def cmd_accept(db, a):
    it = _need_item(db, a.id)
    accept(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "draft"}


def cmd_reanalyze(db, a):
    it = _need_item(db, a.id)
    reanalyze(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "new"}


def cmd_audit_done(db, a):
    """Grok's verdict on the analysis of a big-prize contest. pass: the analysis may go to the user. fail: back to `new`
    with the findings for the analyst; AUDIT_MAX_FAILS fails: the item goes to the user with the findings."""
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("audit-done", it, agent)
    work = dict(it.get("work") or {})
    if not work.get("audit_required"):
        raise CmdError(f"{it['id']} needs no audit")
    findings = (a.findings or "").strip()
    if a.verdict == "fail" and not findings:
        raise CmdError("--findings must say what is wrong when the verdict is fail")
    work["audit"] = {"verdict": a.verdict, "by": agent, "at": now(), "findings": findings}
    if a.verdict == "pass":
        store.update_item(db, it["id"], work=work)
        store.add_history(db, it["id"], agent, "audit", "pass" + (f": {findings[:200]}" if findings else ""))
        return {"ok": True, "id": it["id"], "state": "proposal", "audit": "pass"}
    fails = int(work.get("audit_fails") or 0) + 1
    work["audit_fails"] = fails
    store.add_history(db, it["id"], agent, "audit", f"fail {fails}: {findings[:300]}")
    if fails >= config.AUDIT_MAX_FAILS:
        q = f"the audit failed the analysis {fails} times: {findings[:500]}"
        store.update_item(db, it["id"], state="owner", question=q, work=work, claimed_by=None, claimed_at=None)
        store.add_history(db, it["id"], agent, "owner", q)
        return {"ok": True, "id": it["id"], "state": "owner", "audit": "fail", "fails": fails}
    work["analysis_note"] = "Audit findings to fix: " + findings
    tries = {k: v for k, v in (it.get("tries") or {}).items() if not (k == "no_result" or k.startswith("verify"))}
    store.update_item(db, it["id"], state="new", work=work, tries=tries, lane=None, claimed_by=None, claimed_at=None)
    return {"ok": True, "id": it["id"], "state": "new", "audit": "fail", "fails": fails}


def cmd_approve(db, a):
    it = _need_item(db, a.id)
    approve(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "ready"}


def cmd_revise(db, a):
    it = _need_item(db, a.id)
    n = revise(db, it, _agent(a), a.note)
    return {"ok": True, "id": it["id"], "state": "draft", "revisions": n}


def cmd_submission(db, a):
    """Print the verified immutable frozen submission record for item `a.id`."""
    it = _need_item(db, a.id)
    check_approved_draft(it, db=db)
    frozen = get_frozen_submission(db, it)
    if frozen is None:
        raise CmdError(f"{it['id']} has no frozen submission record")
    return {"ok": True, "id": it["id"], "submission": frozen}


def cmd_sent_report(db, a):
    """Record what the submitter agent sent for `a.id`, verified against the frozen submission record."""
    agent = _agent(a, required=True)
    it = _need_item(db, a.id)
    _check("done", it, agent)
    check_approved_draft(it, db=db)
    frozen = get_frozen_submission(db, it)
    if frozen is None:
        raise CmdError(f"{it['id']} has no frozen submission record")
    try:
        rep = json.loads(a.json)
    except ValueError as e:
        raise CmdError(f"--json is not valid JSON: {e}") from e
    verify_sent_report(frozen, rep)
    store.kv_set(db, SENT_KV_PREFIX + it["id"], rep)
    return {"ok": True, "id": it["id"], "verified": True}


def main(argv: list[str] | None = None) -> int:
    config.utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m cp.workflow")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s1 = sp.add_parser("submission")
    s1.add_argument("id")
    s2 = sp.add_parser("sent-report")
    s2.add_argument("id")
    s2.add_argument("--agent", choices=config.AGENTS, required=True)
    s2.add_argument("--json", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code:
            print(json.dumps({"ok": False, "error": "bad arguments"}, ensure_ascii=False))
        return code
    try:
        if a.cmd == "submission":
            db = store.connect()
            try:
                res = cmd_submission(db, a)
            finally:
                db.close()
        else:
            with store.writer() as db:
                res = cmd_sent_report(db, a)
    except (CmdError, store.StoreError) as e:
        res = {"ok": False, "error": str(e)}
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
