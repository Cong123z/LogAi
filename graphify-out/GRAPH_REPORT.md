# Graph Report - logai2  (2026-10-30)

## Corpus Check
- 278 files · ~361,788 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 7 file(s) not represented in the graph (top: (none) 4, .lab 1, .css 1)

## Summary
- 5018 nodes · 14990 edges · 171 communities (163 shown, 8 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 2565 edges (avg confidence: 0.91)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `68469b00`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- tmpl
- FakeSource
- embed
- logai2 Architecture
- create_app
- Engine
- test_web_alerts_templates.py
- templates.js
- incident_test.py
- PR: task/<id>-<slug>
- documentation.js
- test_es_source.py
- Miners
- sources.js
- a
- echarts.min.js
- DayOut
- Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture
- TestUniversalMaskingRules
- json
- normalize
- test_owner.py
- review.js
- test_links.py
- Task pm6: Memory proof and docs
- periods
- config.py
- Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance
- module_alerts
- record
- HTTP API
- keypicker.js
- link_eval.py
- ui.js
- alerting.js
- test_web_docs_baseline.py
- logai2 core engine + web v1: design
- Web UI and HTTP API
- Baseline
- Owner
- bands.py
- pipeline.js
- test_web_bands.py
- retrain.js
- links.py
- llm.js
- Incident test, 2026-10-20: smoke test
- datetime
- test_lab_run.py
- Resolver
- test_bands.py
- 3B. Baseline page (`#baseline`, charts only)
- hit
- logai2 — instructions for Claude sessions
- test_web_baseline_view.py
- agy-task.sh
- U
- test_reread.py
- test_lab_score.py
- ensure_partitions
- l
- app.py
- .note
- Retrain Implementation Plan
- test_web_pipeline_view.py
- es_source.py
- test_web_retrain.py
- i
- ed
- test_lab_inject.py
- to
- test_lab_sigma.py
- modules
- E
- stress_modules.py
- tail_above
- fv
- Document links smoke test on real data (2026-10-21)
- test_web_llm.py
- Per-service templates, statistical baselines, document-centric alerts
- commit
- A
- Lane
- pytest
- test_lab_synth.py
- Band
- loadData
- stress_pipeline.py
- Rw
- Results
- test_web_modules.py
- test_owner_selection.py
- decide.py
- score.py
- test_bands_tune.py
- test_web_links.py
- 10. Engine loop
- mem_probe.py
- apply_d4
- finish_all
- Task bk10: Test A (in-memory injections), recall and delay, M6/M7 decisions
- synth.py
- Task pm2: On/Off switch per pattern (selection API, reading, Data sources UI)
- T
- evaluation.js
- GenSource
- minute_at
- main.js
- 4. Pipeline page
- 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)
- LLM resolver: every unlinked template, not only alerting or new ones
- Retrain: baseline rebuilds, re-reads from ES, schedule and history
- OpenMinutes
- Two deployments: laptop and company (same code, same images, same page)
- logai2 Core Engine + Web v1 Implementation Plan
- Task 53: Nav — Baseline and Review move from Settings to Catalog
- alert_rows
- Y
- Task 52: `templates.first_seen` moves earlier; `days_back` doc
- Task 55: Retrain schedule setting (on/off, time, week days)
- Fixed
- Task 5: Review page UI (decisions)
- Tracker
- The methods
- random
- 54 Migration 008 + Rebuild recorded as runs
- Pattern On/Off switch and engine memory: implementation plan
- Baseline Bake-off on the laptop: implementation plan (tasks 0–17)
- test_web_sources.py
- Judging protocol (precise rules, the same for every method)
- Baseline bake-off on the laptop: detailed spec
- Bake-off lab: laptop proof (task bk17)
- Retune
- PcaModel
- detect.py
- Baseline bake-off: test alerting methods on real data, judge them, choose per monitor kind
- Memory probe: before (master e8ec04b) and after (build/pattern-switch)
- .status
- Task 6: Engine reports events per minute per service
- Baseline Bake-off Implementation Plan
- Task bk14: Evaluation API
- Task bk07: Tuning, sweeps, bootstrap, decision
- Task eb5: zoom on the Evaluation charts
- ServiceData
- Bake-off judging criteria: how a method wins, and why each rule is there
- commands.py
- Task bk16: Compose lab service, run commands, docs, deferred fixes
- logai2
- Evaluation page (`/#evaluation`): see every method's line, choose, calibrate
- Re-read lane: truth and stress proof (2026-10-26)
- 3. Review page (`#review`, decisions)
- Task bk15: Evaluation page
- Task 60: Re-read proof — truth and stress scenarios
- Read first
- rtDialogChanged
- Hist
- Throughput: profile, faster normalize, parallel parsing, pattern-sharded scale-out
- nb
- Run with Docker Compose
- Task pm0: Process-memory metric and the synthetic big-service probe
- Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope
- te
- Whole pipeline: stress test, normal operation and incidents (2026-10-24)
- Task pm4: Hide Off services in the web and the linker
- step
- test_lab_decide.py
- level_class
- runv.sh
- Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)
- 6. Method contract

## God Nodes (most connected - your core abstractions)
1. `create_app()` - 129 edges
2. `ServiceData` - 125 edges
3. `E()` - 119 edges
4. `FakeSource` - 115 edges
5. `i()` - 83 edges
6. `a()` - 73 edges
7. `Baseline` - 71 edges
8. `o()` - 71 edges
9. `n()` - 69 edges
10. `Engine` - 67 edges

## Surprising Connections (you probably didn't know these)
- `Task 12: Alerting and Templates pages` --references--> `alert_rows()`  [INFERRED]
  docs/plans/2026-10-20-core-engine-v1.md → logai2/alerts.py
- `Task 3: Shared key picker on Baseline and Review` --references--> `root()`  [INFERRED]
  docs/plans/2026-10-22-baseline-review-key-picker.md → logai2/alerts.py
- `Read first` --references--> `WebConfig`  [INFERRED]
  docs/tasks/43-module-route.md → logai2/config.py
- `10.1 Start` --references--> `migrate()`  [INFERRED]
  ARCHITECTURE.md → logai2/db.py
- `Run the UI` --references--> `migrate()`  [INFERRED]
  docs/WEB_UI.md → logai2/db.py

## Import Cycles
- 3-file cycle: `scripts/lab/methods/__init__.py -> scripts/lab/methods/cusum.py -> scripts/lab/methods/sigma.py -> scripts/lab/methods/__init__.py`

## Communities (171 total, 8 thin omitted)

### Community 0 - "tmpl"
Cohesion: 0.23
Nodes (24): test_resolver_skips_off_templates(), tmpl(), vec(), alerting(), Chat, profile(), row(), run() (+16 more)

### Community 1 - "FakeSource"
Cohesion: 0.07
Nodes (73): 55 Schedule setting, Read first, Read first, Tests (write first, see them fail), Tests (write first, see them fail), put_setting(), FakeSource, at() (+65 more)

