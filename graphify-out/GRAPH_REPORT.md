# Graph Report - logai2  (2026-10-23)

## Corpus Check
- 128 files · ~165,993 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 4, .css 1, .ini 1)

## Summary
- 2514 nodes · 7585 edges · 121 communities (111 shown, 10 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 1308 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `34e3a4c6`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- tmpl
- FakeSource
- judge
- linker.py
- create_app
- Engine
- test_web_alerts_templates.py
- templates.js
- incident_test.py
- Life of a task
- documentation.js
- test_es_source.py
- Miners
- sources.js
- i
- echarts.min.js
- normalize.py
- Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture
- TestUniversalMaskingRules
- pytest
- normalize
- Reader
- review.js
- test_links.py
- Task 5: Drain3 per service
- periods
- config.py
- Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance
- module_alerts
- _l
- detect.py
- HTTP API
- link_eval.py
- ui.js
- alerting.js
- test_web_docs_baseline.py
- logai2 core engine + web v1: design
- Design
- commit
- Baseline
- db.py
- pipeline.js
- logai2
- Web UI and HTTP API
- Runner
- llm.js
- Incident test, 2026-10-20: smoke test
- .run
- verdict
- resolver.py
- connect
- Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)
- hit
- logai2 — instructions for Claude sessions
- test_web_baseline_view.py
- agy-task.sh
- baseline.js
- E
- AppConfig
- logai2 Architecture
- D
- app.py
- Baseline and Review: grouped key picker and combined series
- links.py
- a
- qn
- Task 3: Elasticsearch source (search, paging, hit → Line)
- Dm
- alert_rows
- Owner
- mx
- U
- Y
- fv
- Document links smoke test on real data (2026-10-21)
- Xy
- test_web_links.py
- time
- A
- xu
- test_incident_test.py
- es_source.py
- 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)
- te
- P
- logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào)
- Xd
- z
- data_until
- Dw
- bp
- engine.py
- eS
- 8. Detection
- aw
- B
- kt
- vg
- T
- ia
- ic
- to
- Wt
- OpenMinutes
- logai2 Module Alert API Implementation Plan
- Task <id>: <title>
- E
- LLM resolver: every unlinked template, not only alerting or new ones
- Cx
- 5. Core data contracts
- .page
- logai2 Core Engine + Web v1 Implementation Plan
- PR: task/<id>-<slug>
- 3B. Baseline page (`#baseline`, charts only)
- json
- make_source
- re

## God Nodes (most connected - your core abstractions)
1. `E()` - 119 edges
2. `create_app()` - 101 edges
3. `i()` - 83 edges
4. `a()` - 73 edges
5. `o()` - 71 edges
6. `n()` - 69 edges
7. `r()` - 67 edges
8. `T()` - 66 edges
9. `U()` - 66 edges
10. `e()` - 66 edges

## Surprising Connections (you probably didn't know these)
- `Task 12: Alerting and Templates pages` --references--> `alert_rows()`  [INFERRED]
  docs/plans/2026-10-20-core-engine-v1.md → logai2/alerts.py
- `Task 3: Shared key picker on Baseline and Review` --references--> `root()`  [INFERRED]
  docs/plans/2026-10-22-baseline-review-key-picker.md → logai2/alerts.py
- `Read first` --references--> `WebConfig`  [INFERRED]
  docs/tasks/43-module-route.md → logai2/config.py
- `10.1 Start` --references--> `migrate()`  [INFERRED]
  ARCHITECTURE.md → logai2/db.py
- `API` --references--> `class_sql()`  [INFERRED]
  docs/specs/2026-10-21-templates-page-redesign.md → logai2/links.py

## Import Cycles
- None detected.

## Communities (121 total, 10 thin omitted)

### Community 0 - "tmpl"
Cohesion: 0.26
Nodes (23): tmpl(), vec(), alerting(), Chat, profile(), row(), run(), test_a_dead_profile_fails_over_to_the_next_and_is_skipped_for_a_while() (+15 more)

