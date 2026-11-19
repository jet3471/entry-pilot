"""What the user looks at before approving a finished work: the entry as it will be sent.

The drafting agent writes `SEND.json` into the draft folder (prompts_contest/draft.md, step 7):
  {"how": "이메일", "to": "someone@example.kr", "to_source": "where that address or URL was read",
   "subject": "mail subject", "body": "mail text, exactly as sent",
   "files": ["제안서.hwpx"],                 files that are sent, names inside the draft folder
   "preview": {"제안서.hwpx": "제안서.pdf"},   the PDF that shows a file which is not a PDF itself
   "fields": [["표어", "..."], ["설명", "..."]]}   what is typed into a web form, in the form's order
Every key is optional. The review page shows this as one short table, then the pages of each PDF as pictures
(the user reads in the app; a link to a PDF or a page of prose is not a preview).

PDF pages are rendered with PyMuPDF into `<data_dir>/preview-cache/<sha256>/page-N.png` keyed by the PDF bytes.
"""
import hashlib
import json
import math
import os
import re

from cp import config

NAME = "SEND.json"
CACHE = "preview-cache"
SNAPSHOTS = "snapshots"
MAX_PAGES = 30
WIDTH = 1100  # pixels; enough to read a form
MAX_PIXELS = 40_000_000  # max 40 megapixels per rendered page
MAX_DRAFT_FILES = 50
MAX_FILE_BYTES = 200 * 1024 * 1024  # 200 MB per file
HASH_CHUNK = 65536  # 64 KiB streaming read chunk
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


ALIASES = {"method": "how", "url": "to", "recipient": "to", "attachments": "files", "source": "to_source"}
HOW_KO = {"online_form": "누리집 양식 입력", "web_form": "누리집 양식 입력", "form": "누리집 양식 입력",
          "email": "이메일", "upload": "누리집 파일 올리기"}
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_reparse(path: str) -> bool:
    """True when `path` is a symlink or NTFS junction/reparse point."""
    if not path:
        return False
    if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
        return True
    try:
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400 or getattr(st, "st_reparse_tag", 0))
    except OSError:
        return False


def _valid_single_name(name: str) -> bool:
    return bool(
        isinstance(name, str)
        and name
        and name not in (".", "..")
        and os.path.basename(name) == name
        and "/" not in name
        and "\\" not in name
        and ":" not in name
    )


def safe_draft_dir(draft_dir: str, item_id: str | None = None) -> str:
    """Return realpath of `draft_dir` when it is a real folder (no symlinks/junctions) inside config.drafts_dir(), else ''."""
    if not draft_dir or not isinstance(draft_dir, str):
        return ""
    root = config.drafts_dir()
    if is_reparse(root) or is_reparse(draft_dir) or not os.path.isdir(draft_dir):
        return ""
    root_real = os.path.realpath(root)
    dir_real = os.path.realpath(draft_dir)
    try:
        common = os.path.commonpath([root_real, dir_real])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(root_real) or os.path.normcase(dir_real) == os.path.normcase(root_real):
        return ""
    rel = os.path.relpath(dir_real, root_real)
    cur = root_real
    for part in rel.split(os.sep):
        if part in ("", ".", ".."):
            return ""
        cur = os.path.join(cur, part)
        if is_reparse(cur):
            return ""
    if item_id is not None:
        if not _valid_single_name(item_id):
            return ""
        want = os.path.realpath(os.path.join(root_real, item_id))
        if os.path.normcase(dir_real) != os.path.normcase(want):
            return ""
    return dir_real


def safe_draft_file(draft_dir: str, name: str, item_id: str | None = None) -> str:
    """Return realpath of `name` inside `draft_dir` when neither folder nor file is a link and both stay inside drafts root."""
    if not _valid_single_name(name):
        return ""
    dir_real = safe_draft_dir(draft_dir, item_id)
    if not dir_real:
        return ""
    return _file_in_dir(draft_dir, dir_real, name)