### Community 2 - "embed"
Cohesion: 0.09
Nodes (27): B3. One rejected document or template blocks the linker, Requirements, embed(), EmbeddingError, literal(), Linker, axis(), embeddings_handler() (+19 more)

### Community 3 - "logai2 Architecture"
Cohesion: 0.08
Nodes (21): 13. Failure handling, 15. Where the code departs from the spec, 16. Reading the code, 17. When to update this document, 2. System context, 3. Configuration, 6. Reader, 8.1 Keys per closed minute (+13 more)

### Community 4 - "create_app"
Cohesion: 0.06
Nodes (71): Task 11: Web shell, Pipeline and Data sources pages, Task pm2: On/Off switch per pattern (selection, reading, UI), Task pm4: Hide Off services in the web and the linker, Read first, Evaluation (`/api/eval/*`), enqueue(), next_doc_id(), BadRequest (+63 more)

### Community 5 - "Engine"
Cohesion: 0.08
Nodes (13): Task 8: Engine and resolver status fields, Task pm5: Close minutes in bites, stop hoarding while lagging, lag warning, Read first, Interfaces, Read first, Read first, Read first, Requirements (+5 more)

### Community 6 - "test_web_alerts_templates.py"
Cohesion: 0.13
Nodes (23): Global Constraints, Review Focus, Task 1: API — `link_source=review` and `review_total`, Task 4: Docs and wrap-up, Templates Page Redesign Implementation Plan, add_doc(), add_state(), add_template() (+15 more)

### Community 7 - "templates.js"
Cohesion: 0.05
Nodes (67): Task 2: Toolbar mode and the 5-column table, Task 3: Decision-first drawer and Next item, Accessibility (UI UX Pro Max checks), API, Docs, Drawer, Goal, Layout (+59 more)

### Community 8 - "incident_test.py"
Cohesion: 0.06
Nodes (35): Task 13: Test C, Line under the picker, Picker (both pages), Routes and data, Row 1: group buttons, Row 2: Show, Done when, Files (+27 more)

### Community 9 - "PR: task/<id>-<slug>"
Cohesion: 0.06
Nodes (30): Branches and worktrees, Gemini 3.8 Flash (Antigravity CLI), How the tech lead runs each implementer, Ledger, Life of a task, Questions go up, never sideways, Review gates, Roles (+22 more)

### Community 10 - "documentation.js"
Cohesion: 0.15
Nodes (32): decide(), deleteDocument(), $docList, docMatches(), $docPane, docs, $docSearch, $docService (+24 more)

### Community 11 - "test_es_source.py"
Cohesion: 0.09
Nodes (35): hit_to_line(), AuthenticationException, AuthorizationException, BadRequestError, bodies(), line_of(), make_source(), NotFoundError (+27 more)

### Community 12 - "Miners"
Cohesion: 0.09
Nodes (13): B2. Lone UTF-16 surrogate (half an emoji) in an ES field, Task 5: Drain3 per service, Drain3Config, _BlobPersistence, display_template(), Miners, _match(), TestLogMasking (+5 more)

### Community 13 - "sources.js"
Cohesion: 0.13
Nodes (26): conn, connCluster(), $connError, $connForm, $connHosts, connMovesCluster(), $connPassword, connSave() (+18 more)

### Community 14 - "a"
Cohesion: 0.08
Nodes (90): ac(), An(), ba(), bl(), bn(), bp(), Dn(), ds() (+82 more)

### Community 15 - "echarts.min.js"
Cohesion: 0.02
Nodes (129): Ab(), af(), at(), au(), aw(), Ax(), B(), be() (+121 more)

### Community 16 - "DayOut"
Cohesion: 0.08
Nodes (17): fill(), hist_days(), hourly_mean(), hourly_series(), nan_day(), shape(), to_day(), widen() (+9 more)

### Community 17 - "Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture"
Cohesion: 0.13
Nodes (15): 1.1 The Current Problem: Visual Disconnect and Operator Friction, 1.2 The Proposed Solution: Strategy A (Unified Apache ECharts), 1. Executive Summary & Problem Statement, 2. Core Architectural & Design Principles, 3.1 Why Apache ECharts Over Disjointed Micro-Libraries?, 3. Technology Decision: Strategy A (Unified Apache ECharts Engine), 4. End-to-End Visual Workflow & User Experience, 6.1 Extended Baseline Endpoint (`GET /api/baseline?service=...`) (+7 more)

### Community 19 - "json"
Cohesion: 0.11
Nodes (18): _unit(), chat_json(), endpoint(), LlmError, LlmUnavailable, _NoRedirect, parse_json_reply(), _timed_out() (+10 more)

### Community 20 - "normalize"
Cohesion: 0.09
Nodes (11): _collapse(), find_level(), _is_prefix_token(), _mask_hex(), _mask_long_id(), normalize(), _strip_leading_timestamp(), _xml_tag() (+3 more)

### Community 21 - "test_owner.py"
Cohesion: 0.07
Nodes (75): Task 9: Engine writes module counts, Tests (write first, see them fail), Tests (write first, see them fail), Tests (write first, see them fail), _alert_minutes(), counts(), _high_setup(), lines() (+67 more)

### Community 22 - "review.js"
Cohesion: 0.07
Nodes (76): bl, BL_STATE, BL_STATE_SUB, blApplyRoute(), blChangeRoute(), blClear(), blDecision(), blIncKey() (+68 more)

### Community 23 - "test_links.py"
Cohesion: 0.25
Nodes (17): apply_links(), candidates(), test_llm_table_scores_the_confirm_answers(), at(), doc(), docs(), link(), links() (+9 more)

### Community 24 - "Task pm6: Memory proof and docs"
Cohesion: 0.22
Nodes (7): B27. Drain3 `save_state` serialises the whole tree on every flush that learns a template, Done when, Files, Read first, Requirements, Task pm6: Memory proof and docs, Why

### Community 25 - "periods"
Cohesion: 0.12
Nodes (22): 4. Response `200`, Trường cấp gốc, Trường của mỗi template, Task 43: route, token, docs, deploy config, Task 1: Non-NORMAL minutes per hour (shared helper), 4. Response, Tests (write first, see them fail), `GET /api/module-alerts` (+14 more)

### Community 26 - "config.py"
Cohesion: 0.05
Nodes (44): 3.2 Loader (`load.py`), pick_k(), BaselineConfig, DatabaseConfig, DocumentationConfig, DropConfig, EmbeddingConfig, FlushConfig (+36 more)

### Community 27 - "Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance"
Cohesion: 0.14
Nodes (14): 1. Executive Summary & Problem Statement, 2. Core Architectural Principles, 3. End-to-End Processing Workflow, 4. Trigger Criteria & Anti-Spam Filtering, 5.1 Context Payload, 5.2 Structured Prompt, 5. Context Assembly & Prompt Engineering, 6. Database Schema Evolution (+6 more)

### Community 28 - "module_alerts"
Cohesion: 0.07
Nodes (75): Tests (write first, see them fail), _day(), module_alerts(), parse_window(), _to_seconds(), sa(), basic(), pw() (+67 more)

