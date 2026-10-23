# Template modules: stress test (2026-10-23 plan, Task 12)

Script: `scripts/stress_modules.py` (throwaway schema in `logai_test`, `COPY` load, timings vs targets,
`EXPLAIN (ANALYZE, BUFFERS)`, schema dropped at the end). `tests/test_stress_modules.py` runs it at `--scale tiny`.

## Machine

- AMD Ryzen 7 8845H (16 threads), 12 GB RAM (about 3 GB available during the run), NVMe, Linux 7.0.
- PostgreSQL 16.15 (`pgvector/pgvector:pg16` in Docker, defaults: `shared_buffers` 128 MB,
  `max_wal_size` 1 GB). Python 3.14, psycopg 3.3.6, Flask test client (one connection per request, as served).
- Run on 2026-10-24 local time (the window covered 2026-10-23 and 2026-10-24).

## Scale actually run (ruling R9: disk)

Only ~2.3 GB was free on `/`, the disk of the Postgres volume, so the default run loads what the API reads after
partition pruning, not the whole retention:

- 50 services x 2,000 templates = 100k templates; 3 modules each = 300k pairs (`main` in 90 % of a service's
  templates = the large module, `''` in the rest, two smaller ones); 30 % of pairs active per hour.
- `template_module_hours`: the 24 window hours at full density = **2,158,851 rows** (~90k per hour), plus one
  older day partition of 10,000 rows for the retention drop.
- Migration 007 seed: from **300,000** `alert_minutes` rows (not 1M) in its own schema; reported per 1M rows.
- Before loading, the script estimates rows x 150 B x 2 (WAL) and refuses (exit 2) above half the free space
  on `/` or `--max-bytes`. Estimate here: 787 MB against a 1,171 MB limit. Each flush run is followed by an
  untimed `VACUUM` (bounds bloat to one run) and each phase by a `CHECKPOINT`.
- **The full 7-day load (`--days 7`, ~15M rows, ~2 GB of table + WAL) needs >= 5 GB free and was not run.**
  Per-hour density is the same, and every API query reads only the 1-2 window partitions, so query times
  should not change; the flush upserts into 1-2 partitions as well (index depth grows by at most one level).

## Results

p95 of 20 requests (API) or 10 flushes, warm cache. Two runs after the fix; the flush rows also show the
run before it.

| Measure | Before fix | Run 1 | Run 2 | Target |
|---|---|---|---|---|
| Flush, 20k pairs (both module upserts, one transaction) | 0.976 s | 0.557 s | 0.428 s | <= 1.0 s |
| Flush, 60k pairs (catch-up page) | 2.622 s | 1.271 s | 1.267 s | <= 3.0 s |
| `GET /api/templates?service=s&size=100` | 38 ms | 25 ms | 25 ms | <= 300 ms |
| `GET /api/templates?service=s&size=500` | 53 ms | 50 ms | 50 ms | <= 800 ms |
| `GET /api/templates?service=s&module=m&size=100` (m = 1,800 templates) | 39 ms | 36 ms | 37 ms | <= 500 ms |
| `GET /api/modules?service=s` | 33 ms | 30 ms | 31 ms | <= 300 ms |
| Retention: drop one day partition | 8 ms | 5 ms | 6 ms | <= 2 s |
| Migration 007 seed, per 1M `alert_minutes` (2.8-3.1 s for 300k, linear) | 10.2 s | 9.5 s | 9.3 s | <= 60 s |
| Size of 7 days, `template_module_hours` (137 B/row incl. PK x 15.12M rows) | 1,982 MB | 1,982 MB | 1,982 MB | reported |
| `template_modules` (300k pairs) | 41 MB | 41 MB | 41 MB | reported |

The API numbers are whole requests (connect, the count/stats/items queries, baseline and modules lookups,
JSON); the module queries themselves take 3-26 ms (plans below).

## Fix: module upserts by COPY