def _file_in_dir(raw_dir: str, dir_real: str, name: str) -> str:
    """Realpath of `name` directly inside the already validated `dir_real`, or '' for a link or an escape."""
    raw_cand = os.path.join(raw_dir, name)
    cand = os.path.join(dir_real, name)
    if is_reparse(raw_cand) or is_reparse(cand) or not os.path.isfile(cand):
        return ""
    file_real = os.path.realpath(cand)
    try:
        common = os.path.commonpath([dir_real, file_real])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(dir_real) or os.path.normcase(os.path.dirname(file_real)) != os.path.normcase(dir_real):
        return ""
    return file_real


def preview_cache_dir() -> str:
    return os.path.join(config.data_dir(), CACHE)


def snapshots_dir() -> str:
    return os.path.join(config.data_dir(), SNAPSHOTS)


def safe_snapshot_dir(snap_dir: str, item_id: str | None = None, snap_hash: str | None = None) -> str:
    """Return realpath of `snap_dir` when it is a real folder `<data>/snapshots/<ID>/<sha256>` with no links."""
    if not snap_dir or not isinstance(snap_dir, str):
        return ""
    data_root = config.data_dir()
    root = snapshots_dir()
    if not data_root or is_reparse(data_root) or is_reparse(root) or is_reparse(snap_dir) or not os.path.isdir(snap_dir):
        return ""
    root_real = os.path.realpath(root)
    dir_real = os.path.realpath(snap_dir)
    try:
        common = os.path.commonpath([root_real, dir_real])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(root_real) or os.path.normcase(dir_real) == os.path.normcase(root_real):
        return ""
    rel = os.path.relpath(dir_real, root_real)
    parts = rel.split(os.sep)
    if len(parts) != 2:
        return ""
    cid, chash = parts
    if not _valid_single_name(cid) or not _HEX64_RE.fullmatch(chash):
        return ""
    cur = root_real
    for part in parts:
        cur = os.path.join(cur, part)
        if is_reparse(cur):
            return ""
    if item_id is not None:
        if not _valid_single_name(item_id) or os.path.normcase(cid) != os.path.normcase(item_id):
            return ""
    if snap_hash is not None:
        if not isinstance(snap_hash, str) or chash != snap_hash.lower():
            return ""
    return dir_real


def safe_snapshot_file(snap_dir: str, name: str, item_id: str | None = None, snap_hash: str | None = None) -> str:
    """Return realpath of `name` inside `snap_dir` when neither folder nor file is a link."""
    if not _valid_single_name(name):
        return ""
    dir_real = safe_snapshot_dir(snap_dir, item_id, snap_hash)
    if not dir_real:
        return ""
    return _file_in_dir(snap_dir, dir_real, name)


def safe_work_dir(work_dir: str, item_id: str | None = None) -> str:
    """Return realpath when `work_dir` is a valid snapshot folder or draft folder for `item_id`."""
    return safe_snapshot_dir(work_dir, item_id) or safe_draft_dir(work_dir, item_id)


def safe_work_file(work_dir: str, name: str, item_id: str | None = None) -> str:
    """Return realpath of `name` inside a valid snapshot folder or draft folder for `item_id`."""
    return safe_snapshot_file(work_dir, name, item_id) or safe_draft_file(work_dir, name, item_id)


def sha256_file(path: str, max_bytes: int = MAX_FILE_BYTES) -> tuple[str, int]:
    """Stream-hash `path` in chunks, enforcing `max_bytes`. Returns (hex_digest, byte_length)."""
    if not path or is_reparse(path) or not os.path.isfile(path):
        raise ValueError("invalid file path")
    st_size = os.path.getsize(path)
    if st_size > max_bytes:
        raise ValueError(f"file {os.path.basename(path)} exceeds {max_bytes // (1024 * 1024)} MB limit ({st_size} bytes)")
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"file {os.path.basename(path)} exceeds {max_bytes // (1024 * 1024)} MB limit")
            h.update(chunk)
    if total != st_size:
        raise ValueError(f"file {os.path.basename(path)} changed size while hashing")
    return h.hexdigest(), total