### Community 1 - "FakeSource"
Cohesion: 0.06
Nodes (76): 10.5 `worker_status`, Facts from the current code the tasks rely on, 4.1 Status line, 4.2 Topology (ECharts `graph`, fixed layout), 4.3 Lag and throughput, last 60 minutes (ECharts line), 4.4 Volume vs expected, per service (ECharts line, small multiples), 4.5 Services table (HTML), 4.6 Engine and API changes (+68 more)

### Community 2 - "judge"
Cohesion: 0.16
Nodes (18): Baseline, Review and Pipeline Pages, Watchdog Style — Implementation Plan, Global Constraints, judge(), Lookup, Stat, Verdict, test_drop_rules(), test_floor_only_without_baseline() (+10 more)

### Community 3 - "linker.py"
Cohesion: 0.08
Nodes (21): Task 5: Linker process (`linker.py`, `scripts/run_linker.py`), embed(), EmbeddingError, literal(), _unit(), Linker, axis(), embeddings_handler() (+13 more)

### Community 4 - "create_app"
Cohesion: 0.10
Nodes (50): Read first, enqueue(), BadRequest, create_app(), activate_llm_profile(), add_decision(), automatic_link(), baseline_incident() (+42 more)

### Community 5 - "Engine"
Cohesion: 0.19
Nodes (5): Task 8: Engine and resolver status fields, get_setting(), _day(), Engine, local()

### Community 6 - "test_web_alerts_templates.py"
Cohesion: 0.11
Nodes (24): Global Constraints, Review Focus, Task 1: API — `link_source=review` and `review_total`, Task 4: Docs and wrap-up, Templates Page Redesign Implementation Plan, day_hour(), daytype(), minute_of() (+16 more)

### Community 7 - "templates.js"
Cohesion: 0.05
Nodes (58): Task 2: Toolbar mode and the 5-column table, Task 3: Decision-first drawer and Next item, Accessibility (UI UX Pro Max checks), API, Docs, Drawer, Goal, Layout (+50 more)

### Community 8 - "incident_test.py"
Cohesion: 0.16
Nodes (13): alert_onsets(), check1_deadline(), doc_key_state(), doc_row_state(), main(), onset(), parse_metrics(), _phase() (+5 more)

### Community 9 - "Life of a task"
Cohesion: 0.15
Nodes (13): Branches and worktrees, Gemini 3.8 Flash (Antigravity CLI), How the tech lead runs each implementer, Ledger, Life of a task, Questions go up, never sideways, Review gates, Roles (+5 more)

### Community 10 - "documentation.js"
Cohesion: 0.16
Nodes (31): decide(), deleteDocument(), $docList, docMatches(), $docPane, docs, $docSearch, $docService (+23 more)

### Community 11 - "test_es_source.py"
Cohesion: 0.11
Nodes (24): hit_to_line(), AuthenticationException, AuthorizationException, BadRequestError, line_of(), NotFoundError, test_hit_to_line_fields(), test_level_field_is_normalised() (+16 more)

### Community 12 - "Miners"
Cohesion: 0.14
Nodes (9): Drain3Config, Miners, _match(), TestLogMasking, test_ids_are_per_service(), test_placeholder_display(), test_state_round_trip(), test_generalized_template_survives_restore() (+1 more)

### Community 13 - "sources.js"
Cohesion: 0.12
Nodes (24): conn, connCluster(), $connError, $connForm, $connHosts, connMovesCluster(), $connPassword, connSave() (+16 more)

### Community 14 - "i"
Cohesion: 0.06
Nodes (58): e(), Ci(), n(), dc(), dd(), ea(), en(), u() (+50 more)

### Community 15 - "echarts.min.js"
Cohesion: 0.03
Nodes (29): bw(), bz(), co(), ct(), dx(), ho(), HS(), Hz() (+21 more)

### Community 16 - "normalize.py"
Cohesion: 0.12
Nodes (8): _collapse(), find_level(), _is_prefix_token(), _mask_hex(), _mask_long_id(), _strip_leading_timestamp(), _xml_tag(), TestFindLevel

