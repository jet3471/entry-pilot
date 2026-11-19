"""Minimum, expected and maximum reward of a contest entry, in KRW.

  minimum   the smallest prize a winning entry gets (items.prize_min_value). A contest pays nothing to an entry that
            does not win, so this is "at least this much when it wins", not a guaranteed amount, except when
            all_rewarded = 1 (every entrant gets it).
  maximum   the top prize (work.top_prize_krw; when missing, the minimum).
  expected  chance-weighted amount = prize pool / number of entries, taking every entry as equally likely to win.

The notice rarely tells the pool as a number or how many people enter, so both are estimates (assumption):
  pool     work.prize_pool_krw when an agent recorded it, else top prize + minimum x (winners - 1), which is the
           least the organiser can pay out for that many prizes.
  entries  work.entrants_est when recorded, else ENTRANTS[work_type]: rough sizes of Korean and Japanese public
           contests by kind of work (short text draws many more entries than a video).
`basis` says which of the two were estimated, and the page prints it, so nobody reads the figure as a fact.
"""

ENTRANTS = {"slogan": 2000, "naming": 2000, "essay": 400, "story": 400, "idea": 200, "plan": 150, "design": 150,
            "poster": 150, "photo": 300, "video": 80, "code": 60, "other": 300}


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def estimate(it: dict) -> dict | None:
    """{"min", "expected", "max", "pool", "entrants", "basis"} or None when the item has no prize value at all."""
    w = it.get("work") or {}
    lo, hi = _num(it.get("prize_min_value")), _num(w.get("top_prize_krw"))
    if lo is None and hi is None:
        return None
    lo = lo if lo is not None else 0
    hi = max(hi if hi is not None else lo, lo)
    winners = max(_num(it.get("winners")) or 1, 1)
    basis = []
    pool = _num(w.get("prize_pool_krw"))
    if pool is None:
        pool = hi + lo * (winners - 1)
        basis.append("상금 총액 추정")
    entrants = _num(w.get("entrants_est"))
    if not entrants or entrants < 1:
        entrants = ENTRANTS.get(w.get("work_type") or "other", ENTRANTS["other"])
        basis.append("응모자 수 추정")
    entrants = max(entrants, winners)
    expected = pool if it.get("all_rewarded") == 1 and winners >= entrants else round(pool / entrants)
    return {"min": lo, "expected": min(expected, hi) if expected > hi else expected, "max": hi, "pool": pool,
            "entrants": entrants, "basis": basis}


def totals(items: list) -> dict:
    """Sums over items that have an estimate: {"n", "min", "expected", "max"}."""
    out = {"n": 0, "min": 0, "expected": 0, "max": 0}
    for it in items:
        e = estimate(it)
        if e:
            out["n"] += 1
            for k in ("min", "expected", "max"):
                out[k] += e[k]
    return out
