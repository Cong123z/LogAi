# Evaluation bands (4 methods on read) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the lab-run Evaluation page with one that draws, for one key, the hourly rate and the ranges of
4 methods (sigma, nb, median, ewma), computed from Postgres on request, each at the k a walk-forward tune picked.

**Architecture:** A pure module `logai2/bands.py` reads (n, s, q) per day-hour from the v15 tables, fits each
method and estimates minutes outside a band. `tune()` walks the last 3 days and stores the best k per method × kind
in `settings.bands_tune`. The web serves bands per key on request and runs Re-tune in a thread. The page is
rewritten in `evaluation.js` with ECharts.

**Tech Stack:** Python 3.11, psycopg 3, Flask, Postgres 16, vanilla JS + vendored ECharts, pytest.

**Spec:** `docs/specs/2026-10-30-evaluation-bands.md` (read it first; section numbers below refer to it).
Mockup: https://claude.ai/artifact/MH5YqMboT6BAGKwbAr9u22.

## Global Constraints

- Read only tables of migrations 001-009 (`hourly_counts`, `service_hours`, `day_status`, `review_exclusions`,
  `templates`, `services`, `settings`) plus `services.monitored` (012). No new migration, no new table.
- The web reads no `lab_*` table after this build. `scripts/lab/` is not imported by `logai2/`.
- No new dependency (no scipy, numpy only if already imported by the web; prefer `math` / `statistics`).
- Methods, in this order everywhere: `("sigma", "nb", "median", "ewma")`.
- k grid `(2, 2.5, 3, 3.5, 4, 5, 6, 8)`; target coverage `0.995`; untuned k `3`; test days default `3`.
- `settings` key `bands_tune`, `"version": 1`.
- Kinds: `"*total"`, `"*error"`, `"*warn/*info"`, `"busy"`, `"sparse"`; busy = template mean ≥ 2 per minute.
- Flags (exact text): `"never reaches 99.5 %"`, `"not enough days"`, `""`.
- Page name and nav label: `Evaluation`; route `#evaluation/<service>/<key>/<days>`; days `1` or `7`.
- Tests: `.venv/bin/pytest -q` against the test Postgres (`docker compose up -d postgres`). Implementers and
  reviewers run the focused set named in their task; the tech lead runs the full suite before the master merge.
- One branch + worktree per task (`task/eb1-bands` …), PR file in `.team/prs/`, merge into `build/eval-bands`,
  master only after the user confirms. Stage by explicit path; no AI attribution in commits.

## Review Focus

1. A key-hour with a `service_hours` row but no `hourly_counts` row is 0 counts over n minutes, not a missing hour
   (REBUILD parity); a day with no `service_hours` row for the hour is not a training day for it. → eb1 test
   `test_missing_counts_row_is_zero`.
2. An hour excluded in Review, or with `n_minutes = 0`, is skipped as training data and as a test hour. → eb1
   `test_excluded_and_empty_hours_skipped`, eb2 `test_tune_skips_excluded_test_hour`.
3. Rounding makes q/n − m² slightly negative on a flat key: v clamps to 0 and nb becomes Poisson, never a math
   domain error. → eb1 `test_flat_key_variance_clamped`.
4. A weekend with fewer than 3 days: `median` has no weekend band and says so; the other methods still draw;
   a brand-new service with no committed day returns 200 with every method `missing: "not enough days"`. → eb1
   `test_min_days_per_method`, eb3 `test_bands_no_history`.
5. Re-tune twice, or the web dies mid-run: second POST is 409, the stored result is the previous one until the run
   finishes. → eb3 `test_retune_conflict_and_old_result_kept`.

---

### Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope

**Files:**
- Create: `logai2/bands.py`
- Test: `tests/test_bands.py` (fixtures: copy the `service`, `hours`, `committed`, `exclude` helpers from
  `tests/test_web_baseline_view.py`)

