"""What the scheduler does when a run ends: classify it, apply the agent rules (spent, limit, auth, crash) and the item
rules (a run that ended without a result)."""
import os

from cp import accounts, cli, config, store
from cp.agents import base


class Outcomes:
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
            if self.crash_streak[agent] >= config.CRASH_BLOCK_AFTER:  # (assumption)
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
                self.set_kv("render_content_hash", h)  # the scheduler's own key; render/publish own `publish_hash`
        if cls == "ok":
            return
        if "module not available" in (r["tail"] or ""):
            msg = f"cp {kind} is not available yet (module missing); not retried until the module exists"
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
        if kind == "draft" and st == "drafting":
            return self.no_result_draft(it, r)
        if kind == "audit" and st == "proposal":
            return self.no_result_audit(it, r)
        if kind == "submit" and st == "submitting":
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

    def no_result_draft(self, it, r):
        """A draft run ended without `cp draft-done`: back to the draft queue; at DRAFT_MAX_TRIES runs -> owner."""
        agent = r["agent"]
        tries = dict(it.get("tries") or {})
        tries["no_result"] = tries.get("no_result", 0) + 1
        with self._w():
            if tries.get("draft", 0) >= config.DRAFT_MAX_TRIES:
                q = f"agents ended {tries['draft']} draft runs without a result; log: {r['log']}"
                store.update_item(self.db, it["id"], state="owner", question=q, tries=tries, claimed_by=None,
                                  claimed_at=None)
                store.add_history(self.db, it["id"], agent, "owner", q)
            else:
                store.update_item(self.db, it["id"], state="draft", claimed_by=None, claimed_at=None, tries=tries)
                store.add_history(self.db, it["id"], agent, "no_result", self._why(r) + "; back to draft")
        return "no_result"

    def no_result_audit(self, it, r):
        """An audit run ended. A verdict (work.audit.verdict) means success; otherwise count it, and after
        AUDIT_MAX_TRIES runs mark the audit skipped so the user sees the analysis with that fact (never silent)."""
        work = dict(it.get("work") or {})
        if (work.get("audit") or {}).get("verdict"):
            return None
        tries = dict(it.get("tries") or {})
        if tries.get("audit", 0) >= config.AUDIT_MAX_TRIES:
            work["audit"] = {"verdict": "skipped", "by": r["agent"], "at": store.now_iso(),
                             "findings": f"the audit did not produce a verdict in {tries['audit']} runs; log: {r['log']}"}
            with self._w():
                store.update_item(self.db, it["id"], work=work)
                store.add_history(self.db, it["id"], r["agent"], "audit", "skipped: " + work["audit"]["findings"])
            self.add_error(r["agent"], f"audit of {it['id']} skipped after {tries['audit']} runs without a verdict")
        else:
            with self._w():
                store.add_history(self.db, it["id"], r["agent"], "no_result", self._why(r) + "; audit will be retried")
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
        if n >= config.NO_RESULT_MAX:  # owner rule: completion unknown -> never resubmit automatically
            self._fail_unknown(it, r, agent)
        else:
            res = cli.call(["release", it["id"], "--agent", agent])
            if not res.get("ok"):
                self.add_error("system", f"release of {it['id']} failed ({res.get('error')}); failing it instead")
                self._fail_unknown(it, r, agent)
        return "no_result"