### Community 29 - "record"
Cohesion: 0.11
Nodes (19): 61 Migration 010 + LabConfig + recorder, Execution, Phase 1: collect, 1.2 Config, 1.4 Retention, 1.5 Tests (`tests/test_recorder.py`, using the owner fixtures of `tests/test_owner.py`), 1.6 Docs, Task 1: Migration 010, `LabConfig`, live recorder (+11 more)

### Community 30 - "HTTP API"
Cohesion: 0.08
Nodes (27): 12.1 Settings keys, 12.2 Commands, 12. Web ↔ engine contract, Done when, Files, Interfaces, Requirements (each has a test), Task 57: Re-read lane — replace chosen services' hours from Elasticsearch, then rebuild (+19 more)

### Community 31 - "keypicker.js"
Cohesion: 0.21
Nodes (16): createKeyPicker(), choose(), options(), render(), renderGroups(), renderList(), setOpen(), keyGroup() (+8 more)

### Community 32 - "link_eval.py"
Cohesion: 0.25
Nodes (13): 14. Known limits, Task 5: LLM profiles call order, active_profiles(), corpus_top2(), layer2_table(), pick(), llm_table(), main() (+5 more)

### Community 33 - "ui.js"
Cohesion: 0.26
Nodes (10): confirmDialog(), focusables(), modalObserver, modalOpeners, notifyDialog(), onModalClosed(), onModalOpened(), openModals() (+2 more)

### Community 34 - "alerting.js"
Cohesion: 0.22
Nodes (14): al, alActions(), alAnchor(), alBaseline(), ALERT_STATES, $alertCards, alLine(), alLink() (+6 more)

### Community 35 - "test_web_docs_baseline.py"
Cohesion: 0.09
Nodes (16): Phase 1: web only, Task 1: Alerting links and deep links, Task 2: Every level in the filters, Task 3: Templates first seen sort and service search, Task 4: Baseline exact group counts, `GET /api/alerts`, cluster_edges(), cluster() (+8 more)

### Community 36 - "logai2 core engine + web v1: design"
Cohesion: 0.18
Nodes (11): 1. Repo layout, 2. Data flow, 3. Detection, 4. Postgres schema (`001_init.sql`), 6. Error handling, 7. Configuration, 8. Testing, Decisions (+3 more)

### Community 37 - "Web UI and HTTP API"
Cohesion: 0.07
Nodes (29): 1. Templates per service, 2. Detection: statistical baseline, no training, 3. From alert to document: templates link directly (option D, confirmed), 4. Writing documentation: by hand, with an AI chat alongside, 4a. LLM confirm or suggest, reviewed by a person, 5. Pipeline: Elasticsearch → engine, one process first, 6. Storage: PostgreSQL only, 7. Module alert API (+21 more)

### Community 38 - "Baseline"
Cohesion: 0.07
Nodes (48): 5. Core data contracts, Task 7: Baseline band and daily commit, Task 9: Owner and the flush transaction (exactly once), Baseline, Review and Pipeline Pages, Watchdog Style — Implementation Plan, Global Constraints, Task 9: Pipeline API: services, volume, stages, 4.1 σ (#1), Facts this plan is built on (checked 2026-10-27) (+40 more)

### Community 39 - "Owner"
Cohesion: 0.05
Nodes (49): 7.3 The flush transaction, B10. Open UI tabs keep running old JS after a deploy, B11. An LLM profile named `order` cannot be created or edited, B12. Every flush rewrites `last_seen` of every template seen in the last 2 s, B13. A request-id-like `moduleCode` grows `modules` and `template_modules` forever, B15. A flush can get stuck on a dropped lab / module-hours partition, B17. Prophet's `uncertainty_samples` limits the 0.999 / 0.9999 band tails, B18. LightGBM cost at 3k keys is unmeasured (+41 more)

### Community 40 - "bands.py"
Cohesion: 0.14
Nodes (14): best_k(), floor_of(), kind_of(), _lookback(), main(), _plan(), _services(), _test_days() (+6 more)

### Community 41 - "pipeline.js"
Cohesion: 0.10
Nodes (43): fetchPipeline(), pl, PL_COLORS, PL_MAIN, PL_NODES, plApplyRoute(), plBusiest(), plCard() (+35 more)

### Community 42 - "test_web_bands.py"
Cohesion: 0.20
Nodes (16): Task eb3: API + Evaluation page; delete the lab-run page, bands_series(), cell(), client(), seed(), test_bands_errors(), test_bands_no_history(), test_bands_shape() (+8 more)

### Community 44 - "retrain.js"
Cohesion: 0.14
Nodes (34): loadRetrain(), rt, RT_BADGE, RT_DAYS, rtAt(), rtBar(), rtBuildDialog(), rtClock() (+26 more)

### Community 45 - "links.py"
Cohesion: 0.24
Nodes (7): Task 4: Links SQL (`links.py`), the core, _by_class(), class_sql(), desired_corpus(), desired_inherited(), _params(), similar()

### Community 46 - "llm.js"
Cohesion: 0.26
Nodes (12): llm, llmBanner(), llmKey(), llmMeta(), $llmModal, $llmOrder, llmSlot(), $llmUnused (+4 more)

### Community 47 - "Incident test, 2026-10-20: smoke test"
Cohesion: 0.25
Nodes (7): Checks, Incident test, 2026-10-20: smoke test, Inject errors and anomalies, Recovery, Targets, What is meaningful, Why B3 and B5 failed

### Community 48 - "datetime"
Cohesion: 0.05
Nodes (27): day_hour(), daytype(), local(), minute_of(), window_args(), window_hours(), Cusum, Spec (+19 more)

### Community 49 - "test_lab_run.py"
Cohesion: 0.04
Nodes (72): F7. Test A never committed its recall rows or the run status (fixed in bk17, `b5118d6`), Interfaces, select(), check(), main(), make_part(), parse_args(), run() (+64 more)

### Community 50 - "Resolver"
Cohesion: 0.12
Nodes (11): Global Constraints, LLM Resolver for Every Unlinked Template, No Call Limits — Implementation Plan, Review Focus, Task 1: Remove the alerting / 24 h gate, Task 2: Limits default to 0 = no limit, Task 3: Web shows "no limit", Task 4: Merge, deploy, watch the backlog, LlmConfig (+3 more)

### Community 51 - "test_bands.py"
Cohesion: 0.21
Nodes (23): Review Focus, Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope, Task eb4 (tech lead): laptop proof, Chrome, deploy note, DayHour, history(), training(), committed(), hours() (+15 more)

### Community 52 - "3B. Baseline page (`#baseline`, charts only)"
Cohesion: 0.25
Nodes (8): 3B.1 Header and cards, 3B.2 Actual vs expected (ECharts line), 3B.3 Possible incidents, 3B.4 Incident detail, 3B.5 Days × hours (read-only), 3B.6 The baseline itself (ECharts line), 3B.7 API, 3B. Baseline page (`#baseline`, charts only)

### Community 53 - "hit"
Cohesion: 0.12
Nodes (33): How it works, 51 Live reader: one cursor per pattern, Tests (write first, see them fail), KnownLines, Reader, run_e1(), hit(), make() (+25 more)

### Community 54 - "logai2 — instructions for Claude sessions"
Cohesion: 0.50
Nodes (3): logai2 — instructions for Claude sessions, Project rules, Team workflow (when the user says to use it)

### Community 55 - "test_web_baseline_view.py"
Cohesion: 0.13
Nodes (41): Review Focus, Task 11: Docs, merge readiness, Task 7: Baseline page UI (charts), base(), cell(), committed(), exclude(), hours() (+33 more)

### Community 57 - "U"
Cohesion: 0.07
Nodes (40): Al(), ay(), bv(), ct(), E(), en(), Ez(), go() (+32 more)

### Community 58 - "test_reread.py"
Cohesion: 0.08
Nodes (60): 57 Re-read lane, Tests (write first, see them fail) — `tests/test_reread.py` unless noted, Tests (write first, see them fail), Scope, start(), at(), docs(), drive() (+52 more)

### Community 59 - "test_lab_score.py"
Cohesion: 0.17
Nodes (36): Incident, _cfg(), ctx_for(), flags_for(), make_sd(), mase_world(), metrics(), out() (+28 more)

### Community 60 - "ensure_partitions"
Cohesion: 0.12
Nodes (17): Task 1: Skeleton, config, database and schema, 1.1 Schema (migration 010, all lab tables at once), Spec/plan facts confirmed in code, _merge_dataclass(), drop_partitions_before(), ensure_partitions(), migrate(), StaleRevision (+9 more)

### Community 61 - "l"
Cohesion: 0.12
Nodes (30): aA(), ap(), cp(), dp(), Dy(), ep(), fp(), l() (+22 more)

### Community 62 - "app.py"
Cohesion: 0.09
Nodes (38): Task 2: Review matrix and coverage API (`/api/baseline/matrix`), Task 3: Review inspector API (`/api/baseline/inspect`), Task 6: Baseline series and incident API, Task 2: Web resolves `verdict:` keys and serves the new keys, Baseline and Review: grouped key picker and combined series, Deploy, Derived keys: `verdict:action`, `verdict:safe`, Docs (+30 more)

### Community 63 - ".note"
Cohesion: 0.18
Nodes (7): DiskLow, stop(), sleep(), timed_flush(), timed_handle(), timed_poll(), Stalled

### Community 64 - "Retrain Implementation Plan"
Cohesion: 0.15
Nodes (12): 52 `first_seen` LEAST + `days_back` doc, 53 Nav: Baseline and Review to Catalog, 58 Retrain API, 59 Retrain page, 60 Truth and stress, Execution, Global Constraints (every brief repeats them), Phase 1: fixes (+4 more)

### Community 65 - "test_web_pipeline_view.py"
Cohesion: 0.14
Nodes (16): Task 7: Pipeline page: pick services, throughput chart, _age(), _by_service(), services(), _stage(), stage_states(), throughput(), test_malformed_last_commit_is_not_overdue() (+8 more)

### Community 66 - "es_source.py"
Cohesion: 0.09
Nodes (15): B14. One failing index fails the whole pattern's search, Task 3: Elasticsearch source (search, paging, hit → Line), ElasticsearchConfig, ReaderConfig, _build_client(), _coerce_message(), EsSource, _extract_level() (+7 more)

### Community 67 - "test_web_retrain.py"
Cohesion: 0.15
Nodes (24): Done when, Files, Requirements, Task 58: Retrain HTTP API, Tests (write first, see them fail), Why, add_run(), client() (+16 more)

### Community 68 - "i"
Cohesion: 0.07
Nodes (50): A(), e(), Ci(), n(), dc(), dd(), ea(), et() (+42 more)

### Community 69 - "ed"
Cohesion: 0.11
Nodes (20): Ad(), BD(), Bs(), a(), ed(), gs(), Hn(), ii() (+12 more)

### Community 70 - "test_lab_inject.py"
Cohesion: 0.09
Nodes (32): slot_of(), apply(), arr(), change(), derived(), Expect, Injection, _near() (+24 more)

### Community 71 - "to"
Cohesion: 0.09
Nodes (26): 1. Gọi nhanh, 2. Xác thực, 3. Request, 5. Khi nào một template được trả về, 6. Lỗi, 7. Ví dụ code, logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào), Làm tròn: độ phân giải 1 phút, mở rộng ra ngoài (+18 more)

