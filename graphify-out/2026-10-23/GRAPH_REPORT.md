# Graph Report - logai2  (2026-10-23)

## Corpus Check
- 129 files · ~166,428 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 4, .css 1, .ini 1)

## Summary
- 2528 nodes · 7615 edges · 116 communities (108 shown, 8 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 1317 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `22f681ac`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- tmpl
- FakeSource
- Linker
- embed
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
- i
- echarts.min.js
- normalize.py
- Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture
- TestUniversalMaskingRules
- pytest
- normalize
- test_owner.py
- review.js
- test_links.py
- Task 5: Drain3 per service
- periods
- config.py
- Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance
- module_alerts
- find_level
- Baseline
- HTTP API
- link_eval.py
- ui.js
- alerting.js
- test_web_docs_baseline.py
- logai2 core engine + web v1: design
- Design
- keypicker.js
- File map
- engine.py
- pipeline.js
- logai2
- Web UI and HTTP API
- Review Focus
- llm.js
- Incident test, 2026-10-20: smoke test
- commit
- .reload_links
- Resolver
- main.js
- hit
- logai2 — instructions for Claude sessions
- test_web_baseline_view.py
- agy-task.sh
- baseline.js
- E
- AppConfig
- X
- app.py
- Task 3: Elasticsearch source (search, paging, hit → Line)
- links.py
- h
- n
- .run
- commands.py
- run_engine.py
- Task 41: engine — a module per service, and the streak's first minute
- mx
- U
- bf
- fv
- Document links smoke test on real data (2026-10-21)
- Per-service templates, statistical baselines, document-centric alerts
- owner.py
- A
- xu
- test_incident_test.py
- q
- es_source.py
- 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)
- Xd
- hit_to_line
- Dw
- H
- eS
- alert_rows
- aw
- fb
- xC
- j
- ia
- o
- to
- yi
- 4. Pipeline page
- T
- LLM resolver: every unlinked template, not only alerting or new ones
- Cx
- OpenMinutes
- rvRenderAll
- logai2 Core Engine + Web v1 Implementation Plan
- test_web_links.py
- gF
- make_source
- kl
- `GET /api/pipeline`
- json
- px
- gV
- lg
- co

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
- `Run the UI` --references--> `migrate()`  [INFERRED]
  docs/WEB_UI.md → logai2/db.py

## Import Cycles
- None detected.

## Communities (116 total, 8 thin omitted)

### Community 0 - "tmpl"
Cohesion: 0.21
Nodes (25): Task 1: Remove the alerting / 24 h gate, LlmError, tmpl(), vec(), alerting(), Chat, profile(), row() (+17 more)

### Community 1 - "FakeSource"
Cohesion: 0.14
Nodes (32): put_setting(), FakeSource, catch_up(), Clock, history(), make(), build(), sleep() (+24 more)

### Community 2 - "Linker"
Cohesion: 0.13
Nodes (11): B3. One rejected document or template blocks the linker, Linker, FakeHttp, make(), q(), server(), test_a_text_changed_meanwhile_is_not_overwritten(), embed_then_change() (+3 more)

### Community 3 - "embed"
Cohesion: 0.14
Nodes (12): embed(), EmbeddingError, literal(), _unit(), axis(), embeddings_handler(), handler(), server() (+4 more)

### Community 4 - "create_app"
Cohesion: 0.09
Nodes (51): Task 11: Web shell, Pipeline and Data sources pages, Read first, enqueue(), next_doc_id(), BadRequest, create_app(), activate_llm_profile(), add_decision() (+43 more)

### Community 5 - "Engine"
Cohesion: 0.14
Nodes (7): 7.3 The flush transaction, Task 8: Engine and resolver status fields, get_setting(), _day(), Engine, Owner, test_text_change_clears_the_vector_and_only_alerting_links_load()

### Community 6 - "test_web_alerts_templates.py"
Cohesion: 0.11
Nodes (25): Global Constraints, Review Focus, Task 1: API — `link_source=review` and `review_total`, Task 4: Docs and wrap-up, Templates Page Redesign Implementation Plan, day_hour(), daytype(), local() (+17 more)