### Community 17 - "Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture"
Cohesion: 0.13
Nodes (15): 1.1 The Current Problem: Visual Disconnect and Operator Friction, 1.2 The Proposed Solution: Strategy A (Unified Apache ECharts), 1. Executive Summary & Problem Statement, 2. Core Architectural & Design Principles, 3.1 Why Apache ECharts Over Disjointed Micro-Libraries?, 3. Technology Decision: Strategy A (Unified Apache ECharts Engine), 4. End-to-End Visual Workflow & User Experience, 6.1 Extended Baseline Endpoint (`GET /api/baseline?service=...`) (+7 more)

### Community 19 - "pytest"
Cohesion: 0.12
Nodes (16): chat_json(), endpoint(), LlmError, LlmUnavailable, _NoRedirect, parse_json_reply(), _timed_out(), profile() (+8 more)

### Community 20 - "normalize"
Cohesion: 0.20
Nodes (3): normalize(), TestNormalize, TestNormalizeMissingParts

### Community 21 - "Reader"
Cohesion: 0.17
Nodes (5): 6. Reader, KnownLines, Line, Reader, test_known_lines_lru()

### Community 22 - "review.js"
Cohesion: 0.07
Nodes (68): Task 4: ECharts, chart helper, chart colour tokens, Task 5: Review page UI (decisions), 2. ECharts in this console, baseOption(), _chartObservers, chartTokens(), disposeCharts(), currentTheme() (+60 more)

### Community 23 - "test_links.py"
Cohesion: 0.22
Nodes (18): apply_links(), candidates(), cluster_edges(), test_llm_table_scores_the_confirm_answers(), at(), doc(), docs(), link() (+10 more)

### Community 24 - "Task 5: Drain3 per service"
Cohesion: 0.15
Nodes (4): 7.1 Templates, Task 5: Drain3 per service, _BlobPersistence, display_template()

### Community 25 - "periods"
Cohesion: 0.11
Nodes (21): Trường của mỗi template, Done when, Files, Interfaces, Read first, Requirements, Task 42: window parser and the module query, Tests (write first, see them fail) (+13 more)

### Community 26 - "config.py"
Cohesion: 0.10
Nodes (19): Task 1: Skeleton, config, database and schema, BaselineConfig, DatabaseConfig, DocumentationConfig, DropConfig, EmbeddingConfig, FlushConfig, load_config() (+11 more)

### Community 27 - "Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance"
Cohesion: 0.14
Nodes (14): 1. Executive Summary & Problem Statement, 2. Core Architectural Principles, 3. End-to-End Processing Workflow, 4. Trigger Criteria & Anti-Spam Filtering, 5.1 Context Payload, 5.2 Structured Prompt, 5. Context Assembly & Prompt Engineering, 6. Database Schema Evolution (+6 more)

### Community 28 - "module_alerts"
Cohesion: 0.10
Nodes (66): _day(), module_alerts(), parse_window(), _to_seconds(), basic(), pw(), test_alerting_cooling_alerting_two_periods(), test_backdated_start_before_window_before_row() (+58 more)

### Community 29 - "_l"
Cohesion: 0.15
Nodes (24): ap(), cp(), dp(), Dy(), ep(), fp(), hp(), I() (+16 more)

### Community 30 - "detect.py"
Cohesion: 0.25
Nodes (9): Task 8: Detection and alert state, AlertConfig, ClosedMinute, AlertRow, Change, step(), _worst(), test_step_matches_logai_engine_sequence() (+1 more)

### Community 31 - "HTTP API"
Cohesion: 0.06
Nodes (34): 11. Postgres schema, 4. Writing documentation: by hand, with an AI chat alongside, 4a. LLM confirm or suggest, reviewed by a person, Task 42: window parser and the module query, Task 10: Pipeline page UI, Decisions (confirmed 2026-10-21 unless marked), Task 3: Embeddings client (`embed.py`), 5. Web (+26 more)

### Community 32 - "link_eval.py"
Cohesion: 0.30
Nodes (11): active_profiles(), corpus_top2(), layer2_table(), pick(), llm_table(), main(), nearest_manual(), score() (+3 more)

### Community 33 - "ui.js"
Cohesion: 0.26
Nodes (10): confirmDialog(), focusables(), modalObserver, modalOpeners, notifyDialog(), onModalClosed(), onModalOpened(), openModals() (+2 more)

