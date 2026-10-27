# Task bk11: Lab image and heavy methods (#8 ETS, #9 Prophet, #10 LightGBM, #11 Chronos-Bolt, #12 Matrix Profile)

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk11-heavy-methods (worktree ../logai2-task-bk11, created by the tech lead)
PR file: .team/prs/task-bk11-heavy-methods.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Five methods need packages the app image must not carry (spec §1 invariant 3). They live in a separate lab image that
works with no network (the company cluster has none): packages and the Chronos weights are baked in at build time.

## Read first
- Methods: `docs/specs/2026-10-26-bakeoff-methods.md` (#8–#12). Spec `docs/specs/2026-10-27-bakeoff-laptop.md` §2
  (laptop RAM: the Docker VM has ~7.75 GB; bge-m3 is off), §6 (contract, D4, NaN bands), §6.5, §13 (targets).
- Code: `scripts/lab/methods/__init__.py` (`Spec`, `DayOut`, `apply_d4`, `Unavailable`, registry, `GROUPS["heavy"]`),
  `scripts/lab/methods/sigma.py` (`rebuild_stats`: σ's profile, used here for the within-hour shape),
  `scripts/lab/run.py` (`--check`, `main`), `scripts/lab/data.py`, `Dockerfile` (the app image: base, user, paths),
  `requirements.txt`, `docker-compose.yml`.

## Files
- Create: `requirements-lab.txt`, `Dockerfile.lab`, `scripts/lab/methods/ets.py`, `scripts/lab/methods/prophet_m.py`,
  `scripts/lab/methods/lgbm.py`, `scripts/lab/methods/chronos_m.py`, `scripts/lab/methods/mprofile.py`,
  `scripts/lab/methods/heavy.py` (shared hourly→minute helpers), `tests/lab/test_lab_heavy.py`
- Modify: `scripts/lab/methods/__init__.py` (register the five in `METHODS` and `GROUPS["heavy"]`, lazy factories only),
  `README.md` (build + check commands, single lines)
Touching any other file needs a QUESTION first. Never change `requirements.txt` or `Dockerfile`.

## Requirements
1. **Lazy imports**: every heavy package is imported inside the method's factory or `fit`, never at module import;
   a missing package raises `Unavailable("<package> not installed")` from the factory. `--methods core` and the app
   test suite never import them.
2. **Hourly → minute** (`heavy.py`): methods #8–#11 forecast per hour (mean count per minute in that hour and
   quantiles). Per minute: expected = hourly value × the key's within-hour shape (from σ's profile: per-minute mean of
   that hour's history is flat, so use shape 1.0 unless per-minute history exists: then the mean per slot of the last
   7 days for that hour, normalized to mean 1); low/high scaled the same way; flags via `apply_d4`; NaN band when the
   method has no forecast.
3. **#8 `ets`** — kinds M1–M3, knobs = prediction-interval coverage (0.98, 0.99, 0.995, 0.999, 0.9999), default 0.995,
   cost 3, `min_hour_days` 14. statsmodels `ExponentialSmoothing` (additive, no trend; seasonal 24, or 168 when ≥ 21
   days); refit per fit day on the hourly series before D; interval from the residual std (normal) per knob.
4. **#9 `prophet`** — kinds M1, M2, M5, knobs as #8, cost 5, `min_hour_days` 14. Daily + weekly seasonality, holidays
   from `scripts/lab/holidays.py` if present on master when you start (else `cfg.lab.holidays` directly);
   `interval_width` = knob; refit per fit day; silence its logging.
5. **#10 `lgbm`** — kinds M1–M6, knobs = quantile tail (0.01, 0.005, 0.0025, 0.001, 0.0005), default 0.0025, cost 4,
   `min_hour_days` 14. One model per fit day per service over all keys' hourly rows: features hour, weekday, holiday,
   lag 1 h, 24 h, 168 h (missing → NaN), log of the key's mean, kind code; objective quantile at q and 1 − q per knob
   (train once per knob pair; keep it small: `n_estimators` ≤ 200, `num_leaves` ≤ 31); a key with no history (M6)
   gets lags NaN and still a forecast.
6. **#11 `chronos`** — kinds M1, M2, M6 (only a seeded 20 % sample of M6 keys per service), knobs as #10, cost 6.
   `chronos-forecasting` Bolt-small from `/models/chronos-bolt-small` (env `LOGAI2_CHRONOS_PATH`); context = last
   7 days hourly, horizon 24; quantiles from its output (interpolate the requested tails); CPU only, `torch.set_num_threads(4)`.
7. **#12 `mprofile`** — kinds M1, M3 (top 200 M3 keys per service by mean), score method (no band), knobs = distance
   threshold in z-normalized units (2, 3, 4, 5, 6), default 4, cost 5, `min_minute_days` 7. `stumpy.stump` with window
   60 on the last 7 days + the scored day's per-minute series (unobserved → linear fill, then excluded from flags);
   score per minute = matrix-profile distance of the window ending at that minute; high via `apply_d4(score=...)`.
8. **`requirements-lab.txt`** pins exact versions (statsmodels, prophet, lightgbm, stumpy, torch CPU wheel,
   chronos-forecasting, numpy, pandas) compatible with Python 3.11.
9. **`Dockerfile.lab`**: `FROM logai2:${TAG:-latest}` (the app image built locally from `Dockerfile`), install
   `requirements-lab.txt` (torch from the CPU index), download Chronos-Bolt-small weights **at build time** into
   `/models/chronos-bolt-small`, `ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 LOGAI2_CHRONOS_PATH=/models/chronos-bolt-small`.
10. **Build and check (real, on the laptop)**: `docker build -t logai2:latest .` if no `logai2` image exists, then
    `docker build -f Dockerfile.lab -t logai2-lab:latest .`, then
    `docker run --rm --network none logai2-lab:latest python -m scripts.lab.run --site laptop --test B --methods heavy --days 1 --check`
    → every heavy method `available`. Report image size and build time. You may build images (this task allows it);
    do not start, stop or recreate the running containers.

## Tests (write first, see them fail) — `tests/lab/test_lab_heavy.py`
- In the app venv (no packages): each heavy factory raises `Unavailable`; `--methods heavy --check` lists all five
  unavailable and exits 1; `--methods core` imports no heavy package (`sys.modules` check).
- `heavy.py` hourly→minute scaling and NaN handling (pure Python).
- Per method, `pytest.importorskip(<package>)`: a 3-week synthetic sine (24 h) + Poisson series → band covers ≥ 98 % of
  clean minutes of a held-out day at the default knob; a spike ×3 for 30 min on an ERROR key is flagged on ≥ 20 minutes
  (band methods) / score above threshold (mprofile); no look-ahead (data on D or later does not change the fit).
  Run these inside the lab image once:
  `docker run --rm --network none -v $PWD:/app/src -w /app/src logai2-lab:latest python -m pytest -q tests/lab/test_lab_heavy.py`
  (adapt the mount to the image layout) and paste the output in the PR file. Do NOT run the full suite in the image.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes in the app venv, output clean (heavy tests skipped there)
- [ ] the lab image builds, `--check` with `--network none` lists all five available, heavy tests pass in the image
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in (image size, build time, check output)
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
