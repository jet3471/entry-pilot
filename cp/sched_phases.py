"""The phases of one scheduler loop, in launch order: submit, accept, draft, audit, verify, register, pool promotion, scout,
backfill; plus housekeeping (Korean translation, notices for the user, tab sweep)."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime

from cp import accounts, cli, config, dossier_ko, model, notify, policy, register, store, workflow
from cp.agents import base
from cp.sched_launch import BROWSER_LANES

TICK_BACKFILL = "tick.backfill"
TICK_NOTIFY = "tick.notify"
TICK_TRANSLATE = "tick.translate"
TRANSLATE_EVERY_SEC = 300
NOTIFY_EVERY_SEC = 60


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")



class Phases:
    def phase_submit(self, g, s):
        ready = [it for it in store.items(self.db, "state='ready'")
                 if not (it.get("flags") or {}).get("no_resubmit") and not model.is_expired(it)]
        ready.sort(key=model.order_key)
        for it in ready:
            if workflow.needs_refreeze_review(self.db, it):
                self.report_once(f"approved.{it['id']}", "다시 확인해 주세요", notes=s["notes"])
                if not self.dry:
                    with self._w():
                        workflow.send_back_for_reapproval(self.db, it, agent="system", note_text="다시 확인해 주세요")
                continue
            ok_draft, why_draft = workflow.approved_draft_ok(it, db=self.db)
            if not ok_draft:
                self.report_once(f"approved.{it['id']}", why_draft, notes=s["notes"])
                if not self.dry:
                    work = dict(it.get("work") or {})
                    work["notified_review"] = None
                    with self._w():
                        workflow.clear_approval(self.db, it["id"], work)
                        store.update_item(self.db, it["id"], state="review", work=work, claimed_by=None, claimed_at=None)
                        store.add_history(self.db, it["id"], "system", "review", why_draft)
                continue
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

    def phase_draft(self, g, s):
        """Verified contest items waiting for a written work (state draft), soonest deadline first."""
        rows = [it for it in store.items(self.db, "state='draft'") if not model.is_expired(it)]
        rows.sort(key=model.order_key)
        for it in rows:
            if not self.slot_free(g):
                break
            if (it.get("tries") or {}).get("draft", 0) >= config.DRAFT_MAX_TRIES:
                if not self.dry:
                    q = f"drafting failed {config.DRAFT_MAX_TRIES} times; tell the agents what to change or exclude it"
                    res = cli.call(["owner", it["id"], "--agent", "system", "--question", q], internal=True)
                    if not res.get("ok"):
                        self.add_error("system", f"cannot move {it['id']} to owner after draft tries: {res.get('error')}")
                continue
            cands = self.candidates(g, "draft")
            if not cands:
                continue
            self.start(g, s, cands[0], "draft", it)

    def phase_audit(self, g, s):
        """An auditor agent checks the analysis of big-prize contests (state proposal, work.audit_required, no verdict yet).
        The app connects no auditor, so audit_required is never set there and this phase does nothing."""
        running = {x["item_id"] for x in base.active_runs(self.db) if x["kind"] == "audit"}
        rows = []
        for it in store.items(self.db, "state='proposal'"):
            w = it.get("work") or {}
            if (not w.get("audit_required") or (w.get("audit") or {}).get("verdict") or it["id"] in running
                    or model.is_expired(it)):
                continue
            if (it.get("tries") or {}).get("audit", 0) >= config.AUDIT_MAX_TRIES:
                continue
            rows.append(it)
        rows.sort(key=model.order_key)
        for it in rows:
            if not self.slot_free(g):
                break
            cands = self.candidates(g, "audit")
            if not cands:
                s["notes"].append("audit waits: no auditor available (no auditor agent connected)")
                break
            self.start(g, s, cands[0], "audit", it)

    def phase_accept(self, g, s):
        """proposal_policy 'auto': accept every analysis whose audit passed or that needs no audit (off by default; the
        app leaves 'owner'). An analysis whose audit has no verdict (still running, or out of tries) stays for the user."""
        if g["pol"].get("proposal_policy") != "auto":
            return
        for it in store.items(self.db, "state='proposal'"):
            w = it.get("work") or {}
            if (model.is_expired(it)
                    or policy._valid_positive_prize(w.get("top_prize_krw")) is None
                    or (w.get("audit_required") and (w.get("audit") or {}).get("verdict") != "pass")):
                continue
            s.setdefault("accepted", []).append(it["id"])
            if self.dry:
                continue
            try:
                with self._w():
                    workflow.accept(self.db, it, "system", "auto-accepted: proposal_policy auto", _auto=True)
            except Exception as e:
                self.add_error("system", f"cannot auto-accept {it['id']}: {e}")

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
            if dl and dl >= today:  # (assumption) pool rows without a parseable deadline are never promoted
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
        if pipeline >= config.PIPELINE_MAX or g["scouts"] >= config.SCOUT_MAX_ACTIVE or not self.slot_free(g):
            return
        if config.CRAWLER_SCOUT and not self.due("tick.scout", config.SCOUT_MIN_INTERVAL_SEC):
            return  # crawler scouts end fast: space them out
        quota = config.SCOUT_QUOTA
        used = dict(self.kv("scout_round", {}) or {})
        region = next((r for r in ("KR", "JP", "US")
                       if r in config.ACTIVE_REGIONS and used.get(r, 0) < quota.get(r, 0)), None)
        if region is None:  # every quota used: the round ends and a new one starts
            used, region = {}, "KR"
        first = not any(used.values())
        today = model.today_kst().isoformat()
        exclude = ()
        if float(self.kv(f"gemini_cost.{today}", 0) or 0) >= float(g["pol"].get("gemini_daily_usd", 10)):
            exclude = ("gemini",)
            s["notes"].append("Gemini daily cost cap reached: no Gemini scouting today")
        cands = self.candidates(g, "scout", None, exclude, ordered=config.CRAWLER_SCOUT)
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
            if region == "KR" and config.CRAWLER_SCOUT and os.path.exists(os.path.join(config.PROJECT_DIR, "cp", "crawler.py")):
                # the Python crawler collects, ONE agent call judges (no step by step browsing)
                wrapper = [sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "cp", "scout_run.py"),
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
            if agent == "gemini":  # (assumption) the CLI reports no cost: estimate per scout run
                cost = float(store.kv_get(self.db, f"gemini_cost.{today}", 0) or 0) + config.GEMINI_SCOUT_RUN_USD
                store.kv_set(self.db, f"gemini_cost.{today}", round(cost, 4))
        self.note_launch(g, agent, "scout")
        s["launched"].append({"run": run_id, "agent": agent, "kind": "scout", "item": None, "region": region})
        self.log(f"launched run {run_id} {agent} scout {region}")

    def phase_backfill(self, g, s):
        if g["backfill_active"] or not self.slot_free(g) or not self.system_available(
                config.BACKFILL_UNAVAILABLE_KEY, "backfill"):
            return
        if not self.due(TICK_BACKFILL, config.BACKFILL_EVERY_SEC):
            return
        # (assumption) nothing to do when no item lacks a minimum prize value
        if not self.db.execute("SELECT 1 FROM items WHERE prize_min_value IS NULL AND COALESCE(prize,'') != '' "
                               "LIMIT 1").fetchone():
            return
        if self.launch_system(g, s, "backfill", ["backfill", "--limit", str(config.BACKFILL_LIMIT)]) is not None:
            self.tick(TICK_BACKFILL)

    def housekeeping(self, g, s):
        self.phase_translate(s)
        self.phase_notify(s)
        self.tab_sweep(s)

    def phase_translate(self, s):
        """Korean for the English dossiers and audit findings the user reads: `cp translate --dossiers` runs
        detached, one at a time, when a waiting item has no cached translation. The app shows the cached Korean and marks
        the English that is still waiting."""
        proc = getattr(self, "_translate_proc", None)
        if proc is not None and proc.poll() is None:
            return
        if self.dry or not config.TRANSLATE_PHASE or not self.due(TICK_TRANSLATE, TRANSLATE_EVERY_SEC):
            return
        try:
            n = len(dossier_ko.pending_long(self.db))
        except Exception as e:
            self.report_once("translate", f"translate check failed: {type(e).__name__}: {e}", notes=s["notes"])
            return
        self.tick(TICK_TRANSLATE)
        if not n:
            self.clear_once("translate")
            return
        os.makedirs(config.logs_dir(), exist_ok=True)
        log = open(os.path.join(config.logs_dir(), "translate.log"), "a", encoding="utf-8")
        self._translate_proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", os.path.join(config.PROJECT_DIR, "cp.py"), "translate", "--dossiers"],
            cwd=config.PROJECT_DIR, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=0x08000000, close_fds=True)
        s["actions"].append(f"started the Korean translation of {n} dossier text(s)")

    def phase_notify(self, s):
        """Notices (cp/notify.py): analyses and finished works waiting for the user are put into the outbox. Dry run only lists what is due."""
        if not self.due(TICK_NOTIFY, NOTIFY_EVERY_SEC):
            return
        try:
            r = notify.run(dry_run=self.dry)
        except Exception as e:  # never swallow: recorded and shown, the loop goes on
            self.report_once("notify", f"notice failed: {type(e).__name__}: {e}", notes=s["notes"])
            return
        if not self.dry:
            self.tick(TICK_NOTIFY)
        for d in r.get("due", []):
            if self.dry:
                s["actions"].append(f"would send notice {d['kind']}: {d['ids']}")
        for d in r.get("sent", []):
            s["actions"].append(f"sent notice {d['kind']} ({', '.join(d['channels'])}): {d['ids']}")
        if r.get("failed"):
            self.report_once("notify", f"notice failed: {r['failed']}", notes=s["notes"])
        elif not self.dry and not r.get("waiting_retry_until"):
            self.clear_once("notify")