def _list_regular_files(dir_real: str) -> tuple[list[str], list[str]]:
    """Return (regular_files, errors) inside `dir_real`, ignoring dot entries and real subfolders, rejecting links."""
    files, errs = [], []
    for entry in sorted(os.listdir(dir_real)):
        p = os.path.join(dir_real, entry)
        if is_reparse(p):
            errs.append(f"draft entry {entry} cannot be a link")
            continue
        if entry.startswith("."):
            continue
        if os.path.isdir(p):
            continue
        fp = safe_work_file(dir_real, entry)
        if not fp:
            errs.append(f"draft entry {entry} is not a regular file inside the draft folder")
            continue
        files.append(entry)
    return files, errs


def validate_send(draft_dir: str, files: list | None = None) -> tuple[dict | None, list[str]]:
    """Strictly validate SEND.json types and draft file limits. Returns (normalized_dict, []) or (None, errors)."""
    dir_real = safe_work_dir(draft_dir)
    if not dir_real:
        return None, ["draft folder is invalid, outside drafts root, or a link"]
    if files is None:
        files, dir_errs = _list_regular_files(dir_real)
        if dir_errs:
            return None, dir_errs
    if len(files) > MAX_DRAFT_FILES:
        return None, [f"draft folder has too many files ({len(files)} > {MAX_DRAFT_FILES})"]
    for name in files:
        if not isinstance(name, str):
            return None, ["draft file name must be a string"]
        fp = safe_work_file(dir_real, name)
        if not fp:
            return None, [f"{name} is a link or outside the draft folder"]
        sz = os.path.getsize(fp)
        if sz > MAX_FILE_BYTES:
            return None, [f"{name} exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit ({sz} bytes)"]
    sfp = safe_work_file(dir_real, NAME)
    if not sfp:
        return None, [f"{NAME} is missing (prompts_contest/draft.md step 7)"]
    if os.path.getsize(sfp) > MAX_FILE_BYTES:
        return None, [f"{NAME} exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit"]
    try:
        with open(sfp, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None, [f"{NAME} is not valid JSON"]
    if not isinstance(raw, dict):
        return None, [f"{NAME} is not a JSON object"]
    d = _norm_send_fields(raw)
    out = []
    how = d.get("how")
    if how is None or (isinstance(how, str) and not how.strip()):
        out.append("`how` is missing (how the entry is sent, in Korean)")
        norm_how = ""
    elif not isinstance(how, str):
        out.append("`how` must be a string")
        norm_how = ""
    else:
        norm_how = HOW_KO.get(how.strip().lower(), how.strip())

    to = d.get("to")
    if to is None or (isinstance(to, str) and not to.strip()):
        out.append("`to` is missing (address or page URL the entry goes to)")
        norm_to: str | list[str] = ""
    elif isinstance(to, str):
        norm_to = to.strip()
    elif isinstance(to, list):
        if not to or not all(isinstance(x, str) and x.strip() for x in to):
            out.append("`to` must be a non-empty string or list of non-empty strings")
            norm_to = []
        else:
            norm_to = [x.strip() for x in to]
    else:
        out.append("`to` must be a string or list of strings")
        norm_to = ""

    for key in ("subject", "body", "to_source"):
        val = d.get(key)
        if val is not None and not isinstance(val, str):
            out.append(f"`{key}` must be a string")
    norm_subject = d["subject"] if isinstance(d.get("subject"), str) else ""
    norm_body = d["body"] if isinstance(d.get("body"), str) else ""
    norm_source = d["to_source"] if isinstance(d.get("to_source"), str) else ""

    raw_fields = d.get("fields")
    norm_fields: list[list[str]] = []
    if raw_fields is not None:
        if not isinstance(raw_fields, list):
            out.append("`fields` must be a list of [str, str] pairs")
        else:
            bad_f = False
            for item in raw_fields:
                if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], str):
                    norm_fields.append([item[0], item[1]])
                else:
                    bad_f = True
            if bad_f:
                out.append("`fields` entries must each be [str, str]")

    raw_files = d.get("files")
    norm_files: list[str] = []
    if raw_files is not None:
        if not isinstance(raw_files, list):
            out.append("`files` must be a list of file names")
        else:
            bad_fl = False
            for elem in raw_files:
                if isinstance(elem, str) and elem:
                    norm_files.append(elem)
                else:
                    bad_fl = True
            if bad_fl:
                out.append("`files` entries must be non-empty strings")

    if not (norm_files or norm_fields or norm_body):
        out.append("nothing to send: give `files`, `fields` or `body`")
    for name in norm_files:
        if name == NAME or name not in files or not safe_work_file(dir_real, name):
            out.append(f"`files` names {name}, which is not in the draft folder")
    if out:
        return None, out
    return {
        "how": norm_how,
        "to": norm_to,
        "to_source": norm_source,
        "subject": norm_subject,
        "body": norm_body,
        "fields": norm_fields,
        "files": norm_files,
        "preview": {},
    }, []


