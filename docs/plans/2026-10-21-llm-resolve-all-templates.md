# LLM Resolver for Every Unlinked Template — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **not built** (the gate is still in `resolver.py`; `llm.new_hours` still in `config.py`).

**Goal:** The LLM resolver takes every template that has a vector and no link row, whatever its level, alert state or age.

**Architecture:** One SQL condition removed from `Resolver.run`'s eligibility query, the config field that fed it removed, ARCHITECTURE brought in line. Order and every rate limit stay as they are.

**Tech Stack:** Python 3, psycopg 3, pytest against the test Postgres on 127.0.0.1:5433.

**Spec:** `docs/specs/2026-10-21-llm-resolve-all-templates.md`

## Global Constraints

- Work on a new branch from `master` (e.g. `llm-resolve-all`); never push; stage by explicit path; no AI attribution in commits.
- Unchanged: order ALERTING first, then ERROR > WARN > INFO, then `last_seen DESC`; `PER_CYCLE` 3; `llm.per_service_hour` 5; `llm.daily_max` 50; 30 min failure back-off; a `rejected` or manual row keeps a template out.
- `llm.new_hours` is removed, not left unused.
- Tests: `.venv/bin/pytest -q` (`docker compose up -d postgres`). After code changes `graphify update .`.

## Review Focus

1. **Backlog flood**: hundreds of old INFO templates become eligible at once; the per-service and daily caps must still hold and ERROR/WARN must still go first. (Task 1 test `test_any_age_or_state_is_resolved_worst_level_first`.)
2. **Alerting still jumps the queue** over the backlog. (Existing `test_eligibility_order_and_limits`, unchanged assertions.)
3. **Old `config.yaml` still setting `llm.new_hours`**: must load without error (unknown keys are ignored by `_merge_dataclass`). (Task 1 step 5.)
4. **Rejected templates are never resent** even though age no longer matters. (Existing `test_eligibility_order_and_limits`, `LINKED` row.)
5. **The LIMIT 50 query window**: with hundreds eligible, capped services and backed-off templates must stay excluded in SQL so they never fill the window. (Existing `test_failed_calls_count_against_the_budget_and_capped_services_do_not_block`.)

---

### Task 1: Remove the alerting / 24 h gate

**Files:**
- Modify: `logai2/resolver.py` (eligibility query in `Resolver.run`, ~lines 130–143)
- Modify: `logai2/config.py` (`LlmConfig.new_hours`, line ~127)
- Modify: `ARCHITECTURE.md` (§5.1 resolver paragraph ~line 362; config table row `llm.new_hours` ~line 188)
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `tmpl`, `vec`, `doc` from `tests.test_links`; `Chat`, `run`, `profile` fixture in `tests/test_resolver.py`.
- Produces: nothing new; `LlmConfig` loses `new_hours`.

- [ ] **Step 1: Write the failing test**

```python
def test_any_age_or_state_is_resolved_worst_level_first(conn, cfg, profile):
    cfg.llm.per_service_hour = 1
    for tid, level, n in (("OLD_INFO", "INFO", 1), ("OLD_ERROR", "ERROR", 2)):
        tmpl(conn, tid, level, v=vec((n, 1.0)))
        conn.execute("UPDATE templates SET first_seen = now() - interval '10 days' WHERE template_id = %s", (tid,))
    run(conn, cfg, Chat({"title": "t", "handling": "h"}))
    # neither alerting nor new: still resolved, ERROR before INFO under the per-service cap
    assert [r["template_id"] for r in conn.execute(
        "SELECT template_id FROM template_doc_links WHERE source = 'draft'")] == ["OLD_ERROR"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest -q tests/test_resolver.py -k any_age`
Expected: FAIL (`[] == ['OLD_ERROR']`: the gate leaves both out).

- [ ] **Step 3: Implement**

In `Resolver.run`'s query drop `AND (a.state = 'ALERTING' OR t.first_seen > now() - make_interval(hours => %s))` and its `lc.new_hours` parameter; keep the `LEFT JOIN alert_state` (the ORDER BY uses it). Delete `new_hours` from `LlmConfig`. In `test_eligibility_order_and_limits` change only the comments that give the gate as the reason (`OLD` and `NEW_INFO` now wait for the per-service cap, not the gate); its assertions stay.

- [ ] **Step 4: Run the resolver tests, then the suite**

Run: `.venv/bin/pytest -q tests/test_resolver.py` then `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Old config still loads**

Run: `f=$(mktemp --suffix .yaml) && printf 'llm:\n  new_hours: 24\n' > $f && .venv/bin/python -c "import sys; from logai2.config import load_config; print(load_config(sys.argv[1]).llm)" $f`
Expected: prints `LlmConfig(...)` without `new_hours`, no error.

- [ ] **Step 6: Docs**

ARCHITECTURE §5.1: the resolver takes templates "that have a vector and no link row at all, of any level, alert state or age, ALERTING first then ERROR > WARN > INFO …" (drop "ALERTING or first seen within `llm.new_hours` (24)"); remove the `llm.new_hours` row from the §3 config table. Mark the spec's Status line "built".

- [ ] **Step 7: Commit**

```bash
git add logai2/resolver.py logai2/config.py tests/test_resolver.py ARCHITECTURE.md docs/specs/2026-10-21-llm-resolve-all-templates.md
git commit -m "Resolver: every unlinked template goes to the LLM, any level, state or age; drop llm.new_hours"
```

### Task 2: Merge and deploy the linker

- [ ] **Step 1:** Final review, merge `--no-ff` into `master`, `.venv/bin/pytest -q` on the merged result.
- [ ] **Step 2:** `docker compose build logai2-linker && docker compose up -d logai2-linker` (web and engine are not restarted).
- [ ] **Step 3:** Check `worker_status['linker']` within a few cycles: `status->'llm'` counts rising, no `llm_last_error`; the 3 ERROR templates (`recharge:T00265`, `T00141`, `T00304`) get an `llm` or `draft` row first; they appear in the Templates page's To review.
- [ ] **Step 4:** `agents.md` entry; `graphify update .`.