**Interfaces:**
- Produces:
  - `METHODS = ("sigma", "nb", "median", "ewma")`, `MIN_DAYS = {"sigma": 1, "nb": 1, "median": 3, "ewma": 2}`,
    `EWMA_DAYS = 28`, `HALF_LIFE = 2.0`, `UNTUNED_K = 3.0`
  - `class DayHour(NamedTuple): day: date; n: int; s: int; q: int` with properties `m` (s/n) and `v`
    (max(0, q/n − m²))
  - `class Band(NamedTuple): low: float; high: float`
  - `history(conn, service: str, key: str, first: date, last: date) -> dict[tuple[date, int], DayHour]` — every
    committed day in [first, last] with a `service_hours` row, n > 0, not excluded (REBUILD rules, spec §3)
  - `training(hist: dict[tuple[date, int], DayHour], day: date, hour: int, method: str) -> list[DayHour]` — the
    rows a method fits on to judge `day` (picked 5/4 days of `day`'s daytype before it; ewma: same daytype, the
    28 days before)
  - `band(method: str, rows: list[DayHour], k: float) -> Band | None` — None below `MIN_DAYS[method]`
  - `nb_quantiles(mu: float, var: float, p: float) -> tuple[int, int]` — port of
    `scripts/lab/methods/nb.py::_quantiles` with the normal branch for mu > 1000
  - `tail_above(m: float, v: float, t: int) -> float` = P(X ≥ t); `tail_below(m: float, v: float, t: int) -> float`
    = P(X ≤ t); X ~ NB / Poisson / normal as spec §5.2
  - `floor_of(cfg, key: str, level: str | None) -> int | None`
  - `kind_of(key: str, mean: float) -> str`
  - `in_scope(conn, service: str, key: str, now: datetime) -> bool`

- [ ] **Step 1: Write the failing tests** in `tests/test_bands.py`:
  - `test_sigma_equals_rebuild`: seed 6 weekdays × 3 hours with varied (s, q, n), run
    `logai2.jobs.commit.commit(conn, cfg, today)`, then for every (key, hour): `band("sigma", training(...), 3)`
    == `(mean − 3·std, mean + 3·std)` of the `baseline` row, `pytest.approx(rel=1e-9)`.
  - `test_nb_quantiles_match_lab`: for mu in (0.2, 1, 3.7, 25, 400), var in (mu, 2·mu, 10·mu) and k in the grid:
    `nb_quantiles(mu, var, 1 − Φ(k)) == scripts.lab.methods.nb.nb_quantiles(mu, var, 1 − Φ(k))`.
  - `test_nb_poisson_when_var_le_mean`: `nb_quantiles(4, 3, p) == nb_quantiles(4, 4, p)`.
  - `test_median_robust`: m_d = 2, 3, 2, 8, 3 and v_d = 2, 3, 2, 10, 3 (n = 60 each) →
    `band("median", rows, 3).high == pytest.approx(9.8398, abs=1e-3)`; sigma on the same rows `== approx(12.62, abs=1e-2)`.
  - `test_ewma_weights`: the same rows, oldest first, newest age 0 → `band("ewma", rows, 3).high ==
    pytest.approx(13.510, abs=1e-3)`.
  - `test_missing_counts_row_is_zero`, `test_excluded_and_empty_hours_skipped`, `test_flat_key_variance_clamped`,
    `test_min_days_per_method` (Review Focus 1-4).
  - `test_warn_info_born_rule`: `*warn` with rows only from day 3 has no training day before day 3.
  - `test_tails`: `tail_above(2.0, 2.0, 0) == 1.0`; Poisson(2): `tail_above(2, 2, 5) == approx(0.05265, abs=1e-5)`;
    `tail_below(2, 2, 0) == approx(0.13534, abs=1e-5)`; m = 5000 uses the normal branch.
  - `test_kind_and_scope`: `kind_of("*warn", 9) == "*warn/*info"`, template mean 2.0 → `"busy"`, 1.99 →
    `"sparse"`; `in_scope` true for `*total`, false for `doc:x`, `verdict:action`, a template first seen 23 h ago,
    true at 25 h.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_bands.py` — expect FAIL (module missing).
- [ ] **Step 3: Implement `logai2/bands.py`.** `history` is one SQL query: `service_hours` LEFT JOIN
  `hourly_counts` (coalesce 0), JOIN committed `day_status`, NOT EXISTS excluded `review_exclusions`, the `*warn`/
  `*info` born day as in `REBUILD`. Picked days per daytype come from `day_status` (reuse `DAY_CAP` from
  `logai2/owner/baseline.py`). Φ via `math.erfc`. Module docstring: one line, "Pure band methods for the
  Evaluation page; spec docs/specs/2026-10-30-evaluation-bands.md".
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_bands.py tests/test_commit.py` — expect PASS.
- [ ] **Step 5: Commit** `git add logai2/bands.py tests/test_bands.py && git commit -m "bands: 4 methods, tails, scope (eb1)"`.

Review: Opus.

---

### Task eb2: `tune` — walk-forward fit test, CLI, estimate check

**Files:**
- Modify: `logai2/bands.py` (add tune, CLI `__main__`)
- Test: `tests/test_bands_tune.py`
- Create: `scripts/bands_estimate_check.py`, report `docs/validation/2026-10-30-bands-tune.md`

