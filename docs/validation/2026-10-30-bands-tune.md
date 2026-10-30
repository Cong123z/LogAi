# Bands tune: estimate check and timing (eb2, 2026-10-30)

Spec section 5 gate: coverage estimated from hourly moments vs coverage counted minute by minute must differ by
<= 0.2 pp per method x kind at each k, and give the same best k (one grid step apart allowed, with a note).

## Result: gate PASS (judged at the best k and above)

Rule (spec section 5, user ruling): gap <= 0.2 pp per method x kind at the best k and every larger k, and the same
best k (one grid step apart allowed, with a note). Command: `.venv/bin/python scripts/bands_estimate_check.py --schema lab_s_w28`
(synthetic, 3 services, test days 2026-10-24..26; `tune` writes `settings.bands_tune` into that schema).

- Gap at k >= best k: at most 0.078 pp in all 20 cells (table below, column "max gap, k >= best").
- Best k: estimated and exact agree in 19 of 20 cells; `*warn/*info` nb is one step apart (3 estimated, 2.5 exact; the
  estimate is the safer side). The k stored by `tune` equals the estimated k in all 20.
- Full gap table kept: judged over all k, 9 of 20 cells exceed 0.2 pp, all at k 2 or 2.5 where coverage is only 88-97 %
  (`*total` nb 0.52, `*error` 0.45-0.59, `busy` 0.2-0.37). No best k depends on them.
- `*error` slight optimism at k >= 3.5: the estimate is above the count, e.g. sigma k 3.5: 99.02 estimated vs 98.90 exact;
  median 3.5: 98.76 vs 98.68; k 4: 99.61 vs 99.58. The gap is 0.05-0.07 pp at the best k, but at the margin the
  estimate can pick a k one step too small for `*error`.
- Likely cause of the small-k gaps (not investigated): the hour's NB/Poisson model fed with (m, v) is a poorer fit than
  the true minute distribution when counts are bursty inside the hour.

## Output of the check

schema lab_s_w28, today 2026-10-27, test days 2026-10-24..2026-10-26, tune 41.3 s