### Community 72 - "test_lab_sigma.py"
Cohesion: 0.12
Nodes (23): Done when, Files, Requirements, Task bk05: Scoring sink (Test B metrics, episodes, drawn bands), Tests (write first, see them fail), Why, Sigma, _slot_of_commit() (+15 more)

### Community 73 - "modules"
Cohesion: 0.04
Nodes (48): 4.1 Elasticsearch document, 4.2 Query, paging and the millisecond re-read, 4.3 Index selection and connection, 4.4 Documentation seed, 4. External input contract, Facts from the current code the tasks rely on, logai2 Module Alert API Implementation Plan, Out of scope (spec §9) (+40 more)

### Community 74 - "E"
Cohesion: 0.06
Nodes (49): aR(), br(), bw(), c(), Cn(), Dk(), DO(), Dr() (+41 more)

### Community 75 - "stress_modules.py"
Cohesion: 0.16
Nodes (16): api_p95(), connect(), estimate(), explains(), flush_runs(), load(), main(), measure() (+8 more)

### Community 76 - "tail_above"
Cohesion: 0.10
Nodes (22): Evaluation bands (4 methods on read) Implementation Plan, Global Constraints, Self-review, Done when, Files, Read first, Rulings from the eb1 review (tech lead, 2026-10-30), Task eb2: tune — walk-forward fit test, CLI, estimate check (+14 more)

### Community 77 - "fv"
Cohesion: 0.14
Nodes (16): cv(), dv(), ev(), fv(), hv(), jy(), kV(), lv() (+8 more)

### Community 78 - "Document links smoke test on real data (2026-10-21)"
Cohesion: 0.40
Nodes (4): Document links smoke test on real data (2026-10-21), Reading, Result, Setup

### Community 79 - "test_web_llm.py"
Cohesion: 0.29
Nodes (7): client(), listed(), put_profile(), test_new_profile_priority_goes_last_and_edit_keeps_it(), test_profile_order_rejects_bad_bodies_and_changes_nothing(), test_profile_order_sets_priority_and_active(), test_profiles_crud_key_is_write_only_and_one_active()

### Community 80 - "Per-service templates, statistical baselines, document-centric alerts"
Cohesion: 0.22
Nodes (9): Goal, Known weak points (accepted for v1), Open questions, Per-service templates, statistical baselines, document-centric alerts, Pipeline risks and fixes, Results (2026-10-19, live recharge stack), Test: template → document vs group centroid → document, Validation (after building) (+1 more)

### Community 81 - "commit"
Cohesion: 0.10
Nodes (26): Done when, Files, Interfaces, Requirements, Task 54: Migration 008 `retrain_runs`; every Rebuild recorded as a run, Tests (write first, see them fail), Why, commit() (+18 more)

