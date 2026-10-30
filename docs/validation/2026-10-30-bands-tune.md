# Bands tune: estimate check and timing (eb2, 2026-10-30)

Spec section 5 gate: coverage estimated from hourly moments vs coverage counted minute by minute must differ by
<= 0.2 pp per method x kind at each k, and give the same best k (one grid step apart allowed, with a note).

## Result: gate FAILED (gap), best k agrees

Command: `.venv/bin/python scripts/bands_estimate_check.py --schema lab_s_w28` (schema `lab_s_w28`, synthetic, 3 services,
test days 2026-10-24..26, `tune` writes `settings.bands_tune` into that schema). `tune` itself took 94.1 s there.
The script's whole run (tune + the minute-by-minute pass) finished inside the 10 minute limit.

- Best k: estimated and exact are the same in 19 of 20 method x kind cells; `*warn/*info` / `nb` is one step apart
  (3 estimated, 2.5 exact; the estimate is the safer side). The k stored by `tune` equals the script's estimated k in all 20.
- Gap: 9 of 20 cells exceed 0.2 pp at some k (`*total`/nb 0.52, `*error` all four 0.45-0.59, `busy` sigma/median/ewma
  0.33-0.37 and nb 0.200 rounded up). All of them are at the small k (k 2, or 2.5 for a few): the coverage there is 88-97 %,
  far below the 99.5 % target, so no best k depends on it. At the k that matters (>= the best k) the gaps are
  <= 0.1 pp except `*error` sigma/median/ewma at k 3.5-5 (<= 0.13 pp) -- read from the per-k tables below.
- Per spec, a failed gate means the page labels coverage "estimated" and we discuss before eb3 merges. Not tuned away.
- Likely cause (not investigated further): the hour's NB/Poisson model fed with (m, v) of the hour is a poorer fit than the
  true minute distribution when counts are bursty inside the hour (`*error`, `busy`), and the estimator is biased
  at k 2 (it predicts fewer outside minutes than are counted for `*total`/`busy`, more for `*error`).

## Output of the check

schema lab_s_w28, today 2026-10-27, test days 2026-10-24..2026-10-26, tune 94.1 s

| kind | method | max abs gap (pp) | at k | best k est | best k exact | stored k | verdict |
|---|---|---|---|---|---|---|---|
| *total | sigma | 0.178 | 2.5 | 4 | 4 | 4 | ok |
| *total | nb | 0.518 | 2 | 3 | 3 | 3 | FAIL |
| *total | median | 0.135 | 2.5 | 4 | 4 | 4 | ok |
| *total | ewma | 0.191 | 2.5 | 4 | 4 | 4 | ok |
| *error | sigma | 0.545 | 2 | 4 | 4 | 4 | FAIL |
| *error | nb | 0.450 | 2 | 3 | 3 | 3 | FAIL |
| *error | median | 0.585 | 2 | 5 | 5 | 5 | FAIL |
| *error | ewma | 0.480 | 2 | 4 | 4 | 4 | FAIL |
| *warn/*info | sigma | 0.083 | 3.5 | 5 | 5 | 5 | ok |
| *warn/*info | nb | 0.051 | 2.5 | 3 | 2.5 | 3 | ok, best k one step apart |
| *warn/*info | median | 0.111 | 3 | 6 | 6 | 6 | ok |
| *warn/*info | ewma | 0.093 | 3 | 5 | 5 | 5 | ok |
| busy | sigma | 0.371 | 2 | 8 | 8 | 8 | FAIL |
| busy | nb | 0.200 | 2 | 3 | 3 | 3 | FAIL |
| busy | median | 0.333 | 2 | 8 | 8 | 8 | FAIL |
| busy | ewma | 0.371 | 2 | 8 | 8 | 8 | FAIL |
| sparse | sigma | 0.123 | 3 | 8 | 8 | 8 | ok |
| sparse | nb | 0.111 | 2 | 2.5 | 2.5 | 2.5 | ok |
| sparse | median | 0.095 | 2 | 8 | 8 | 8 | ok |
| sparse | ewma | 0.144 | 2 | 8 | 8 | 8 | ok |

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

GATE: FAIL [('*total', 'nb'), ('*error', 'sigma'), ('*error', 'nb'), ('*error', 'median'), ('*error', 'ewma'), ('busy', 'sigma'), ('busy', 'nb'), ('busy', 'median'), ('busy', 'ewma')]


## Timing

- `lab_s_w28`: `tune` 94.1 s for 3 services (about 820 keys: 4 service keys x 3 + 810 templates), 3 test days.
  About 0.11 s per key. Extrapolation (labelled, not measured): at 27,000 templates that is roughly 50 minutes
  single-threaded. A Re-tune button on a company-size database will be slow; eb3 must run it in a thread and show progress.
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