### Community 34 - "alerting.js"
Cohesion: 0.24
Nodes (11): al, ALERT_STATES, $alertCards, alLine(), alLink(), alNormal(), alNum(), alTitle() (+3 more)

### Community 35 - "test_web_docs_baseline.py"
Cohesion: 0.18
Nodes (4): add_days(), client(), test_decision_warns_under_3_days(), test_warning_counts_only_the_days_the_rebuild_uses()

### Community 36 - "logai2 core engine + web v1: design"
Cohesion: 0.20
Nodes (10): 2. Data flow, 3. Detection, 4. Postgres schema (`001_init.sql`), 6. Error handling, 7. Configuration, 8. Testing, Decisions, logai2 core engine + web v1: design (+2 more)

### Community 37 - "Design"
Cohesion: 0.11
Nodes (18): 1. Templates per service, 2. Detection: statistical baseline, no training, 3. From alert to document: templates link directly (option D, confirmed), 5. Pipeline: Elasticsearch → engine, one process first, 6. Storage: PostgreSQL only, 7. Module alert API, 8. Starting on a running server (v1: sampled catch-up), 9. Operator console, Datadog Watchdog style (sub-project 9) (+10 more)

### Community 38 - "commit"
Cohesion: 0.27
Nodes (10): commit(), baseline_rows(), seed(), test_commit_is_idempotent_and_catches_up_missed_days(), test_commit_keeps_last_5_weekdays_and_skips_today(), test_commit_mean_std_and_silent_minutes(), test_exclusion_removes_hour(), test_failed_commit_rolls_back_everything() (+2 more)

### Community 39 - "Baseline"
Cohesion: 0.39
Nodes (12): Task 9: Pipeline API: services, volume, stages, Baseline, Detector, cm(), test_burst_alerts_and_recovers(), test_doc_key_band_is_sum_of_linked_bands(), test_doc_key_sums_linked_templates(), test_historic_minute_changes_nothing() (+4 more)

### Community 40 - "db.py"
Cohesion: 0.11
Nodes (14): 14. Known limits, Task 11: Web shell, Pipeline and Data sources pages, Task 7: Baseline band and daily commit, Run the UI, drop_partitions_before(), ensure_partitions(), migrate(), put_setting() (+6 more)

### Community 41 - "pipeline.js"
Cohesion: 0.12
Nodes (39): fetchPipeline(), pl, PL_MAIN, PL_NODES, plCard(), plClear(), plDisposeMini(), plDuration() (+31 more)

### Community 42 - "logai2"
Cohesion: 0.18
Nodes (11): Configuration, First start, How it works, Incident test, logai2, Module alert API, Repo layout, Requirements (+3 more)

### Community 44 - "Web UI and HTTP API"
Cohesion: 0.18
Nodes (11): Alerting, Baseline, Data sources, Implementation map, LLM profiles, Pipeline, Review, Security (+3 more)

### Community 46 - "llm.js"
Cohesion: 0.31
Nodes (7): llm, llmBanner(), $llmModal, llmStats(), llmStatusBadge(), $llmTbody, loadLlmPage()

### Community 47 - "Incident test, 2026-10-20: smoke test"
Cohesion: 0.25
Nodes (7): Checks, Incident test, 2026-10-20: smoke test, Inject errors and anomalies, Recovery, Targets, What is meaningful, Why B3 and B5 failed

### Community 48 - ".run"
Cohesion: 0.24
Nodes (8): Global Constraints, LLM Resolver for Every Unlinked Template, No Call Limits — Implementation Plan, Review Focus, Task 1: Remove the alerting / 24 h gate, Task 2: Limits default to 0 = no limit, Task 3: Web shows "no limit", Task 4: Merge, deploy, watch the backlog, LlmConfig

### Community 49 - "verdict"
Cohesion: 0.12
Nodes (17): Edge cases (each has a test), File map, Global Constraints, logai2 Embeddings, Document Links and LLM Resolution Implementation Plan, Out of scope, Phase B: LLM confirm or suggest (absorbs sub-project 8), Review Focus, Task 10: LLM client (`llm.py`) (+9 more)