### Community 82 - "A"
Cohesion: 0.07
Nodes (34): cA(), D(), da(), Eh(), fa(), fR(), ga(), gp() (+26 more)

### Community 83 - "Lane"
Cohesion: 0.09
Nodes (20): 9. Daily commit and review exclusions, Task 2: Recorder in the re-read lane, 4.5 Re-read lane, Done when, Files, Interfaces, Task bk01: Migration 010, LabConfig, live recorder, Why (+12 more)

### Community 84 - "pytest"
Cohesion: 0.06
Nodes (76): nb_quantiles(), D(), have(), make_sd(), mp_day(), run_day(), test_band_covers_clean_day(), test_check_lists_all_unavailable() (+68 more)

### Community 85 - "test_lab_synth.py"
Cohesion: 0.10
Nodes (40): Tests (write first, see them fail), `GET /api/baseline/inspect`, generate(), expected(), main(), _place_incidents(), run_engine(), write() (+32 more)

### Community 86 - "Band"
Cohesion: 0.21
Nodes (9): Band, expected_outside(), _mad(), nb_quantiles(), p_of(), test_nb_normal_branch_close_to_poisson_tail(), test_nb_poisson_when_var_le_mean(), test_nb_quantiles_match_lab() (+1 more)

### Community 87 - "loadData"
Cohesion: 0.16
Nodes (13): Checks (no JS test framework in this repo), Done when, Files, Read first, Requirements, Task 59: Retrain page, Why, Requirements (+5 more)

### Community 88 - "stress_pipeline.py"
Cohesion: 0.06
Nodes (60): bad_streams(), streams(), build_world(), burst_measure(), burst_streams(), caught_up(), compare(), crash_hook() (+52 more)

### Community 89 - "Rw"
Cohesion: 0.20
Nodes (17): bi(), Cw(), di(), Dw(), Ew(), fi(), hi(), hw() (+9 more)

### Community 90 - "Results"
Cohesion: 0.18
Nodes (11): E1 real Elasticsearch (temporary index `stress-pipeline-<hex>`, deleted after), I1 error burst (svc00's busiest ERROR template, 34/min, x20 for 10 min), I2 volume drop (svc01 at 10 % for 15 min), I3 template storm (5,000 new stack-trace-like templates in 5 min on svc04, ~6 events each), I4 late data (svc02's index 3 min late for 10 min), I5 Postgres outage (both connections closed, reconnect refused for 2 simulated minutes), I6 bad data (one doc every 20 s for 2 min in svc00's index), I7 module cardinality spike (svc03's moduleCode request-id-like, 10,000 values, 5 min) (+3 more)

### Community 91 - "test_web_modules.py"
Cohesion: 0.08
Nodes (31): Disk, EXPLAIN (ANALYZE, BUFFERS): /api/modules window aggregate, EXPLAIN (ANALYZE, BUFFERS): module filter, window lines first (/api/templates items), EXPLAIN (ANALYZE, BUFFERS): window lines of 100 templates (modules_for), EXPLAIN highlights, Machine, Plans (run 1, after the fix), Results (+23 more)

### Community 92 - "test_owner_selection.py"
Cohesion: 0.20
Nodes (25): batch(), count(), drive(), make(), rows(), test_disable_drops_open_minutes(), test_disable_resets_alerts(), test_disable_slowest_pattern_jump_writes_no_zeros() (+17 more)

### Community 93 - "decide.py"
Cohesion: 0.16
Nodes (12): B16. M6 decisions need a Test B M6 tune alert rate, point_ratio(), Cand, _decide_kind(), _detect_decision(), _gates(), method_verdict(), _recalls() (+4 more)

### Community 94 - "score.py"
Cohesion: 0.05
Nodes (28): 18. Lab (bake-off), _spec(), Acc, bin_of(), day_from(), IncidentSink, InjectSink, _mean5() (+20 more)

### Community 95 - "test_bands_tune.py"
Cohesion: 0.29
Nodes (20): Task eb2: `tune` — walk-forward fit test, CLI, estimate check, stored_tune(), exclude(), build(), cell(), method(), run(), sparse_build() (+12 more)

### Community 96 - "test_web_links.py"
Cohesion: 0.07
Nodes (37): 1. Purpose and scope, 5.1 Document links, Edge cases (each has a test), File map, Global Constraints, How it works (what the user confirms), logai2 Embeddings, Document Links and LLM Resolution Implementation Plan, Other behavior (+29 more)

### Community 97 - "10. Engine loop"
Cohesion: 0.08
Nodes (20): 10.1 Start, 10.2 Tick, 10.3 Commit timing, 10.4 Bootstrap and `days_back`, 10.5 `worker_status`, 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108), 10. Engine loop, 7.2 Open and closed minutes (+12 more)

### Community 98 - "mem_probe.py"
Cohesion: 0.06
Nodes (31): 11. Postgres schema, 7.1 Templates, 7. Owner and exactly-once flush, Before (master e8ec04b), Task 5: Scoring, Test B metrics, Order and dependencies, Task pm0: Process-memory metric and the synthetic vas_066 probe, Task pm6: Memory proof and docs (+23 more)

### Community 99 - "apply_d4"
Cohesion: 0.06
Nodes (41): Done when, Files, Interfaces (later tasks rely on these exact names), Requirements, `scripts/lab/data.py`, `scripts/lab/kinds.py`, `scripts/lab/load.py`, `scripts/lab/methods/__init__.py` (+33 more)

### Community 100 - "finish_all"
Cohesion: 0.21
Nodes (25): finish_all(), finish_run(), put_result(), put_sweep(), all_rows(), board(), board_kind(), m6() (+17 more)

### Community 101 - "Task bk10: Test A (in-memory injections), recall and delay, M6/M7 decisions"
Cohesion: 0.33
Nodes (5): Done when, Files, Task bk10: Test A (in-memory injections), recall and delay, M6/M7 decisions, Tests (write first, see them fail) — `tests/lab/test_lab_inject.py` (+ the run/decide test files), Why

### Community 102 - "synth.py"
Cohesion: 0.06
Nodes (25): Task 12: Synthetic world, `engine` mode, Done when, Files, Interfaces, Task bk06: Synthetic world, `counts` mode, Tests (write first, see them fail), Why, Done when (+17 more)

### Community 103 - "Task pm2: On/Off switch per pattern (selection API, reading, Data sources UI)"
Cohesion: 0.33
Nodes (5): Done when, Files, Interfaces, Task pm2: On/Off switch per pattern (selection API, reading, Data sources UI), Why

### Community 104 - "T"
Cohesion: 0.06
Nodes (56): Am(), ao(), bB(), bf(), cb(), Cm(), db(), df() (+48 more)

### Community 105 - "evaluation.js"
Cohesion: 0.12
Nodes (32): ev, EV_IDS, EV_KINDS, EV_METHODS, EV_MON, evApplyRoute(), evBuild(), evClear() (+24 more)

### Community 106 - "GenSource"
Cohesion: 0.09
Nodes (9): 4.3 Tests, Requirements, base_streams(), GenSource, timed(), Stream, at(), burst() (+1 more)

