# LLM resolver: every unlinked template, not only alerting or new ones

Date: 2026-10-21, amended 2026-10-22 (no rate limits by default). Status:
approved by the user, built (merge 623e888).

## Problem

The resolver (`logai2/resolver.py`, layers 3–4 of ARCHITECTURE §5.1) only
takes a template that is **ALERTING now or first seen in the last 24 h**
(`llm.new_hours`). A template that missed both windows is never sent to the
LLM, whatever its level. On 2026-10-21 the live database had three ERROR
templates in that state: no manual neighbour to inherit from, no document
ERROR line at the 0.85 threshold (best 0.782, 0.668, 0.574), NORMAL and older
than a day. Only a person linking them by hand would ever document them.

User ruling: the LLM call must not depend on alerting or age. All levels.

Amendment (user, 2026-10-22): no call limits either. INFO templates go too;
the LLM itself decides `verdict` (action / safe) as the current prompt
already asks, so most INFO lines become SAFE drafts, and an INFO line that
reports a failure can still be ACTION. The verdict is not forced by level.

## How a template that was never seen before is handled (for context)

1. **Reader**: the line is normalized (ids, numbers, timestamps → `<*>`)
   and looked up in the known-line cache; a never-seen line is a miss.
2. **Owner / Drain3**: the miss goes to its service's Drain3 tree; with no
   cluster at similarity 0.6 a new cluster is created (`recharge:T00318`)
   and the cache learns it.
3. **Counts**: counted per minute from its first minute; its `templates` row
   (text, level, `first_seen`) is written at the next flush (≤ 2 s).
4. **Detection without history**: floors only (ERROR ≥ 5/min, WARN
   ≥ 10/min, INFO never an upper alert); WARMING after 2 high minutes,
   ALERTING after 3 more.
5. **Linker** (≤ 30 s): embedded (alerting and newest first), then layer 1
   (inherit from a manually linked similar template) and layer 2 (a
   document's ERROR line).
6. **LLM** (this change): still unlinked → sent to the LLM, which confirms
   one of the 3 nearest documents or writes a draft; both wait in the
   Templates page's To review.
7. **Baseline**: the 02:00 commit gives it mean ± σ per hour; young band
   (k 4) until 5 weekdays / 4 weekend days are committed, then k 3.

## Change

- **Eligible**: every template that has a vector and **no**
  `template_doc_links` row, any level, any alert state, any age. Unchanged:
  a `rejected` row keeps a template out for good, a manual "no document" row
  too, and a template in failure back-off (or of a capped service, when a
  cap is set) is left out of the query.
- **Order** (unchanged): ALERTING first, then ERROR > WARN > INFO, then
  newest `last_seen`. So an alerting template still jumps the queue, and the
  backlog is worked worst level first.
- **Limits** (amended): `llm.daily_max` and `llm.per_service_hour` default
  to **0 = no limit**. A positive value in `config.yaml` still caps exactly
  as before (the safety valve for a paid or rate-limited provider). Kept:
  `PER_CYCLE` 3 calls per linker cycle (not a budget: the LLM runs inside
  the linker cycle, and an unbounded cycle would stop embedding, layers 1–2
  and the alerting-first order for hours), 30 min back-off after a failure,
  profile failover (a 429 or other HTTP error skips the profile 5 min).
  `llm_today` is still counted and reported.
- **Config**: `llm.new_hours` is removed (nothing else reads it). A
  `config.yaml` that still sets it is ignored silently, as for any unknown key.
- **Web**: with a limit of 0 the Pipeline LLM node reads "N in 24 h" (not
  "N / 0"), its detail "N LLM calls counted in the last 24 h", and the LLM
  page cards "no limit" instead of "limit 0".

Expected effect on the 2026-10-22 data (1 service, 297 unlinked, 0 eligible
today): all 297 become eligible; ERROR and WARN in the first cycles, then
INFO, at about 3 calls per cycle (cycle = 30 s sleep + the calls, 6–60 s
each), so roughly 100–300 per hour and the backlog done in 1–3 h, mostly as
SAFE drafts in To review. Known and accepted: similar templates each get
their own draft (no grouping before the call); approving one lets layer 1
link its neighbours and their unused drafts become `rejected`.

## Tests (`tests/test_resolver.py`)

- New: a NORMAL INFO template first seen 10 days ago, with a vector and no
  link, is resolved (fails on the current code).
- `test_eligibility_order_and_limits`: `OLD` (3 days, not alerting) and
  `NEW_INFO` are now eligible; the assertions keep checking order (alerting
  first) and the per-service cap, which still decides who goes first.
- New: with both limits 0, every eligible template up to `PER_CYCLE` is
  called in one cycle and no "daily limit" warning is raised.
- New: a positive `llm.daily_max` still stops the cycle and warns.
- `tests/test_web_llm.py`, `tests/test_web_pipeline_view.py`: the default
  limits reported by the API are 0.

## Docs (with the code, since ARCHITECTURE describes what is implemented)

- ARCHITECTURE §3 config table (`llm.new_hours` row removed, limits 0 = no
  limit), §5.1 resolver paragraph, §10.5 `llm_today` line.
- README (the LLM gate sentence), `docs/WEB_UI.md` (Pipeline LLM node, LLM
  usage `limits`), `docs/design/per-service-baseline.md` §4a.

## Deploy

The resolver runs in the linker, the "no limit" labels in the web:
`docker compose build logai2-linker logai2-web && docker compose up -d
logai2-linker logai2-web`. The engine is not restarted.
