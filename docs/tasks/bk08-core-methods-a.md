# Task bk08: Core methods A (#2 NB, #3 clipped NB, #4 MAD, #7 weekly + holidays)

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk08-core-methods-a (worktree ../logai2-task-bk08, created by the tech lead)
PR file: .team/prs/task-bk08-core-methods-a.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Four pure-Python methods that compete with σ, each aimed at a known σ weakness (spec methods doc): count tails (#2),
past incidents widening the band (#3), bursty per-minute data (#4), the weekly shape and Vietnamese holidays (#7).

## Read first
- Methods: `docs/specs/2026-10-26-bakeoff-methods.md` (#2, #3, #4, #7). Spec `docs/specs/2026-10-27-bakeoff-laptop.md`
  §6 (contract, D4 incl. the ruling: keys without a floor are never high upward), §6.5.
- Code: `scripts/lab/methods/__init__.py` (`Spec`, `DayOut`, `apply_d4`, `floor_for`, registry),
  `scripts/lab/methods/sigma.py` (`rebuild_stats`: reuse it for σ's days and profile; how `day()` handles the
  02:00 switch, NaN bands, unobserved minutes, view-only keys), `scripts/lab/data.py` (`ServiceData`, `level`),
  `logai2/config.py` (`LabConfig.holidays`: YAML gives `datetime.date` values), `tests/lab/test_lab_sigma.py`.

## Files
- Create: `scripts/lab/methods/nb.py`, `scripts/lab/methods/mad.py`, `scripts/lab/methods/weekly.py`,
  `scripts/lab/holidays.py`, `tests/lab/test_lab_methods.py`
- Modify: `scripts/lab/methods/__init__.py` (register in `METHODS` and `GROUPS["core"]` only)
Touching any other file needs a QUESTION first. Pure Python (`math`, `array`, `statistics`); no numpy.

## Requirements (all: `fit` sees only data before the day; `day()` returns every knob; flags via `apply_d4`;
NaN band where none; unobserved minutes: NaN band, score 0, flag 0; `*warn`/`*info` never flagged; the 02:00 switch
uses `prev_model` like σ for methods whose profile is rebuilt daily)
1. **#2 `nb`** — kinds M1–M6, knobs tail p ∈ (1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 1e-5), default 1e-3, cost 1, band.
   Profile = σ's (`rebuild_stats` stats: mean μ, std s → var v = s²) for (key, daytype, hour) and the next hour as σ
   uses both. Per minute: NB with mean μ and dispersion r = μ² / (v − μ) when v > μ, else Poisson(μ); `high` = the
   smallest c with P(X > c) ≤ p, `low` = the largest c with P(X < c) ≤ p (0 when none); expected = μ. Quantiles in
   pure Python via the pmf recursion, cached per (μ, v, p) rounded to 1e-6; stop at μ + 50·sqrt(v) + 50.
   No stats for the key → NaN band (floor-only behaviour is σ's, not NB's).
2. **#3 `nb_clip`** — as #2 (same kinds/knobs, cost 2), but the profile is rebuilt from per-minute counts with
   outliers clipped: for each picked (day, hour) of σ's rule, minutes above the NB(1e-4) high of a first fit are
   replaced by that high before computing μ and v. Uses `sd.minutes` for the per-minute values (hourly sums cannot be
   clipped); hours without per-minute data fall back to the unclipped hourly stats.
3. **#4 `mad`** — kinds M3, M4 (+M6), knobs k ∈ (3, 4, 5, 6, 8, 10), default 5, cost 1, band, `min_minute_days` 7.
   Per (key, hour of day): median m and MAD of the observed per-minute counts of the last 7 days before D;
   band = m ± k·1.4826·max(MAD, 1); expected = m; low ≥ 0. Fewer than 7 days with data → `fit` returns None.
4. **#7 `weekly`** — kinds M1, M2, knobs k ∈ (2.0, 2.5, 3.0, 3.5, 4.0, 5.0), default 3.0, cost 1, band,
   `min_hour_days` 14. Profile per (key, weekday 0–6, hour) = mean and std per minute from `sd.hourly`/`sd.n_minutes`
   over the last 2–4 weeks before D (up to 4 same-weekday days; at least 2), excluding holidays and Review-excluded
   hours; holiday days (`holidays.is_holiday`) use a separate "holiday" profile built from past holidays if ≥ 1 exists,
   else the Sunday profile. Band = mean ± k·std; expected = mean. Fewer than 14 days of hourly history → None.
5. `scripts/lab/holidays.py`: `is_holiday(day, cfg, extra=()) -> bool` from `cfg.lab.holidays` (dates or ISO strings)
   plus `extra` (a scenario's holiday days, later).
6. Register `nb`, `nb_clip`, `mad`, `weekly` in `METHODS` and `GROUPS["core"]`.

## Tests (write first, see them fail) — `tests/lab/test_lab_methods.py`, hand-made `ServiceData`, no DB
For each method:
- clean Poisson(5) data (seeded) for its history → on a clean test day, flagged minutes ≤ 1 % at the default knob
  (for an ERROR-level key, so floors apply);
- a spike ×3 for 10 min (ERROR key, mean ≥ 5) is flagged on ≥ 8 of those minutes;
- `*total` drop to 0.3× for 30 min with mean ≥ 20 is flagged by #2/#3/#7 (drop rule);
- not enough history → `fit` returns None.
Specific:
- NB quantiles equal a brute-force cumulative sum for 5 (μ, v, p) cases; Poisson fallback when v ≤ μ.
- #3: a past incident hour in the history narrows #3's band vs #2's.
- #4: one huge minute in the history moves σ's mean but not MAD's median.
- #7: Monday and Thursday profiles differ when the history differs; a configured holiday with a 0.4× day stays
  unflagged by #7 while #2 flags the drop; `is_holiday` accepts date and ISO string.
- No look-ahead: changing data on day D or later does not change `fit(sd, D)`.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