**Interfaces:**
- Consumes: everything eb1 produces.
- Produces:
  - `KGRID = (2, 2.5, 3, 3.5, 4, 5, 6, 8)`, `TARGET = 0.995`, `TUNE_VERSION = 1`
  - `tune(conn, cfg, today: date, test_days: int = 3, on_key=None) -> dict` — returns and stores the spec §5 value
    in `settings.bands_tune` (one transaction, `put_setting`); `on_key(done, total)` for progress
  - `stored_tune(conn) -> dict | None` — the setting, or None when missing or `version != 1`
  - `best_k(tune_result: dict | None, kind: str, method: str) -> tuple[float, bool]` — (k, tuned); untuned →
    `(UNTUNED_K, False)`
  - `coverage_exact(...)` lives only in `scripts/bands_estimate_check.py`, not in `logai2/`.
  - CLI: `python -m logai2.bands tune [config.yaml] [--test-days N]`, prints one line per kind and the seconds.

- [ ] **Step 1: Write the failing tests** in `tests/test_bands_tune.py`:
  - `test_best_k_smallest_reaching_target`: a fixture where sigma covers 99.5 % first at k 3.5 → stored
    `kinds["*total"]["methods"]["sigma"]["k"] == 3.5`, flag `""`.
  - `test_never_reaches_flag`: a key with a step change on the test days → some method `k == 8` and flag
    `"never reaches 99.5 %"`.
  - `test_not_enough_days_flag`: only 2 committed weekdays → median flag `"not enough days"`, k None.
  - `test_floor_applies`: `*error` with high 2.4 and floor 5: expected minutes above use t = 5, not 3.
  - `test_low_side_only_total`: a drop on `*total` counts; the same drop on `*info` does not.
  - `test_ratio_sigma_is_one`: `ratio` of sigma is 1.0 in every kind with keys.
  - `test_tune_skips_excluded_test_hour` (Review Focus 2).
  - `test_stored_once_and_versioned`: `stored_tune` returns None before, the dict after; a setting with
    `version: 2` → None.
  - `test_off_service_skipped`: a service with `monitored = false` contributes no key.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_bands_tune.py` — expect FAIL.
- [ ] **Step 3: Implement** `tune`: per monitored service, per in-scope key (stream keys, one `history` call per key
  over [first test day − 28 − 7 days, last test day]), per test day × hour × method: fit once, then evaluate all k.
  Accumulate (outside, n, width) per method × kind × k; pick best k; compute ratio vs sigma at sigma's best k.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_bands.py tests/test_bands_tune.py` — expect PASS.
- [ ] **Step 5: Estimate check.** `scripts/bands_estimate_check.py --schema lab_s_w28` runs `tune` on the schema's
  hourly tables and counts the same bands against `lab_minute_counts.counts` minute by minute. Gate (spec §5):
  |gap| ≤ 0.2 pp per method × kind × k and the same best k (± one step, noted). Also run `python -m logai2.bands tune`
  on the live laptop DB (`recharge`) and record the seconds. Write both into
  `docs/validation/2026-10-30-bands-tune.md` (tables: gap per method × kind; best k estimated vs exact; timing).
  A failed gate is reported as such, not tuned away.
- [ ] **Step 6: Commit** `git add logai2/bands.py tests/test_bands_tune.py scripts/bands_estimate_check.py docs/validation/2026-10-30-bands-tune.md && git commit -m "bands: tune, CLI, estimate check (eb2)"`.

Review: Opus.

---

### Task eb3: API + Evaluation page; delete the lab-run page

**Files:**
- Create: `logai2/web/bands_view.py`
- Modify: `logai2/web/app.py` (remove every `/api/eval/*` route and the `eval_view` import; add the 4 routes of spec
  §6), `logai2/web/static/js/views/evaluation.js` (rewrite), `logai2/web/static/index.html` (the
  `#view-evaluation` section, nav label "Evaluation"), `logai2/web/static/js/main.js` (`evaluation: 'Evaluation'`,
  route params), `logai2/web/static/css/app.css` (only page-specific rules; colours from `tokens.css`)
- Delete: `logai2/web/eval_view.py`, `tests/test_web_eval_view.py`
- Test: `tests/test_web_bands.py`
- Docs: `docs/WEB_UI.md` (Evaluation section + API), `README.md` (Re-tune / CLI line), `ARCHITECTURE.md` (web reads
  no `lab_*`; bands on read), `docs/BUGS.md` (anything found)

**Interfaces:**
- Consumes: `history`, `training`, `band`, `kind_of`, `in_scope`, `METHODS` (eb1); `tune`, `stored_tune`, `best_k`
  (eb2).
