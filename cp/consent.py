"""Record consents, subscriptions, notification sign-ups, registrations and identity verifications that an agent made on
the user's behalf.

User-facing log: the "board" folder of the data folder, file 동의_기록.md (one table row per record; Korean column layout kept)
Raw log: <data dir>\\consents.jsonl

  python -m cp.consent add ID --agent codex --kind KIND --site SITE --what "what was agreed to" [--why "required"]

Kinds (--kind): privacy_required, marketing_required, third_party_required, notification_subscription (newsletter,
channel add, alert sign-up), app_signup, member_signup, identity_verification, other.
The old Korean kind names are accepted too and mapped to these.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from cp.config import DATA_DIR
except Exception:
    DATA_DIR = os.environ.get("CP_DATA_DIR") or (os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\data\contest"

KST = timezone(timedelta(hours=9))
JSONL = os.path.join(DATA_DIR, "consents.jsonl")
MD = os.path.join(DATA_DIR, "board", "동의_기록.md")
# Header of the user's Korean markdown (user-facing text stays Korean, as before). It says: "Consents given on the
# user's behalf. Optional marketing consents are never given. Look at this table to turn alerts
# off or leave later." Columns: when | item | who | kind | site | what | why
HEAD = ("# 대리 동의 기록\n\n에이전트가 나 대신 한 동의, 구독, 알림 신청, 가입, 본인인증. 선택 마케팅 동의는 하지 않는다().\n"
        "나중에 알림을 끄거나 탈퇴할 때 이 표를 본다.\n\n| 때 | 항목 | 누가 | 종류 | 사이트 | 무엇 | 왜 |\n|---|---|---|---|---|---|---|\n")
# English kind -> Korean label written in the user's markdown (the old tool's kind values)
KINDS = {
    "privacy_required": "개인정보필수",
    "marketing_required": "마케팅필수",
    "third_party_required": "제3자제공필수",
    "notification_subscription": "알림구독",
    "app_signup": "앱가입",
    "member_signup": "회원가입",
    "identity_verification": "본인인증",
    "other": "기타",
}
KO_TO_EN = {v: k for k, v in KINDS.items()}


def _main(argv):
    ap = argparse.ArgumentParser(prog="cp consent")
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("add")
    x.add_argument("id"); x.add_argument("--agent", required=True)
    x.add_argument("--kind", required=True, choices=[*KINDS, *KO_TO_EN])
    x.add_argument("--site", required=True); x.add_argument("--what", required=True); x.add_argument("--why", default="")
    a = ap.parse_args(argv)
    kind = KO_TO_EN.get(a.kind, a.kind)
    rec = {"at": datetime.now(KST).isoformat(timespec="seconds"), "id": a.id, "agent": a.agent, "kind": kind,
           "site": a.site, "what": a.what, "why": a.why}
    os.makedirs(os.path.dirname(JSONL), exist_ok=True)
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
    new = not os.path.exists(MD)
    with open(MD, "a", encoding="utf-8") as f:
        if new:
            f.write(HEAD)
        f.write(f"| {rec['at'][5:16].replace('T', ' ')} | {cell(a.id)} | {cell(a.agent)} | {KINDS[kind]} | {cell(a.site)} | {cell(a.what)} | {cell(a.why)} |\n")
    print(json.dumps({"ok": True, "logged": MD, "kind": kind}, ensure_ascii=False), flush=True)
    return 0


def main(argv):
    try:
        return _main(list(argv))
    except SystemExit as e:  # argparse errors
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