### Community 50 - "resolver.py"
Cohesion: 0.17
Nodes (6): next_doc_id(), Resolver, call(), _template(), _text(), chat()

### Community 51 - "connect"
Cohesion: 0.18
Nodes (7): connect(), Metrics, main(), main(), row(), test_row_back_to_normal_leaves_log_alert_state(), value()

### Community 52 - "Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)"
Cohesion: 0.15
Nodes (13): 1. Questions each page answers, 3.1 Header and cards, 3.2 Matrix: days × hours (ECharts heatmap), 3.3 Inspector: one Watchdog card for the selection, 3.4 Coverage: what the baseline is built from (ECharts heatmap), 3.5 Removed, 3.6 API, 3. Review page (`#review`, decisions) (+5 more)

### Community 53 - "hit"
Cohesion: 0.19
Nodes (20): hit(), make(), test_aggregates_and_dedups(), test_cache_hit_sends_template_id(), test_caught_up_flips_back_when_new_doc_arrives(), test_continuous_stream_still_catches_up(), test_cursors_use_boundary_but_not_before_floor(), test_failing_index_stops_holding_watermark() (+12 more)

### Community 54 - "logai2 — instructions for Claude sessions"
Cohesion: 0.50
Nodes (3): logai2 — instructions for Claude sessions, Project rules, Team workflow (when the user says to use it)

### Community 55 - "test_web_baseline_view.py"
Cohesion: 0.12
Nodes (43): Review Focus, Task 11: Docs, merge readiness, Task 1: Non-NORMAL minutes per hour (shared helper), Task 7: Baseline page UI (charts), Task 2: Web resolves `verdict:` keys and serves the new keys, base(), cell(), client() (+35 more)

### Community 60 - "baseline.js"
Cohesion: 0.07
Nodes (50): After the build, Global Constraints, Grouped key picker and combined series — Implementation Plan, Review Focus, Task 1: Engine counts `*warn` / `*info`, detector skips them, Task 3: Shared key picker on Baseline and Review, level_class(), createKeyPicker() (+42 more)

### Community 61 - "E"
Cohesion: 0.06
Nodes (50): af(), aR(), Cl(), Cn(), Dk(), Dl(), DO(), ef() (+42 more)

### Community 62 - "AppConfig"
Cohesion: 0.11
Nodes (29): Task 2: Review matrix and coverage API (`/api/baseline/matrix`), Task 3: Review inspector API (`/api/baseline/inspect`), Task 6: Baseline series and incident API, Derived keys: `verdict:action`, `verdict:safe`, Keys, Review page, Stored keys: `*warn`, `*info`, Tests (+21 more)

### Community 63 - "logai2 Architecture"
Cohesion: 0.13
Nodes (17): 10.1 Start, 10.2 Tick, 10.3 Commit timing, 10.4 Bootstrap and `days_back`, 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108), 10. Engine loop, 12.1 Settings keys, 12.2 Commands (+9 more)

### Community 64 - "D"
Cohesion: 0.17
Nodes (12): D(), Im(), iy(), ls(), mh(), mm(), Oo(), oy() (+4 more)

### Community 65 - "app.py"
Cohesion: 0.09
Nodes (15): iso(), _age(), _by_service(), service_data(), services(), _stage(), stage_states(), volume() (+7 more)

### Community 66 - "Baseline and Review: grouped key picker and combined series"
Cohesion: 0.25
Nodes (7): Baseline and Review: grouped key picker and combined series, Deploy, Docs, Goal, `incident()` for the new keys, Later, Out of scope

### Community 67 - "links.py"
Cohesion: 0.12
Nodes (17): 5.1 Document links, Phase A: embeddings and automatic links (no LLM), Task 1: Schema and config, Task 2: Engine touch points, Task 4: Links SQL (`links.py`), the core, Task 6: Alert clustering (`alerts.py`), Task 7: Web API, Task 8: Web views (Gemini-suited) (+9 more)

### Community 68 - "a"
Cohesion: 0.07
Nodes (111): A(), aA(), ac(), An(), ba(), bi(), bl(), bn() (+103 more)

