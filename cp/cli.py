"""argparse subcommands. Every command prints exactly one JSON line; exit code 1 on error.

Programmatic use: `cp.cli.call(["status"])` runs a core command and returns its result dict without printing.
"""
import argparse
import importlib
import json
import sys

from cp import config, model, store
from cp.cmdbase import CmdError, ELIGIBILITY, WORK_TYPES
from cp.intake import cmd_add, cmd_check, cmd_pool_add, cmd_pool_list, cmd_pool_promote
from cp.transitions import (cmd_claim, cmd_done, cmd_error, cmd_exclude, cmd_fail, cmd_get, cmd_list, cmd_manual,
    cmd_next_batch, cmd_owner, cmd_owner_done, cmd_release, cmd_requeue, cmd_resolve, cmd_set, cmd_status)
from cp.workflow import (cmd_accept, cmd_approve, cmd_audit_done, cmd_draft_claim, cmd_draft_done, cmd_reanalyze,
    cmd_redraft, cmd_revise, cmd_submission, cmd_verify)
from cp.records import cmd_rewards, cmd_signups, cmd_win, cmd_wins


REST_MODULES = {"chrome": "chrome", "form-read": "form_read", "consent": "consent",
                "secret": "secrets", "notify": "notify"}
FULL_MODULES = {"run": "scheduler", "stop": "scheduler", "resume": "scheduler", "block": "scheduler",
                "accounts": "accounts", "translate": "translate",
                "register": "register", "backfill": "backfill"}


def build_parser(internal: bool = False) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a subparser default never overwrites an --agent given before the command name.
    # The CLI never accepts "owner"; "system" is accepted only for in-process scheduler calls (internal=True).
    actors = config.AGENTS + ("system",) if internal else config.AGENTS
    common.add_argument("--agent", choices=actors, default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="cp", parents=[common])
    sp = p.add_subparsers(dest="cmd", required=True)

    def sub(name):
        return sp.add_parser(name, parents=[common])

    sub("status")
    x = sub("list"); x.add_argument("--state", choices=model.STATES); x.add_argument("--region")
    x.add_argument("--lane", choices=config.LANES); x.add_argument("--limit", type=int); x.add_argument("--fields")
    x.add_argument("--flag")
    x = sub("get"); x.add_argument("id")
    x = sub("check"); x.add_argument("url")
    for name in ("add", "pool-add"):
        x = sub(name); x.add_argument("--json"); x.add_argument("--file")
    x = sub("pool-list"); x.add_argument("--region"); x.add_argument("--all", action="store_true")
    x = sub("pool-promote"); x.add_argument("ids", nargs="+")
    x = sub("next-batch"); x.add_argument("--size", type=int, default=10)
    x = sub("verify"); x.add_argument("id"); x.add_argument("--lane", required=True, choices=config.LANES)
    x.add_argument("--notes"); x.add_argument("--answers"); x.add_argument("--url")
    x.add_argument("--work-type", dest="work_type", choices=WORK_TYPES)
    x.add_argument("--no-work", dest="no_work", action="store_true")
    x.add_argument("--analysis-file", dest="analysis_file", help="the analysis dossier (Markdown, required unless --no-work)")
    for opt in ("--theme", "--format", "--submit-method", "--top-prize"):
        x.add_argument(opt, dest=opt[2:].replace("-", "_"))
    x.add_argument("--top-prize-krw", dest="top_prize_krw", type=int); x.add_argument("--team-size", dest="team_size", type=int)
    x.add_argument("--eligibility", choices=ELIGIBILITY); x.add_argument("--result-date", dest="result_date")
    x = sub("accept"); x.add_argument("id"); x.add_argument("--note")
    x = sub("reanalyze"); x.add_argument("id"); x.add_argument("--note", required=True)
    x = sub("audit-done"); x.add_argument("id"); x.add_argument("--verdict", required=True, choices=("pass", "fail"))
    x.add_argument("--findings", default="")
    x = sub("exclude"); x.add_argument("id"); x.add_argument("--reason", required=True)
    x = sub("owner"); x.add_argument("id"); x.add_argument("--question", required=True)
    x = sub("manual"); x.add_argument("id"); x.add_argument("--need", required=True, choices=config.MANUAL_NEEDS)
    x.add_argument("--reason", required=True)
    x = sub("claim"); x.add_argument("--lane", choices=config.LANES); x.add_argument("--id")
    x = sub("done"); x.add_argument("id"); x.add_argument("--evidence", required=True)
    x.add_argument("--sent-json", dest="sent_json", help="JSON of what was sent (recipient, fields, body, attachments)")
    x = sub("fail"); x.add_argument("id"); x.add_argument("--reason", required=True)
    x.add_argument("--no-resubmit", dest="no_resubmit", action="store_true")
    x = sub("release"); x.add_argument("id"); x.add_argument("--force", action="store_true")
    x = sub("requeue"); x.add_argument("id"); x.add_argument("--note")
    x = sub("set"); x.add_argument("id")
    for opt in ("--result-date", "--note", "--verify-notes", "--region", "--deadline", "--deadline-time",
                "--title-en", "--source-url", "--url", "--tz"):
        x.add_argument(opt, dest=opt[2:].replace("-", "_"))
    x.add_argument("--eligibility", choices=ELIGIBILITY); x.add_argument("--flag", action="append")
    x.add_argument("--work-type", dest="work_type", choices=WORK_TYPES)
    for opt in ("--theme", "--format", "--submit-method", "--top-prize"):
        x.add_argument(opt, dest=opt[2:].replace("-", "_"))
    x.add_argument("--top-prize-krw", dest="top_prize_krw", type=int); x.add_argument("--team-size", dest="team_size", type=int)
    x = sub("draft-claim"); x.add_argument("--id")
    x = sub("draft-done"); x.add_argument("id"); x.add_argument("--summary", required=True)
    x.add_argument("--facts", help="user-given facts used in the work, separated by |")
    x = sub("approve"); x.add_argument("id"); x.add_argument("--note")
    x = sub("submission"); x.add_argument("id")
    x = sub("revise"); x.add_argument("id"); x.add_argument("--note", required=True)
    x = sub("redraft"); x.add_argument("id"); x.add_argument("--note", required=True)
    x = sub("owner-done"); x.add_argument("id"); x.add_argument("--evidence", required=True)
    x = sub("error"); x.add_argument("--message", required=True)
    x = sub("resolve"); x.add_argument("--id", type=int)
    x = sub("win"); x.add_argument("id", help="item id, or - when unknown"); x.add_argument("--prize", required=True)
    x.add_argument("--date"); x.add_argument("--note")
    sub("wins")
    x = sub("rewards"); x.add_argument("--state", default="submitted", choices=model.STATES)
    sub("signups")
    return p