### Community 107 - "minute_at"
Cohesion: 0.10
Nodes (28): Tests (write first, see them fail), minute_at(), load_service(), services(), top_templates(), fill(), put(), test_load_exact_arrays_and_history() (+20 more)

### Community 108 - "main.js"
Cohesion: 0.18
Nodes (8): currentTheme(), mainContent, POLL_MS, $sidebar, $sidebarCollapse, updateThemeToggle(), viewOrder, viewTitles

### Community 109 - "4. Pipeline page"
Cohesion: 0.29
Nodes (7): 4.1 Status line, 4.2 Topology (ECharts `graph`, fixed layout), 4.3 Lag and throughput, last 60 minutes (ECharts line), 4.4 Volume vs expected, per service (ECharts line, small multiples), 4.5 Services table (HTML), 4.6 Engine and API changes, 4. Pipeline page

### Community 110 - "5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)"
Cohesion: 0.22
Nodes (9): 5.1 The Baseline Matrix & Visual Band Inspector (`/#baseline`), 5.2 The Pipeline Data Stream Topology (`/#pipeline`), 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive), 5. Detailed UI/UX Specifications, Pillar 1: Watchdog Insights Banner & Outlier Classification, Pillar 2: Log Pattern Volume Delta (% Change vs. Baseline), Pillar 3: 1-Click Incident Marking & Baseline Exclusion Loop, Tier 1: Weekly Baseline Health Matrix (24 Hours $\times$ 7 Committed Days) (+1 more)

### Community 111 - "LLM resolver: every unlinked template, not only alerting or new ones"
Cohesion: 0.25
Nodes (7): Change, Deploy, Docs (with the code, since ARCHITECTURE describes what is implemented), How a template that was never seen before is handled (for context), LLM resolver: every unlinked template, not only alerting or new ones, Problem, Tests (`tests/test_resolver.py`)

### Community 112 - "Retrain: baseline rebuilds, re-reads from ES, schedule and history"
Cohesion: 0.18
Nodes (10): 1. Goal, 2. Two kinds of run, 3. Re-read, 4. Schedule (`settings.retrain_schedule`, revisioned), 5. History (`retrain_runs`), 6. API, 7. Risks, 8. Phase 1 (first) (+2 more)

### Community 113 - "OpenMinutes"
Cohesion: 0.24
Nodes (12): OpenMinutes, M(), test_close_includes_empty_minutes_and_service_keys(), test_first_start_backlog_is_linear(), test_late_count_is_dropped(), test_level_class_keys_add_up_to_total(), test_levels_error_keys_and_most_severe(), test_silent_key_forgotten_next_hour() (+4 more)

### Community 114 - "Two deployments: laptop and company (same code, same images, same page)"
Cohesion: 0.29
Nodes (7): 3.2 Matrix: days × hours (ECharts heatmap), How the two are compared, Laptop build/run additions, Synthetic world on the laptop: 7 / 14 / 28 days without waiting, Test C (laptop only): real incidents written into ES, with known times, Two deployments: laptop and company (same code, same images, same page), label()

### Community 115 - "logai2 Core Engine + Web v1 Implementation Plan"
Cohesion: 0.40
Nodes (4): Global Constraints, logai2 Core Engine + Web v1 Implementation Plan, Review Focus, Spec coverage check

### Community 116 - "Task 53: Nav — Baseline and Review move from Settings to Catalog"
Cohesion: 0.25
Nodes (7): Done when, Files, Read first, Requirements, Task 53: Nav — Baseline and Review move from Settings to Catalog, Tests, Why

### Community 117 - "alert_rows"
Cohesion: 0.18
Nodes (12): alert_rows(), root(), hook(), fraction(), r_kill(), hook(), starter(), probe() (+4 more)

### Community 118 - "Y"
Cohesion: 0.09
Nodes (27): ag(), cc(), Cf(), cg(), Cr(), dg(), eM(), fD() (+19 more)

### Community 119 - "Task 52: `templates.first_seen` moves earlier; `days_back` doc"
Cohesion: 0.20
Nodes (9): Done when, Files, Interfaces, Read first, Requirements, Task 52: `templates.first_seen` moves earlier; `days_back` doc, Tests (write first, see them fail), Why (+1 more)

### Community 120 - "Task 55: Retrain schedule setting (on/off, time, week days)"
Cohesion: 0.33
Nodes (5): Done when, Files, Requirements, Task 55: Retrain schedule setting (on/off, time, week days), Why

### Community 121 - "Fixed"
Cohesion: 0.18
Nodes (10): B24. The engine loads every service's state at start, also services no pattern reads (OOMKilled loop) (fixed in pm1 `73a9e47` + pm3 `a41425f`), B5. First catch-up memory when the index pattern covers several indices (fixed, task 51), F10. mad, mprofile and pca never fitted on the first 7 days of a window (fixed in bk17, `4cf66fb`), F11. A tie at recall 0 named a leader for M6 / M7 (fixed in bk17, `84fa91f`), F1. NUL byte in an ES field stalled the flush, F2. mprofile looked ahead inside the scored day (fixed in bk16, `14bc644`), F8. Test A injected where nothing can alert, and bursts as INFO (fixed in bk17, `9cde33a`), F9. Test A chose the least sensitive tail probability (fixed in bk17, `58ea334`) (+2 more)

### Community 122 - "Task 5: Review page UI (decisions)"
Cohesion: 0.31
Nodes (8): Task 4: ECharts, chart helper, chart colour tokens, Task 5: Review page UI (decisions), 2. ECharts in this console, baseOption(), chartFor(), _chartObservers, chartTokens(), disposeCharts()

### Community 123 - "Tracker"
Cohesion: 0.23
Nodes (11): Episode, episodes(), Tracker, flags(), test_close_while_warming_or_normal_is_none(), test_cooling_interrupted_by_a_high_stays_one_episode(), test_five_highs_three_lows_one_episode(), test_open_at_close_returns_with_window_end() (+3 more)

### Community 124 - "The methods"
Cohesion: 0.10
Nodes (20): #10 LightGBM global quantile model, #11 Chronos-Bolt (small), #12 Matrix Profile (STUMPY), #13 PCA on the per-service template vector (Xu et al. 2009), #1 mean ± kσ (today), #2 Negative-binomial tail (Poisson when var ≤ mean), #3 #2 with Farrington-style outlier clipping at rebuild, #4 Median ± k·MAD (+12 more)

### Community 125 - "random"
Cohesion: 0.24
Nodes (5): ci90(), percentile(), aggregate(), alert_rate(), test_aggregate_two_services_hand_calculation()

### Community 126 - "54 Migration 008 + Rebuild recorded as runs"
Cohesion: 0.25
Nodes (7): 54 Migration 008 + Rebuild recorded as runs, 56 `index_services` + ES oldest doc, Phase 2: engine, finish(), test_migration_008(), test_run_log_keeps_warnings(), test_runs_finish_sets_state_and_log()

