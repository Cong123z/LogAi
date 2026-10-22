# LLM Resolver for Every Unlinked Template, No Call Limits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **not built**. Rewritten 2026-10-22 for the spec's amendment (limits default to 0 = none); the 2026-10-21 version (gate only) was never started.

**Goal:** The LLM resolver takes every template that has a vector and no link row, whatever its level, alert state or age, with no daily or per-service cap unless one is configured.

**Architecture:** One SQL condition removed from `Resolver.run`'s eligibility query and `llm.new_hours` deleted; `llm.daily_max` / `llm.per_service_hour` default to 0, which `Resolver.run` reads as "no cap"; `PER_CYCLE` keeps one linker cycle short. The web shows "no limit" instead of "0".

**Tech Stack:** Python 3, psycopg 3, pytest against the test Postgres on 127.0.0.1:5433; vanilla JS (no JS tests in this repo).

**Spec:** `docs/specs/2026-10-21-llm-resolve-all-templates.md` (amended 2026-10-22)

## Global Constraints

- Work on a new branch `llm-resolve-all` from `master`; never push; stage by explicit path; no `Co-Authored-By` / AI attribution in commits.
- Unchanged: order ALERTING first, then ERROR > WARN > INFO, then `last_seen DESC`; `PER_CYCLE = 3`; 30 min failure back-off; profile failover; a `rejected` or manual row (also "no document", `doc_id NULL`) keeps a template out; the SUGGEST prompt and its verdict rule (the LLM decides action / safe; not forced by level).
- `llm.new_hours` is removed, not left unused. `llm.daily_max` and `llm.per_service_hour` stay as knobs, default `0` = no limit; a positive value caps exactly as today.
- Tests: `.venv/bin/pytest -q` (`docker compose up -d postgres`).
- After the merge, in `~/Documents/logai2`: `graphify update .`, an `agents.md` entry.

## Review Focus

