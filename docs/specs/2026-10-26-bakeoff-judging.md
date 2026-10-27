# Bake-off judging criteria: how a method wins, and why each rule is there

Companion to `docs/specs/2026-10-26-baseline-bakeoff.md`. The exact rules are J1–J8 in that spec;
this doc states them as criteria and gives the reason for each. The methods are in
`docs/specs/2026-10-26-bakeoff-methods.md`.

## The question the bake-off answers
For each monitor kind (M1–M7): **does any method describe normal traffic better than today's σ,
at the same false-alarm cost, without detecting less?** If yes, it becomes an adopt candidate for
that kind. If not, σ stays (possibly with a recalibrated k).

- **Company run (Test B):** measures fit on real traffic. Nothing is injected and nothing is labelled,
  so the company run judges *how well a band describes normal minutes*, not how many incidents it catches.
- **Laptop run (Tests A and C):** measures detection on planted incidents. It is a gate: a method that fits
  well but detects worse than σ cannot be adopted.

## Ground rules (apply to every criterion)

| Rule | What it says | Why |
|---|---|---|
| Same minutes | Every method is scored on one shared set of normal minutes: all minutes minus Review `exclude` hours (laptop: minus planted windows ± 15 min). | Scoring a method "outside its own alerts" would let a noisy method hide its misses. |
| Same alert rule | Every method's minute verdicts go through the live `detect.step` (2/3/3 hysteresis) to make episodes. | Differences come from the method, not from a different alarm rule. |
| Kinds fixed per run | Each key's kind (M1–M7) is assigned from the tune window (J1). INFO templates under 2/min are not scored. | A method is judged on the keys it is meant for; unused keys would only add noise. |
| Tune and test apart | Knobs are chosen on the first half of the data, scored on the second half. The test window is never used for tuning. | Otherwise a method with more knobs looks better by overfitting. |
| Reproducible | Each run stores code version, image tag, site, window, seed, knob grids and kind assignment (J8). | Two runs can be compared, and a change in the ranking can be traced to data, code or knobs. |

## Criterion 1: equal coverage (fair tuning, J4)
- **Band methods:** the knob is set so that coverage on the tune window is closest to **99.5 %**
  (the band holds 99.5 % of normal minutes).
- **Score-only methods** (CUSUM, Matrix Profile, PCA): the most sensitive knob whose alert rate stays
  within the budget: **1 episode per 100 key-days** (1 per service-week for M1).
- **σ is tuned the same way**, so "recalibrated σ (k = …)" is a valid outcome.
- **Why:** any method can look sensitive by drawing a narrow band and alarming all day, or quiet by
  drawing a wide one. Fixing the coverage (or the false-alarm budget) first means the rest of the
  comparison is about one thing: who draws the tighter band at the same cost.

## Criterion 2: fit (the ranking, J3)

| Metric | Definition | Target / use | Why |
|---|---|---|---|
| **Coverage** | share of normal minutes with `low ≤ count ≤ high` | pass if 99.0–99.9 % on the test window | checks the tune-window knob still holds on unseen days |
| **Interval score** (Winkler, α = 0.005) | mean of band width + a penalty `(2/α)` × how far a count falls outside; ÷ key mean, averaged over keys, **relative to σ = 1.00** | **the ranking metric**; lower is better | one number that rewards a narrow band and punishes misses; a band cannot win by being narrow and wrong |
| **Sharpness** | median `(high − low) / expected` | shown, not ranked | a narrow band reacts sooner; this shows how narrow |
| **MASE** | forecast error ÷ error of "same minute 7 days ago" (or 1 day) | tie-breaker | says whether the expected line beats the trivial guess |
| **Alert rate** | episodes per 100 key-days on normal minutes | gate: within budget | the false-alarm cost people actually feel |
| **Flap share** | episodes < 5 min, or starting < 30 min after the previous one on the key | gate: ≤ 10 % | flapping alerts get muted, which defeats the monitor |

## Criterion 3: detection (laptop gate, J5)
- **Test A:** spikes, drops, ramps, level shifts, bursts (M6) and mix swaps (M7) injected in memory at
  3 sizes relative to the key's own expected level; 5 passes with different seeds; ≥ 3 h apart per key.
- **Test C:** real incidents written into the laptop ES through the replayer, so they cross the whole
  pipeline. If a method's recall in Test A and Test C differs by > 10 points, the injector is fixed first.
- **Recall** = injections with an episode start within 15 min ÷ injections. **Delay** = median and p90 of
  episode start − injection start.
- **Gate:** at the alert budget, recall (medium size) is **not worse than σ by more than 5 points**.
  No laptop run of the same code version → "detect: not checked", and the method cannot be adopted.
- **Why:** the company run has no incidents to count, so fit alone could pick a smooth band that
  misses real failures. The laptop gate stops that.

## Criterion 4: confidence (J6)
- **Day-block bootstrap:** resample whole test days 1,000 times → a 90 % interval for the interval-score
  ratio, MASE and alert rate. Whole days, because minutes in one day are not independent.
- **"Fits better than σ"** = interval-score ratio **≤ 0.90** and the upper end of its 90 % interval **< 1.00**.
  A 10 % gain that might be noise is not enough to change production.
- **Ties go to the simplest:** methods whose intervals overlap the leader's are tied; the tie goes to the
  core set before the heavy set, then lower cost. A heavy model must be clearly better to earn its image and runtime.
- **Verdict strength:**

  | Strength | Data |
  |---|---|
  | exploratory | < 14 days |
  | provisional | 14 days |
  | final | ≥ 28 days and ≥ 2 test weekends |

- **Stability:** a final choice needs the same leader (or a tie that includes it) in two consecutive runs
  (~6 Nov and ~20 Nov). One good run can be luck.

## The decision per monitor kind (J7)
1. **Hard gates.** Failing any one means "not suitable":
   - a band exists (M1–M5: people need to see the expected range);
   - run cost within budget;
   - coverage 99.0–99.9 % on the test window;
   - alert rate within budget on the test window;
   - flap share ≤ 10 %;
   - laptop detect check passed.
2. **Rank** the survivors by interval score relative to σ. Ties → simpler method, then lower MASE.
3. **Service check:** the leader must not fit worse than σ on more than 20 % of services; otherwise
   the verdict is "mixed: depends on service", with the per-service table. A method that wins on
   average but breaks a fifth of services is not a safe default.
4. **Outcome:**
   - **adopt candidate:** fits better than σ (criterion 4) and passes every gate, shown with its strength;
   - **σ stays:** nothing fits better than σ, or recalibrated σ wins;
   - **mixed:** see the service check.

An adopt candidate is not a production change. Moving a method into live alerting is a separate, later spec.