### Community 69 - "qn"
Cohesion: 0.20
Nodes (12): BD(), Bs(), a(), gs(), Hn(), ii(), jn(), oi() (+4 more)

### Community 70 - "Task 3: Elasticsearch source (search, paging, hit → Line)"
Cohesion: 0.16
Nodes (7): Task 3: Elasticsearch source (search, paging, hit → Line), _coerce_message(), _extract_level(), _extract_level_from_message(), _extract_service(), _parse_timestamp(), _sort_seconds()

### Community 72 - "alert_rows"
Cohesion: 0.36
Nodes (6): alert_rows(), root(), st(), test_rows_group_by_document(), test_service_rows_split_by_service_and_doc_key_alone(), test_similar_undocumented_templates_become_one_cluster_row()

### Community 73 - "Owner"
Cohesion: 0.11
Nodes (16): 1. Purpose and scope, 7.3 The flush transaction, File map, Task 10: Engine loop, bootstrap, commands, metrics, Task 12: Alerting and Templates pages, Task 13: Documentation and Baseline pages, Task 14: Containers, recharge run and incident test, Task 2: Copied text processing (normalize) and its tests (+8 more)

### Community 74 - "mx"
Cohesion: 0.17
Nodes (15): Bx(), dE(), fe(), Gx(), Hx(), jx(), kg(), mx() (+7 more)

### Community 75 - "U"
Cohesion: 0.16
Nodes (15): ay(), bv(), fk(), hy(), mv(), Ql(), qm(), m() (+7 more)

### Community 76 - "Y"
Cohesion: 0.23
Nodes (11): Ad(), ed(), fD(), hB(), hw(), kd(), Ld(), nd() (+3 more)

### Community 77 - "fv"
Cohesion: 0.14
Nodes (16): cv(), dv(), ev(), fv(), gg(), gV(), hv(), kV() (+8 more)

### Community 78 - "Document links smoke test on real data (2026-10-21)"
Cohesion: 0.40
Nodes (4): Document links smoke test on real data (2026-10-21), Reading, Result, Setup

### Community 79 - "Xy"
Cohesion: 0.25
Nodes (9): Eh(), fR(), Lh(), Oh(), Ph(), Rh(), Uh(), xo() (+1 more)

### Community 80 - "test_web_links.py"
Cohesion: 0.21
Nodes (11): How it works (what the user confirms), Other behavior, The corpus document, The layers, client(), source(), test_a_manual_link_over_a_draft_rejects_the_draft(), test_apply_to_similar_lists_pre_ticks_and_links_in_one_request() (+3 more)

### Community 81 - "time"
Cohesion: 0.16
Nodes (4): client(), test_password_never_returned(), test_pipeline_stale_flag(), test_selection_round_trip_and_command()

### Community 82 - "A"
Cohesion: 0.07
Nodes (28): cA(), da(), fa(), ga(), gp(), Hh(), o(), it() (+20 more)

### Community 83 - "xu"
Cohesion: 0.20
Nodes (11): au(), BG(), bu(), cu(), du(), fg(), fu(), jg() (+3 more)

### Community 84 - "test_incident_test.py"
Cohesion: 0.26
Nodes (10): doc_row(), sample(), test_alert_onsets(), test_doc_row_and_doc_key_state(), test_match_template_fails_fast_on_none_or_several(), test_onset_carried_over_is_not_counted(), test_onset_inside_phase_and_check1_timing(), test_row_key() (+2 more)

### Community 85 - "es_source.py"
Cohesion: 0.25
Nodes (7): ElasticsearchConfig, ReaderConfig, _build_client(), EsSource, retry(), test_reconnect_builds_new_client(), test_resolve_and_list_indices()

### Community 86 - "5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)"
Cohesion: 0.22
Nodes (9): 5.1 The Baseline Matrix & Visual Band Inspector (`/#baseline`), 5.2 The Pipeline Data Stream Topology (`/#pipeline`), 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive), 5. Detailed UI/UX Specifications, Pillar 1: Watchdog Insights Banner & Outlier Classification, Pillar 2: Log Pattern Volume Delta (% Change vs. Baseline), Pillar 3: 1-Click Incident Marking & Baseline Exclusion Loop, Tier 1: Weekly Baseline Health Matrix (24 Hours $\times$ 7 Committed Days) (+1 more)