- Produces:
  - `bands_series(conn, cfg, service: str, key: str, ndays: int, today: date, now: float) -> dict` — the spec §6
    body; raises `baseline_view.UnknownKey` for unknown or out-of-scope keys
  - `eval_keys(conn, service: str, now: datetime) -> list[dict]`
  - `class Retune` with `start(dsn: str, cfg) -> bool` (False when running), `status() -> dict`; one module-level
    instance; the thread opens its own connection and catches every exception into `error`
  - Routes: `GET /api/eval/bands`, `GET /api/eval/keys`, `GET /api/eval/tune`, `POST /api/eval/tune`; all use
    `known_service`.

- [ ] **Step 1: Write the failing tests** in `tests/test_web_bands.py` (client fixture as in
  `tests/test_web_baseline_view.py`):
  - `test_bands_shape`: 7 days → `points` 6·24 + hours up to now; 4 methods in order; each `bands` list as long as
    `points`; untuned → every `k == 3`, `tuned` false.
  - `test_bands_use_tuned_k`: after `tune`, `methods[1]["k"]` equals the stored nb k of the key's kind.
  - `test_each_day_fitted_on_days_before_it`: the sigma band of day d equals `band("sigma", training(hist, d, h,
    "sigma"), k)`.
  - `test_outside_counts_low_only_total`.
  - `test_bands_no_history` (Review Focus 4): 200, every method `missing == "not enough days"`.
  - `test_bands_errors`: days=14 → 400; `doc:x` → 404; template first seen 1 h ago → 404; Off service → 404.
  - `test_eval_keys_scope_and_order`.
  - `test_retune_conflict_and_old_result_kept` (Review Focus 5): start a run with a patched slow `tune`; second
    POST → 409; `GET /api/eval/tune` → running and `result` the previous value.
  - `test_no_lab_routes`: `GET /api/eval/runs` → 404.
  - `test_bands_timing`: a fixture of 30 days × 24 h for one key answers in < 300 ms (skip on CI flag if flaky,
    with a note).
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_bands.py` — expect FAIL.
- [ ] **Step 3: Implement `bands_view.py` and the routes.**
- [ ] **Step 4: Rewrite the page** to the mockup and spec §7: tune status bar with Re-tune (polls
  `GET /api/eval/tune` every 2 s while running); 4 method cards (anchor links to their chart; "Narrowest at 99.5 %"
  on the smallest ratio among unflagged methods; `sigma` tagged live); one panel with 4 ECharts instances on a
  shared y max, `echarts.connect` for the synced hover, markArea for runs of hours outside, scatter for hours
  outside; the hour slider (`<input type="range">`) moves the axis pointer; tune table with kind tabs. Key picker:
  a plain `<select>` fed by `/api/eval/keys` (do not reuse `keypicker.js`, whose groups include documents).
  `baseOption()`, `chartFor()`, `disposeCharts()` from `charts.js`.
- [ ] **Step 5: Run** `.venv/bin/pytest -q tests/test_web_bands.py tests/test_web_baseline_view.py tests/test_web_monitored.py` — expect PASS; `grep -rn "eval_view\|lab_" logai2/web` prints nothing.
- [ ] **Step 6: Commit** the files above by explicit path: `git commit -m "Evaluation page: 4 band methods on read, Re-tune; lab-run page removed (eb3)"`.

Review: Sonnet.

---

### Task eb4 (tech lead): laptop proof, Chrome, deploy note

- [ ] Merge eb1-eb3 into `build/eval-bands`; full suite once.
- [ ] Rebuild `logai2:latest`, recreate engine/web/linker; Re-tune on the live DB (`recharge`, 26-30 Oct).
- [ ] Chrome: `#evaluation/recharge/*total/7`, `*error`, one busy and one sparse template, 1 day; not-tuned state
  (empty setting), Re-tune running, light and dark; screenshot each into the report.
- [ ] README "Upgrading from v15": add "then press Re-tune on Evaluation once (or `python -m logai2.bands tune`)".
- [ ] `.team/ledger.md` + `agents.md`; ask the user to confirm the master merge; after it, build v18 and
  `graphify update .`.

---

## Self-review

- Spec coverage: §3 methods → eb1; §4 scope/kinds → eb1; §5 tune + estimate check → eb2; §6 API → eb3; §7 page →
  eb3; §8 restarts → eb3 (`Retune` in-process, one transaction from eb2) and eb4 deploy note; §2 constraints →
  Global Constraints.
- Names used across tasks: `history`, `training`, `band`, `nb_quantiles`, `tail_above`, `tail_below`, `kind_of`,
  `in_scope`, `tune`, `stored_tune`, `best_k`, `bands_series`, `eval_keys`, `Retune` — each defined once above.
- The old plan `docs/plans/2026-10-29-method-bands.md` is superseded by this file.