The first run met every target, but the 20k-pair flush took 0.976 s of its 1.0 s budget with `executemany`.
`Owner.flush` now writes the two module tables with `owner.copy_upsert`: `COPY` into a session temp table
(`CREATE TEMP TABLE IF NOT EXISTS tmp_<table> (LIKE <table>) ON COMMIT DELETE ROWS`), then one
`INSERT … SELECT … ON CONFLICT` (`SQL["template_module_hours"]`, `SQL["template_modules"]`), still inside the
flush transaction. A failed flush rolls the temp rows back with everything else (`test_owner.py`
`test_module_hours_exact_across_failed_flush` covers the retry on the same session). Result: 0.98 -> 0.43-0.56 s
(20k), 2.62 -> 1.27 s (60k). The other flush tables still use `executemany` (ARCHITECTURE §14).

## EXPLAIN highlights

- **Window lines of 100 templates** (`modules_for`): partition pruning to the two window days, one PK index probe
  per template per partition, 2.8 ms.
- **Module filter, window lines first** (`/api/templates?module=`): the `EXISTS` on `template_modules` becomes a
  hash join on an index-only scan of the PK (`service`, `module_code` as index conditions; 1,800 rows); the
  `ORDER BY EXISTS` subplan is one index-only probe per filtered template (1,800 loops, ~3 us each) and the
  sort is a top-N heapsort. 8.6 ms. The cost is linear in the module's templates within one service: no fix
  needed at 2,000 per service; at ~50k templates in one module of one service it would be ~150-250 ms.
- **`/api/modules` window aggregate**: bitmap scans of the service in the two window partitions (47k rows),
  hash aggregate, hash join with the service's 6,000 pairs. 26 ms, linear in a service's window rows
  (~0.5 us/row); 300 ms would be ~500k window rows in one service.
- All buffers were cache hits (the window partitions are ~300 MB, in the OS cache). A cold cache adds disk reads
  on the first requests.

No index or query change was needed.

## Disk

Free on `/` before and after each run: 2,342 -> 2,342 MB, 2,343 -> 2,328 MB, 2,328 -> 2,320 MB; no `stress%`
schema left, `logai_test` 155 MB. `pg_wal` holds 960 MB of recycled segments (bounded by `max_wal_size`
1 GB; the first run gave back exactly what it took, so it was that size before). The 15 MB and 8 MB drift of
the later runs is not in `logai_test`; it was not attributed (other containers share the disk).

## Plans (run 1, after the fix)

### EXPLAIN (ANALYZE, BUFFERS): window lines of 100 templates (modules_for)

```
HashAggregate  (cost=1738.38..1770.57 rows=2146 width=28) (actual time=2.742..2.780 rows=300 loops=1)
  Group Key: h.service, h.template_id, h.module_code
  Batches: 1  Memory Usage: 177kB
  Buffers: shared hit=2826
  ->  Hash Semi Join  (cost=2.54..1716.92 rows=2146 width=28) (actual time=0.059..2.342 rows=2357 loops=1)
        Hash Cond: ((h.day = unnest_1.unnest) AND (h.hour = unnest_1.unnest_1))
        Buffers: shared hit=2826
        ->  Nested Loop  (cost=1.93..1678.76 rows=2146 width=34) (actual time=0.044..2.061 rows=2357 loops=1)
              Buffers: shared hit=2826
              ->  HashAggregate  (cost=1.50..2.50 rows=100 width=64) (actual time=0.026..0.036 rows=100 loops=1)
                    Group Key: unnest.unnest, unnest.unnest_1
                    Batches: 1  Memory Usage: 24kB
                    ->  Function Scan on unnest  (cost=0.01..1.00 rows=100 width=64) (actual time=0.007..0.013 rows=100 loops=1)
              ->  Append  (cost=0.43..16.74 rows=2 width=34) (actual time=0.006..0.019 rows=24 loops=100)
                    Buffers: shared hit=2826
                    ->  Index Scan using template_module_hours_20261023_pkey on template_module_hours_20261023 h_1  (cost=0.43..8.45 rows=1 width=34) (actual time=0.005..0.013 rows=20 loops=100)
                          Index Cond: ((service = unnest.unnest) AND (template_id = unnest.unnest_1))
                          Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                          Buffers: shared hit=2259
                    ->  Index Scan using template_module_hours_20261024_pkey on template_module_hours_20261024 h_2  (cost=0.42..8.28 rows=1 width=34) (actual time=0.004..0.005 rows=4 loops=100)
                          Index Cond: ((service = unnest.unnest) AND (template_id = unnest.unnest_1))
                          Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                          Buffers: shared hit=567
        ->  Hash  (cost=0.24..0.24 rows=24 width=8) (actual time=0.010..0.010 rows=24 loops=1)
              Buckets: 1024  Batches: 1  Memory Usage: 9kB
              ->  Function Scan on unnest_1  (cost=0.01..0.24 rows=24 width=8) (actual time=0.006..0.007 rows=24 loops=1)
Planning:
  Buffers: shared hit=174
Planning Time: 0.367 ms
Execution Time: 2.845 ms
```