1. **A cycle with no caps must stay short**: with 300 eligible templates and limits 0, one `run()` makes at most `PER_CYCLE` calls (a template's second call may add one), so embedding, layers 1–2 and the alerting-first order keep running every cycle. (Task 2 `test_zero_limits_mean_no_cap_but_one_cycle_stays_bounded`.)
2. **Alerting still jumps the backlog** of hundreds of old INFO templates. (Existing `test_eligibility_order_and_limits`, assertions unchanged, Task 1.)
3. **An existing `config.yaml`** that still sets `llm.new_hours`, or sets `daily_max: 50`, must load, and a positive cap must still hold. (Task 1 Step 5; Task 2 `test_a_positive_daily_max_still_stops_and_warns`.)
4. **Rejected and manual "no document" templates are never resent**, now that age no longer filters them. (Task 1 test, `NODOC` assertion; existing `LINKED` row.)
5. **The web must not read "N / 0 in 24 h" or "limit 0"**, which looks like "nothing allowed". (Task 3 Step 3.)

---

### Task 1: Remove the alerting / 24 h gate

**Files:**
- Modify: `logai2/resolver.py` (eligibility query in `Resolver.run`, ~lines 130–143)
- Modify: `logai2/config.py` (`LlmConfig.new_hours`, line 127)
- Modify: `ARCHITECTURE.md` (§3 config row `llm.new_hours` line 189; §5.1 resolver paragraph lines 364–366)
- Modify: `README.md` line 45; `docs/design/per-service-baseline.md` §4a (~line 315)
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `tmpl`, `vec` from `tests.test_links`; `Chat`, `run`, `profile` in `tests/test_resolver.py`.
- Produces: `LlmConfig` without `new_hours`; nothing else changes shape.

- [ ] **Step 1: Write the failing test**

```python
def test_any_age_or_state_is_resolved_worst_level_first(conn, cfg, profile):
    cfg.llm.per_service_hour = 1
    for tid, level, n in (("OLD_INFO", "INFO", 1), ("OLD_ERROR", "ERROR", 2), ("NODOC", "ERROR", 3)):
        tmpl(conn, tid, level, v=vec((n, 1.0)))
        conn.execute("UPDATE templates SET first_seen = now() - interval '10 days' WHERE template_id = %s", (tid,))
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                 "VALUES ('s', 'NODOC', NULL, 'manual')")     # a person said "no document": never sent
    chat = Chat({"title": "t", "handling": "h"})
    run(conn, cfg, chat)
    # neither alerting nor new: still resolved, ERROR before INFO under the per-service cap
    assert [r["template_id"] for r in conn.execute(
        "SELECT template_id FROM template_doc_links WHERE source = 'draft'")] == ["OLD_ERROR"]
    assert [s["template"]["text"] for s in chat.seen] == ["text OLD_ERROR"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest -q tests/test_resolver.py -k any_age`
Expected: FAIL (`[] == ['OLD_ERROR']`: the gate leaves every template out).

- [ ] **Step 3: Implement**

In `Resolver.run`'s query drop `AND (a.state = 'ALERTING' OR t.first_seen > now() - make_interval(hours => %s))` and its `lc.new_hours` argument; keep the `LEFT JOIN alert_state` (the `ORDER BY` uses it). Delete `new_hours` from `LlmConfig`. In `test_eligibility_order_and_limits` change only the comments that give the gate as the reason (`OLD` and `NEW_INFO` now wait for the per-service cap); its assertions stay.

- [ ] **Step 4: Run the resolver tests, then the suite**

Run: `.venv/bin/pytest -q tests/test_resolver.py` then `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: An old config still loads**

Run: `f=$(mktemp --suffix .yaml) && printf 'llm:\n  new_hours: 24\n  daily_max: 50\n' > $f && .venv/bin/python -c "import sys; from logai2.config import load_config; print(load_config(sys.argv[1]).llm)" $f`
Expected: prints `LlmConfig(...)` with `daily_max=50` and no `new_hours`, no error.

- [ ] **Step 6: Docs**

ARCHITECTURE §5.1: the resolver takes templates "that have a vector and no link row at all, of any level, alert state or age, ALERTING first then ERROR > WARN > INFO …" (drop "that are ALERTING or first seen within `llm.new_hours` (24)"); delete the §3 row `llm.new_hours`. README line 45: unmatched templates "go to the LLM" (drop "if ALERTING or first seen in the last 24 h"). Design doc §4a: the same sentence change.

- [ ] **Step 7: Commit**

```bash
git add logai2/resolver.py logai2/config.py tests/test_resolver.py ARCHITECTURE.md README.md docs/design/per-service-baseline.md
git commit -m "Resolver: every unlinked template goes to the LLM, any level, state or age; drop llm.new_hours"
```

### Task 2: Limits default to 0 = no limit

**Files:**
- Modify: `logai2/resolver.py` (`Resolver.run`: `capped`, the loop's two checks, the daily-limit warning, ~lines 125–176)
- Modify: `logai2/config.py` (`LlmConfig.per_service_hour`, `daily_max`, lines 125–126)
- Modify: `tests/test_web_llm.py:70`, `tests/test_web_pipeline_view.py:100` (default now 0)
- Modify: `ARCHITECTURE.md` (§3 row line 188; §5.1 lines 367–368; §10.5 line 726)
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: Task 1's query (no gate).
- Produces: `LlmConfig.per_service_hour: int = 0`, `LlmConfig.daily_max: int = 0`, comment `# 0 = no limit`. `status["llm_today"]` is still set every run; the warning `"LLM daily limit reached (N)"` only when `daily_max > 0`. The web API keeps returning both values unchanged (`limits`, `llm_daily_max`), now 0 by default.

- [ ] **Step 1: Write the failing tests**

```python
def test_zero_limits_mean_no_cap_but_one_cycle_stays_bounded(conn, cfg, profile):
    cfg.llm.daily_max = cfg.llm.per_service_hour = 0
    for n in range(5):
        tmpl(conn, f"T{n}", "INFO", v=vec((n, 1.0)))
    status = run(conn, cfg, Chat(*[{"title": "t", "handling": "h"}] * 3))
    assert status["llm"]["suggested"] == 3          # PER_CYCLE bounds the cycle, not a budget
    assert status["llm_today"] == 3
    assert not [w for w in status["warnings"] if "daily limit" in w]


def test_a_positive_daily_max_still_stops_and_warns(conn, cfg, profile):
    cfg.llm.daily_max, cfg.llm.per_service_hour = 2, 0
    for n in range(3):
        tmpl(conn, f"T{n}", v=vec((n, 1.0)))
    status = run(conn, cfg, Chat(*[{"title": "t", "handling": "h"}] * 2))
    assert status["llm"]["suggested"] == 2
    assert "LLM daily limit reached (2)" in status["warnings"]
```

In `tests/test_web_llm.py:70` change `u["limits"]["daily_max"] == 50` to `== 0`; in `tests/test_web_pipeline_view.py:100` change `== 50` to `== 0`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_resolver.py -k "zero_limits or positive_daily" tests/test_web_llm.py tests/test_web_pipeline_view.py`
Expected: both resolver tests FAIL (`0 == 3`, `0 == 2`: today a 0 limit blocks every call, `day >= 0` and `per_service >= 0` are always true); the two web tests FAIL (`50 == 0`).

- [ ] **Step 3: Implement**

In `Resolver.run`, a limit applies only when it is > 0: `capped` is empty when `lc.per_service_hour <= 0`; the loop's `break` on `day >= lc.daily_max` and its per-service `continue` are skipped for a 0 limit; the `"LLM daily limit reached"` warning only when `lc.daily_max > 0`. Counting (`day`, `per_service`, `_spent`, `llm_today`) is unchanged. In `LlmConfig` set both defaults to `0` with `# 0 = no limit`. Leave `PER_CYCLE` and its comment alone.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (the existing tests set positive caps explicitly, so they still cap).

- [ ] **Step 5: Docs**

ARCHITECTURE §3 row: `llm.per_service_hour` / `daily_max` | `0 / 0` | "LLM results per service per hour, in all per day; 0 = no limit". §5.1: "about 3 LLM calls per cycle (…), and, when set above 0, `llm.per_service_hour` per service and `llm.daily_max` in all (both 0 = no limit by default)". §10.5 line 726: `llm_today` is "the rolling 24 h count (checked against `llm.daily_max` when it is above 0)".

- [ ] **Step 6: Commit**

```bash
git add logai2/resolver.py logai2/config.py tests/test_resolver.py tests/test_web_llm.py tests/test_web_pipeline_view.py ARCHITECTURE.md
git commit -m "Resolver: llm.daily_max and per_service_hour default to 0 = no limit; PER_CYCLE still bounds a cycle"
```

### Task 3: Web shows "no limit"

**Files:**
- Modify: `logai2/web/static/js/views/pipeline.js` lines 84 and 186
- Modify: `logai2/web/static/js/views/llm.js` lines 42 and 44
- Modify: `docs/WEB_UI.md` lines 102 and 594–595

**Interfaces:**
- Consumes: Task 2's API values (`d.llm_daily_max`, `usage.limits.per_service_hour`, `usage.limits.daily_max`), 0 meaning no limit.
- Produces: nothing.

- [ ] **Step 1: Implement the copy**

| Place | Limit > 0 (unchanged) | Limit 0 |
|---|---|---|
| `pipeline.js:84` node headline | `N / M in 24 h` | `N in 24 h` |
| `pipeline.js:186` LLM detail | `N of M LLM calls counted in the last 24 h (rolling; …)` | `N LLM calls in the last 24 h (rolling; …), no daily limit` |
| `llm.js:42` Last hour card | `limit M per service` | `no limit` |
| `llm.js:44` Last 24 h card | `… · limit M` | `… · no limit` |

- [ ] **Step 2: Syntax check**

Run: `node --check logai2/web/static/js/views/pipeline.js && node --check logai2/web/static/js/views/llm.js`
Expected: no output.

- [ ] **Step 3: See it in the app**

Run the branch's web on the host against the live database, which it only reads here (`LOGAI2_DATABASE_DSN=postgresql://logai:logai@localhost:5433/logai LOGAI2_WEB_PORT=5555 .venv/bin/python scripts/run_web.py`), open `/#pipeline` (click the LLM resolver node) and `/#llm`.
Expected: the table's "Limit 0" wording everywhere; no "/ 0" and no "limit 0".

- [ ] **Step 4: Docs**

`docs/WEB_UI.md:102`: "N / M in 24 h" … "or "N in 24 h" when `llm.daily_max` is 0 (no limit)". Lines 594–595: `limits` = `llm.per_service_hour`, `llm.daily_max` (0 = no limit, shown as "no limit").

- [ ] **Step 5: Commit**

```bash
git add logai2/web/static/js/views/pipeline.js logai2/web/static/js/views/llm.js docs/WEB_UI.md
git commit -m "Console: LLM limits of 0 read as no limit"
```

### Task 4: Merge, deploy, watch the backlog

- [ ] **Step 1:** Final review of the branch; `git merge --no-ff llm-resolve-all` into `master`; `.venv/bin/pytest -q` on the merged result; mark the spec's Status line "built (<merge sha>)".
- [ ] **Step 2:** `docker compose build logai2-linker logai2-web && docker compose up -d logai2-linker logai2-web` (the engine is not restarted).
- [ ] **Step 3: Watch the first cycles** on the live database (`docker exec logai2-postgres-1 psql -U logai -d logai -Atc "<sql>"`):
  - `SELECT now() - updated_at, status->'llm', status->>'llm_today', status->>'llm_last_error' FROM worker_status WHERE worker = 'linker'`. Expected: age under 90 s at every check (the linker never looks stale), `llm_today` rising by about 3 per cycle, no `llm_last_error`.
  - `SELECT t.level, l.source, count(*) FROM template_doc_links l JOIN templates t USING (service, template_id) WHERE l.source IN ('llm', 'draft') GROUP BY 1, 2`. Expected: the ERROR and WARN templates first, then INFO.
  - `SELECT verdict, count(*) FROM documents WHERE status = 'draft' GROUP BY 1`. Expected: mostly `safe` as INFO drafts arrive.
  - Pipeline page: LLM node "N in 24 h"; LLM page: "no limit".
- [ ] **Step 4:** After roughly 1–3 h: `SELECT count(*) FROM templates t WHERE embedding IS NOT NULL AND NOT EXISTS (SELECT 1 FROM template_doc_links l WHERE l.service = t.service AND l.template_id = t.template_id)` returns 0, except templates in failure back-off.
- [ ] **Step 5:** `agents.md` entry; `graphify update .` in `~/Documents/logai2`.