### Community 7 - "templates.js"
Cohesion: 0.05
Nodes (58): Task 2: Toolbar mode and the 5-column table, Task 3: Decision-first drawer and Next item, Accessibility (UI UX Pro Max checks), API, Docs, Drawer, Goal, Layout (+50 more)

### Community 8 - "incident_test.py"
Cohesion: 0.11
Nodes (16): alert_onsets(), check1_deadline(), doc_key_state(), doc_row_state(), http(), main(), match_template(), onset() (+8 more)

### Community 9 - "PR: task/<id>-<slug>"
Cohesion: 0.06
Nodes (30): Branches and worktrees, Gemini 3.8 Flash (Antigravity CLI), How the tech lead runs each implementer, Ledger, Life of a task, Questions go up, never sideways, Review gates, Roles (+22 more)

### Community 10 - "documentation.js"
Cohesion: 0.16
Nodes (31): decide(), deleteDocument(), $docList, docMatches(), $docPane, docs, $docSearch, $docService (+23 more)

### Community 11 - "test_es_source.py"
Cohesion: 0.15
Nodes (16): AuthenticationException, AuthorizationException, BadRequestError, line_of(), NotFoundError, test_level_field_is_normalised(), test_level_from_message_prefix(), test_malformed_hits_are_none() (+8 more)

### Community 12 - "Miners"
Cohesion: 0.14
Nodes (9): Drain3Config, Miners, _match(), TestLogMasking, test_ids_are_per_service(), test_placeholder_display(), test_state_round_trip(), test_generalized_template_survives_restore() (+1 more)

### Community 13 - "sources.js"
Cohesion: 0.12
Nodes (24): conn, connCluster(), $connError, $connForm, $connHosts, connMovesCluster(), $connPassword, connSave() (+16 more)

### Community 14 - "i"
Cohesion: 0.06
Nodes (70): A(), aA(), ba(), e(), Ci(), n(), dc(), dd() (+62 more)

### Community 15 - "echarts.min.js"
Cohesion: 0.03
Nodes (26): cs(), ct(), HS(), Hz(), Is(), jf(), Lb(), Mk() (+18 more)

### Community 16 - "normalize.py"
Cohesion: 0.17
Nodes (6): _collapse(), _is_prefix_token(), _mask_hex(), _mask_long_id(), _strip_leading_timestamp(), _xml_tag()

### Community 17 - "Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture"
Cohesion: 0.13
Nodes (15): 1.1 The Current Problem: Visual Disconnect and Operator Friction, 1.2 The Proposed Solution: Strategy A (Unified Apache ECharts), 1. Executive Summary & Problem Statement, 2. Core Architectural & Design Principles, 3.1 Why Apache ECharts Over Disjointed Micro-Libraries?, 3. Technology Decision: Strategy A (Unified Apache ECharts Engine), 4. End-to-End Visual Workflow & User Experience, 6.1 Extended Baseline Endpoint (`GET /api/baseline?service=...`) (+7 more)

### Community 19 - "pytest"
Cohesion: 0.14
Nodes (14): chat_json(), endpoint(), _NoRedirect, parse_json_reply(), _timed_out(), profile(), reply(), server() (+6 more)

### Community 20 - "normalize"
Cohesion: 0.20
Nodes (3): normalize(), TestNormalize, TestNormalizeMissingParts

### Community 21 - "test_owner.py"
Cohesion: 0.16
Nodes (29): Facts from the current code the tasks rely on, Read first, _more_severe(), _alert_minutes(), counts(), _high_setup(), lines(), make() (+21 more)

### Community 22 - "review.js"
Cohesion: 0.14
Nodes (38): rv, RV_DOW, RV_MON, rvBindMatrix(), rvCard(), rvCellTip(), rvChangeRoute(), rvCloseInspector() (+30 more)

### Community 23 - "test_links.py"
Cohesion: 0.26
Nodes (16): apply_links(), cluster_edges(), at(), doc(), docs(), link(), links(), test_corpus_link_needs_threshold_and_margin_and_an_active_document() (+8 more)

### Community 24 - "Task 5: Drain3 per service"
Cohesion: 0.15
Nodes (4): 7.1 Templates, Task 5: Drain3 per service, _BlobPersistence, display_template()

