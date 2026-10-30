"""Band rules: committed mean/σ per (service, key, daytype, hour) and the high/low verdict. Pure."""
from __future__ import annotations

import sys
import uuid
from array import array
from datetime import date
from typing import NamedTuple

from logai2.config import AppConfig
from logai2.timeutil import daytype

DAY_CAP = {"weekday": 5, "weekend": 4}
FLOOR_LEVEL = {"ERROR": "ERROR", "FATAL": "ERROR", "CRITICAL": "ERROR", "WARN": "WARN", "WARNING": "WARN"}
KEY_LEVEL = {"*total": "INFO", "*error": "ERROR"}

_NAN = [float("nan")]


class Stat(NamedTuple):
    mean: float
    std: float


class Lookup(NamedTuple):
    this: Stat | None
    next: Stat | None
    days: int
    upper_only: bool
    daytype: str = "weekday"   # the profile used (a borrowed weekday one included)


def full_after(cfg: AppConfig, daytype: str) -> int:
    """Committed days for a full baseline: never more than the days the rebuild keeps."""
    return min(cfg.baseline.full_after_days, DAY_CAP[daytype])


class Verdict(NamedTuple):
    high: bool
    score: float
    band_low: float | None
    band_high: float | None


class Baseline:
    """Packed stats: one array("d") of 96 per (service, key); slot = weekend*48 + hour*2 (mean), +1 (std); NaN = none."""

    def __init__(self, stats: dict[tuple[str, str, str, int], Stat] | None = None,
                 days: dict[tuple[str, str], int] | None = None):
        self._rows: dict[tuple[str, str], array] = {}
        self.days = dict(days or {})
        for (service, key, dt, hour), st in (stats or {}).items():
            self._put(service, key, dt, hour, st.mean, st.std)

    def _put(self, service, key, dt, hour, mean, std) -> None:
        row = self._rows.get((service, key))
        if row is None:
            row = self._rows[(sys.intern(service), key)] = array("d", _NAN * 96)
        slot = (dt == "weekend") * 48 + hour * 2
        row[slot], row[slot + 1] = mean, std

    @property
    def stats(self) -> dict[tuple[str, str, str, int], Stat]:
        """Dict form, built on demand. Lab scripts and tests only: it costs the memory the packed form saves."""
        return {(s, k, dt, h): st for (s, k), row in self._rows.items()
                for dt in ("weekday", "weekend") for h in range(24) if (st := self.stat(s, k, dt, h))}

    def stat(self, service: str, key: str, daytype: str, hour: int) -> Stat | None:
        row = self._rows.get((service, key))
        if row is None:
            return None
        slot = (daytype == "weekend") * 48 + hour * 2
        mean = row[slot]
        return None if mean != mean else Stat(mean, row[slot + 1])

    def has(self, service: str, key: str) -> bool:
        return (service, key) in self._rows

    def update(self, other: Baseline) -> None:
        self._rows.update(other._rows)
        self.days.update(other.days)

    def drop(self, services) -> None:
        """Forget these services (one name or a set): one pass over the rows."""
        services = {services} if isinstance(services, str) else set(services)
        for k in [k for k in self._rows if k[0] in services]:
            del self._rows[k]
        for k in [k for k in self.days if k[0] in services]:
            del self.days[k]

    @classmethod
    def load(cls, conn, keys: list[tuple[str, str]] | None = None, services: list[str] | None = None) -> Baseline:
        """keys: only these (service, key) pairs (the web's page of templates); services: only these services;
        else the whole table, streamed through a server-side cursor (the owner's path)."""
        sql, args, where = "SELECT service, key, daytype, hour, mean, std FROM baseline", (), []
        if keys is not None:
            where.append("(service, key) IN (SELECT * FROM unnest(%s::text[], %s::text[]))")
            args = ([s for s, _ in keys], [k for _, k in keys])
        if services is not None:
            where.append("service = ANY(%s)")
            args += (services,)
        if where:
            sql += " WHERE " + " AND ".join(where)
        out = cls()
        if keys is None:
            with conn.cursor(name=f"baseline_load_{uuid.uuid4().hex}") as cur:
                cur.itersize = 50_000
                cur.execute(sql, args)
                for r in cur:
                    out._put(r["service"], r["key"], r["daytype"], r["hour"], r["mean"], r["std"])
        else:
            for r in conn.execute(sql, args):
                out._put(r["service"], r["key"], r["daytype"], r["hour"], r["mean"], r["std"])
        dsql = "SELECT service, daytype, count(*) AS n FROM day_status WHERE committed"
        dargs = ()
        if services is not None:
            dsql, dargs = dsql + " AND service = ANY(%s)", (services,)
        out.days = {(r["service"], r["daytype"]): min(r["n"], DAY_CAP[r["daytype"]])
                    for r in conn.execute(dsql + " GROUP BY 1, 2", dargs)}
        return out

    def lookup(self, service: str, key: str, day: date, hour: int) -> Lookup:
        dt = daytype(day)
        upper_only = dt == "weekend" and self.days.get((service, dt), 0) < 2
        if upper_only:
            dt = "weekday"     # too few weekend days: borrow the weekday profile, never alert on drops
        return Lookup(self.stat(service, key, dt, hour),
                      self.stat(service, key, dt, (hour + 1) % 24),
                      self.days.get((service, dt), 0), upper_only, dt)

    def lookup_doc(self, service: str, template_ids, day: date, hour: int) -> Lookup:
        """Doc key band: Σmean ± k·Σσ over its linked templates; templates without stats add nothing."""
        lks = [self.lookup(service, t, day, hour) for t in template_ids]

        def total(stats):
            stats = [s for s in stats if s is not None]
            return Stat(sum(s.mean for s in stats), sum(s.std for s in stats)) if stats else None

        return Lookup(total(lk.this for lk in lks), total(lk.next for lk in lks),
                      min((lk.days for lk in lks), default=0), any(lk.upper_only for lk in lks),
                      next((lk.daytype for lk in lks), "weekday"))


def judge(count: int, key: str, level: str, lk: Lookup, cfg: AppConfig) -> Verdict:
    floor_level = FLOOR_LEVEL.get(KEY_LEVEL.get(key, level.upper()))
    floor = cfg.floors[floor_level] if floor_level else None
    stats = [s for s in (lk.this, lk.next) if s is not None]
    if not stats:
        high = floor is not None and count >= floor
        return Verdict(high, count / floor if floor else 0.0, None, None)
    full = lk.days >= full_after(cfg, lk.daytype)
    k = cfg.baseline.sigma_multiplier if full else cfg.baseline.young_sigma_multiplier
    band_high = max(s.mean + k * s.std for s in stats)
    band_low = min(s.mean - k * s.std for s in stats)
    upper = floor is not None and count > band_high and count >= floor
    drop = (key == "*total" and not lk.upper_only and full and lk.this is not None
            and lk.this.mean >= cfg.drop.min_mean and count < band_low)
    score = count / band_high if band_high > 0 else float(count > 0)
    return Verdict(upper or drop, score, band_low, band_high)
