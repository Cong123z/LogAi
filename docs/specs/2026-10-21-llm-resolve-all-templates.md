# LLM resolver: every unlinked template, not only alerting or new ones

Date: 2026-10-21. Status: approved by the user, not built.

## Problem

The resolver (`logai2/resolver.py`, layers 3–4 of ARCHITECTURE §5.1) only
takes a template that is **ALERTING now or first seen in the last 24 h**
(`llm.new_hours`). A template that missed both windows is never sent to the
LLM, whatever its level. On 2026-10-21 the live database had three ERROR
templates in that state: no manual neighbour to inherit from, no document
ERROR line at the 0.85 threshold (best 0.782, 0.668, 0.574), NORMAL and older
than a day. Only a person linking them by hand would ever document them.

User ruling: the LLM call must not depend on alerting or age. All levels.

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
  too, and a template in failure back-off or of a capped service is left
  out of the query.
- **Order** (unchanged): ALERTING first, then ERROR > WARN > INFO, then
  newest `last_seen`. So an alerting template still jumps the queue, and the
  backlog is worked worst level first.
- **Limits** (unchanged): about 3 LLM calls per 30 s cycle,
  `llm.per_service_hour` 5, `llm.daily_max` 50, 30 min back-off after a
  failure, profile failover.
- **Config**: `llm.new_hours` is removed (nothing else reads it). A
  `config.yaml` that still sets it is ignored silently, as for any unknown key.

Expected effect on the 2026-10-21 data (1 service, 297 eligible: 3 ERROR,
2 WARN, 292 INFO): ERROR and WARN in the first hour; INFO at 5 per hour,
capped at 50 per day, so about 6 days, mostly as SAFE drafts in To review.
Raise `llm.daily_max` in `config.yaml` to go faster if the LLM quota allows.

## Tests (`tests/test_resolver.py`)

- New: a NORMAL INFO template first seen 10 days ago, with a vector and no
  link, is resolved (fails on the current code).
- `test_eligibility_order_and_limits`: `OLD` (3 days, not alerting) and
  `NEW_INFO` are now eligible; the assertions keep checking order (alerting
  first) and the per-service cap, which still decides who goes first.

## Docs (with the code, since ARCHITECTURE describes what is implemented)

- ARCHITECTURE §5.1 resolver paragraph and the config table (`llm.new_hours`
  row removed).
- `docs/design/per-service-baseline.md` §4a: updated now (it is the design).

## Deploy

The resolver runs in the linker: `docker compose build logai2-linker &&
docker compose up -d logai2-linker`. Web and engine are not restarted.
