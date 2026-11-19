"""Scheduler: `cp run [--once] [--dry-run] [--force]`, `cp stop`, `cp resume <agent>`, `cp block <agent> --reason T`.

One loop:
  1. reap finished runs, classify, apply outcome rules (no_result, spent/limit/auth/crash)
  2. gates (free RAM, active agent processes, browser jobs, blocked agents, stop)
  3. housekeeping: Chrome tab sweep, Korean translation of waiting dossiers, notices for the user
  4. launches in order: submit, draft, audit, verify, register, pool promotion, scout, backfill
Agents finish their own items with `cp done/fail/verify/...`; the scheduler only reads the item after the process ends.
`--dry-run` is read-only: it writes nothing (no claims, no reaping, no kv) and starts no process; it reports what it
would launch. Every main() prints exactly one JSON line; progress lines go to stderr (scheduler.log when hidden).
"""
import argparse
import contextlib
import json
import os
import sys

from cp import accounts, config, store
from cp.agents import base
from cp.sched_core import Core
from cp.sched_launch import Launch, SystemAdapter  # noqa: F401  (tests reach SystemAdapter through this module)
from cp.sched_outcomes import Outcomes
from cp.sched_phases import Phases



class Scheduler(Outcomes, Launch, Phases, Core):
    """One scheduler: Core (state), Outcomes (ended runs), Launch (starting runs), Phases (what to start, in order)."""

    # ---------- one loop ----------
    def loop_once(self) -> dict:
        self._acct, self._chrome = {}, None
        s = {"launched": [], "would": [], "reaped": [], "actions": [], "notes": [], "stopped": False}
        if not self.dry:
            s["reaped"] = self.reap_and_apply()
        if not self.dry and self.due("tick.codex_room", 300):  # the Codex account with the most room
            self.tick("tick.codex_room")
            try:
                best = accounts.codex_pick_most_room(self.db)
                if best:
                    self.log(f"codex account with most room: {accounts.codex_name(best)}")
            except Exception as e:  # visible, not swallowed
                self.add_error("system", f"codex room pick failed: {type(e).__name__}: {e}")
        g = self.gates()
        s["gates"] = {k: g[k] for k in ("free_gb", "floor", "active", "max_agents", "browser", "counts",
                                        "blocked", "stop")}
        s["gates"]["free_gb"] = round(g["free_gb"], 2)
        s["gates"]["counts"] = dict(g["counts"])  # snapshot before this loop's launches
        self.housekeeping(g, s)
        if g["stop"]:
            s["notes"].append("stop flag set: nothing is launched")
            if g["active"] == 0 and not g["render_active"] and not self.dry:
                self.del_kv("stop")
                s["stopped"] = True
        else:
            self.phase_submit(g, s)
            self.phase_accept(g, s)
            self.phase_draft(g, s)
            self.phase_audit(g, s)
            self.phase_verify(g, s)
            self.phase_register(g, s)
            promoted = self.phase_promote(g, s)
            self.phase_scout(g, s, self.pipeline() + (promoted if self.dry else 0))
            self.phase_backfill(g, s)
        self.totals["loops"] += 1
        self.totals["launched"] += len(s["launched"])
        self.totals["reaped"] += len(s["reaped"])
        return s

    def run(self, once=False) -> dict:
        while True:
            try:
                s = self.loop_once()
            except Exception as e:  # never die silently and never spin: record, wait, try again
                msg = f"scheduler loop failed: {type(e).__name__}: {e}"
                self.log(msg)
                s = {"error": msg, "stopped": False, "launched": [], "would": [], "reaped": [], "actions": [],
                     "notes": []}
                if not self.dry:
                    with contextlib.suppress(Exception):
                        self.report_once("loop", msg)
                if once:
                    return s
            if once or s.get("stopped"):
                return s
            self.sleep(config.LOOP_SEC)


# ---------- commands ----------
def _emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, default=str))
    return code


def cmd_run(args) -> int:
    ap = argparse.ArgumentParser(prog="cp run")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the single-instance guard (stale scheduler.pid)")
    a = ap.parse_args(args)
    db = store.connect()
    try:
        if not a.dry_run:
            pid = store.kv_get(db, config.SCHEDULER_PID_KEY)
            if (not a.force and isinstance(pid, int) and pid != os.getpid() and store.pid_alive(pid)):
                return _emit({"ok": False, "error": f"scheduler already running (pid {pid}); use --force only when "
                                                    "that pid is not a scheduler"}, 1)
            with store.writer(db):
                store.kv_set(db, config.SCHEDULER_PID_KEY, os.getpid())
        sched = Scheduler(dry_run=a.dry_run, db=db)
        try:
            s = sched.run(once=a.once or a.dry_run)
        finally:
            if not a.dry_run and store.kv_get(db, config.SCHEDULER_PID_KEY) == os.getpid():
                with store.writer(db):
                    store.kv_del(db, config.SCHEDULER_PID_KEY)
        gates = s.get("gates", {})
        out = {"ok": "error" not in s, "once": a.once, "dry_run": a.dry_run, "stopped": s.get("stopped", False),
               "blocked": gates.get("blocked", {}),
               "stop": gates.get("stop", False), "gates": gates, "loops": sched.totals["loops"],
               "launched": s.get("launched", []), "would_launch": s.get("would", []), "reaped": s.get("reaped", []),
               "actions": s.get("actions", []), "notes": s.get("notes", []), "totals": sched.totals}
        if "error" in s:
            out["error"] = s["error"]
        return _emit(out, 0 if out["ok"] else 1)
    finally:
        db.close()


def cmd_stop(args) -> int:
    db = store.connect()
    try:
        with store.writer(db):
            store.kv_set(db, "stop", True)
        n = len(base.active_runs(db))
        return _emit({"ok": True, "stop": True, "active_runs": n})
    finally:
        db.close()


def cmd_block_resume(cmd, args) -> int:
    ap = argparse.ArgumentParser(prog=f"cp {cmd}")
    ap.add_argument("agent", choices=list(config.AGENTS))
    if cmd == "block":
        ap.add_argument("--reason", required=True)
    a = ap.parse_args(args)
    db = store.connect()
    try:
        if cmd == "block":
            accounts.block(db, a.agent, a.reason)
            return _emit({"ok": True, "blocked": a.agent, "reason": a.reason})
        had = accounts.unblock(db, a.agent)
        return _emit({"ok": True, "resumed": a.agent, "was_blocked": had})
    finally:
        db.close()


def main(argv) -> int:
    config.utf8_stdio()
    args = list(argv)
    cmd, rest = (args[0], args[1:]) if args else ("run", [])
    try:
        if cmd == "run":
            return cmd_run(rest)
        if cmd == "stop":
            return cmd_stop(rest)
        if cmd in ("block", "resume"):
            return cmd_block_resume(cmd, rest)
        return _emit({"ok": False, "error": f"unknown scheduler command {cmd}"}, 1)
    except SystemExit:
        return _emit({"ok": False, "error": f"bad arguments: {' '.join(args)}"}, 1)
    except Exception as e:  # never swallow: report as the command result
        return _emit({"ok": False, "error": f"{type(e).__name__}: {e}"}, 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