### Community 25 - "periods"
Cohesion: 0.12
Nodes (20): Done when, Files, Interfaces, Read first, Requirements, Task 42: window parser and the module query, Tests (write first, see them fail), Why (+12 more)

### Community 26 - "config.py"
Cohesion: 0.10
Nodes (20): BaselineConfig, DatabaseConfig, DocumentationConfig, DropConfig, EmbeddingConfig, FlushConfig, load_config(), _merge_dataclass() (+12 more)

### Community 27 - "Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance"
Cohesion: 0.14
Nodes (14): 1. Executive Summary & Problem Statement, 2. Core Architectural Principles, 3. End-to-End Processing Workflow, 4. Trigger Criteria & Anti-Spam Filtering, 5.1 Context Payload, 5.2 Structured Prompt, 5. Context Assembly & Prompt Engineering, 6. Database Schema Evolution (+6 more)

### Community 28 - "module_alerts"
Cohesion: 0.10
Nodes (66): _day(), module_alerts(), parse_window(), _to_seconds(), basic(), pw(), test_alerting_cooling_alerting_two_periods(), test_backdated_start_before_window_before_row() (+58 more)

### Community 30 - "Baseline"
Cohesion: 0.06
Nodes (62): 13. Failure handling, 14. Known limits, 16. Reading the code, 17. When to update this document, 2. System context, 3. Configuration, 4.1 Elasticsearch document, 4.2 Query, paging and the millisecond re-read (+54 more)

### Community 31 - "HTTP API"
Cohesion: 0.06
Nodes (35): 11. Postgres schema, 15. Where the code departs from the spec, 9. Daily commit and review exclusions, Task 3: Embeddings client (`embed.py`), Baseline and Review: grouped key picker and combined series, Deploy, Docs, Goal (+27 more)

### Community 32 - "link_eval.py"
Cohesion: 0.24
Nodes (13): candidates(), active_profiles(), corpus_top2(), layer2_table(), pick(), llm_table(), main(), nearest_manual() (+5 more)

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
Cohesion: 0.17
Nodes (12): 1. Repo layout, 2. Data flow, 3. Detection, 4. Postgres schema (`001_init.sql`), 5. Web, 6. Error handling, 7. Configuration, 8. Testing (+4 more)

### Community 37 - "Design"
Cohesion: 0.17
Nodes (13): 1. Templates per service, 2. Detection: statistical baseline, no training, 3. From alert to document: templates link directly (option D, confirmed), 4. Writing documentation: by hand, with an AI chat alongside, 4a. LLM confirm or suggest, reviewed by a person, 5. Pipeline: Elasticsearch → engine, one process first, 6. Storage: PostgreSQL only, 7. Module alert API (+5 more)

### Community 38 - "keypicker.js"
Cohesion: 0.20
Nodes (17): createKeyPicker(), choose(), options(), render(), renderGroups(), renderList(), setOpen(), keyGroup() (+9 more)

### Community 39 - "File map"
Cohesion: 0.07
Nodes (25): 10.1 Start, 10.2 Tick, 10.3 Commit timing, 10.4 Bootstrap and `days_back`, 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108), 10. Engine loop, 6. Reader, B1. A value Postgres can never store stalls the engine forever (+17 more)

### Community 40 - "engine.py"
Cohesion: 0.12
Nodes (12): Task 1: Skeleton, config, database and schema, Task 7: Baseline band and daily commit, connect(), drop_partitions_before(), ensure_partitions(), migrate(), StaleRevision, conn() (+4 more)

### Community 41 - "pipeline.js"
Cohesion: 0.12
Nodes (39): fetchPipeline(), pl, PL_MAIN, PL_NODES, plCard(), plClear(), plDisposeMini(), plDuration() (+31 more)

### Community 42 - "logai2"
Cohesion: 0.18
Nodes (11): Configuration, First start, How it works, Incident test, logai2, Module alert API, Repo layout, Requirements (+3 more)

### Community 44 - "Web UI and HTTP API"
Cohesion: 0.17
Nodes (12): Alerting, Baseline, Data sources, Implementation map, LLM profiles, Pipeline, Review, Run the UI (+4 more)

