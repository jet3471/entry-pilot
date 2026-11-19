"""Reading the analysis dossier of a contest. The dossier is a Markdown file written by an agent in
English with '## ' sections (see workflow.ANALYSIS_SECTIONS); the section whose heading contains "summary" is Korean for the
user. The app and the notices use these helpers."""
import os


def written_in_korean(text: str) -> bool:
    """True for a dossier written in Korean (2026-11-17 on): its first '## ' heading has Hangul. Such a dossier is
    shown as written and never sent for translation; older English dossiers still are."""
    for ln in text.splitlines():
        if ln.startswith("## "):
            return any("\uac00" <= c <= "\ud7a3" for c in ln)
    return False


def plain_headings(text: str) -> str:
    """'## 공모전 (Contest)' -> '## 공모전' for showing: the bracketed English word is for the program's check only."""
    import re
    return re.sub(r"(?m)^(## .*[가-힣].*?)\s*\([A-Za-z ]+\)\s*$", r"\1", text)


def read_text(item: dict) -> str:
    path = (item.get("work") or {}).get("analysis_path")
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)] for each '## ' section, in file order."""
    out, head, buf = [], None, []
    for ln in text.splitlines():
        if ln.startswith("## "):
            if head is not None:
                out.append((head, "\n".join(buf).strip()))
            head, buf = ln[3:].strip(), []
        elif head is not None:
            buf.append(ln)
    if head is not None:
        out.append((head, "\n".join(buf).strip()))
    return out


def section(text: str, keyword: str) -> str:
    for head, body in sections(text):
        if keyword.lower() in head.lower():
            return body
    return ""


def summary_ko(item: dict) -> str:
    """The Korean owner summary of the item's analysis ('' when there is no dossier)."""
    return section(read_text(item), "summary")


def plan_ko(item: dict) -> str:
    """The execution plan the agent put at the end of the Korean summary (the block after a line starting 수행 계획),
    or '' for dossiers written before that rule."""
    lines = summary_ko(item).splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lstrip("-* ").startswith("수행 계획"):
            return "\n".join(lines[i:]).strip()
    return ""