### Community 127 - "Pattern On/Off switch and engine memory: implementation plan"
Cohesion: 0.22
Nodes (8): Agent roles, Company rollout (after pm6, by the user), Cost, Global Constraints, Pattern On/Off switch and engine memory: implementation plan, Per task, Review Focus, Waves (parallel only when the file lists are disjoint; at most 3 agents at once)

### Community 128 - "Baseline Bake-off on the laptop: implementation plan (tasks 0–17)"
Cohesion: 0.09
Nodes (21): 6.1 World file (`world.py` → `lab/world.json`, not committed: it contains the laptop's template texts), 6.2 Scenario YAML, 6.3 `synth.py --mode counts --scenario X --schema lab_s_X`, 6.4 Tests, Agent roles, Baseline Bake-off on the laptop: implementation plan (tasks 0–17), Containers during the build, Per task (+13 more)

### Community 129 - "test_web_sources.py"
Cohesion: 0.15
Nodes (9): Tests (write first, see them fail), entries(), put(), test_password_never_returned(), test_pipeline_stale_flag(), test_selection_disable_and_reenable(), test_selection_disabled_must_be_selected(), test_selection_round_trip_and_command() (+1 more)

### Community 130 - "Judging protocol (precise rules, the same for every method)"
Cohesion: 0.22
Nodes (9): J1. Which keys are which monitor kind (fixed per run, computed from the tune window), J2. Units, J3. Formulas, J4. Fair tuning (equal coverage, compare sharpness), J5. Injections (laptop only), J6. Confidence with little data, J7. Decision tree per monitor kind (company run), J8. Reproducible (+1 more)

### Community 131 - "Baseline bake-off on the laptop: detailed spec"
Cohesion: 0.09
Nodes (22): 10. Storage (migration 010), 11. Runner, 12. Evaluation API and page, 13. Performance targets (laptop), 14. Correctness gates, 15. Risks, 1. Purpose and scope, 2. Laptop environment (+14 more)

### Community 132 - "Bake-off lab: laptop proof (task bk17)"
Cohesion: 0.11
Nodes (18): 10. Plain-language conclusion per kind (synthetic data only), 11. Limits, 12. Ready for the company plan?, 13. Reproduce, 1. What this document answers, 2. Setup, 3. How to read the lab (for a reader who knows the system but not the lab), 4. What changed between the first runs and the final runs, and why (+10 more)

### Community 135 - "detect.py"
Cohesion: 0.10
Nodes (34): File map, Task 12: Alerting and Templates pages, Task 13: Documentation and Baseline pages, Task 14: Containers, recharge run and incident test, Task 2: Copied text processing (normalize) and its tests, Task 6: Open minutes and minute closing, Task 8: Detection and alert state, 62 Lab skeleton (+26 more)

### Community 136 - "Baseline bake-off: test alerting methods on real data, judge them, choose per monitor kind"
Cohesion: 0.12
Nodes (15): Baseline bake-off: test alerting methods on real data, judge them, choose per monitor kind, Context, Dated timeline for the current company system (it has recorded since Fri 23 Oct 2026), Decisions (answered 2026-10-26), Images (Docker Hub only, nothing downloaded in the cluster), Mockup, Monitor kinds, Performance estimate (lab Job, 4–8 cores) (+7 more)

### Community 137 - "Memory probe: before (master e8ec04b) and after (build/pattern-switch)"
Cohesion: 0.25
Nodes (7): After (build/pattern-switch 23ed042: pm1 packed baseline, pm5 bites, pm3 active services), Before / after (MiB), Extrapolated, not measured: 135,000 templates / 6.48M baseline rows, Extrapolated, not measured: 135,000 templates / 6.48M baseline rows (after), Measured, Memory probe: before (master e8ec04b) and after (build/pattern-switch), Scaling

### Community 138 - ".status"
Cohesion: 0.14
Nodes (11): Interfaces, Bake-off preflight (task 0), 2026-10-27, Decisions, `incident_test.py` run directory (input of task 13), Laptop and containers, Live database (laptop), Replayer API (used by `scripts/incident_test.py`, never changed by us), sum_days() (+3 more)

### Community 140 - "Baseline Bake-off Implementation Plan"
Cohesion: 0.12
Nodes (16): 63 Core methods, 64 Scoring, 65 Test A injector, 66 Lab image + heavy methods, 68 Evaluation API, 69 Evaluation page, 70 Synthetic world, 71 Test C, compose, k8s, docs (+8 more)

### Community 141 - "Task bk14: Evaluation API"
Cohesion: 0.20
Nodes (9): Done when, Endpoints (all read `lab_*` only, unqualified names; JSON; times as ISO via `iso`), Files, Read first, Task bk14: Evaluation API, Tests (write first, see them fail) — `tests/test_web_eval_view.py`, hand-made `lab_*` rows on the `conn` fixture, Why, module_alerts_route() (+1 more)

### Community 142 - "Task bk07: Tuning, sweeps, bootstrap, decision"
Cohesion: 0.25
Nodes (7): Done when, Files, Read first, Requirements, Task bk07: Tuning, sweeps, bootstrap, decision, Tests (write first, see them fail), Why

### Community 143 - "Task eb5: zoom on the Evaluation charts"
Cohesion: 0.25
Nodes (7): Done when, Files, Read first, Requirements, Task eb5: zoom on the Evaluation charts, Tests, Why

### Community 144 - "ServiceData"
Cohesion: 0.03
Nodes (36): 3.1 Data model (`data.py`), 3.3 Kinds (`kinds.py`, J1), 3.4 Method contract (`methods/__init__.py`), 3.5 Runner (`run.py`) and storage (`store.py`), 3.6 Tests, Task 3: Lab foundation, Requirements, Done when (+28 more)

### Community 145 - "Bake-off judging criteria: how a method wins, and why each rule is there"
Cohesion: 0.22
Nodes (8): Bake-off judging criteria: how a method wins, and why each rule is there, Criterion 1: equal coverage (fair tuning, J4), Criterion 2: fit (the ranking, J3), Criterion 3: detection (laptop gate, J5), Criterion 4: confidence (J6), Ground rules (apply to every criterion), The decision per monitor kind (J7), The question the bake-off answers

### Community 146 - "commands.py"
Cohesion: 0.32
Nodes (3): finish(), take(), test_enqueue_take_finish()

### Community 147 - "Task bk16: Compose lab service, run commands, docs, deferred fixes"
Cohesion: 0.22
Nodes (8): Added after bk15 (page follow-ups), Done when, Files, Read first, Requirements, Task bk16: Compose lab service, run commands, docs, deferred fixes, Tests, Why

### Community 148 - "logai2"
Cohesion: 0.25
Nodes (8): How it works, Incident test, logai2, Module alert API, Repo layout, Requirements, Test, Validation

### Community 149 - "Evaluation page (`/#evaluation`): see every method's line, choose, calibrate"
Cohesion: 0.29
Nodes (7): 1. Run bar (top), 2. Scoreboard (per monitor kind M1–M7), 3. Fit chart (the main view): each method draws its line, 4. Calibrate, API (read-only, plus choices), Evaluation page (`/#evaluation`): see every method's line, choose, calibrate, What the run stores for drawing (sized so Postgres stays small)

### Community 150 - "Re-read lane: truth and stress proof (2026-10-26)"
Cohesion: 0.33
Nodes (6): Commands and results, Findings, How it works, Machine, R2 (10 services x 300 templates, 2,000 ev/s past data, 10k ev/s live, 6 h): default scale, logai2-engine stopped, Re-read lane: truth and stress proof (2026-10-26)

### Community 151 - "3. Review page (`#review`, decisions)"
Cohesion: 0.33
Nodes (6): 3.1 Header and cards, 3.3 Inspector: one Watchdog card for the selection, 3.4 Coverage: what the baseline is built from (ECharts heatmap), 3.5 Removed, 3.6 API, 3. Review page (`#review`, decisions)

### Community 152 - "Task bk15: Evaluation page"
Cohesion: 0.25
Nodes (7): Done when, Files, Read first, Requirements, Task bk15: Evaluation page, Tests, Why

### Community 153 - "Task 60: Re-read proof — truth and stress scenarios"
Cohesion: 0.33
Nodes (5): Done when, Files, Requirements: four scenarios, selectable with `--scenarios R1,R2,R3,R4`, Task 60: Re-read proof — truth and stress scenarios, Why

### Community 154 - "Read first"
Cohesion: 0.10
Nodes (8): Read first, _factory(), f(), _report(), _wrap(), _lazy(), make(), Method

### Community 155 - "rtDialogChanged"
Cohesion: 0.60
Nodes (5): rtBody(), rtDialogChanged(), rtKindValue(), rtSelectedDays(), rtWait()

### Community 158 - "Throughput: profile, faster normalize, parallel parsing, pattern-sharded scale-out"
Cohesion: 0.25
Nodes (7): Not in scope, Risks, Roles (team workflow, docs/TEAM_WORKFLOW.md), Targets, Tasks, Throughput: profile, faster normalize, parallel parsing, pattern-sharded scale-out, Why

### Community 159 - "nb"
Cohesion: 0.09
Nodes (21): Constraint: the company runs v15, Data each method needs, Goal, Image update and pod restarts (k8s), Limits, Method bands: 4 methods computed on read, k tuned per method (plan, tasks mb1, mb2, mb4, mb5), Not changed, Tasks (+13 more)

### Community 161 - "Run with Docker Compose"
Cohesion: 0.22
Nodes (9): First start, Lab image (heavy bake-off methods), Run with Docker Compose, Synthetic world (bake-off lab), Test C (real incidents through the engine), Upgrading from v15 (pattern On/Off, memory), Upgrading to migration 007, Upgrading to migration 010 (lab recorder) (+1 more)

### Community 162 - "Task pm0: Process-memory metric and the synthetic big-service probe"
Cohesion: 0.29
Nodes (6): Done when, Files, Interfaces, Requirements, Task pm0: Process-memory metric and the synthetic big-service probe, Why

### Community 164 - "Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope"
Cohesion: 0.33
Nodes (5): Done when, Files, Read first, Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope, Why

### Community 165 - "te"
Cohesion: 0.28
Nodes (9): ce(), ee(), er(), n(), he(), le(), te(), ue() (+1 more)

### Community 166 - "Whole pipeline: stress test, normal operation and incidents (2026-10-24)"
Cohesion: 0.40
Nodes (5): Findings, Machine, Not run, or scaled down, Parameters, Whole pipeline: stress test, normal operation and incidents (2026-10-24)

### Community 167 - "Task pm4: Hide Off services in the web and the linker"
Cohesion: 0.14
Nodes (11): Done when, Files, Read first, Rulings from the eb1 review (tech lead, 2026-10-30), Task eb3: API + Evaluation page; delete the lab-run page, Done when, Files, Interfaces (+3 more)

### Community 168 - "step"
Cohesion: 0.50
Nodes (3): chain.sh script, step(), runc.sh script

### Community 169 - "test_lab_decide.py"
Cohesion: 0.13
Nodes (22): fits_better(), judge_kind(), _overlap(), tune(), cand(), fake(), mkspec(), test_gate_failure_is_not_suitable_and_cannot_lead() (+14 more)

### Community 171 - "level_class"
Cohesion: 0.27
Nodes (9): After the build, Global Constraints, Grouped key picker and combined series — Implementation Plan, Review Focus, Task 1: Engine counts `*warn` / `*info`, detector skips them, Task 3: Shared key picker on Baseline and Review, Read first, Read first (+1 more)

### Community 174 - "Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)"
Cohesion: 0.33
Nodes (6): 1. Questions each page answers, 5. Build order, 6. Out of scope, 7. Open questions, Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2), Where the code differs from this spec