| kind | method | max gap, all k (pp) | at k | max gap, k >= best (pp) | best k est | best k exact | stored k | verdict |
|---|---|---|---|---|---|---|---|---|
| *total | sigma | 0.178 | 2.5 | 0.032 | 4 | 4 | 4 | ok |
| *total | nb | 0.518 | 2 | 0.046 | 3 | 3 | 3 | ok |
| *total | median | 0.135 | 2.5 | 0.020 | 4 | 4 | 4 | ok |
| *total | ewma | 0.191 | 2.5 | 0.032 | 4 | 4 | 4 | ok |
| *error | sigma | 0.545 | 2 | 0.048 | 4 | 4 | 4 | ok |
| *error | nb | 0.450 | 2 | 0.051 | 3 | 3 | 3 | ok |
| *error | median | 0.585 | 2 | 0.052 | 5 | 5 | 5 | ok |
| *error | ewma | 0.480 | 2 | 0.068 | 4 | 4 | 4 | ok |
| *warn/*info | sigma | 0.083 | 3.5 | 0.039 | 5 | 5 | 5 | ok |
| *warn/*info | nb | 0.051 | 2.5 | 0.051 | 3 | 2.5 | 3 | ok, best k one step apart |
| *warn/*info | median | 0.111 | 3 | 0.019 | 6 | 6 | 6 | ok |
| *warn/*info | ewma | 0.093 | 3 | 0.059 | 5 | 5 | 5 | ok |
| busy | sigma | 0.371 | 2 | 0.012 | 8 | 8 | 8 | ok |
| busy | nb | 0.200 | 2 | 0.029 | 3 | 3 | 3 | ok |
| busy | median | 0.333 | 2 | 0.007 | 8 | 8 | 8 | ok |
| busy | ewma | 0.371 | 2 | 0.015 | 8 | 8 | 8 | ok |
| sparse | sigma | 0.123 | 3 | 0.015 | 8 | 8 | 8 | ok |
| sparse | nb | 0.111 | 2 | 0.078 | 2.5 | 2.5 | 2.5 | ok |
| sparse | median | 0.095 | 2 | 0.017 | 8 | 8 | 8 | ok |
| sparse | ewma | 0.144 | 2 | 0.007 | 8 | 8 | 8 | ok |

Per k (estimated / exact coverage %, gap pp):

*total

| method | k 2 | k 2.5 | k 3 | k 3.5 | k 4 | k 5 | k 6 | k 8 |
|---|---|---|---|---|---|---|---|---|
| sigma | 94.32 / 94.20 | 97.47 / 97.29 | 98.86 / 98.90 | 99.47 / 99.48 | 99.74 / 99.78 | 99.93 / 99.94 | 99.98 / 99.99 | 100.00 / 100.00 |
| nb | 97.22 / 96.70 | 99.37 / 99.31 | 99.85 / 99.86 | 99.95 / 99.99 | 99.97 / 100.00 | 99.99 / 100.00 | 100.00 / 100.00 | 100.00 / 100.00 |
| median | 94.05 / 93.96 | 97.16 / 97.02 | 98.65 / 98.64 | 99.34 / 99.31 | 99.67 / 99.65 | 99.91 / 99.92 | 99.97 / 99.98 | 100.00 / 100.00 |
| ewma | 93.96 / 93.81 | 97.32 / 97.13 | 98.79 / 98.84 | 99.43 / 99.41 | 99.71 / 99.75 | 99.92 / 99.92 | 99.97 / 99.98 | 100.00 / 100.00 |

*error

| method | k 2 | k 2.5 | k 3 | k 3.5 | k 4 | k 5 | k 6 | k 8 |
|---|---|---|---|---|---|---|---|---|
| sigma | 87.75 / 88.29 | 94.45 / 94.90 | 97.62 / 97.60 | 99.02 / 98.90 | 99.61 / 99.58 | 99.94 / 99.89 | 99.99 / 99.98 | 100.00 / 99.99 |
| nb | 94.26 / 94.71 | 98.51 / 98.48 | 99.69 / 99.68 | 99.94 / 99.89 | 99.99 / 99.97 | 100.00 / 99.99 | 100.00 / 100.00 | 100.00 / 100.00 |
| median | 88.00 / 88.58 | 94.09 / 94.61 | 97.23 / 97.21 | 98.76 / 98.68 | 99.49 / 99.44 | 99.91 / 99.86 | 99.99 / 99.97 | 100.00 / 99.99 |
| ewma | 88.83 / 89.31 | 94.86 / 95.14 | 97.81 / 97.79 | 99.16 / 99.05 | 99.70 / 99.63 | 99.96 / 99.92 | 99.99 / 99.98 | 100.00 / 99.99 |

*warn/*info

| method | k 2 | k 2.5 | k 3 | k 3.5 | k 4 | k 5 | k 6 | k 8 |
|---|---|---|---|---|---|---|---|---|
| sigma | 93.50 / 93.47 | 96.57 / 96.55 | 97.99 / 98.08 | 98.68 / 98.76 | 99.11 / 99.19 | 99.58 / 99.62 | 99.77 / 99.79 | 99.89 / 99.90 |
| nb | 97.97 / 97.97 | 99.48 / 99.53 | 99.85 / 99.88 | 99.95 / 99.98 | 99.98 / 99.98 | 99.99 / 99.99 | 100.00 / 100.00 | 100.00 / 100.00 |
| median | 93.53 / 93.57 | 96.42 / 96.46 | 97.79 / 97.90 | 98.62 / 98.68 | 98.97 / 99.05 | 99.45 / 99.47 | 99.71 / 99.72 | 99.88 / 99.90 |
| ewma | 93.55 / 93.61 | 96.21 / 96.20 | 97.69 / 97.78 | 98.55 / 98.61 | 99.06 / 99.14 | 99.53 / 99.59 | 99.70 / 99.73 | 99.87 / 99.88 |

busy

| method | k 2 | k 2.5 | k 3 | k 3.5 | k 4 | k 5 | k 6 | k 8 |
|---|---|---|---|---|---|---|---|---|
| sigma | 94.03 / 93.66 | 95.87 / 95.60 | 97.08 / 96.88 | 97.90 / 97.75 | 98.46 / 98.38 | 99.13 / 99.09 | 99.48 / 99.48 | 99.78 / 99.79 |
| nb | 97.06 / 96.86 | 99.09 / 99.08 | 99.73 / 99.76 | 99.92 / 99.94 | 99.98 / 99.98 | 100.00 / 100.00 | 100.00 / 100.00 | 100.00 / 100.00 |
| median | 94.00 / 93.67 | 95.77 / 95.49 | 96.95 / 96.74 | 97.76 / 97.61 | 98.32 / 98.23 | 99.01 / 98.96 | 99.39 / 99.36 | 99.73 / 99.73 |
| ewma | 94.04 / 93.67 | 95.83 / 95.59 | 97.02 / 96.82 | 97.83 / 97.67 | 98.39 / 98.29 | 99.07 / 99.03 | 99.43 / 99.40 | 99.75 / 99.77 |

sparse

| method | k 2 | k 2.5 | k 3 | k 3.5 | k 4 | k 5 | k 6 | k 8 |
|---|---|---|---|---|---|---|---|---|
| sigma | 94.94 / 95.04 | 95.95 / 96.07 | 97.33 / 97.45 | 98.08 / 98.19 | 98.42 / 98.52 | 98.98 / 99.04 | 99.34 / 99.37 | 99.68 / 99.69 |
| nb | 98.71 / 98.82 | 99.55 / 99.63 | 99.86 / 99.90 | 99.95 / 99.97 | 99.98 / 99.99 | 100.00 / 100.00 | 100.00 / 100.00 | 100.00 / 100.00 |
| median | 94.74 / 94.84 | 95.47 / 95.56 | 96.44 / 96.51 | 97.12 / 97.18 | 97.56 / 97.61 | 98.05 / 98.07 | 98.32 / 98.32 | 98.74 / 98.72 |
| ewma | 96.03 / 96.17 | 97.04 / 97.18 | 97.72 / 97.84 | 98.23 / 98.34 | 98.72 / 98.80 | 99.17 / 99.22 | 99.43 / 99.46 | 99.67 / 99.68 |

GATE (best k and above): PASS


## Timing

- `lab_s_w28`, about 820 keys (4 service keys x 3 + 810 templates), 3 test days:
  before the recursive pmf: `tune` 94.1 s (0.115 s/key); after (fix round 1): `tune` 41.3 s (0.050 s/key). The
  result tables are identical. Extrapolation (labelled, not measured): 27,000 templates is about 23 minutes
  single-threaded (was about 52). A Re-tune button on a company-size database will be slow; eb3 must run it in a thread and show progress.
- Live laptop DB (`recharge`, only 4 committed days 26-29 Oct): `python -m logai2.bands tune config.yaml`, 1.4 s wall,
  184 keys (4 service, 29 busy, 151 sparse). Few training days make most hours skip, so this is not representative.
  Result stored in the live `settings.bands_tune`.
- `tail_above` / `tail_below` on busy NB keys (per call, 50 calls averaged, warm cache not involved):

  | m, v | call | ms |
  |---|---|---|
  | 900, 9000 | tail_above t=1184 (far side) | 0.33 |
  | 900, 9000 | tail_below t=852 (near/complement side) | 0.38 |
  | 900, 9000 | tail_above t=900 | 0.42 |
  | 50, 400 | tail_above t=110 / tail_below t=40 | 0.14 / 0.02 |
  | 5, 30 | tail_above t=21 | 0.08 |

  Inside `tune` they are wrapped by an `lru_cache` (an hour's (m, v) is evaluated by 4 methods x 8 k, many with the same t).
