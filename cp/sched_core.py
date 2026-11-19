"""Scheduler core: the database handle, kv helpers, visible-once error reporting, ticks."""
import contextlib
import sys
import time

from cp import policy, store
from cp.agents import ADAPTERS


class Core:
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
        return self.db.execute("SELECT COUNT(*) FROM items WHERE state IN ('new','verifying','proposal','draft','drafting','ready')").fetchone()[0]