### Community 175 - "6. Method contract"
Cohesion: 0.50
Nodes (4): 6.1 Interface, 6.2 Same alert shape for every method (decision D4), 6.5 The 14 methods (detail in the methods doc), 6. Method contract

## Knowledge Gaps
- **625 isolated node(s):** `runv.sh script`, `DatabaseConfig`, `RetrainConfig`, `FlushConfig`, `StartConfig` (+620 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 1406 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `create_app()` connect `create_app` to `test_web_sources.py`, `test_web_alerts_templates.py`, `Task bk14: Evaluation API`, `periods`, `config.py`, `module_alerts`, `HTTP API`, `test_web_docs_baseline.py`, `Web UI and HTTP API`, `Baseline`, `test_web_bands.py`, `level_class`, `links.py`, `datetime`, `test_web_baseline_view.py`, `test_reread.py`, `app.py`, `test_web_pipeline_view.py`, `test_web_retrain.py`, `to`, `modules`, `stress_modules.py`, `test_web_llm.py`, `test_web_modules.py`, `test_bands_tune.py`, `test_web_links.py`, `alert_rows`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `create_app()` (e.g. with `Read first` and `AppConfig`) actually correct?**
  _`create_app()` has 4 INFERRED edges - model-reasoned connections that need verification._
- **What connects `runv.sh script`, `DatabaseConfig`, `RetrainConfig` to the rest of the system?**
  _625 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `FakeSource` be split into smaller, more focused modules?**
  _Cohesion score 0.06747474747474748 - nodes in this community are weakly interconnected._
- **Why does `ServiceData` connect `ServiceData` to `Baseline Bake-off on the laptop: implementation plan (tasks 0–17)`, `apply_d4`, `Baseline bake-off on the laptop: detailed spec`, `test_lab_inject.py`, `test_lab_sigma.py`, `incident_test.py`, `minute_at`, `datetime`, `DayOut`, `test_lab_run.py`, `pytest`, `Read first`, `test_lab_score.py`, `score.py`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Are the 52 inferred relationships involving `ServiceData` (e.g. with `18. Lab (bake-off)` and `3.6 Tests`) actually correct?**
  _`ServiceData` has 52 INFERRED edges - model-reasoned connections that need verification._
- **Should `embed` be split into smaller, more focused modules?**
  _Cohesion score 0.08521870286576169 - nodes in this community are weakly interconnected._