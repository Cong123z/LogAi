"""Daily commit: finished days -> day_status, rebuild baseline, roll partitions. Idempotent;
"Recompute baseline now" and a missed 02:00 run call the same function."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from logai2.config import AppConfig
from logai2.db import drop_partitions_before, ensure_partitions, put_setting

MARK_DAYS = """
INSERT INTO day_status (service, day, committed, daytype)
SELECT DISTINCT service, day, true,
       CASE WHEN extract(isodow FROM day) >= 6 THEN 'weekend' ELSE 'weekday' END
FROM service_hours WHERE day < %s
ON CONFLICT (service, day) DO UPDATE SET committed = true WHERE NOT day_status.committed
"""

# ponytail: keys × hours join recomputes every key nightly; restrict to keys touched since last commit if it gets slow at 27k templates.
REBUILD = """
WITH ranked AS (
  SELECT service, day, daytype,
         row_number() OVER (PARTITION BY service, daytype ORDER BY day DESC) AS rn
  FROM day_status WHERE committed),
picked AS (
  SELECT service, day, daytype FROM ranked
  WHERE (daytype = 'weekday' AND rn <= 5) OR (daytype = 'weekend' AND rn <= 4)),
hours AS (
  SELECT sh.service, p.daytype, sh.day, sh.hour, sh.n_minutes
  FROM service_hours sh JOIN picked p USING (service, day)
  WHERE sh.n_minutes > 0 AND NOT EXISTS (
    SELECT 1 FROM review_exclusions x
    WHERE x.decision = 'exclude' AND x.service = sh.service AND x.day = sh.day
      AND sh.hour >= x.hour_from AND sh.hour < x.hour_to)),
keys AS (
  SELECT DISTINCT hc.service, hc.key FROM hourly_counts hc
  JOIN hours h USING (service, day, hour))
INSERT INTO baseline (service, key, daytype, hour, mean, std, days)
SELECT k.service, k.key, h.daytype, h.hour,
       sum(coalesce(hc.sum, 0))::float / sum(h.n_minutes),
       sqrt(greatest(sum(coalesce(hc.sumsq, 0))::float / sum(h.n_minutes)
                     - (sum(coalesce(hc.sum, 0))::float / sum(h.n_minutes))^2, 0)),
       count(*)
FROM keys k JOIN hours h ON h.service = k.service
LEFT JOIN hourly_counts hc
  ON hc.service = k.service AND hc.key = k.key AND hc.day = h.day AND hc.hour = h.hour
GROUP BY k.service, k.key, h.daytype, h.hour
"""


def commit(conn, cfg: AppConfig, today: date) -> dict:
    """Call on an idle connection (no open transaction): otherwise the block below is only a
    savepoint and nothing is committed until the caller commits."""
    # db helpers open their own transaction() blocks; inside this one they become savepoints.
    with conn.transaction():
        days = conn.execute(MARK_DAYS, (today,)).rowcount
        conn.execute("DELETE FROM baseline")
        rows = conn.execute(REBUILD).rowcount
        ensure_partitions(conn, today, today + timedelta(days=2))
        cutoff = today - timedelta(days=cfg.retention.counts_days)
        for table in ("service_hours", "hourly_counts", "alert_minutes"):
            drop_partitions_before(conn, table, cutoff)
        conn.execute("DELETE FROM alert_changes WHERE at < now() - make_interval(days => %s)",
                     (cfg.retention.alert_changes_days,))
        put_setting(conn, "last_commit", {"at": datetime.now(timezone.utc).isoformat(),
                                          "today": today.isoformat()})
    return {"days_committed": days, "baseline_rows": rows}