COMMANDS = {
    "status": cmd_status, "list": cmd_list, "get": cmd_get, "check": cmd_check, "add": cmd_add,
    "next-batch": cmd_next_batch, "verify": cmd_verify, "exclude": cmd_exclude, "owner": cmd_owner,
    "manual": cmd_manual, "claim": cmd_claim, "done": cmd_done, "fail": cmd_fail, "release": cmd_release,
    "requeue": cmd_requeue, "set": cmd_set, "owner-done": cmd_owner_done, "error": cmd_error,
    "resolve": cmd_resolve, "draft-claim": cmd_draft_claim, "draft-done": cmd_draft_done,
    "approve": cmd_approve, "submission": cmd_submission, "revise": cmd_revise, "redraft": cmd_redraft, "accept": cmd_accept, "reanalyze": cmd_reanalyze,
    "audit-done": cmd_audit_done, "pool-add": cmd_pool_add, "pool-list": cmd_pool_list,
    "pool-promote": cmd_pool_promote,
    "win": cmd_win, "wins": cmd_wins, "rewards": cmd_rewards, "signups": cmd_signups,
}
READ_ONLY = {"get", "check", "submission", "pool-list", "wins", "rewards", "signups"}


def call(argv: list[str], *, internal: bool = False) -> dict:
    """Run one core command and return its result dict (errors are returned as {"ok": False, "error": ...})."""
    parser = build_parser(internal=internal)
    try:
        a = parser.parse_args(argv)
    except SystemExit as e:
        if e.code == 0:
            return {"ok": True, "help": True}
        return {"ok": False, "error": f"bad arguments: {' '.join(argv)}"}
    fn = COMMANDS[a.cmd]
    try:
        if a.cmd in READ_ONLY:
            db = store.connect()
            try:
                return fn(db, a)
            finally:
                db.close()
        with store.writer() as db:
            return fn(db, a)
    except CmdError as e:
        return {"ok": False, "error": str(e)}
    except store.StoreError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # never swallow: report type and message as the command result
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def dispatch_external(argv: list[str]):
    """Commands owned by other modules. Returns an exit code, or None when argv is a core command."""
    if not argv:
        return None
    cmd = argv[0]
    if cmd in REST_MODULES:
        name, args = REST_MODULES[cmd], argv[1:]
    elif cmd in FULL_MODULES:
        name, args = FULL_MODULES[cmd], argv
    else:
        return None
    try:
        mod = importlib.import_module(f"cp.{name}")
        fn = getattr(mod, "main")
    except (ImportError, AttributeError):
        print(json.dumps({"ok": False, "error": f"module not available: {name}"}, ensure_ascii=False))
        return 1
    rc = fn(args)
    return rc if isinstance(rc, int) else 0


def main(argv: list[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config.utf8_stdio()
    rc = dispatch_external(argv)
    if rc is not None:
        sys.exit(rc)
    if argv and argv[0] in ("-h", "--help") or not argv:
        build_parser().print_help()
        sys.exit(0 if argv else 1)
    r = call(argv)
    print(json.dumps(r, ensure_ascii=False, default=str))
    sys.exit(0 if r.get("ok") else 1)