### EXPLAIN (ANALYZE, BUFFERS): module filter, window lines first (/api/templates items)

```
Limit  (cost=1932.52..1932.57 rows=20 width=24) (actual time=8.563..8.571 rows=100 loops=1)
  Buffers: shared hit=5506
  ->  Sort  (cost=1932.52..1932.57 rows=20 width=24) (actual time=8.562..8.566 rows=100 loops=1)
        Sort Key: ((SubPlan 1)) DESC, t.last_seen DESC, t.template_id
        Sort Method: top-N heapsort  Memory: 37kB
        Buffers: shared hit=5506
        ->  Hash Join  (cost=247.41..1932.08 rows=20 width=24) (actual time=0.577..8.180 rows=1800 loops=1)
              Hash Cond: (t.template_id = m.template_id)
              Buffers: shared hit=5506
              ->  Bitmap Heap Scan on templates t  (cost=47.06..1621.33 rows=1890 width=23) (actual time=0.064..0.203 rows=2000 loops=1)
                    Recheck Cond: (service = 's00'::text)
                    Heap Blocks: exact=29
                    Buffers: shared hit=39
                    ->  Bitmap Index Scan on templates_pkey  (cost=0.00..46.59 rows=1890 width=0) (actual time=0.059..0.059 rows=2000 loops=1)
                          Index Cond: (service = 's00'::text)
                          Buffers: shared hit=10
              ->  Hash  (cost=186.79..186.79 rows=1084 width=15) (actual time=0.494..0.494 rows=1800 loops=1)
                    Buckets: 2048  Batches: 1  Memory Usage: 99kB
                    Buffers: shared hit=62
                    ->  Index Only Scan using template_modules_pkey on template_modules m  (cost=0.42..186.79 rows=1084 width=15) (actual time=0.016..0.358 rows=1800 loops=1)
                          Index Cond: ((service = 's00'::text) AND (module_code = 'main'::text))
                          Heap Fetches: 0
                          Buffers: shared hit=62
              SubPlan 1
                ->  Nested Loop Semi Join  (cost=0.43..10.11 rows=2 width=0) (actual time=0.004..0.004 rows=1 loops=1800)
                      Join Filter: ((h.day = unnest.unnest) AND (h.hour = unnest.unnest_1))
                      Rows Removed by Join Filter: 2
                      Buffers: shared hit=5405
                      ->  Append  (cost=0.43..8.91 rows=2 width=6) (actual time=0.003..0.003 rows=1 loops=1800)
                            Buffers: shared hit=5405
                            ->  Index Only Scan using template_module_hours_20261023_pkey on template_module_hours_20261023 h_1  (cost=0.43..4.45 rows=1 width=6) (actual time=0.003..0.003 rows=1 loops=1800)
                                  Index Cond: ((service = t.service) AND (template_id = t.template_id) AND (module_code = 'main'::text))
                                  Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                                  Heap Fetches: 0
                                  Buffers: shared hit=5401
                            ->  Index Only Scan using template_module_hours_20261024_pkey on template_module_hours_20261024 h_2  (cost=0.42..4.45 rows=1 width=6) (actual time=0.011..0.011 rows=1 loops=1)
                                  Index Cond: ((service = t.service) AND (template_id = t.template_id) AND (module_code = 'main'::text))
                                  Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                                  Heap Fetches: 0
                                  Buffers: shared hit=4
                      ->  Function Scan on unnest  (cost=0.01..0.24 rows=24 width=8) (actual time=0.000..0.000 rows=3 loops=1800)
Planning:
  Buffers: shared hit=52
Planning Time: 0.350 ms
Execution Time: 8.597 ms
```

### EXPLAIN (ANALYZE, BUFFERS): /api/modules window aggregate