### Community 87 - "te"
Cohesion: 0.28
Nodes (9): ce(), ee(), er(), n(), he(), le(), te(), ue() (+1 more)

### Community 88 - "P"
Cohesion: 0.18
Nodes (11): go(), s(), HR(), id(), lF(), mB(), P(), tt() (+3 more)

### Community 89 - "logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào)"
Cohesion: 0.17
Nodes (9): 1. Gọi nhanh, 2. Xác thực, 4. Response `200`, 5. Khi nào một template được trả về, 7. Lỗi, 8. Ví dụ code, 9. Cách dùng khuyến nghị, logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào) (+1 more)

### Community 90 - "Xd"
Cohesion: 0.29
Nodes (7): bk(), Gd(), Hd(), Kr(), Wd(), Xd(), zd()

### Community 91 - "z"
Cohesion: 0.25
Nodes (9): Al(), io(), ml(), QO(), sy(), V(), z(), uy() (+1 more)

### Community 92 - "data_until"
Cohesion: 0.18
Nodes (11): 10. Hỏi đáp, 6. `data_until`: rỗng chưa chắc là "không có lỗi", Done when, Files, Requirements, Task 43: GET /api/module-alerts, optional token, docs, deploy config, Tests (write first, see them fail), Why (+3 more)

### Community 93 - "Dw"
Cohesion: 0.50
Nodes (5): Cw(), di(), Dw(), fi(), pi()

### Community 94 - "bp"
Cohesion: 0.40
Nodes (3): bp(), re(), wp()

### Community 95 - "engine.py"
Cohesion: 0.14
Nodes (3): finish(), take(), test_enqueue_take_finish()

### Community 96 - "eS"
Cohesion: 0.60
Nodes (5): eS(), Fw(), jw(), NS(), qw()

### Community 97 - "8. Detection"
Cohesion: 0.33
Nodes (6): 8.1 Keys per closed minute, 8.2 Floors and levels, 8.3 Band, 8.4 Historic minutes, 8.5 Hysteresis, 8. Detection

### Community 98 - "aw"
Cohesion: 0.50
Nodes (4): Ab(), aw(), Gt(), oW()

### Community 99 - "B"
Cohesion: 0.50
Nodes (4): at(), B(), gB(), z()

### Community 100 - "kt"
Cohesion: 0.40
Nodes (5): Dt(), ek(), Hu(), kt(), zt()

### Community 101 - "vg"
Cohesion: 0.22
Nodes (10): cc(), mt(), og(), Pn(), ut(), vg(), h(), p() (+2 more)

### Community 102 - "T"
Cohesion: 0.05
Nodes (52): ag(), Am(), ao(), bB(), bf(), c(), Cf(), cg() (+44 more)

### Community 103 - "ia"
Cohesion: 0.67
Nodes (3): ia(), oA(), rA()

### Community 104 - "ic"
Cohesion: 0.28
Nodes (9): Cr(), Dr(), eM(), Fm(), hc(), ic(), oc(), po() (+1 more)

### Community 105 - "to"
Cohesion: 0.15
Nodes (19): 3. Request, Làm tròn: độ phân giải 1 phút, mở rộng ra ngoài, Phạm vi, Định dạng thời gian (`from`, `to`), 10. Decisions (2026-10-22, user), 1. Input data (checked on a real `udcntt*` document), 2. Request, 3. What counts as "alerting" (+11 more)

### Community 106 - "Wt"
Cohesion: 0.25
Nodes (11): be(), bh(), Fh(), Gn(), Ie(), Se(), vi(), we() (+3 more)

### Community 107 - "OpenMinutes"
Cohesion: 0.23
Nodes (9): 7.2 Open and closed minutes, 7. Owner and exactly-once flush, OpenMinutes, test_close_includes_empty_minutes_and_service_keys(), test_first_start_backlog_is_linear(), test_late_count_is_dropped(), test_level_class_keys_add_up_to_total(), test_levels_error_keys_and_most_severe() (+1 more)