def load(draft_dir: str) -> dict:
    """SEND.json as a sanitized dict for display; {} when missing or not readable (never returns wrong types)."""
    clean, _ = validate_send(draft_dir)
    if clean is not None:
        return clean
    out = lenient_send_from_file(safe_work_file(draft_dir, NAME))
    if out:
        out["preview"] = {}
    return out


def _norm_send_fields(raw: dict) -> dict:
    """Copy of a raw SEND.json object with the alias keys (method, url, recipient, ...) mapped to their main key."""
    d = dict(raw)
    for alias, key in ALIASES.items():
        if alias in d and key not in d:
            d[key] = d[alias]
    return d


def lenient_send_from_file(sfp: str) -> dict:
    """Best-effort sanitized SEND.json at `sfp` (no `preview` key): wrong types are dropped; {} on a link, a
    missing or oversized file, bad JSON or a non-object."""
    if not sfp or is_reparse(sfp) or not os.path.isfile(sfp):
        return {}
    try:
        if os.path.getsize(sfp) > MAX_FILE_BYTES:
            return {}
        with open(sfp, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    d = _norm_send_fields(d)
    how = d.get("how")
    how_s = HOW_KO.get(how.strip().lower(), how.strip()) if isinstance(how, str) else ""
    to = d.get("to")
    if isinstance(to, str):
        to_v: str | list[str] = to.strip()
    elif isinstance(to, list):
        to_v = [x.strip() for x in to if isinstance(x, str) and x.strip()]
    else:
        to_v = ""
    f = d.get("fields")
    fields_v = ([[x[0], x[1]] for x in f if isinstance(x, (list, tuple)) and len(x) == 2
                 and isinstance(x[0], str) and isinstance(x[1], str)] if isinstance(f, list) else [])
    files_v = [x for x in d["files"] if isinstance(x, str) and x] if isinstance(d.get("files"), list) else []
    return {
        "how": how_s,
        "to": to_v,
        "to_source": d["to_source"] if isinstance(d.get("to_source"), str) else "",
        "subject": d["subject"] if isinstance(d.get("subject"), str) else "",
        "body": d["body"] if isinstance(d.get("body"), str) else "",
        "fields": fields_v,
        "files": files_v,
    }


def problems(draft_dir: str, files: list) -> list:
    """Why this draft folder cannot be shown or frozen as a submission preview; [] when valid."""
    _, errs = validate_send(draft_dir, files)
    return errs


def pdfs(draft_dir: str, files: list, send: dict) -> list:
    """(title, pdf name) of PDF attachments themselves, in order (never substitutes a separate preview PDF).
    Without SEND.json: every PDF among the draft files."""
    out, seen = [], set()
    for name in send.get("files") or []:
        if isinstance(name, str) and name.lower().endswith(".pdf") and name in files and name not in seen:
            out.append((name, name))
            seen.add(name)
    if not send:
        out = [(n, n) for n in files if isinstance(n, str) and n.lower().endswith(".pdf")]
    return out


def page_count(draft_dir: str, pdf: str) -> int:
    """Return the real total page count of `pdf` inside `draft_dir` (snapshot or draft folder)."""
    src = safe_work_file(draft_dir, pdf)
    if not src or not pdf.lower().endswith(".pdf"):
        raise ValueError("invalid PDF path")
    if os.path.getsize(src) > MAX_FILE_BYTES:
        raise ValueError(f"PDF exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
    import pymupdf
    with pymupdf.open(src) as doc:
        return doc.page_count


def _safe_cache_slot(digest: str, n: int) -> tuple[str, str]:
    """Return (pdf_cache_dir, out_png_path) under config.data_dir()/preview-cache/<digest>, or ('','') on link/escape."""
    data_root = config.data_dir()
    if not data_root or is_reparse(data_root):
        return "", ""
    os.makedirs(data_root, exist_ok=True)
    root_real = os.path.realpath(data_root)
    cache_base = os.path.join(root_real, CACHE)
    if os.path.exists(cache_base) and is_reparse(cache_base):
        return "", ""
    pdf_cache = os.path.join(cache_base, digest)
    if os.path.exists(pdf_cache) and is_reparse(pdf_cache):
        return "", ""
    out = os.path.join(pdf_cache, f"page-{n}.png")
    if os.path.exists(out) and is_reparse(out):
        return "", ""
    try:
        if os.path.normcase(os.path.commonpath([root_real, os.path.abspath(out)])) != os.path.normcase(root_real):
            return "", ""
    except ValueError:
        return "", ""
    return pdf_cache, out


def _render_scale(w_pt: float, h_pt: float) -> float:
    """Compute PyMuPDF zoom scale bounded by WIDTH and MAX_PIXELS (0.0 when dimensions cannot fit <= MAX_PIXELS)."""
    if not (math.isfinite(w_pt) and math.isfinite(h_pt)) or w_pt <= 0 or h_pt <= 0:
        return 0.0
    scale = min(WIDTH / w_pt, math.sqrt(MAX_PIXELS / (w_pt * h_pt)))
    for _ in range(16):
        if math.ceil(w_pt * scale) * math.ceil(h_pt * scale) <= MAX_PIXELS:
            break
        scale *= 0.99
    rw, rh = math.ceil(w_pt * scale), math.ceil(h_pt * scale)
    if scale <= 0 or rw < 1 or rh < 1 or rw * rh > MAX_PIXELS:
        return 0.0
    return scale


def page_png(draft_dir: str, pdf: str, n: int) -> bytes | None:
    """PNG of page n (0-based) of a PDF in the snapshot/draft folder, cached under data_dir()/preview-cache/<sha256>/page-N.png."""
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < MAX_PAGES:
        return None
    dir_real = safe_work_dir(draft_dir)
    src = safe_work_file(dir_real, pdf) if dir_real else ""
    if not src or not isinstance(pdf, str) or not pdf.lower().endswith(".pdf"):
        return None
    try:
        digest, _ = sha256_file(src)
    except (OSError, ValueError):
        return None
    pdf_cache, out = _safe_cache_slot(digest, n)
    if not out:
        return None
    if os.path.isfile(out) and not is_reparse(out):
        with open(out, "rb") as f:
            return f.read()
    import pymupdf
    try:
        with pymupdf.open(src) as doc:
            if n >= doc.page_count:
                return None
            page = doc[n]
            scale = _render_scale(float(page.rect.width), float(page.rect.height))
            if scale <= 0:
                return None
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            if pix.width * pix.height > MAX_PIXELS:
                return None
            data = pix.tobytes("png")
    except Exception:
        return None
    try:
        after_digest, _ = sha256_file(src)
    except (OSError, ValueError):
        return None
    if after_digest != digest:
        return None
    os.makedirs(pdf_cache, exist_ok=True)
    _, out_checked = _safe_cache_slot(digest, n)
    if not out_checked or is_reparse(pdf_cache):
        return None
    tmp = f"{out_checked}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    if is_reparse(tmp):
        return None
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            os.replace(tmp, out_checked)
        except PermissionError:
            if not os.path.isfile(out_checked):
                raise
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return data
