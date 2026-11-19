"""Scheduler: `ep run [--once] [--dry-run] [--force]`, `ep stop`, `ep resume <agent>`, `ep block <agent> --reason T`.

One loop:
  1. reap finished runs, classify, apply outcome rules (no_result, spent/limit/auth/crash)
  2. gates (free RAM, active agent processes, browser jobs, blocked agents, stop)
  3. housekeeping: sync-xlsx every 60 s, Chrome tab sweep
  4. launches in order: submit, verify, register, pool promotion, scout, backfill; then render
Agents finish their own items with `ep done/fail/verify/...`; the scheduler only reads the item after the process ends.
`--dry-run` is read-only: it writes nothing (no claims, no reaping, no kv) and starts no process; it reports what it
would launch. Every main() prints exactly one JSON line; progress lines go to stderr (scheduler.log when hidden).
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ep import accounts, chrome, cli, config, model, policy, profile, prompts, ram, register, store
from ep.agents import ADAPTERS, base

BROWSER_LANES = ("browser",)
SUBMIT_KINDS = ("submit",)
TICK_SYNC, TICK_BACKFILL, TICK_RENDER = "tick.sync", "tick.backfill", "tick.render"


class SystemAdapter(base.Adapter):
    """Runs `python -X utf8 ep.py <args>` as a tracked child (render, backfill). Not an agent: no account, no lanes."""
    name = "system"

    def __init__(self, args):
        self.args = list(args)

    def command(self, prompt, kind):
        return [sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "ep.py"), *self.args]


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sns_logged_in() -> list:
    """Sites logged in according to the cached sns.json (no Chrome launch)."""
    try:
        with open(os.path.join(config.data_dir(), "sns.json"), encoding="utf-8") as f:
            d = json.load(f)
        return sorted(k for k, v in d.items() if isinstance(v, dict) and v.get("logged_in"))
    except (OSError, ValueError, AttributeError):
        return []


class Scheduler:
    def __init__(self, dry_run=False, adapters=None, db=None, log=None):
        self.dry = dry_run
        self.adapters = adapters or ADAPTERS
        self.db = db or store.connect()
        self.log = log or (lambda m: print(f"{store.now_iso()} {m}", file=sys.stderr, flush=True))
        self.sleep = time.sleep
        self.policy_fn = policy.load_policy
        self.crash_streak = {}
        self.render_hash = {}
        self.promote_bad = set()
        self.totals = {"launched": 0, "reaped": 0, "loops": 0}
        self._acct = {}
        self._chrome = None

    # ---------- small helpers ----------
    @contextlib.contextmanager
    def _w(self):
        if self.dry:
            raise RuntimeError("dry run must not write")
        with store.writer(self.db):
            yield self.db

    def kv(self, key, default=None):
        return store.kv_get(self.db, key, default)

    def set_kv(self, key, value):
        with self._w():
            store.kv_set(self.db, key, value)

    def del_kv(self, key):
        with self._w():
            store.kv_del(self.db, key)

    def add_error(self, agent, msg):
        self.log(f"error [{agent}] {msg[:300]}")
        with self._w():
            store.add_error(self.db, agent, msg)

    def report_once(self, key, msg, agent="system", notes=None):
        """Error row only when the message differs from the last one for this key (no row every 15 s)."""
        if self.dry:
            if notes is not None:
                notes.append(msg)
            return
        if str(self.kv(f"err.{key}")) != msg:
            self.add_error(agent, msg)
            self.set_kv(f"err.{key}", msg)

    def clear_once(self, key):
        if not self.dry and self.kv(f"err.{key}") is not None:
            self.del_kv(f"err.{key}")

    def due(self, key, sec) -> bool:
        try:
            last = float(self.kv(key, 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        return time.time() - last >= sec

    def tick(self, key):
        if not self.dry:
            self.set_kv(key, time.time())

    def pipeline(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM items WHERE state IN ('new','verifying','ready')").fetchone()[0]

    # ---------- reaping and outcome rules ----------
    def reap_and_apply(self) -> list:
        out = []
        for r in base.reap(self.db, self.adapters):
            try:
                out.append(self.finish_run(r))
            except Exception as e:  # never swallow: the run row stays ended, the problem is visible
                self.add_error("system", f"outcome handling of run {r['run']} failed: {type(e).__name__}: {e}")
        return out

    def marker_text(self, r) -> str:
        rx = {"spent": base.SPENT_RE, "limit": base.LIMIT_RE, "auth": base.AUTH_RE}.get(r["cls"])
        for line in (r["tail"] or "").splitlines():
            if rx and rx.search(line):
                return line.strip()[:200]
        return "no marker line"

    def finish_run(self, r) -> dict:
        agent, kind, iid = r["agent"], r["kind"], r["item_id"]
        if agent in config.AGENTS:
            self.agent_outcome(r)
        elif kind in ("render", "backfill"):
            self.system_outcome(r)
        if kind == "scout":
            self.scout_done(r)
        item_outcome = self.item_outcome(r) if iid else None
        # the agent-level class (spent/limit/auth/crash) names the run; item outcomes label clean runs only
        if r.get("timeout"):
            final = "timeout"
        elif r["orphan"]:
            final = "orphan"
        else:
            final = r["cls"] if r["cls"] != "ok" else (item_outcome or "ok")
        base.set_outcome(self.db, r["run"], final)
        self.log(f"ended run {r['run']} {agent} {kind} {iid or ''} code={r['code']} class={r['cls']} -> {final}")
        return {"run": r["run"], "agent": agent, "kind": kind, "item": iid, "code": r["code"], "class": r["cls"],
                "outcome": final}

    def codex_home_for(self, name):
        return next((h for h in accounts.codex_homes() if accounts.codex_name(h) == name), None)

    def agent_outcome(self, r):
        agent, kind, cls, code = r["agent"], r["kind"], r["cls"], r["code"]
        if cls == "ok":
            self.crash_streak[agent] = 0
            return
        if cls == "crash":
            if r.get("timeout"):
                limit = config.RUN_TIMEOUT_SEC.get(kind, config.RUN_TIMEOUT_DEFAULT_SEC)
                msg = f"{agent} {kind} run timed out after {limit} s and was killed; log {r['log']}"
            else:
                msg = f"{agent} {kind} run crashed (exit {code}); log {r['log']}"
            self.add_error(agent, msg)
            self.crash_streak[agent] = self.crash_streak.get(agent, 0) + 1
            if self.crash_streak[agent] >= config.CRASH_BLOCK_AFTER:
                accounts.block(self.db, agent, f"{self.crash_streak[agent]} consecutive crashes; last: {msg}")
            return
        self.crash_streak[agent] = 0
        msg = f"{agent} {kind} run ended {cls} (exit {code}): {self.marker_text(r)}; log {r['log']}"
        if cls == "auth":
            accounts.block(self.db, agent, msg)
        elif agent == "codex":
            if accounts.limit_transient("codex", r["account"]):
                self.add_error(agent, msg + "; live usage is under 100 % and fresh: treated as transient")
            elif accounts.codex_switch(self.db, self.codex_home_for(r["account"])):
                pass  # codex_switch recorded "Codex account X spent; switched to Y" (or another run already switched)
            else:
                accounts.block(self.db, agent, msg + "; every Codex account is spent")
        elif agent == "claude":
            acct = r["account"]
            if acct:
                spent = self.kv("claude_spent", []) or []
                if acct not in spent:
                    spent.append(acct)
                with self._w():
                    store.kv_set(self.db, "claude_spent", spent)
            self._acct.pop("claude", None)
            new = accounts.claude_pick(self.db)
            if new:
                self.add_error(agent, f"Claude account {acct} {cls}; switched to {new}")
            else:
                accounts.block(self.db, agent, msg + "; every Claude account is spent or at 100 %")
        else:
            accounts.block(self.db, agent, msg)

    def system_outcome(self, r):
        kind, cls = r["kind"], r["cls"]
        key = config.RENDER_UNAVAILABLE_KEY if kind == "render" else config.BACKFILL_UNAVAILABLE_KEY
        if kind == "render":
            h = self.render_hash.pop(r["run"], None)
            if cls == "ok" and r["code"] == 0 and h:
                self.set_kv("render_content_hash", h)  # the scheduler's own key; render owns `render_hash`
        if cls == "ok":
            return
        if "module not available" in (r["tail"] or ""):
            msg = f"ep {kind} is not available yet (module missing); not retried until the module exists"
            if self.kv(key) is None:
                self.add_error("system", msg)
            self.set_kv(key, msg)
        else:
            self.add_error("system", f"{kind} run ended {cls} (exit {r['code']}); log {r['log']}")

    def scout_done(self, r):
        out = self.kv(f"scout.out.{r['run']}")
        with self._w():
            store.kv_del(self.db, f"scout.out.{r['run']}")
        if r["cls"] == "ok" and isinstance(out, str) and not os.path.exists(out):
            self.add_error(r["agent"], f"scout run {r['run']} ended without writing {os.path.basename(out)}")

    def item_outcome(self, r):
        it = store.get_item(self.db, r["item_id"])
        if not it:
            return None
        kind, st = r["kind"], it["state"]
        if kind == "verify" and st == "verifying":
            return self.no_result_verify(it, r)
        if kind in SUBMIT_KINDS:
            if st == "submitting":
                return self.no_result_submit(it, r)
        return None

    def _why(self, r) -> str:
        return f"{r['kind']} run {r['run']} ended {r['cls']} (exit {r['code']}) without a result"

    def no_result_verify(self, it, r):
        tries = dict(it.get("tries") or {})
        n = tries["no_result"] = tries.get("no_result", 0) + 1
        agent = r["agent"]
        with self._w():
            if n >= config.NO_RESULT_MAX:
                q = f"agents ended {n} verify runs without a result; log: {r['log']}"
                store.update_item(self.db, it["id"], state="owner", question=q, tries=tries, claimed_by=None,
                                  claimed_at=None)
                store.add_history(self.db, it["id"], agent, "owner", q)
            else:
                # verify tries stay as they are (not reset); only the no_result counter grows
                store.update_item(self.db, it["id"], state="new", lane=None, claimed_by=None, claimed_at=None,
                                  tries=tries)
                store.add_history(self.db, it["id"], agent, "no_result", self._why(r) + "; back to new")
        return "no_result"

    def _fail_unknown(self, it, r, agent):
        actor = agent if it.get("claimed_by") == agent else "system"
        res = cli.call(["fail", it["id"], "--agent", actor, "--reason",
                        "agent ended without result; completion unknown", "--no-resubmit"],
                       internal=(actor == "system"))
        if not res.get("ok"):
            self.add_error("system", f"cannot fail {it['id']} after no_result: {res.get('error')}")

    def no_result_submit(self, it, r):
        agent = r["agent"]
        tries = dict(it.get("tries") or {})
        n = tries["no_result"] = tries.get("no_result", 0) + 1
        with self._w():
            store.update_item(self.db, it["id"], tries=tries)
            store.add_history(self.db, it["id"], agent, "no_result", self._why(r) + f" (no_result {n})")
        if n >= config.NO_RESULT_MAX:  # completion unknown -> never resubmit automatically
            self._fail_unknown(it, r, agent)
        else:
            res = cli.call(["release", it["id"], "--agent", agent])
            if not res.get("ok"):
                self.add_error("system", f"release of {it['id']} failed ({res.get('error')}); failing it instead")
                self._fail_unknown(it, r, agent)
        return "no_result"

    # ---------- gates ----------
    def gates(self) -> dict:
        pol = self.policy_fn()
        runs = base.active_runs(self.db)
        if self.dry:  # nothing is reaped in a dry run: only runs whose process is still alive count
            runs = [x for x in runs if x["pid"] and store.pid_alive(x["pid"])]
        agent_runs = [x for x in runs if x["kind"] != "render"]
        g = {"pol": pol, "free_gb": ram.free_gb(), "floor": float(pol.get("ram_floor_gb", config.RAM_FLOOR_GB)),
             "max_agents": int(pol.get("max_agents", config.MAX_AGENTS)), "active": len(agent_runs),
             "counts": {a: 0 for a in config.AGENTS}, "browser": 0, "scouts": 0,
             "backfill_active": any(x["kind"] == "backfill" for x in agent_runs),
             "render_active": any(x["kind"] == "render" for x in runs),
             "blocked": accounts.blocked(self.db),
             "stop": bool(self.kv("stop", False))}
        for x in agent_runs:
            if x["agent"] in g["counts"]:
                g["counts"][x["agent"]] += 1
            if x["kind"] == "scout":
                g["scouts"] += 1
            if x["kind"] == "submit" and x["item_id"]:
                it = store.get_item(self.db, x["item_id"]) or {}
                if it.get("lane") in BROWSER_LANES:
                    g["browser"] += 1
        return g

    def ram_ok(self, g) -> bool:
        return ram.free_gb() >= g["floor"]

    def local_ok(self, g) -> bool:
        """This PC has room for a job that must run here (scout, backfill, render)."""
        return ram.free_gb() >= g["floor"]

    def slot_free(self, g) -> bool:
        return g["active"] < g["max_agents"] and self.ram_ok(g)

    def note_launch(self, g, agent, kind, lane=None):
        if kind != "render":
            g["active"] += 1
        if agent in g["counts"]:
            g["counts"][agent] += 1
        if kind == "scout":
            g["scouts"] += 1
        if kind == "backfill":
            g["backfill_active"] = True
        if lane in BROWSER_LANES:
            g["browser"] += 1

    def account_ok(self, agent) -> bool:
        """The agent has a usable account right now (cached per loop). An agent with none is blocked (visible)."""
        if agent not in ("claude", "codex"):
            return True
        if agent not in self._acct:
            if agent == "claude":
                if self.dry:
                    spent = self.kv("claude_spent", []) or []
                    cur = self.kv("claude_account")
                    acct = (accounts.claude_rank(accounts.usage("claude"), spent, cur) or [""])[0]
                else:
                    acct = accounts.claude_pick(self.db)
            else:
                acct = accounts.codex_home_readonly(self.db) if self.dry else accounts.codex_home(self.db)
            self._acct[agent] = acct
            if not acct and not self.dry:
                accounts.block(self.db, agent, f"no {agent} account available (all spent, at 100 %, or none configured)")
        return bool(self._acct[agent])

    def candidates(self, g, pref_key, lane=None, exclude=()) -> list:
        """Agents that may take the job: not blocked, capable, usable account, grok only when policy says `on`.
        Best first: fewest active jobs, then the order in config.AGENT_PREFERENCE."""
        out = []
        for i, a in enumerate(config.AGENT_PREFERENCE[pref_key]):
            if a in g["blocked"] or a in exclude or a not in self.adapters or a not in config.ENABLED_AGENTS:
                continue
            if a == "grok" and g["pol"].get("grok") != "on":  # off_until_needed and off: never launched
                continue
            if lane and lane not in config.CAPABILITIES.get(a, set()):
                continue
            if not self.account_ok(a):
                continue
            if a in config.PER_AGENT_MAX and g["counts"].get(a, 0) >= config.PER_AGENT_MAX[a]:
                continue
            out.append((g["counts"].get(a, 0), i, a))
        return [a for _, _, a in sorted(out)]

    # ---------- Chrome ----------
    def chrome_ready(self, s) -> bool:
        """Chrome with CDP is up (started hidden when needed). Cached per loop; never blocks longer than ~25 s."""
        if self._chrome is None:
            if chrome.alive():
                self._chrome = True
            elif self.dry:
                s["notes"].append("Chrome is not running: a real loop would run `ep chrome ensure` first")
                self._chrome = True
            else:
                self._chrome = self.ensure_chrome()
        return self._chrome

    def ensure_chrome(self) -> bool:
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "ep.py"), "chrome",
                                "ensure"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=config.CHROME_ENSURE_TIMEOUT_SEC, stdin=subprocess.DEVNULL,
                               creationflags=base.NO_WINDOW)
            ok = json.loads((p.stdout or "").strip().splitlines()[-1]).get("ok") is True
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError) as e:
            ok = False
            p = None
            why = f"{type(e).__name__}"
        if ok:
            self.clear_once("chrome-ensure")
            return True
        if p is not None:
            why = f"exit {p.returncode}: {(p.stdout or p.stderr or '')[-200:]}"
        self.report_once("chrome-ensure", f"chrome ensure failed ({why}); browser jobs wait")
        return False

    def tab_sweep(self, s):
        if self.dry:
            s["actions"].append("would sweep Chrome tabs (close-stale) when Chrome runs")
            return
        if not chrome.alive():
            return
        try:
            r = chrome.close_stale(config.TAB_MAX_AGE_MIN)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r.get("ok"):
            self.clear_once("tab-sweep")
            if r.get("closed"):
                s["actions"].append(f"closed {r['closed']} stale Chrome tab(s)")
        else:
            self.report_once("tab-sweep", f"tab sweep failed: {r.get('error')}")

    # ---------- prompts and launching ----------
    def make_prompt(self, kind, item, agent, out=None, known=None) -> str:
        region = (item.get("region") or "KR").upper()
        ctx = {"agent": agent, "today": model.today_kst().isoformat(), "region": region,
               "sns_logged_in": sns_logged_in()}
        if kind == "scout":
            ctx["out"], ctx["known"] = out, known
        else:
            ctx["profile_lines"] = profile.summary_for_prompt(region)
            if (item.get("tries") or {}).get("submit", 0) > 0 and item.get("reason"):
                ctx["last_failure"] = item["reason"]
        return prompts.build(kind, item, ctx)

    def plan_entry(self, adapter, agent, kind, item, prompt, tag=None) -> dict:
        iid = item.get("id") if item else None
        cmd = ["<prompt %d chars>" % len(prompt) if c == prompt else c for c in adapter.command(prompt, kind)]
        return {"agent": agent, "kind": kind, "item": iid, "log": base.log_path(agent, kind, iid or tag, create=False), "command": cmd}

    def undo_claim(self, kind, item, agent, why):
        iid = item["id"]
        if kind == "verify":
            fresh = store.get_item(self.db, iid) or item
            tries = dict(fresh.get("tries") or {})
            for k in ("verify", f"verify.{agent}"):
                tries[k] = max(0, tries.get(k, 0) - 1)
            with self._w():
                store.update_item(self.db, iid, state="new", claimed_by=None, claimed_at=None, tries=tries)
                store.add_history(self.db, iid, "system", "launch-failed", why)
        else:
            with self._w():
                store.add_history(self.db, iid, "system", "launch-failed", why)
            res = cli.call(["release", iid, "--agent", "system"], internal=True)
            if not res.get("ok"):
                self.add_error("system", f"could not release {iid} after a failed launch: {res.get('error')}")

    def start(self, g, s, agent, kind, item) -> bool:
        """Claim (submit by id, verify by next-batch) and launch one run. Dry run: only plan. True = started."""
        adapter = self.adapters[agent]
        lane = item.get("lane") if item else None
        if not self.ram_ok(g):
            s["notes"].append(f"{agent} {kind} {item.get('id') if item else ''} waits: not enough free RAM")
            return False
        if not self.account_ok(agent):
            return False
        if self.dry:
            try:
                prompt = self.make_prompt(kind, item, agent)
            except Exception as e:
                s["notes"].append(f"prompt for {item.get('id')} cannot be built: {type(e).__name__}: {e}")
                return False
            s["would"].append(self.plan_entry(adapter, agent, kind, item, prompt))
            self.note_launch(g, agent, kind, lane)
            return True
        if kind == "verify":
            r = cli.call(["next-batch", "--agent", agent, "--size", "1"])
            items = r.get("items") or []
            if not r.get("ok") or not items:
                return False
            item = items[0]
        else:
            r = cli.call(["claim", "--agent", agent, "--id", item["id"]])
            if not r.get("ok") or not r.get("item"):
                s["notes"].append(f"claim of {item['id']} by {agent} refused: {r.get('error') or r.get('reason')}")
                return False
            item = r["item"]
        try:
            prompt = self.make_prompt(kind, item, agent)
            run_id = base.launch(adapter, kind, item, prompt, self.db, account=self._acct.get(agent) or None)
        except base.LaunchError as e:
            self.undo_claim(kind, item, agent, f"launch failed: {e}")
            return False
        except Exception as e:
            self.add_error(agent, f"{kind} for {item['id']} not launched: {type(e).__name__}: {e}")
            self.undo_claim(kind, item, agent, f"not launched: {type(e).__name__}")
            return False
        self.note_launch(g, agent, kind, item.get("lane"))
        s["launched"].append({"run": run_id, "agent": agent, "kind": kind, "item": item["id"]})
        self.log(f"launched run {run_id} {agent} {kind} {item['id']}")
        return True

    # ---------- launch phases ----------
    def phase_submit(self, g, s):
        ready = [it for it in store.items(self.db, "state='ready'")
                 if not (it.get("flags") or {}).get("no_resubmit") and not model.is_expired(it)]
        ready.sort(key=model.order_key)
        for it in ready:
            if not self.slot_free(g):
                break
            lane = it.get("lane")
            if lane not in config.LANES:
                continue
            if lane in BROWSER_LANES and g["browser"] >= config.MAX_BROWSER:
                continue
            cands = self.candidates(g, "submit_form" if lane == "form" else "submit_browser", lane)
            if not cands:
                continue
            if lane in BROWSER_LANES and not self.chrome_ready(s):
                continue
            self.start(g, s, cands[0], "submit", it)

    def peek_new(self, agent, planned):
        key = f"verify.{agent}"
        rows = [it for it in store.items(self.db, "state='new'")
                if (it.get("tries") or {}).get(key, 0) < config.VERIFY_MAX_TRIES and it["id"] not in planned
                and not model.is_expired(it)]
        rows.sort(key=model.order_key)
        return rows[0] if rows else None

    def phase_verify(self, g, s):
        planned = set()
        while self.slot_free(g):
            if not self.db.execute("SELECT 1 FROM items WHERE state='new' LIMIT 1").fetchone():
                return
            started = False
            for agent in self.candidates(g, "verify"):
                item = self.peek_new(agent, planned) if self.dry else None
                if self.dry and not item:
                    continue
                if self.start(g, s, agent, "verify", item):
                    started = True
                    if item:
                        planned.add(item["id"])
                    break
            if not started:
                return

    def phase_register(self, g, s):
        files = register.pending_files(self.db)
        if not files:
            return
        try:
            r = register.run(dry_run=self.dry)
        except Exception as e:
            self.report_once("register", f"register failed: {type(e).__name__}: {e}", notes=s["notes"])
            return
        self.clear_once("register")
        s["actions"].append(f"{'would register' if self.dry else 'registered'} {r['files']} scout file(s): "
                            f"added {r['added']}, pool {r['pooled']}, duplicate {r['duplicate']}, invalid {r['invalid']}")

    def phase_promote(self, g, s) -> int:
        """Fill the pipeline from the pool (soonest deadline first) before scouting. Returns the planned/added count."""
        gap = config.PIPELINE_MAX - self.pipeline()
        if gap <= 0:
            return 0
        today = model.today_kst().isoformat()
        cands = []
        for p in store.pool_rows(self.db, "promoted_to IS NULL OR promoted_to=''"):
            if p["id"] in self.promote_bad:
                continue
            if (p.get("region") or "KR") not in config.ACTIVE_REGIONS:
                continue
            dl, _ = model.parse_deadline(p.get("deadline"), p.get("region") or "KR")
            if dl and dl >= today:  # pool rows without a parseable deadline are never promoted
                cands.append((dl, config.REGIONS.get(p.get("region") or "KR", {"rank": 9})["rank"], p["id"]))
        ids = [c[2] for c in sorted(cands)[:gap]]
        if not ids:
            return 0
        if self.dry:
            s["actions"].append(f"would promote {len(ids)} pool row(s): {ids[:5]}...")
            return len(ids)
        r = cli.call(["pool-promote", *ids, "--agent", "system"], internal=True)
        if not r.get("ok"):
            self.report_once("promote", f"pool-promote failed: {r.get('error')}")
            return 0
        added, bad = 0, []
        for x in r.get("results", []):
            if x.get("status") == "added":
                added += 1
            elif x.get("status") == "invalid":
                self.promote_bad.add(x.get("pool_id"))
                bad.append(f"{x.get('pool_id')}: {x.get('error')}")
        if bad:
            self.add_error("system", "pool-promote rejected " + "; ".join(bad[:5]))
        if added:
            s["actions"].append(f"promoted {added} pool row(s) into the pipeline")
        return added

    def phase_scout(self, g, s, pipeline):
        if (pipeline >= config.PIPELINE_MAX or g["scouts"] >= config.SCOUT_MAX_ACTIVE or not self.slot_free(g)
                or not self.local_ok(g)):  # scout writes its inbox file on this PC
            return
        if config.CRAWLER_SCOUT and not self.due("tick.scout", config.SCOUT_MIN_INTERVAL_SEC):
            return  # crawler scouts are cheap and end fast: space them out (3 min)
        quota = config.SCOUT_QUOTA
        used = dict(self.kv("scout_round", {}) or {})
        region = next((r for r in ("KR", "JP", "US") if used.get(r, 0) < quota.get(r, 0)), None)
        if region is None:  # every quota used: the round ends and a new one starts
            used, region = {}, "KR"
        first = not any(used.values())
        today = model.today_kst().isoformat()
        exclude = ()
        if float(self.kv(f"gemini_cost.{today}", 0) or 0) >= float(g["pol"].get("gemini_daily_usd", 10)):
            exclude = ("gemini",)
            s["notes"].append("Gemini daily cost cap reached: no Gemini scouting today")
        cands = self.candidates(g, "scout", None, exclude)
        if not cands:
            return
        agent = cands[0]
        out = os.path.join(config.scout_inbox_dir(), f"{_now_stamp()}-{agent}-{region}.jsonl")
        known = config.known_events_path()
        adapter = self.adapters[agent]
        try:
            if not self.dry:
                os.makedirs(config.scout_inbox_dir(), exist_ok=True)
                if first or not os.path.exists(known):
                    register.write_known_events(self.db)
            prompt = self.make_prompt("scout", {"region": region}, agent, out=out, known=known)
        except Exception as e:
            self.report_once("scout-prompt", f"scout prompt/known list failed: {type(e).__name__}: {e}", agent,
                             s["notes"])
            return
        if self.dry:
            s["would"].append(self.plan_entry(adapter, agent, "scout", None, prompt, tag=region) | {"out": out})
            self.note_launch(g, agent, "scout")
            return
        try:
            wrapper = None
            if region == "KR" and config.CRAWLER_SCOUT and os.path.exists(os.path.join(config.PROJECT_DIR, "ep", "crawler.py")):
                # Python crawler collects, ONE agent call judges (no step by step browsing)
                wrapper = [sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "ep", "scout_run.py"),
                           "--agent", agent, "--out", out, "--known", known]
            run_id = base.launch(adapter, "scout", None, prompt, self.db, tag=region,
                                 account=self._acct.get(agent) or None, command=wrapper)
        except base.LaunchError:
            return
        except Exception as e:
            self.add_error(agent, f"scout not launched: {type(e).__name__}: {e}")
            return
        used[region] = used.get(region, 0) + 1
        self.tick("tick.scout")
        with self._w():
            store.kv_set(self.db, "scout_round", used)
            store.kv_set(self.db, f"scout.out.{run_id}", out)
            if agent == "gemini":  # the Gemini CLI reports no cost: estimate per scout run
                cost = float(store.kv_get(self.db, f"gemini_cost.{today}", 0) or 0) + config.GEMINI_SCOUT_RUN_USD
                store.kv_set(self.db, f"gemini_cost.{today}", round(cost, 4))
        self.note_launch(g, agent, "scout")
        s["launched"].append({"run": run_id, "agent": agent, "kind": "scout", "item": None, "region": region})
        self.log(f"launched run {run_id} {agent} scout {region}")

    def system_available(self, key, module) -> bool:
        """False while kv `<key>` says the module was missing and it still is (explicit signal: module found again)."""
        if self.kv(key) is None:
            return True
        if importlib.util.find_spec(f"ep.{module}") is None:
            return False
        if not self.dry:
            self.del_kv(key)
        return True

    def launch_system(self, g, s, kind, args) -> int | None:
        adapter = SystemAdapter(args)
        if self.dry:
            s["would"].append(self.plan_entry(adapter, "system", kind, None, ""))
            self.note_launch(g, "system", kind)
            return -1
        try:
            run_id = base.launch(adapter, kind, None, "", self.db)
        except base.LaunchError:
            return None
        self.note_launch(g, "system", kind)
        s["launched"].append({"run": run_id, "agent": "system", "kind": kind, "item": None})
        self.log(f"launched run {run_id} system {kind}")
        return run_id

    def phase_backfill(self, g, s):
        if g["backfill_active"] or not self.slot_free(g) or not self.local_ok(g) or not self.system_available(
                config.BACKFILL_UNAVAILABLE_KEY, "backfill"):
            return
        if not self.due(TICK_BACKFILL, config.BACKFILL_EVERY_SEC):
            return
        # nothing to do when no item lacks a minimum prize value
        if not self.db.execute("SELECT 1 FROM items WHERE prize_min_value IS NULL AND COALESCE(prize,'') != '' "
                               "LIMIT 1").fetchone():
            return
        if self.launch_system(g, s, "backfill", ["backfill", "--limit", str(config.BACKFILL_LIMIT)]) is not None:
            self.tick(TICK_BACKFILL)

    def content_hash(self) -> str:
        q = self.db.execute
        parts = [q("SELECT COALESCE(MAX(seq),0) FROM history").fetchone()[0],
                 q("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0],
                 q("SELECT COALESCE(MAX(id),0) FROM wins").fetchone()[0],
                 q("SELECT COUNT(*) FROM pool WHERE promoted_to IS NULL OR promoted_to=''").fetchone()[0],
                 model.today_kst().isoformat(), json.dumps(accounts.blocked(self.db), sort_keys=True)]
        return hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()

    def phase_render(self, g, s):
        if g["render_active"] or not self.local_ok(g) or not self.system_available(
                config.RENDER_UNAVAILABLE_KEY, "render"):
            return
        if not self.due(TICK_RENDER, config.RENDER_SEC):
            return
        h = self.content_hash()
        self.tick(TICK_RENDER)
        if h == self.kv("render_content_hash"):
            return
        run_id = self.launch_system(g, s, "render", ["render"])
        if run_id is not None and run_id >= 0:
            self.render_hash[run_id] = h

    def housekeeping(self, g, s):
        if self.due(TICK_SYNC, config.SYNC_XLSX_SEC):
            if self.dry:
                s["actions"].append("would run sync-xlsx")
            else:
                r = cli.call(["sync-xlsx"])
                self.tick(TICK_SYNC)
                if r.get("ok"):
                    self.clear_once("sync-xlsx")
                    if r.get("queued"):
                        s["actions"].append(f"sync-xlsx queued {r['queued']} pending change(s) from the sheet")
                else:
                    self.report_once("sync-xlsx", f"sync-xlsx failed: {r.get('error')}")
        self.tab_sweep(s)

    # ---------- one loop ----------
    def loop_once(self) -> dict:
        self._acct, self._chrome = {}, None
        s = {"launched": [], "would": [], "reaped": [], "actions": [], "notes": [], "stopped": False}
        if not self.dry:
            s["reaped"] = self.reap_and_apply()
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
            self.phase_verify(g, s)
            self.phase_register(g, s)
            promoted = self.phase_promote(g, s)
            self.phase_scout(g, s, self.pipeline() + (promoted if self.dry else 0))
            self.phase_backfill(g, s)
            self.phase_render(g, s)
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
    ap = argparse.ArgumentParser(prog="ep run")
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
    ap = argparse.ArgumentParser(prog=f"ep {cmd}")
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
    for st in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            st.reconfigure(encoding="utf-8")
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