```
Sort  (cost=24581.45..24581.53 rows=29 width=21) (actual time=26.095..26.098 rows=29 loops=1)
  Sort Key: (count(*)) DESC, m.module_code
  Sort Method: quicksort  Memory: 26kB
  Buffers: shared hit=889
  ->  GroupAggregate  (cost=24544.79..24580.75 rows=29 width=21) (actual time=25.494..26.081 rows=29 loops=1)
        Group Key: m.module_code
        Buffers: shared hit=889
        ->  Sort  (cost=24544.79..24553.67 rows=3553 width=37) (actual time=25.468..25.631 rows=6000 loops=1)
              Sort Key: m.module_code
              Sort Method: quicksort  Memory: 425kB
              Buffers: shared hit=889
              ->  Hash Right Join  (cost=23082.45..24335.25 rows=3553 width=37) (actual time=22.290..24.124 rows=6000 loops=1)
                    Hash Cond: ((h.template_id = m.template_id) AND (h.module_code = m.module_code))
                    Buffers: shared hit=889
                    ->  HashAggregate  (cost=22826.56..23390.88 rows=45146 width=48) (actual time=21.159..21.807 rows=6000 loops=1)
                          Group Key: h.template_id, h.module_code
                          Batches: 1  Memory Usage: 1809kB
                          Buffers: shared hit=827
                          ->  Hash Semi Join  (cost=1648.12..22487.96 rows=45146 width=24) (actual time=1.386..14.122 rows=47172 loops=1)
                                Hash Cond: ((h.day = unnest.unnest) AND (h.hour = unnest.unnest_1))
                                Buffers: shared hit=827
                                ->  Append  (cost=1647.52..21697.30 rows=45146 width=30) (actual time=1.366..8.634 rows=47172 loops=1)
                                      Buffers: shared hit=827
                                      ->  Bitmap Heap Scan on template_module_hours_20261023 h_1  (cost=1647.52..18764.59 rows=42205 width=30) (actual time=1.365..5.276 rows=39353 loops=1)
                                            Recheck Cond: (service = 's00'::text)
                                            Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                                            Heap Blocks: exact=355
                                            Buffers: shared hit=668
                                            ->  Bitmap Index Scan on template_module_hours_20261023_pkey  (cost=0.00..1636.96 rows=42205 width=0) (actual time=1.338..1.338 rows=39353 loops=1)
                                                  Index Cond: (service = 's00'::text)
                                                  Buffers: shared hit=313
                                      ->  Bitmap Heap Scan on template_module_hours_20261024 h_2  (cost=183.21..2706.98 rows=2941 width=30) (actual time=0.284..1.057 rows=7819 loops=1)
                                            Recheck Cond: (service = 's00'::text)
                                            Filter: (day = ANY ('{2026-10-23,2026-10-24}'::date[]))
                                            Heap Blocks: exact=74
                                            Buffers: shared hit=159
                                            ->  Bitmap Index Scan on template_module_hours_20261024_pkey  (cost=0.00..182.48 rows=2941 width=0) (actual time=0.275..0.275 rows=7819 loops=1)
                                                  Index Cond: (service = 's00'::text)
                                                  Buffers: shared hit=85
                                ->  Hash  (cost=0.24..0.24 rows=24 width=8) (actual time=0.017..0.017 rows=24 loops=1)
                                      Buckets: 1024  Batches: 1  Memory Usage: 9kB
                                      ->  Function Scan on unnest  (cost=0.01..0.24 rows=24 width=8) (actual time=0.006..0.007 rows=24 loops=1)
                    ->  Hash  (cost=202.60..202.60 rows=3553 width=16) (actual time=1.128..1.128 rows=6000 loops=1)
                          Buckets: 8192 (originally 4096)  Batches: 1 (originally 1)  Memory Usage: 346kB
                          Buffers: shared hit=62
                          ->  Index Only Scan using template_modules_pkey on template_modules m  (cost=0.42..202.60 rows=3553 width=16) (actual time=0.007..0.476 rows=6000 loops=1)
                                Index Cond: (service = 's00'::text)
                                Heap Fetches: 0
                                Buffers: shared hit=62
Planning:
  Buffers: shared hit=31
Planning Time: 0.187 ms
Execution Time: 26.161 ms
```