### Community 45 - "Review Focus"
Cohesion: 0.22
Nodes (8): Review Focus, Task 10: Pipeline page UI, Task 11: Docs, merge readiness, Task 1: Non-NORMAL minutes per hour (shared helper), Task 7: Baseline page UI (charts), Task 9: Pipeline API: services, volume, stages, Documentation, stale()

### Community 46 - "llm.js"
Cohesion: 0.31
Nodes (7): llm, llmBanner(), $llmModal, llmStats(), llmStatusBadge(), $llmTbody, loadLlmPage()

### Community 47 - "Incident test, 2026-10-20: smoke test"
Cohesion: 0.25
Nodes (7): Checks, Incident test, 2026-10-20: smoke test, Inject errors and anomalies, Recovery, Targets, What is meaningful, Why B3 and B5 failed

### Community 48 - "commit"
Cohesion: 0.32
Nodes (10): commit(), baseline_rows(), seed(), test_commit_is_idempotent_and_catches_up_missed_days(), test_commit_keeps_last_5_weekdays_and_skips_today(), test_commit_mean_std_and_silent_minutes(), test_exclusion_removes_hour(), test_failed_commit_rolls_back_everything() (+2 more)

### Community 49 - ".reload_links"
Cohesion: 0.10
Nodes (20): 12.1 Settings keys, 12.2 Commands, 12. Web ↔ engine contract, 1. Purpose and scope, Edge cases (each has a test), File map, Global Constraints, How it works (what the user confirms) (+12 more)

### Community 50 - "Resolver"
Cohesion: 0.18
Nodes (7): 5.1 Document links, LlmUnavailable, Resolver, call(), _template(), _text(), chat()

### Community 52 - "main.js"
Cohesion: 0.05
Nodes (39): Task 4: ECharts, chart helper, chart colour tokens, Task 5: Review page UI (decisions), 1. Questions each page answers, 2. ECharts in this console, 3.1 Header and cards, 3.2 Matrix: days × hours (ECharts heatmap), 3.3 Inspector: one Watchdog card for the selection, 3.4 Coverage: what the baseline is built from (ECharts heatmap) (+31 more)

### Community 53 - "hit"
Cohesion: 0.14
Nodes (23): KnownLines, Reader, hit(), make(), test_aggregates_and_dedups(), test_cache_hit_sends_template_id(), test_caught_up_flips_back_when_new_doc_arrives(), test_continuous_stream_still_catches_up() (+15 more)

### Community 54 - "logai2 — instructions for Claude sessions"
Cohesion: 0.50
Nodes (3): logai2 — instructions for Claude sessions, Project rules, Team workflow (when the user says to use it)

### Community 55 - "test_web_baseline_view.py"
Cohesion: 0.13
Nodes (41): Review Focus, Task 2: Web resolves `verdict:` keys and serves the new keys, base(), cell(), client(), committed(), hours(), low_case() (+33 more)

### Community 60 - "baseline.js"
Cohesion: 0.12
Nodes (29): Task 3: Shared key picker on Baseline and Review, bl, BL_STATE, BL_STATE_SUB, blApplyRoute(), blChangeRoute(), blClear(), blDecision() (+21 more)

### Community 61 - "E"
Cohesion: 0.07
Nodes (51): ap(), aR(), Cl(), cp(), Dk(), DO(), dp(), Dy() (+43 more)

### Community 62 - "AppConfig"
Cohesion: 0.13
Nodes (27): Task 2: Review matrix and coverage API (`/api/baseline/matrix`), Task 3: Review inspector API (`/api/baseline/inspect`), Task 6: Baseline series and incident API, Derived keys: `verdict:action`, `verdict:safe`, Keys, Review page, Tests, View only (+19 more)

### Community 64 - "X"
Cohesion: 0.25
Nodes (9): fk(), Ks(), mh(), mw(), qm(), tC(), vx(), X() (+1 more)

### Community 65 - "app.py"
Cohesion: 0.10
Nodes (14): iso(), _age(), _by_service(), service_data(), services(), _stage(), stage_states(), volume() (+6 more)

