"""How the scheduler starts a run: gates (RAM, slots, blocked agents), choosing the agent, Chrome, building the prompt,
claiming the item and launching, and undoing the claim when the launch fails."""
import importlib.util
import json
import os
import subprocess
import sys

from cp import accounts, chrome, cli, config, model, profile, prompts, ram, store
from cp.agents import base

BROWSER_LANES = ("browser",)


class SystemAdapter(base.Adapter):
    """Runs `python -X utf8 cp.py <args>` as a tracked child (render, backfill). Not an agent: no account, no lanes."""
    name = "system"

    def __init__(self, args):
        self.args = list(args)

    def command(self, prompt, kind):
        return [sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "cp.py"), *self.args]



class Launch:
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
             "blocked": accounts.blocked(self.db), "stop": bool(self.kv("stop", False))}
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

    def candidates(self, g, pref_key, lane=None, exclude=(), ordered=False) -> list:
        """Agents that may take the job: not blocked, capable, usable account, grok only when policy says `on`.
        Best first: fewest active jobs, then the order in config.AGENT_PREFERENCE (assumption on the combination).
        ordered=True: the preference order alone decides (crawler scouts: Gemini only when the others cannot)."""
        out = []
        for i, a in enumerate(config.AGENT_PREFERENCE[pref_key]):
            if a in g["blocked"] or a in exclude or a not in self.adapters:
                continue
            if a == "grok" and g["pol"].get("grok") != "on":  # off_until_needed and off: never launched
                continue
            if lane and lane not in config.CAPABILITIES.get(a, set()):
                continue
            if not self.account_ok(a):
                continue
            if a in config.PER_AGENT_MAX and g["counts"].get(a, 0) >= config.PER_AGENT_MAX[a]:
                continue  #
            out.append((0 if ordered else g["counts"].get(a, 0), i, a))
        return [a for _, _, a in sorted(out)]

    def chrome_ready(self, s) -> bool:
        """Chrome with CDP is up (started hidden when needed). Cached per loop; never blocks longer than ~25 s."""
        if self._chrome is None:
            if chrome.alive():
                self._chrome = True
            elif self.dry:
                s["notes"].append("Chrome is not running: a real loop would run `cp chrome ensure` first")
                self._chrome = True
            else:
                self._chrome = self.ensure_chrome()
        return self._chrome

    def ensure_chrome(self) -> bool:
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "cp.py"), "chrome",
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

    def make_prompt(self, kind, item, agent, out=None, known=None) -> str:
        region = (item.get("region") or "KR").upper()
        ctx = {"agent": agent, "today": model.today_kst().isoformat(), "region": region}
        if kind == "scout":
            ctx["out"], ctx["known"] = out, known
        else:
            ctx["profile_lines"] = profile.summary_for_prompt(region)
            if (item.get("tries") or {}).get("submit", 0) > 0 and item.get("reason"):
                ctx["last_failure"] = item["reason"]
        return self.as_file_if_long(kind, item, agent, prompts.build(kind, item, ctx))

    def as_file_if_long(self, kind, item, agent, prompt: str) -> str:
        r"""Between agents a long message goes as a .md file and only the path is sent; a short one
        stays inline. The file is rewritten per task (one file, no copies) under <data>\prompts."""
        if self.dry or len(prompt) <= config.PROMPT_INLINE_MAX:
            return prompt
        d = config.prompts_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{kind}-{(item or {}).get('id') or (item or {}).get('region') or 'task'}-{agent}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(prompt)
        return (f"Read the file {path} completely with your file-reading tool, then do exactly the task it describes. "
                f"It holds your full instructions (kind {kind}); nothing else is needed from this message.")

    def plan_entry(self, adapter, agent, kind, item, prompt, tag=None) -> dict:
        iid = item.get("id") if item else None
        cmd = ["<prompt %d chars>" % len(prompt) if c == prompt else c for c in adapter.command(prompt, kind)]
        return {"agent": agent, "kind": kind, "item": iid,
                "log": base.log_path(agent, kind, iid or tag, create=False), "command": cmd}

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
        elif kind == "audit":
            fresh = store.get_item(self.db, iid) or item
            tries = dict(fresh.get("tries") or {})
            tries["audit"] = max(0, tries.get("audit", 0) - 1)
            with self._w():
                store.update_item(self.db, iid, tries=tries)
                store.add_history(self.db, iid, "system", "launch-failed", why)
        elif kind == "draft":
            fresh = store.get_item(self.db, iid) or item
            tries = dict(fresh.get("tries") or {})
            tries["draft"] = max(0, tries.get("draft", 0) - 1)
            with self._w():
                store.update_item(self.db, iid, state="draft", claimed_by=None, claimed_at=None, tries=tries)
                store.add_history(self.db, iid, "system", "launch-failed", why)
        else:
            with self._w():
                store.add_history(self.db, iid, "system", "launch-failed", why)
            res = cli.call(["release", iid, "--agent", "system"], internal=True)
            if not res.get("ok"):
                self.add_error("system", f"could not release {iid} after a failed launch: {res.get('error')}")

    def start(self, g, s, agent, kind, item) -> bool:
        """Claim (submit and draft by id, verify by next-batch) and launch one run. Dry run: only plan. True = started."""
        adapter = self.adapters[agent]
        lane = item.get("lane") if item else None
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
        elif kind == "audit":  # no claimed state: the attempt is counted, the item stays in `proposal`
            fresh = store.get_item(self.db, item["id"]) or item
            tries = dict(fresh.get("tries") or {})
            tries["audit"] = tries.get("audit", 0) + 1
            with self._w():
                store.update_item(self.db, item["id"], tries=tries)
                store.add_history(self.db, item["id"], agent, "audit-start", f"try {tries['audit']}")
            item = store.get_item(self.db, item["id"])
        else:
            r = cli.call(["draft-claim" if kind == "draft" else "claim", "--agent", agent, "--id", item["id"]])
            if not r.get("ok") or not r.get("item"):
                s["notes"].append(f"claim of {item['id']} by {agent} refused: {r.get('error') or r.get('reason')}")
                return False
            item = r["item"]
        prompt = ""
        try:
            prompt = self.make_prompt(kind, item, agent)
            run_id = base.launch(adapter, kind, item, prompt, self.db, account=self._acct.get(agent) or None)
        except base.LaunchError as e:
            base.cleanup_prompt_pointer(prompt)
            self.undo_claim(kind, item, agent, f"launch failed: {e}")
            return False
        except Exception as e:
            base.cleanup_prompt_pointer(prompt)
            self.add_error(agent, f"{kind} for {item['id']} not launched: {type(e).__name__}: {e}")
            self.undo_claim(kind, item, agent, f"not launched: {type(e).__name__}")
            return False
        self.note_launch(g, agent, kind, item.get("lane"))
        s["launched"].append({"run": run_id, "agent": agent, "kind": kind, "item": item["id"]})
        self.log(f"launched run {run_id} {agent} {kind} {item['id']}")
        return True

    def system_available(self, key, module) -> bool:
        """False while kv `<key>` says the module was missing and it still is (explicit signal: module found again)."""
        if self.kv(key) is None:
            return True
        if importlib.util.find_spec(f"cp.{module}") is None:
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
