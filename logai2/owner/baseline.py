"""Band rules: committed mean/σ per (service, key, daytype, hour) and the high/low verdict. Pure."""
from __future__ import annotations

from datetime import date
from typing import NamedTuple

from logai2.config import AppConfig
from logai2.timeutil import daytype

DAY_CAP = {"weekday": 5, "weekend": 4}
FLOOR_LEVEL = {"ERROR": "ERROR", "FATAL": "ERROR", "CRITICAL": "ERROR", "WARN": "WARN", "WARNING": "WARN"}
KEY_LEVEL = {"*total": "INFO", "*error": "ERROR"}


class Stat(NamedTuple):
    mean: float
    std: float


class Lookup(NamedTuple):
    this: Stat | None
    next: Stat | None
    days: int
    upper_only: bool


class Verdict(NamedTuple):
    high: bool
    score: float
    band_low: float | None
    band_high: float | None


class Baseline:
    def __init__(self, stats: dict[tuple[str, str, str, int], Stat], days: dict[tuple[str, str], int]):
        self.stats = stats
        self.days = days

    @classmethod
    def load(cls, conn, keys: list[tuple[str, str]] | None = None) -> Baseline:
        """keys: only these (service, key) pairs (the web's page of templates), else the whole table."""
        sql, args = "SELECT service, key, daytype, hour, mean, std FROM baseline", ()
        if keys is not None:
            sql += " WHERE (service, key) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
            args = ([s for s, _ in keys], [k for _, k in keys])
        stats = {(r["service"], r["key"], r["daytype"], r["hour"]): Stat(r["mean"], r["std"])
                 for r in conn.execute(sql, args)}
        days = {(r["service"], r["daytype"]): min(r["n"], DAY_CAP[r["daytype"]])
                for r in conn.execute("SELECT service, daytype, count(*) AS n FROM day_status "
                                      "WHERE committed GROUP BY 1, 2")}
        return cls(stats, days)

    def lookup(self, service: str, key: str, day: date, hour: int) -> Lookup:
        dt = daytype(day)
        upper_only = dt == "weekend" and self.days.get((service, dt), 0) < 2
        if upper_only:
            dt = "weekday"     # too few weekend days: borrow the weekday profile, never alert on drops
        return Lookup(self.stats.get((service, key, dt, hour)),
                      self.stats.get((service, key, dt, (hour + 1) % 24)),
                      self.days.get((service, dt), 0), upper_only)

    def lookup_doc(self, service: str, template_ids, day: date, hour: int) -> Lookup:
        """Doc key band: Σmean ± k·Σσ over its linked templates; templates without stats add nothing."""
        lks = [self.lookup(service, t, day, hour) for t in template_ids]

        def total(stats):
            stats = [s for s in stats if s is not None]
            return Stat(sum(s.mean for s in stats), sum(s.std for s in stats)) if stats else None

        return Lookup(total(lk.this for lk in lks), total(lk.next for lk in lks),
                      min((lk.days for lk in lks), default=0), any(lk.upper_only for lk in lks))


def judge(count: int, key: str, level: str, lk: Lookup, cfg: AppConfig) -> Verdict:
    floor_level = FLOOR_LEVEL.get(KEY_LEVEL.get(key, level.upper()))
    floor = cfg.floors[floor_level] if floor_level else None
    stats = [s for s in (lk.this, lk.next) if s is not None]
    if not stats:
        high = floor is not None and count >= floor
        return Verdict(high, count / floor if floor else 0.0, None, None)
    full = lk.days >= cfg.baseline.full_after_days
    k = cfg.baseline.sigma_multiplier if full else cfg.baseline.young_sigma_multiplier
    band_high = max(s.mean + k * s.std for s in stats)
    band_low = min(s.mean - k * s.std for s in stats)
    upper = floor is not None and count > band_high and count >= floor
    drop = (key == "*total" and not lk.upper_only and full and lk.this is not None
            and lk.this.mean >= cfg.drop.min_mean and count < band_low)
    score = count / band_high if band_high > 0 else float(count > 0)
    return Verdict(upper or drop, score, band_low, band_high)