### Community 66 - "Task 3: Elasticsearch source (search, paging, hit → Line)"
Cohesion: 0.13
Nodes (7): Task 3: Elasticsearch source (search, paging, hit → Line), _coerce_message(), _extract_level(), _extract_level_from_message(), _extract_service(), _parse_timestamp(), _sort_seconds()

### Community 67 - "links.py"
Cohesion: 0.09
Nodes (23): Phase A: embeddings and automatic links (no LLM), Task 1: Schema and config, Task 2: Engine touch points, Task 4: Links SQL (`links.py`), the core, Task 6: Alert clustering (`alerts.py`), Task 7: Web API, Task 8: Web views (Gemini-suited), Task 9: Compose, docs, Phase A validation (+15 more)

### Community 68 - "h"
Cohesion: 0.09
Nodes (60): An(), bi(), bl(), bp(), bw(), cb(), db(), dg() (+52 more)

### Community 69 - "n"
Cohesion: 0.11
Nodes (20): Ad(), BD(), Bs(), a(), ed(), gs(), Hn(), ii() (+12 more)

### Community 70 - ".run"
Cohesion: 0.20
Nodes (8): Task 5: Linker process (`linker.py`, `scripts/run_linker.py`), Global Constraints, LLM Resolver for Every Unlinked Template, No Call Limits — Implementation Plan, Review Focus, Task 2: Limits default to 0 = no limit, Task 3: Web shows "no limit", Task 4: Merge, deploy, watch the backlog, LlmConfig

### Community 71 - "commands.py"
Cohesion: 0.32
Nodes (3): finish(), take(), test_enqueue_take_finish()

### Community 72 - "run_engine.py"
Cohesion: 0.22
Nodes (5): Metrics, main(), row(), test_row_back_to_normal_leaves_log_alert_state(), value()

### Community 73 - "Task 41: engine — a module per service, and the streak's first minute"
Cohesion: 0.25
Nodes (7): Done when, Files, Interfaces, Requirements, Task 41: engine — a module per service, and the streak's first minute, Tests (write first, see them fail), Why

### Community 74 - "mx"
Cohesion: 0.19
Nodes (14): Bx(), dE(), fe(), Gx(), Hx(), jx(), kg(), mx() (+6 more)

### Community 75 - "U"
Cohesion: 0.05
Nodes (58): Al(), ay(), bv(), eM(), Ez(), fD(), Fm(), gp() (+50 more)

### Community 76 - "bf"
Cohesion: 0.29
Nodes (11): bf(), df(), Ff(), hf(), nf(), of(), rf(), tf() (+3 more)

### Community 77 - "fv"
Cohesion: 0.19
Nodes (12): cv(), dv(), ev(), fv(), kV(), lv(), nv(), pv() (+4 more)

### Community 78 - "Document links smoke test on real data (2026-10-21)"
Cohesion: 0.40
Nodes (4): Document links smoke test on real data (2026-10-21), Reading, Result, Setup

### Community 80 - "Per-service templates, statistical baselines, document-centric alerts"
Cohesion: 0.22
Nodes (9): Goal, Known weak points (accepted for v1), Open questions, Per-service templates, statistical baselines, document-centric alerts, Pipeline risks and fixes, Results (2026-10-19, live recharge stack), Test: template → document vs group centroid → document, Validation (after building) (+1 more)

### Community 81 - "owner.py"
Cohesion: 0.09
Nodes (6): _ts(), main(), client(), test_password_never_returned(), test_pipeline_stale_flag(), test_selection_round_trip_and_command()

### Community 82 - "A"
Cohesion: 0.06
Nodes (40): cA(), D(), da(), Eh(), fa(), fR(), ga(), gw() (+32 more)

### Community 83 - "xu"
Cohesion: 0.20
Nodes (11): au(), BG(), bu(), cu(), du(), fg(), fu(), jg() (+3 more)

### Community 84 - "test_incident_test.py"
Cohesion: 0.26
Nodes (10): doc_row(), sample(), test_alert_onsets(), test_doc_row_and_doc_key_state(), test_match_template_fails_fast_on_none_or_several(), test_onset_carried_over_is_not_counted(), test_onset_inside_phase_and_check1_timing(), test_row_key() (+2 more)

