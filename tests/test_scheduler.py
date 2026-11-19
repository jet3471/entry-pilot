"""Scheduler stack tests: accounts, adapters (classification), register, scheduler loop and outcome rules.

Run: python -m unittest discover -s tests -v
Every test uses a fresh temp EP_DATA_DIR / EP_OWNER_DIR. Agents are fake adapters whose command is
`python -c "<script>"`; no real claude/codex/gemini/grok, Chrome or render is ever started
(Scheduler.launch_system and Chrome calls are replaced). The board is driven with ep.cli.call.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_BOOT = tempfile.mkdtemp(prefix="ep-sched-boot-")
os.environ["EP_DATA_DIR"] = os.path.join(_BOOT, "data")
os.environ["EP_AGENTS"] = "claude,codex,gemini,grok"  # the engine tests cover every adapter
os.environ["EP_OWNER_DIR"] = os.path.join(_BOOT, "owner")

from ep import accounts, chrome, cli, config, model, ram, register, scheduler, store  # noqa: E402
from ep.agents import base  # noqa: E402
from ep.agents.base import classify_log  # noqa: E402

TODAY = model.today_kst()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def ep(*argv):
    return cli.call([str(x) for x in argv])


class Fake(base.Adapter):
    """Fake agent: the command prints `text`, optionally sleeps, and exits with `code`."""

    def __init__(self, name, code=0, text="done", sleep=0):
        self.name, self.code, self.text, self.sleep = name, code, text, sleep

    def command(self, prompt, kind):
        return [sys.executable, "-c",
                f"import sys,time; print({self.text!r}); time.sleep({self.sleep}); sys.exit({self.code})", prompt]


def row(name, weekly=None, stale=True, session=0, status="ok"):
    return {"id": name, "status": status, "stale": stale,
            "limits": ([{"kind": "session", "percent": session}] if session is not None else [])
            + ([{"kind": "weekly_all", "percent": weekly}] if weekly is not None else [])}


CLAUDE_ROWS = [row("acct-a", 99), row("acct-c", 100), row("acct-default", 7)]
CODEX_ROWS = [row("acct-b", None) | {"limits": [{"kind": "primary", "percent": 65}, {"kind": "secondary", "percent": 51}]}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-sched-")
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "owner")
        os.makedirs(os.environ["EP_DATA_DIR"])
        os.makedirs(os.environ["EP_OWNER_DIR"])
        self.assertNotEqual(os.path.normcase(config.data_dir()), os.path.normcase(config.DEFAULT_DATA_DIR))
        self.db = store.connect()
        # Codex homes in the temp dir (third one has no auth.json)
        self.homes = []
        for n in ("acct-b", "acct-c", "noauth"):
            h = os.path.join(self.tmp, "codex", n)
            os.makedirs(h)
            if n != "noauth":
                with open(os.path.join(h, "auth.json"), "w") as f:
                    f.write("{}")
            self.homes.append(h)
        self.patch(config, "CODEX_HOMES", self.homes)
        self.patch(config, "CLAUDE_ACCOUNTS", ("acct-default", "acct-a", "acct-c"))
        self.patch(config, "CLAUDE_CONFIG_DIRS", [os.path.join(self.tmp, "claude", n) for n in ("one", "two", "three")])
        self.patch(config, "CRAWLER_SCOUT", False)  # tests use fake adapters: never start the real crawler wrapper
        self.usage_rows = {"claude": CLAUDE_ROWS, "codex": CODEX_ROWS}
        self._real_usage = accounts.usage
        self.patch(accounts, "usage", lambda agent, db=None, live=False: list(self.usage_rows.get(agent, [])))
        self.free = [16.0]
        self.patch(ram, "free_gb", lambda: self.free[0])
        self.patch(chrome, "alive", lambda: True)
        self.patch(chrome, "close_stale", lambda minutes, keep_url=None: {"ok": True, "closed": 0})
        base.PROCS.clear()

    def tearDown(self):
        for p in list(base.PROCS.values()):
            with contextlib.suppress(Exception):
                p.kill()
                p.wait(timeout=10)
        base.PROCS.clear()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    # ---- board helpers ----
    def item(self, iid):
        return store.get_item(self.db, iid)

    def new_item(self, **kw):
        row_ = {"title": "T " + kw.get("url", "x"), "org": "Org", "deadline": d(10), "region": "KR"}
        row_.update(kw)
        r = ep("add", "--agent", "gemini", "--json", json.dumps(row_))
        self.assertEqual(r["results"][0]["status"], "added", r)
        return r["results"][0]["id"]

    def ready_item(self, lane="form", **kw):
        iid = self.new_item(**kw)
        r = ep("verify", iid, "--agent", "codex", "--lane", lane)
        self.assertTrue(r["ok"], r)
        return iid

    def set_state(self, iid, state, **fields):
        store.update_item(self.db, iid, state=state, **fields)

    def policy(self, **kw):
        with open(os.path.join(config.data_dir(), "policy.json"), "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def fakes(self, **codes):
        return {a: Fake(a, codes.get(a, 0)) for a in config.AGENTS}

    def sched(self, dry=False, adapters=None):
        s = scheduler.Scheduler(dry_run=dry, adapters=adapters or self.fakes(), db=self.db, log=lambda m: None)
        s.sys_calls = []

        def launch_system(g, st, kind, args):  # never start render/backfill processes in tests
            s.sys_calls.append((kind, list(args)))
            return 1000 + len(s.sys_calls)  # a fake run id (never in runs)

        s.launch_system = launch_system
        s.ensure_chrome = lambda: True
        return s

    def run_fake(self, sched, agent, kind, item_id=None, code=0, text="ok", account=None, sleep=0):
        ad = Fake(agent, code, text, sleep)
        rid = base.launch(ad, kind, {"id": item_id} if item_id else None, "prompt", self.db, tag="T")
        if account:
            self.db.execute("UPDATE runs SET account=? WHERE id=?", (account, rid))
        if not sleep:
            base.PROCS[rid].wait(timeout=60)
        return rid

    def finish(self, sched):
        for p in list(base.PROCS.values()):
            p.wait(timeout=60)
        return sched.reap_and_apply()

    def kvget(self, key, default=None):
        return store.kv_get(self.db, key, default)

    def errors(self):
        return [r["message"] for r in self.db.execute("SELECT message FROM errors ORDER BY id")]

    def run_outcome(self, rid):
        return self.db.execute("SELECT outcome FROM runs WHERE id=?", (rid,)).fetchone()[0]

    def call_main(self, mod, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(argv)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, buf.getvalue())
        return rc, json.loads(lines[0])


# ---------------------------------------------------------------- accounts
class TestAccounts(Base):
    def test_claude_pick_ordering(self):
        self.assertEqual(accounts.claude_pick(self.db), "acct-default")  # 99 and 100 (stale) lose to 7
        self.assertEqual(self.kvget("claude_account"), "acct-default")
        store.kv_set(self.db, "claude_spent", ["acct-default"])
        self.assertEqual(accounts.claude_pick(self.db), "acct-a")  # acct-c is stale-100 and excluded
        store.kv_set(self.db, "claude_spent", ["acct-default", "acct-a"])
        self.assertEqual(accounts.claude_pick(self.db), "")  # nothing eligible
        store.kv_set(self.db, "claude_spent", [])
        # a fresh 100 % reading is excluded as well; ties go to the current account
        self.usage_rows["claude"] = [row("acct-a", 40, stale=False), row("acct-c", 40, stale=False), row("acct-default", 100, stale=False)]
        store.kv_set(self.db, "claude_account", "acct-c")
        self.assertEqual(accounts.claude_pick(self.db), "acct-c")
        store.kv_set(self.db, "claude_account", "acct-a")
        self.assertEqual(accounts.claude_pick(self.db), "acct-a")
        self.usage_rows["claude"] = []  # unreadable usage: pick still returns the current/first account
        self.assertEqual(accounts.claude_pick(self.db), "acct-a")
        self.assertTrue(any("without usage data" in e for e in self.errors()) or accounts.LAST_USAGE_ERROR == "")

    def test_claude_env_scrubs_and_uses_account_folder(self):
        env = accounts.claude_env({"CLAUDE_CODE_SESSION": "x", "claudeX": "y", "KEEP": "1"}, "acct-a")
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], config.CLAUDE_CONFIG_DIRS[1])
        self.assertFalse([k for k in env if k.upper().startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR"])
        self.assertFalse([k for k in env if "TOKEN" in k.upper()])  # no token is ever read or passed
        with self.assertRaises(ValueError):
            accounts.claude_env({}, "not-an-account")

    def test_usage_has_no_live_source(self):
        accounts.usage = self._real_usage
        self.assertEqual(accounts.usage("claude"), [])
        self.assertEqual(accounts.usage("codex", live=True), [])
        self.assertEqual(accounts.LAST_USAGE_ERROR, "")

    def test_codex_home_and_switch(self):
        a, b = self.homes[0], self.homes[1]
        self.assertEqual(accounts.codex_home(self.db), a)
        self.assertEqual(accounts.codex_homes(), [a, b])  # noauth skipped
        self.assertTrue(accounts.codex_switch(self.db, a))
        self.assertEqual(accounts.codex_home(self.db), b)
        self.assertEqual(self.kvget("codex_spent"), [a])
        self.assertIn("Codex account acct-b spent; switched to acct-c", self.errors())
        self.assertTrue(accounts.codex_switch(self.db, a))  # another run already switched: no change
        self.assertEqual(self.kvget("codex_spent"), [a])
        self.assertFalse(accounts.codex_switch(self.db, b))  # none left
        self.assertEqual(accounts.codex_home(self.db), "")
        accounts.unblock(self.db, "codex")
        self.assertIsNone(self.kvget("codex_spent"))
        self.assertEqual(accounts.codex_home(self.db), a)

    def test_codex_homes_dedup_by_name(self):
        # two folders for one account (same short name) share credits: keep one, and spending it blocks codex
        base = os.path.join(self.tmp, "dup")
        a, b = os.path.join(base, "x", "acct-default"), os.path.join(base, "y", "acct-default")
        for h in (a, b):
            os.makedirs(h)
            open(os.path.join(h, "auth.json"), "w").close()
        self.patch(config, "CODEX_HOMES", [a, b])
        homes = accounts.codex_homes()
        self.assertEqual(len(homes), 1)
        self.assertEqual(accounts.codex_name(homes[0]), "acct-default")
        self.assertFalse(accounts.codex_switch(self.db, a))  # the only account is now spent -> block
        self.assertEqual(accounts.codex_home(self.db), "")

    def test_limit_transient(self):
        def usage(rows):
            self.usage_rows["codex"] = rows
        usage([row("acct-b", None, stale=False) | {"limits": [{"kind": "primary", "percent": 50},
                                                              {"kind": "secondary", "percent": 99}]}])
        self.assertTrue(accounts.limit_transient("codex", self.homes[0]))
        self.assertTrue(accounts.limit_transient("codex", "acct-b"))
        usage([row("acct-b", None, stale=True) | {"limits": [{"kind": "primary", "percent": 10}]}])
        self.assertFalse(accounts.limit_transient("codex", "acct-b"))  # stale reading does not excuse a limit
        usage([row("acct-b", None, stale=False) | {"limits": [{"kind": "primary", "percent": 100}]}])
        self.assertFalse(accounts.limit_transient("codex", "acct-b"))
        usage([])
        self.assertFalse(accounts.limit_transient("codex", "acct-b"))
        self.assertFalse(accounts.limit_transient("gemini", "x"))

    def test_block_unblock(self):
        accounts.block(self.db, "gemini", "billing")
        self.assertEqual(accounts.blocked(self.db), {"gemini": "billing"})
        self.assertTrue(any("gemini blocked" in e for e in self.errors()))
        store.kv_set(self.db, "claude_spent", ["acct-a"])
        accounts.block(self.db, "claude", "x")
        self.assertTrue(accounts.unblock(self.db, "claude"))
        self.assertIsNone(self.kvget("claude_spent"))
        with self.assertRaises(ValueError):
            accounts.block(self.db, "phone", "x")  # no phone pseudo-agent
        with self.assertRaises(ValueError):
            accounts.block(self.db, "nobody", "x")

    def test_classify(self):
        self.assertEqual(classify_log("anything 429 here", 0), "ok")
        self.assertEqual(classify_log("You've hit your 5-hour limit \u00b7 resets 7:10pm (Asia/Seoul)", 0), "limit")
        self.assertEqual(classify_log("x" * 2000 + " rate limit mentioned in a long transcript", 0), "ok")
        self.assertEqual(classify_log("Error: Out of credits", 1), "spent")
        self.assertEqual(classify_log("402 usage balance exhausted", 1), "spent")
        self.assertEqual(classify_log("Payment Required", 1), "spent")
        self.assertEqual(classify_log("You've hit your limit", 1), "limit")
        self.assertEqual(classify_log("HTTP 429 Too Many", 2), "limit")
        self.assertEqual(classify_log("rate limit exceeded", 1), "limit")
        self.assertEqual(classify_log("Not logged in", 1), "auth")
        self.assertEqual(classify_log("status 401", 1), "auth")
        self.assertEqual(classify_log("E1401 failed", 1), "crash")  # 1401 is not 401
        self.assertEqual(classify_log("segfault", 3), "crash")
        self.assertEqual(classify_log("usage limit reached", None), "limit")  # unknown exit code: phrases only
        self.assertEqual(classify_log("E429X", None), "ok")
        from ep.agents import gemini, grok
        self.assertEqual(gemini.GeminiAdapter().classify("403 billing/enable", 1), "spent")
        self.assertEqual(grok.GrokAdapter().classify("HTTP 402", 1), "spent")

    def test_adapter_commands(self):
        from ep import agents
        for name in config.AGENTS:
            self.assertEqual(agents.get(name).capabilities, config.CAPABILITIES[name])
        c = agents.get("codex").command("PROMPT", "verify")
        self.assertIn("model_reasoning_effort=low", c)
        self.assertIn("features.hooks=false", c)
        self.assertEqual(agents.get("codex").command("P", "submit")[c.index("-c") + 1 - 0:][0:1][0].split("=")[0],
                         "model_reasoning_effort")
        self.assertIn("model_reasoning_effort=medium", agents.get("codex").command("P", "submit"))
        self.assertEqual(agents.get("codex").env({"A": "1"}, "verify", "C:\\h")["CODEX_HOME"], "C:\\h")
        self.assertEqual(agents.get("claude").command("P", "verify")[1:2], ["-p"])
        self.assertIn("--dangerously-skip-permissions", agents.get("claude").command("P", "verify"))
        env = base.common_env("E5", {"TERM": "xterm", "X": "1"})
        self.assertNotIn("TERM", env)
        self.assertEqual((env["EVENT_ITEM"], env["PYTHONIOENCODING"]), ("E5", "utf-8"))
        self.assertFalse([k for k in env if k.startswith("LCH_")])


# ---------------------------------------------------------------- register
class TestRegister(Base):
    def write(self, name, lines):
        os.makedirs(config.scout_inbox_dir(), exist_ok=True)
        path = os.path.join(config.scout_inbox_dir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    @staticmethod
    def ev(n, **kw):
        o = {"title": f"Survey number {n}", "org": f"Organization {n}", "url": f"https://example.com/s{n}",
             "deadline": d(7), "region": "KR", "form_type": "google"}
        o.update(kw)
        return json.dumps(o)

    def test_register_lines(self):
        self.write("20261106-010101-gemini-KR.jsonl", [
            self.ev(1), self.ev(1), "not json at all", json.dumps({"title": "x"}), self.ev(2, deadline=d(-3)),
            self.ev(3, deadline=""), self.ev(4, url="", post_url="https://www.instagram.com/p/abc/",
                                             sns_account="@someone"), ""])
        r = register.run()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["files"], r["added"], r["duplicate"], r["invalid"], r["skipped"]), (1, 2, 1, 2, 2), r)
        ids = r["per_file"][0]["ids"]
        it = self.item(ids[1])
        self.assertEqual(it["url"], "https://www.instagram.com/p/abc/")
        self.assertIn("SNS accounts to follow: @someone", it["notes"])
        self.assertEqual(it["added_by"], "gemini")
        self.assertTrue(any("invalid line" in e for e in self.errors()))
        self.assertEqual(register.run()["files"], 0)  # registered once
        self.assertEqual(self.kvget("register.done"), ["20261106-010101-gemini-KR.jsonl"])

    def test_region_from_file_name_and_pool_overflow(self):
        self.write("20261106-010102-codex-JP.jsonl", [self.ev(1, region=""), self.ev(2), self.ev(3)])
        self.patch(config, "PIPELINE_MAX", 1)
        r = register.run()
        self.assertEqual((r["added"], r["pooled"]), (1, 2), r)
        self.assertEqual(self.item(r["per_file"][0]["ids"][0])["region"], "JP")
        self.assertEqual(len(store.pool_rows(self.db)), 2)

    def test_dry_run_writes_nothing_and_busy_file_waits(self):
        self.write("20261106-010103-gemini-KR.jsonl", [self.ev(5)])
        r = register.run(dry_run=True)
        self.assertEqual(r["added"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        self.assertIsNone(self.kvget("register.done"))
        cur = self.db.execute("INSERT INTO runs(kind, agent, pid, started) VALUES('scout','gemini',1,'x')")
        store.kv_set(self.db, f"scout.out.{cur.lastrowid}",
                     os.path.join(config.scout_inbox_dir(), "20261106-010103-gemini-KR.jsonl"))
        self.assertEqual(register.run()["files"], 0)  # its scout run is still active
        self.db.execute("UPDATE runs SET ended='y'")
        self.assertEqual(register.run()["added"], 1)

    def test_main_with_no_files(self):
        rc, out = self.call_main(register, ["register"])
        self.assertEqual((rc, out["ok"], out["files"]), (0, True, 0))

    def test_known_events(self):
        self.new_item(url="https://example.com/known")
        path = register.write_known_events(self.db)
        with open(path, encoding="utf-8") as f:
            self.assertIn("https://example.com/known\tOrg T https://example.com/known", f.read())


# ---------------------------------------------------------------- scheduler
class TestScheduler(Base):
    def test_phase_order_and_launch_order(self):
        self.policy(max_agents=5)  # a free slot is left for backfill after submit, verify and scout
        s = self.sched()
        order = []
        for name in ("phase_submit", "phase_verify", "phase_register", "phase_promote", "phase_scout",
                     "phase_backfill", "phase_render"):
            orig = getattr(s, name)
            setattr(s, name, (lambda o, n: lambda *a: (order.append(n), o(*a))[1])(orig, name))
        self.ready_item("form", url="https://example.com/r1", prize="coffee")
        self.new_item(url="https://example.com/n1", prize="tea")
        out = s.loop_once()
        self.assertEqual(order, ["phase_submit", "phase_verify", "phase_register", "phase_promote", "phase_scout",
                                 "phase_backfill", "phase_render"])
        kinds = [x["kind"] for x in out["launched"]]
        self.assertEqual(kinds[:2], ["submit", "verify"], out["launched"])
        self.assertEqual(self.item(out["launched"][0]["item"])["state"], "submitting")
        self.assertEqual(self.item(out["launched"][1]["item"])["state"], "verifying")
        self.assertEqual([k for k, _ in s.sys_calls], ["backfill", "render"])
        self.finish(s)

    def test_dry_run_once_launches_nothing_and_writes_nothing(self):
        rid = self.ready_item("form", url="https://example.com/d1")
        self.new_item(url="https://example.com/d2")
        before = (self.db.execute("SELECT COUNT(*) FROM kv").fetchone()[0], self.item(rid)["state"],
                  self.db.execute("SELECT COUNT(*) FROM history").fetchone()[0])
        s = self.sched(dry=True)
        out = s.loop_once()
        self.assertEqual(out["launched"], [])
        self.assertGreaterEqual(len(out["would"]), 2)
        w = out["would"][0]
        self.assertEqual((w["kind"], w["item"]), ("submit", rid))
        self.assertTrue(any(c.startswith("<prompt ") and c.endswith(" chars>") for c in w["command"]), w)
        self.assertTrue(w["log"].endswith(f"-{w['agent']}-submit-{rid}.log"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual((self.db.execute("SELECT COUNT(*) FROM kv").fetchone()[0], self.item(rid)["state"],
                          self.db.execute("SELECT COUNT(*) FROM history").fetchone()[0]), before)
        self.assertFalse(os.path.exists(config.logs_dir()))
        rc, j = self.call_main(scheduler, ["run", "--once", "--dry-run"])
        self.assertEqual((rc, j["ok"], j["dry_run"], j["launched"]), (0, True, True, []))
        self.assertNotIn("phone_unavailable", j)

    def test_ram_gate_and_agent_cap(self):
        self.ready_item("form", url="https://example.com/g1")
        s = self.sched()
        self.free[0] = 1.0
        self.assertEqual(s.loop_once()["launched"], [])
        self.free[0] = 16.0
        self.policy(max_agents=1)
        self.ready_item("form", url="https://example.com/g2")
        self.assertEqual(len(s.loop_once()["launched"]), 1)
        self.assertEqual(len(s.loop_once()["launched"]), 0)  # one agent process active already
        self.finish(s)
        self.policy(max_agents=3, ram_floor_gb=20)
        self.assertEqual(s.loop_once()["launched"], [])  # floor from policy.json
        self.finish(s)

    def test_browser_cap(self):
        self.patch(config, "MAX_BROWSER", 1)
        a = self.ready_item("browser", url="https://example.com/b1")
        b = self.ready_item("browser", url="https://example.com/b2")
        s = self.sched()
        out = s.loop_once()
        self.assertEqual([x["item"] for x in out["launched"] if x["kind"] == "submit"], [a])
        self.assertEqual(self.item(b)["state"], "ready")
        self.finish(s)

    def test_gemini_takes_form_only(self):
        iid = self.ready_item("browser", url="https://example.com/c1")
        self.policy(max_agents=3)
        s = self.sched()
        self.assertTrue(all(x["agent"] != "gemini" for x in s.loop_once()["launched"] if x["item"]))
        self.assertEqual(self.item(iid)["claimed_by"] in ("codex", "claude"), True)
        self.finish(s)

    def test_grok_off_until_needed_never_launched(self):
        s = self.sched(adapters={"grok": Fake("grok")})
        self.ready_item("form", url="https://example.com/k1")
        self.assertEqual(s.loop_once()["launched"], [])
        self.policy(grok="on")
        self.assertEqual([x["agent"] for x in s.loop_once()["launched"] if x["item"]], ["grok"])
        self.finish(s)

    def test_blocked_agent_not_launched(self):
        self.ready_item("form", url="https://example.com/z1")
        for a in ("gemini", "codex", "claude"):
            accounts.block(self.db, a, "test")
        s = self.sched()
        self.assertEqual(s.loop_once()["launched"], [])

    def test_scout_quota_rounds_and_gemini_cap(self):
        self.patch(config, "AGENT_PREFERENCE", dict(config.AGENT_PREFERENCE, scout=("gemini", "grok", "claude", "codex")))
        self.patch(config, "SCOUT_QUOTA", {"KR": 3, "JP": 1, "US": 1})  # the round logic itself, not the Korea-only setting
        self.policy(gemini_daily_usd=1.6 * config.GEMINI_SCOUT_RUN_USD)  # two runs fit, the third does not
        s = self.sched()
        seq = []
        for _ in range(6):
            out = s.loop_once()
            seq += [(x["region"], x["agent"]) for x in out["launched"] if x["kind"] == "scout"]
            self.finish(s)
        # quota KR 3 : JP 1 : US 1 per round; Gemini stops once two estimated runs reach the daily cap
        self.assertEqual([r for r, _ in seq], ["KR", "KR", "KR", "JP", "US", "KR"])
        self.assertEqual([a for _, a in seq][:2], ["gemini", "gemini"])
        self.assertTrue(all(a == "claude" for _, a in seq[2:]), seq)
        self.assertAlmostEqual(float(self.kvget(f"gemini_cost.{d(0)}")), 2 * config.GEMINI_SCOUT_RUN_USD)
        self.assertEqual(self.kvget("scout_round"), {"KR": 1})

    def test_scout_waits_for_pipeline_room_and_promotes_pool_first(self):
        for i in range(3):
            ep("pool-add", "--agent", "gemini", "--json",
               json.dumps({"title": f"Pool event {i}", "org": f"Pool org {i}", "url": f"https://example.com/p{i}",
                           "deadline": d(5 + i)}))
        ep("pool-add", "--agent", "gemini", "--json", json.dumps(
            {"title": "Old pool event", "org": "Old org", "url": "https://example.com/old", "deadline": d(-2)}))
        s = self.sched()
        self.patch(config, "PIPELINE_MAX", 3)
        out = s.loop_once()
        self.assertTrue(any("promoted 3" in a for a in out["actions"]), out["actions"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items WHERE state IN ('new','verifying')").fetchone()[0], 3)
        self.assertEqual([x for x in out["launched"] if x["kind"] == "scout"], [])  # pipeline full: no scout
        self.finish(s)

    def test_register_runs_before_scout_when_files_wait(self):
        os.makedirs(config.scout_inbox_dir())
        with open(os.path.join(config.scout_inbox_dir(), "20261106-020202-gemini-US.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"title": "US survey one", "org": "US Org", "url": "https://example.com/us1",
                                "deadline": d(9), "region": "US"}) + "\n")
        s = self.sched()
        out = s.loop_once()
        self.assertTrue(any("registered 1 scout file" in a for a in out["actions"]), out["actions"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items WHERE region='US'").fetchone()[0], 1)
        self.finish(s)

    def test_stop_flag(self):
        self.ready_item("form", url="https://example.com/s1")
        rc, j = self.call_main(scheduler, ["stop"])
        self.assertEqual((rc, j["stop"]), (0, True))
        s = self.sched()
        out = s.loop_once()
        self.assertEqual((out["launched"], out["stopped"]), ([], True))
        self.assertIsNone(self.kvget("stop"))
        # with an active run the loop waits
        store.kv_set(self.db, "stop", True)
        self.run_fake(s, "codex", "scout", sleep=2)
        out = s.loop_once()
        self.assertFalse(out["stopped"])
        self.assertTrue(out["gates"]["stop"])
        self.finish(s)
        self.assertTrue(s.loop_once()["stopped"])

    def test_single_instance_guard(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        store.kv_set(self.db, config.SCHEDULER_PID_KEY, child.pid)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = scheduler.cmd_run(["--once"])
        self.assertEqual(rc, 1)
        self.assertIn("already running", buf.getvalue())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        child.kill()
        child.wait()

    def test_block_resume_commands(self):
        rc, j = self.call_main(scheduler, ["block", "gemini", "--reason", "403 forbidden"])
        self.assertEqual((rc, j["blocked"]), (0, "gemini"))
        self.assertEqual(accounts.blocked(self.db), {"gemini": "403 forbidden"})
        rc, j = self.call_main(scheduler, ["resume", "gemini"])
        self.assertEqual((rc, j["resumed"], j["was_blocked"]), (0, "gemini", True))
        self.assertEqual(self.call_main(scheduler, ["block", "phone", "--reason", "locked"])[0], 1)  # no phone pseudo-agent
        self.assertEqual(self.call_main(scheduler, ["bogus"])[0], 1)

    def test_backfill_and_render_ticks(self):
        self.new_item(url="https://example.com/bf", prize="coffee")
        s = self.sched()
        s.loop_once()
        self.assertEqual([k for k, _ in s.sys_calls], ["backfill", "render"])
        self.assertEqual(s.sys_calls[0][1], ["backfill", "--limit", "20"])
        self.assertEqual(s.sys_calls[1][1], ["render"])
        s.sys_calls.clear()
        s.loop_once()  # ticks were set: nothing due now
        self.assertEqual(s.sys_calls, [])

    def test_missing_module_runs_are_remembered(self):
        s = self.sched()
        rid = base.launch(scheduler.SystemAdapter(["definitely-not-a-command"]), "render", None, "", self.db)
        # replace the child by a fake that prints the dispatcher's message
        base.PROCS[rid].wait(timeout=60)
        self.db.execute("UPDATE runs SET log=? WHERE id=?", (os.path.join(self.tmp, "m.log"), rid))
        with open(os.path.join(self.tmp, "m.log"), "w") as f:
            f.write('{"ok": false, "error": "module not available: render"}')
        base.PROCS[rid].returncode = 1
        s.reap_and_apply()
        self.assertIn("render", self.kvget(config.RENDER_UNAVAILABLE_KEY))
        self.assertEqual(sum("render" in e for e in self.errors()), 1)


class TestOutcomes(Base):
    def test_verify_no_result_back_to_new_then_owner(self):
        s = self.sched()
        iid = self.new_item(url="https://example.com/v1")
        r = ep("next-batch", "--agent", "codex", "--size", 1)
        self.assertEqual(r["items"][0]["id"], iid)
        for n in (1, 2):
            self.run_fake(s, "codex", "verify", iid)
            out = self.finish(s)
            self.assertEqual(out[0]["outcome"], "no_result")
            it = self.item(iid)
            self.assertEqual((it["state"], it["lane"], it["claimed_by"]), ("new", None, None))
            self.assertEqual(it["tries"]["no_result"], n)
            self.assertEqual(it["tries"]["verify"], 1)  # verify tries are not reset
            self.assertEqual(store.history(self.db, iid)[-1]["action"], "no_result")
            self.set_state(iid, "verifying")
        self.run_fake(s, "claude", "verify", iid)
        self.finish(s)
        it = self.item(iid)
        self.assertEqual(it["state"], "owner")
        self.assertTrue(it["question"].startswith("agents ended 3 verify runs without a result; log: "), it["question"])

    def test_verify_with_result_is_ok(self):
        s = self.sched()
        iid = self.new_item(url="https://example.com/v2")
        ep("next-batch", "--agent", "codex", "--size", 1)
        rid = self.run_fake(s, "codex", "verify", iid)
        ep("verify", iid, "--agent", "codex", "--lane", "form")  # the agent did its job before the process ended
        self.finish(s)
        self.assertEqual(self.run_outcome(rid), "ok")
        self.assertEqual(self.item(iid)["state"], "ready")

    def test_submit_no_result_release_then_fail_no_resubmit(self):
        s = self.sched()
        iid = self.ready_item("form", url="https://example.com/x1")
        for n in (1, 2):
            self.assertTrue(ep("claim", "--agent", "codex", "--id", iid)["ok"])
            self.run_fake(s, "codex", "submit", iid)
            self.assertEqual(self.finish(s)[0]["outcome"], "no_result")
            it = self.item(iid)
            self.assertEqual((it["state"], it["tries"]["no_result"]), ("ready", n))
        self.assertTrue(ep("claim", "--agent", "codex", "--id", iid)["ok"])
        self.run_fake(s, "codex", "submit", iid)
        self.finish(s)
        it = self.item(iid)
        self.assertEqual(it["state"], "failed")
        self.assertEqual(it["reason"], "agent ended without result; completion unknown")
        self.assertEqual(it["flags"].get("no_resubmit"), 1)

    def test_codex_spent_switches_account_then_blocks(self):
        s = self.sched()
        iid = self.ready_item("form", url="https://example.com/cx1")
        self.assertTrue(ep("claim", "--agent", "codex", "--id", iid)["ok"])
        self.assertEqual(accounts.codex_home(self.db), self.homes[0])
        rid = self.run_fake(s, "codex", "submit", iid, code=1, text="Error: out of credits", account="acct-b")
        out = self.finish(s)
        self.assertEqual(out[0]["class"], "spent")
        self.assertEqual(accounts.codex_home(self.db), self.homes[1])
        self.assertEqual(accounts.blocked(self.db), {})
        self.assertEqual(self.item(iid)["state"], "ready")  # released by the no_result rule
        self.assertIn("Codex account acct-b spent; switched to acct-c", self.errors())
        self.assertTrue(ep("claim", "--agent", "codex", "--id", iid)["ok"])
        self.run_fake(s, "codex", "submit", iid, code=1, text="rate limit exceeded", account="acct-c")
        self.finish(s)
        self.assertIn("codex", accounts.blocked(self.db))
        self.assertEqual(self.run_outcome(rid), "spent")
        self.assertTrue(any(e.startswith("codex blocked") for e in self.errors()))

    def test_codex_limit_with_fresh_low_usage_is_transient(self):
        s = self.sched()
        self.usage_rows["codex"] = [row("acct-b", None, stale=False) | {
            "limits": [{"kind": "primary", "percent": 40}, {"kind": "secondary", "percent": 10}]}]
        iid = self.ready_item("form", url="https://example.com/cx2")
        ep("claim", "--agent", "codex", "--id", iid)
        self.run_fake(s, "codex", "submit", iid, code=1, text="429 rate limit", account="acct-b")
        self.finish(s)
        self.assertEqual(accounts.codex_home(self.db), self.homes[0])  # no switch
        self.assertEqual(accounts.blocked(self.db), {})
        self.assertTrue(any("transient" in e for e in self.errors()))

    def test_claude_spent_marks_account_and_picks_other(self):
        s = self.sched()
        self.run_fake(s, "claude", "scout", code=1, text="You've hit your limit", account="acct-default")
        self.finish(s)
        self.assertEqual(self.kvget("claude_spent"), ["acct-default"])
        self.assertEqual(self.kvget("claude_account"), "acct-a")
        self.assertEqual(accounts.blocked(self.db), {})
        self.run_fake(s, "claude", "scout", code=1, text="You've hit your limit", account="acct-a")
        self.finish(s)
        self.assertIn("claude", accounts.blocked(self.db))  # acct-c is at 100 %: none left

    def test_gemini_spent_and_auth_block(self):
        s = self.sched()
        self.run_fake(s, "gemini", "scout", code=1, text="403 billing/enable required")
        self.finish(s)
        self.assertIn("gemini", accounts.blocked(self.db))
        self.run_fake(s, "grok", "scout", code=1, text="Not logged in")
        self.finish(s)
        self.assertIn("grok", accounts.blocked(self.db))
        self.assertNotIn("Not logged in\n", accounts.blocked(self.db)["grok"])  # only the marker line, with context

    def test_three_crashes_block_the_agent(self):
        s = self.sched()
        for n in (1, 2):
            self.run_fake(s, "gemini", "scout", code=3, text="boom")
            self.finish(s)
            self.assertEqual(accounts.blocked(self.db), {})
        self.run_fake(s, "gemini", "scout", code=0, text="fine")  # a clean run resets the streak
        self.finish(s)
        for n in (1, 2, 3):
            self.run_fake(s, "gemini", "scout", code=3, text="boom")
            self.finish(s)
        self.assertIn("gemini", accounts.blocked(self.db))
        self.assertGreaterEqual(sum("crashed" in e for e in self.errors()), 5)

    def test_orphan_run_is_adopted_with_no_result_rules(self):
        s = self.sched()
        iid = self.new_item(url="https://example.com/o1")
        ep("next-batch", "--agent", "codex", "--size", 1)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        cur = self.db.execute("INSERT INTO runs(kind, item_id, agent, pid, log, started) VALUES('verify',?,?,?,?,?)",
                              (iid, "codex", dead.pid, os.path.join(self.tmp, "nolog.log"), store.now_iso()))
        out = s.reap_and_apply()
        self.assertEqual([(o["outcome"], o["run"]) for o in out], [("orphan", cur.lastrowid)])
        self.assertEqual(self.item(iid)["state"], "new")
        self.assertEqual(self.item(iid)["tries"]["no_result"], 1)
        self.assertEqual(self.run_outcome(cur.lastrowid), "orphan")



if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- run timeouts
class TestRunTimeout(Base):
    def test_long_run_is_killed_and_labelled_timeout(self):
        self.patch(config, "RUN_TIMEOUT_SEC", {"verify": 1})
        iid = self.new_item(url="https://t.example/timeout")
        self.set_state(iid, "verifying", claimed_by="codex")
        s = self.sched()
        rid = self.run_fake(s, "codex", "verify", iid, sleep=60)
        self.assertEqual(s.reap_and_apply(), [])  # still within the limit: nothing reaped
        time.sleep(1.5)
        done = s.reap_and_apply()
        self.assertEqual([x["outcome"] for x in done], ["timeout"])
        self.assertEqual(self.run_outcome(rid), "timeout")
        self.assertIsNotNone(base.PROCS.get(rid) is None or base.PROCS.get(rid).poll())
        self.assertEqual(self.item(iid)["state"], "new")  # no_result rule applied after the kill
        self.assertTrue(any("timed out after 1 s" in e for e in self.errors()), self.errors())

    def test_timed_out_helper_tolerates_bad_dates(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(config.KST_TZ))
        self.assertFalse(base._timed_out({"kind": "verify", "started": "garbage"}, now))
        self.assertFalse(base._timed_out({"kind": "verify", "started": now.isoformat()}, now))