### Community 108 - "logai2 Module Alert API Implementation Plan"
Cohesion: 0.18
Nodes (11): Finish, logai2 Module Alert API Implementation Plan, Out of scope (spec §9), Part A — Execution with Opus and Sonnet, Part B — Tasks, Per-task loop (tech lead), Review focus (task reviewers and the final review), Roles (+3 more)

### Community 109 - "Task <id>: <title>"
Cohesion: 0.18
Nodes (8): Done when, Files, Interfaces, Read first, Requirements, Task <id>: <title>, Tests (write first, see them fail), Why

### Community 110 - "E"
Cohesion: 0.19
Nodes (13): br(), E(), Ez(), k(), kh(), oS(), R(), tr() (+5 more)

### Community 111 - "LLM resolver: every unlinked template, not only alerting or new ones"
Cohesion: 0.25
Nodes (7): Change, Deploy, Docs (with the code, since ARCHITECTURE describes what is implemented), How a template that was never seen before is handled (for context), LLM resolver: every unlinked template, not only alerting or new ones, Problem, Tests (`tests/test_resolver.py`)

### Community 112 - "Cx"
Cohesion: 0.67
Nodes (3): Ax(), Cx(), kx()

### Community 113 - "5. Core data contracts"
Cohesion: 0.28
Nodes (5): 3. Configuration, 5. Core data contracts, Task 4: Reader (cursors, dedup, cache, MinuteBatch), Count, MinuteBatch

### Community 114 - ".page"
Cohesion: 0.22
Nodes (5): 4.1 Elasticsearch document, 4.2 Query, paging and the millisecond re-read, 4.3 Index selection and connection, 4.4 Documentation seed, 4. External input contract

### Community 115 - "logai2 Core Engine + Web v1 Implementation Plan"
Cohesion: 0.40
Nodes (4): Global Constraints, logai2 Core Engine + Web v1 Implementation Plan, Review Focus, Spec coverage check

### Community 116 - "PR: task/<id>-<slug>"
Cohesion: 0.22
Nodes (9): Deviations from the brief, Files changed, Fix rounds (implementer appends), How it was tested, Open questions / concerns, PR: task/<id>-<slug>, Review (reviewer appends), Summary (+1 more)

### Community 117 - "3B. Baseline page (`#baseline`, charts only)"
Cohesion: 0.25
Nodes (8): 3B.1 Header and cards, 3B.2 Actual vs expected (ECharts line), 3B.3 Possible incidents, 3B.4 Incident detail, 3B.5 Days × hours (read-only), 3B.6 The baseline itself (ECharts line), 3B.7 API, 3B. Baseline page (`#baseline`, charts only)

### Community 119 - "make_source"
Cohesion: 0.33
Nodes (7): bodies(), make_source(), test_empty_response_is_empty_list(), test_full_page_past_from_ms_is_not_reread(), test_millisecond_beyond_largest_page_falls_back_to_search_after(), test_page_query_shape(), test_page_reads_whole_millisecond()

## Knowledge Gaps
- **266 isolated node(s):** `DatabaseConfig`, `FlushConfig`, `StartConfig`, `BaselineConfig`, `DropConfig` (+261 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 627 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `db()` connect `logai2 Architecture` to `a`, `T`, `db.py`, `Owner`, `echarts.min.js`?**
  _High betweenness centrality (0.152) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `E()` (e.g. with `I()` and `T()`) actually correct?**
  _`E()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `DatabaseConfig`, `FlushConfig`, `StartConfig` to the rest of the system?**
  _266 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `FakeSource` be split into smaller, more focused modules?**
  _Cohesion score 0.06074561403508772 - nodes in this community are weakly interconnected._
- **Why does `to()` connect `to` to `D`, `a`, `logai2 Module Alert API Implementation Plan`, `i`, `echarts.min.js`, `Cx`, `logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào)`, `data_until`, `HTTP API`?**
  _High betweenness centrality (0.148) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `create_app()` (e.g. with `Read first` and `AppConfig`) actually correct?**
  _`create_app()` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Should `linker.py` be split into smaller, more focused modules?**
  _Cohesion score 0.0824829931972789 - nodes in this community are weakly interconnected._