### Community 85 - "q"
Cohesion: 0.22
Nodes (10): Am(), ao(), Ix(), km(), ko(), lk(), lN(), Pm() (+2 more)

### Community 86 - "es_source.py"
Cohesion: 0.25
Nodes (7): ElasticsearchConfig, ReaderConfig, _build_client(), EsSource, retry(), test_reconnect_builds_new_client(), test_resolve_and_list_indices()

### Community 88 - "5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)"
Cohesion: 0.22
Nodes (9): 5.1 The Baseline Matrix & Visual Band Inspector (`/#baseline`), 5.2 The Pipeline Data Stream Topology (`/#pipeline`), 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive), 5. Detailed UI/UX Specifications, Pillar 1: Watchdog Insights Banner & Outlier Classification, Pillar 2: Log Pattern Volume Delta (% Change vs. Baseline), Pillar 3: 1-Click Incident Marking & Baseline Exclusion Loop, Tier 1: Weekly Baseline Health Matrix (24 Hours $\times$ 7 Committed Days) (+1 more)

### Community 90 - "Xd"
Cohesion: 0.17
Nodes (12): bk(), Gd(), Hd(), Im(), Kr(), ls(), mm(), Tm() (+4 more)

### Community 92 - "hit_to_line"
Cohesion: 0.13
Nodes (12): F1. NUL byte in an ES field stalled the flush, Fixed, Known bugs and risks, hit_to_line(), Line, test_hit_to_line_fields(), test_malformed_hit_is_none(), test_missing_module_and_service() (+4 more)

### Community 93 - "Dw"
Cohesion: 0.50
Nodes (5): Cw(), di(), Dw(), fi(), pi()

### Community 94 - "H"
Cohesion: 0.22
Nodes (9): Cm(), Cn(), ef(), lt(), na(), RN(), H(), zc() (+1 more)

### Community 96 - "eS"
Cohesion: 0.60
Nodes (5): eS(), Fw(), jw(), NS(), qw()

### Community 97 - "alert_rows"
Cohesion: 0.36
Nodes (6): alert_rows(), root(), st(), test_rows_group_by_document(), test_service_rows_split_by_service_and_doc_key_alone(), test_similar_undocumented_templates_become_one_cluster_row()

### Community 98 - "aw"
Cohesion: 0.50
Nodes (4): Ab(), aw(), Gt(), oW()

### Community 99 - "fb"
Cohesion: 0.33
Nodes (6): at(), B(), fb(), gB(), yb(), z()

### Community 100 - "xC"
Cohesion: 0.40
Nodes (5): Dt(), Ht(), Hu(), xC(), zt()

### Community 102 - "j"
Cohesion: 0.17
Nodes (10): bB(), eb(), If(), j(), jy(), lM(), nb(), qy() (+2 more)

### Community 103 - "ia"
Cohesion: 0.67
Nodes (3): ia(), oA(), rA()

### Community 104 - "o"
Cohesion: 0.07
Nodes (63): ac(), bn(), br(), bz(), c(), cc(), ce(), Cf() (+55 more)

### Community 105 - "to"
Cohesion: 0.05
Nodes (51): 1. Gọi nhanh, 2. Xác thực, 3. Request, 4. Response `200`, 5. Khi nào một template được trả về, 6. Lỗi, 7. Ví dụ code, logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào) (+43 more)

### Community 106 - "yi"
Cohesion: 0.40
Nodes (6): be(), bh(), Se(), vi(), xe(), yi()

### Community 109 - "4. Pipeline page"
Cohesion: 0.29
Nodes (7): 4.1 Status line, 4.2 Topology (ECharts `graph`, fixed layout), 4.3 Lag and throughput, last 60 minutes (ECharts line), 4.4 Volume vs expected, per service (ECharts line, small multiples), 4.5 Services table (HTML), 4.6 Engine and API changes, 4. Pipeline page

### Community 110 - "T"
Cohesion: 0.18
Nodes (12): E(), Eg(), et(), md(), Mg(), mo(), oS(), pt() (+4 more)

### Community 111 - "LLM resolver: every unlinked template, not only alerting or new ones"
Cohesion: 0.25
Nodes (7): Change, Deploy, Docs (with the code, since ARCHITECTURE describes what is implemented), How a template that was never seen before is handled (for context), LLM resolver: every unlinked template, not only alerting or new ones, Problem, Tests (`tests/test_resolver.py`)

### Community 112 - "Cx"
Cohesion: 0.67
Nodes (3): Ax(), Cx(), kx()

### Community 113 - "OpenMinutes"
Cohesion: 0.27
Nodes (7): OpenMinutes, test_close_includes_empty_minutes_and_service_keys(), test_first_start_backlog_is_linear(), test_late_count_is_dropped(), test_level_class_keys_add_up_to_total(), test_levels_error_keys_and_most_severe(), test_silent_key_forgotten_next_hour()

### Community 114 - "rvRenderAll"
Cohesion: 0.25
Nodes (11): loadInspector(), loadReview(), rvApplyRoute(), rvIndexMatrix(), rvPollCommand(), rvReloadData(), rvRenderAll(), rvRenderLegend() (+3 more)

### Community 115 - "logai2 Core Engine + Web v1 Implementation Plan"
Cohesion: 0.40
Nodes (4): Global Constraints, logai2 Core Engine + Web v1 Implementation Plan, Review Focus, Spec coverage check

### Community 116 - "test_web_links.py"
Cohesion: 0.31
Nodes (7): client(), source(), test_a_manual_link_over_a_draft_rejects_the_draft(), test_apply_to_similar_lists_pre_ticks_and_links_in_one_request(), test_confirm_and_reject_each_automatic_source(), test_delete_on_a_draft_link_rejects_its_document(), url()

### Community 117 - "gF"
Cohesion: 0.25
Nodes (8): af(), F(), gF(), nz(), Pz(), rz(), vz(), yz()

### Community 118 - "make_source"
Cohesion: 0.33
Nodes (7): bodies(), make_source(), test_empty_response_is_empty_list(), test_full_page_past_from_ms_is_not_reread(), test_millisecond_beyond_largest_page_falls_back_to_search_after(), test_page_query_shape(), test_page_reads_whole_millisecond()

### Community 119 - "kl"
Cohesion: 0.25
Nodes (8): Dl(), GC(), kl(), Mr(), Sr(), tg(), vt(), wr()

### Community 120 - "`GET /api/pipeline`"
Cohesion: 0.50
Nodes (4): 10.5 `worker_status`, `GET /api/pipeline`, LLM profiles, warnings()

### Community 122 - "px"
Cohesion: 0.29
Nodes (5): Jm(), px(), f(), Qd(), Rm()

### Community 126 - "gV"
Cohesion: 0.50
Nodes (4): gg(), gV(), hv(), yp()

### Community 128 - "lg"
Cohesion: 0.67
Nodes (3): ag(), lg(), sg()

### Community 129 - "co"
Cohesion: 0.67
Nodes (3): co(), dx(), ho()

## Knowledge Gaps
- **268 isolated node(s):** `DatabaseConfig`, `FlushConfig`, `StartConfig`, `BaselineConfig`, `DropConfig` (+263 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 630 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `to()` connect `to` to `fb`, `h`, `i`, `echarts.min.js`, `Cx`, `A`?**
  _High betweenness centrality (0.152) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `E()` (e.g. with `I()` and `T()`) actually correct?**
  _`E()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `DatabaseConfig`, `FlushConfig`, `StartConfig` to the rest of the system?**
  _268 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `FakeSource` be split into smaller, more focused modules?**
  _Cohesion score 0.13506012950971322 - nodes in this community are weakly interconnected._
- **Why does `db()` connect `h` to `create_app`, `j`, `File map`, `echarts.min.js`, `Baseline`?**
  _High betweenness centrality (0.151) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `create_app()` (e.g. with `Read first` and `AppConfig`) actually correct?**
  _`create_app()` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Should `Linker` be split into smaller, more focused modules?**
  _Cohesion score 0.13405797101449277 - nodes in this community are weakly interconnected._