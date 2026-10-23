# Graph Report - logai2  (2026-10-22)

## Corpus Check
- 118 files · ~155,170 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 4, .css 1, .ini 1)

## Summary
- 2352 nodes · 7059 edges · 119 communities (110 shown, 9 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 1233 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6c600f3b`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- tmpl
- test_engine.py
- judge
- linker.py
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
- Reader
- review.js
- test_links.py
- Task 5: Drain3 per service
- baseline_view.py
- config.py
- Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance
- link_eval.py
- _l
- engine.py
- HTTP API
- Metrics
- ui.js
- alerting.js
- test_web_docs_baseline.py
- logai2 core engine + web v1: design
- Design
- datetime
- Baseline
- gF
- pipeline.js
- logai2
- logai2 Architecture
- Per-service templates, statistical baselines, document-centric alerts
- llm.js
- Incident test, 2026-10-20: smoke test
- es_source.py
- verdict
- resolver.py
- test_owner.py
- Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)
- hit
- logai2 — instructions for Claude sessions
- test_web_baseline_view.py
- agy-task.sh
- baseline.js
- E
- app.py
- keypicker.js
- D
- kl
- incident
- links.py
- a
- test_web_links.py
- Any
- Dm
- alert_rows
- Owner
- mx
- U
- ed
- fv
- Document links smoke test on real data (2026-10-21)
- O
- EsSource
- time
- A
- xu
- test_incident_test.py
- .page
- 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)
- te
- json
- find_level
- Xd
- mw
- level_class
- Rw
- ug
- commands.py
- eS
- rt
- aw
- B
- Hu
- Y
- bf
- ia
- Fm
- logai2 Module Alert API Implementation Plan
- Wt
- OpenMinutes
- 3B. Baseline page (`#baseline`, charts only)
- q
- T
- LLM resolver: every unlinked template, not only alerting or new ones
- `GET /api/pipeline`
- 4. Pipeline page
- vo
- logai2 Core Engine + Web v1 Implementation Plan
- cg
- co
- index

## God Nodes (most connected - your core abstractions)
1. `E()` - 119 edges
2. `create_app()` - 93 edges
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
- `API` --references--> `class_sql()`  [INFERRED]
  docs/specs/2026-10-21-templates-page-redesign.md → logai2/links.py
- `8.3 Band` --references--> `Stat`  [INFERRED]
  ARCHITECTURE.md → logai2/owner/baseline.py
- `3B.2 Actual vs expected (ECharts line)` --references--> `judge()`  [INFERRED]
  docs/specs/2026-10-21-baseline-pipeline-pages.md → logai2/owner/baseline.py

## Import Cycles
- None detected.

## Communities (119 total, 9 thin omitted)

### Community 0 - "tmpl"
Cohesion: 0.22
Nodes (27): Review Focus, Task 1: Remove the alerting / 24 h gate, Task 3: Web shows "no limit", Task 4: Merge, deploy, watch the backlog, tmpl(), vec(), alerting(), Chat (+19 more)

### Community 1 - "test_engine.py"
Cohesion: 0.14
Nodes (32): put_setting(), FakeSource, catch_up(), Clock, history(), make(), build(), sleep() (+24 more)

### Community 2 - "judge"
Cohesion: 0.16
Nodes (20): 5. Core data contracts, Baseline, Review and Pipeline Pages, Watchdog Style — Implementation Plan, Global Constraints, total(), judge(), Lookup, Stat, Verdict (+12 more)

### Community 3 - "linker.py"
Cohesion: 0.09
Nodes (19): embed(), EmbeddingError, literal(), _unit(), Linker, axis(), embeddings_handler(), handler() (+11 more)

### Community 4 - "create_app"
Cohesion: 0.08
Nodes (53): Task 11: Web shell, Pipeline and Data sources pages, enqueue(), next_doc_id(), BadRequest, create_app(), activate_llm_profile(), add_decision(), automatic_link() (+45 more)

### Community 5 - "Engine"
Cohesion: 0.20
Nodes (4): Task 8: Engine and resolver status fields, get_setting(), _day(), Engine

### Community 6 - "test_web_alerts_templates.py"
Cohesion: 0.11
Nodes (26): Task 2: ALERTING periods and the module query, Global Constraints, Review Focus, Task 1: API — `link_source=review` and `review_total`, Task 4: Docs and wrap-up, Templates Page Redesign Implementation Plan, day_hour(), daytype() (+18 more)

### Community 7 - "templates.js"
Cohesion: 0.05
Nodes (58): Task 2: Toolbar mode and the 5-column table, Task 3: Decision-first drawer and Next item, Accessibility (UI UX Pro Max checks), API, Docs, Drawer, Goal, Layout (+50 more)

### Community 8 - "incident_test.py"
Cohesion: 0.18
Nodes (11): alert_onsets(), check1_deadline(), doc_key_state(), doc_row_state(), main(), onset(), _phase(), row_key() (+3 more)

### Community 9 - "PR: task/<id>-<slug>"
Cohesion: 0.06
Nodes (30): Branches and worktrees, Gemini 3.8 Flash (Antigravity CLI), How the tech lead runs each implementer, Ledger, Life of a task, Questions go up, never sideways, Review gates, Roles (+22 more)

### Community 10 - "documentation.js"
Cohesion: 0.16
Nodes (31): decide(), deleteDocument(), $docList, docMatches(), $docPane, docs, $docSearch, $docService (+23 more)

### Community 11 - "test_es_source.py"
Cohesion: 0.12
Nodes (23): AuthenticationException, AuthorizationException, BadRequestError, bodies(), line_of(), make_source(), NotFoundError, test_empty_response_is_empty_list() (+15 more)

### Community 12 - "Miners"
Cohesion: 0.14
Nodes (9): Drain3Config, Miners, _match(), TestLogMasking, test_ids_are_per_service(), test_placeholder_display(), test_state_round_trip(), test_generalized_template_survives_restore() (+1 more)

### Community 13 - "sources.js"
Cohesion: 0.12
Nodes (24): conn, connCluster(), $connError, $connForm, $connHosts, connMovesCluster(), $connPassword, connSave() (+16 more)

### Community 14 - "i"
Cohesion: 0.05
Nodes (67): A(), Al(), bn(), br(), c(), cb(), e(), Ci() (+59 more)

### Community 15 - "echarts.min.js"
Cohesion: 0.03
Nodes (28): bz(), cs(), ct(), gg(), HS(), Hz(), Is(), jf() (+20 more)

### Community 16 - "normalize.py"
Cohesion: 0.17
Nodes (6): _collapse(), _is_prefix_token(), _mask_hex(), _mask_long_id(), _strip_leading_timestamp(), _xml_tag()

### Community 17 - "Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture"
Cohesion: 0.13
Nodes (15): 1.1 The Current Problem: Visual Disconnect and Operator Friction, 1.2 The Proposed Solution: Strategy A (Unified Apache ECharts), 1. Executive Summary & Problem Statement, 2. Core Architectural & Design Principles, 3.1 Why Apache ECharts Over Disjointed Micro-Libraries?, 3. Technology Decision: Strategy A (Unified Apache ECharts Engine), 4. End-to-End Visual Workflow & User Experience, 6.1 Extended Baseline Endpoint (`GET /api/baseline?service=...`) (+7 more)

### Community 19 - "pytest"
Cohesion: 0.11
Nodes (17): chat_json(), endpoint(), LlmError, LlmUnavailable, _NoRedirect, parse_json_reply(), _timed_out(), profile() (+9 more)

### Community 20 - "normalize"
Cohesion: 0.20
Nodes (3): normalize(), TestNormalize, TestNormalizeMissingParts

### Community 21 - "Reader"
Cohesion: 0.19
Nodes (6): 10.2 Tick, 6. Reader, KnownLines, Reader, test_aggregates_and_dedups(), test_known_lines_lru()

### Community 22 - "review.js"
Cohesion: 0.06
Nodes (69): Task 4: ECharts, chart helper, chart colour tokens, Task 5: Review page UI (decisions), 2. ECharts in this console, baseOption(), chartFor(), _chartObservers, chartTokens(), disposeCharts() (+61 more)

### Community 23 - "test_links.py"
Cohesion: 0.22
Nodes (18): apply_links(), candidates(), cluster_edges(), test_llm_table_scores_the_confirm_answers(), at(), doc(), docs(), link() (+10 more)

### Community 24 - "Task 5: Drain3 per service"
Cohesion: 0.15
Nodes (4): 7.1 Templates, Task 5: Drain3 per service, _BlobPersistence, display_template()

### Community 25 - "baseline_view.py"
Cohesion: 0.07
Nodes (35): Review Focus, Task 11: Docs, merge readiness, Task 1: Non-NORMAL minutes per hour (shared helper), Task 2: Review matrix and coverage API (`/api/baseline/matrix`), Task 3: Review inspector API (`/api/baseline/inspect`), Task 6: Baseline series and incident API, Task 7: Baseline page UI (charts), `GET /api/baseline/incident` (+27 more)

### Community 26 - "config.py"
Cohesion: 0.09
Nodes (21): BaselineConfig, DatabaseConfig, DocumentationConfig, DropConfig, EmbeddingConfig, FlushConfig, load_config(), MetricsConfig (+13 more)

### Community 27 - "Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance"
Cohesion: 0.14
Nodes (14): 1. Executive Summary & Problem Statement, 2. Core Architectural Principles, 3. End-to-End Processing Workflow, 4. Trigger Criteria & Anti-Spam Filtering, 5.1 Context Payload, 5.2 Structured Prompt, 5. Context Assembly & Prompt Engineering, 6. Database Schema Evolution (+6 more)

### Community 28 - "link_eval.py"
Cohesion: 0.30
Nodes (11): active_profiles(), corpus_top2(), layer2_table(), pick(), llm_table(), main(), nearest_manual(), score() (+3 more)

### Community 29 - "_l"
Cohesion: 0.15
Nodes (24): ap(), cp(), dp(), Dy(), ep(), fp(), hp(), I() (+16 more)

### Community 30 - "engine.py"
Cohesion: 0.16
Nodes (3): AppConfig, Count, MinuteBatch

### Community 31 - "HTTP API"
Cohesion: 0.11
Nodes (19): 11. Postgres schema, 15. Where the code departs from the spec, 9. Daily commit and review exclusions, Commands, Documents, Error bodies, `GET /api/alerts`, `GET /api/baseline/matrix` (+11 more)

### Community 32 - "Metrics"
Cohesion: 0.27
Nodes (4): Metrics, row(), test_row_back_to_normal_leaves_log_alert_state(), value()

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

### Community 38 - "datetime"
Cohesion: 0.13
Nodes (16): Task 7: Baseline band and daily commit, drop_partitions_before(), ensure_partitions(), StaleRevision, commit(), baseline_rows(), seed(), test_commit_is_idempotent_and_catches_up_missed_days() (+8 more)

### Community 39 - "Baseline"
Cohesion: 0.18
Nodes (21): Task 8: Detection and alert state, Task 9: Pipeline API: services, volume, stages, AlertConfig, Baseline, ClosedMinute, AlertRow, Change, Detector (+13 more)

### Community 40 - "gF"
Cohesion: 0.22
Nodes (9): af(), F(), gF(), iz(), nz(), Pz(), rz(), vz() (+1 more)

### Community 41 - "pipeline.js"
Cohesion: 0.12
Nodes (39): fetchPipeline(), pl, PL_MAIN, PL_NODES, plCard(), plClear(), plDisposeMini(), plDuration() (+31 more)

### Community 42 - "logai2"
Cohesion: 0.20
Nodes (10): Configuration, First start, How it works, Incident test, logai2, Repo layout, Requirements, Run with Docker Compose (+2 more)

### Community 44 - "logai2 Architecture"
Cohesion: 0.06
Nodes (34): 10.1 Start, 10.3 Commit timing, 10.4 Bootstrap and `days_back`, 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108), 10. Engine loop, 12.1 Settings keys, 12.2 Commands, 12. Web ↔ engine contract (+26 more)

### Community 45 - "Per-service templates, statistical baselines, document-centric alerts"
Cohesion: 0.22
Nodes (9): Goal, Known weak points (accepted for v1), Open questions, Per-service templates, statistical baselines, document-centric alerts, Pipeline risks and fixes, Results (2026-10-19, live recharge stack), Test: template → document vs group centroid → document, Validation (after building) (+1 more)

### Community 46 - "llm.js"
Cohesion: 0.31
Nodes (7): llm, llmBanner(), $llmModal, llmStats(), llmStatusBadge(), $llmTbody, loadLlmPage()

### Community 47 - "Incident test, 2026-10-20: smoke test"
Cohesion: 0.25
Nodes (7): Checks, Incident test, 2026-10-20: smoke test, Inject errors and anomalies, Recovery, Targets, What is meaningful, Why B3 and B5 failed

### Community 48 - "es_source.py"
Cohesion: 0.14
Nodes (15): Task 3: Elasticsearch source (search, paging, hit → Line), _coerce_message(), _extract_level(), _extract_level_from_message(), _extract_service(), hit_to_line(), Line, _parse_timestamp() (+7 more)

### Community 49 - "verdict"
Cohesion: 0.14
Nodes (16): Edge cases (each has a test), File map, Global Constraints, How it works (what the user confirms), logai2 Embeddings, Document Links and LLM Resolution Implementation Plan, Other behavior, Out of scope, Phase B: LLM confirm or suggest (absorbs sub-project 8) (+8 more)

### Community 50 - "resolver.py"
Cohesion: 0.12
Nodes (9): Global Constraints, LLM Resolver for Every Unlinked Template, No Call Limits — Implementation Plan, Task 2: Limits default to 0 = no limit, LlmConfig, Resolver, call(), _template(), _text() (+1 more)

### Community 51 - "test_owner.py"
Cohesion: 0.25
Nodes (19): counts(), lines(), make(), rows(), run_minutes(), test_catch_up_days_back_writes_old_partitions(), test_counts_exact(), test_crash_between_flushes_is_exact() (+11 more)

### Community 52 - "Baseline, Review and Pipeline pages: what they show (sub-project 9, phases 1–2)"
Cohesion: 0.15
Nodes (13): 1. Questions each page answers, 3.1 Header and cards, 3.2 Matrix: days × hours (ECharts heatmap), 3.3 Inspector: one Watchdog card for the selection, 3.4 Coverage: what the baseline is built from (ECharts heatmap), 3.5 Removed, 3.6 API, 3. Review page (`#review`, decisions) (+5 more)

### Community 53 - "hit"
Cohesion: 0.20
Nodes (19): hit(), make(), test_cache_hit_sends_template_id(), test_caught_up_flips_back_when_new_doc_arrives(), test_continuous_stream_still_catches_up(), test_cursors_use_boundary_but_not_before_floor(), test_failing_index_stops_holding_watermark(), test_forget_before_keeps_ids_at_or_after_position() (+11 more)

### Community 54 - "logai2 — instructions for Claude sessions"
Cohesion: 0.50
Nodes (3): logai2 — instructions for Claude sessions, Project rules, Team workflow (when the user says to use it)

### Community 55 - "test_web_baseline_view.py"
Cohesion: 0.14
Nodes (42): Task 2: Web resolves `verdict:` keys and serves the new keys, Tests, matrix(), series(), base(), cell(), committed(), hours() (+34 more)

### Community 60 - "baseline.js"
Cohesion: 0.14
Nodes (26): bl, BL_STATE, BL_STATE_SUB, blApplyRoute(), blChangeRoute(), blClear(), blDecision(), blIncKey() (+18 more)

### Community 61 - "E"
Cohesion: 0.06
Nodes (48): BD(), Cl(), Cm(), Dk(), DO(), ef(), el(), Fl() (+40 more)

### Community 62 - "app.py"
Cohesion: 0.11
Nodes (14): _lookup(), _age(), _by_service(), service_data(), services(), _stage(), stage_states(), volume() (+6 more)

### Community 63 - "keypicker.js"
Cohesion: 0.20
Nodes (17): createKeyPicker(), choose(), options(), render(), renderGroups(), renderList(), setOpen(), keyGroup() (+9 more)

### Community 64 - "D"
Cohesion: 0.20
Nodes (11): Ax(), D(), eo(), iy(), Lo(), Oo(), oy(), Sh() (+3 more)

### Community 65 - "kl"
Cohesion: 0.17
Nodes (12): Dl(), GC(), Hh(), o(), kl(), Mr(), so(), Sr() (+4 more)

### Community 66 - "incident"
Cohesion: 0.11
Nodes (18): Baseline and Review: grouped key picker and combined series, Deploy, Derived keys: `verdict:action`, `verdict:safe`, Docs, Goal, `incident()` for the new keys, Keys, Later (+10 more)

### Community 67 - "links.py"
Cohesion: 0.12
Nodes (17): Phase A: embeddings and automatic links (no LLM), Task 1: Schema and config, Task 2: Engine touch points, Task 4: Links SQL (`links.py`), the core, Task 5: Linker process (`linker.py`, `scripts/run_linker.py`), Task 6: Alert clustering (`alerts.py`), Task 7: Web API, Task 8: Web views (Gemini-suited) (+9 more)

### Community 68 - "a"
Cohesion: 0.09
Nodes (88): aA(), An(), ba(), bk(), bl(), Dn(), ds(), y() (+80 more)

### Community 69 - "test_web_links.py"
Cohesion: 0.42
Nodes (6): source(), test_a_manual_link_over_a_draft_rejects_the_draft(), test_apply_to_similar_lists_pre_ticks_and_links_in_one_request(), test_confirm_and_reject_each_automatic_source(), test_delete_on_a_draft_link_rejects_its_document(), url()

### Community 70 - "Any"
Cohesion: 0.26
Nodes (4): http(), match_template(), parse_metrics(), Runner

### Community 72 - "alert_rows"
Cohesion: 0.36
Nodes (6): alert_rows(), root(), st(), test_rows_group_by_document(), test_service_rows_split_by_service_and_doc_key_alone(), test_similar_undocumented_templates_become_one_cluster_row()

### Community 73 - "Owner"
Cohesion: 0.08
Nodes (19): 1. Purpose and scope, 3. Configuration, 5.1 Document links, 7.3 The flush transaction, File map, Task 10: Engine loop, bootstrap, commands, metrics, Task 12: Alerting and Templates pages, Task 13: Documentation and Baseline pages (+11 more)

### Community 74 - "mx"
Cohesion: 0.17
Nodes (15): Bx(), dE(), fe(), Gx(), Hx(), jx(), kg(), mx() (+7 more)

### Community 75 - "U"
Cohesion: 0.05
Nodes (48): ay(), bB(), Bs(), a(), bv(), eb(), Ez(), go() (+40 more)

### Community 76 - "ed"
Cohesion: 0.38
Nodes (6): Ad(), ed(), kd(), Ld(), nd(), Od()

### Community 77 - "fv"
Cohesion: 0.16
Nodes (14): cv(), dv(), ev(), fv(), hv(), kV(), lv(), nv() (+6 more)

### Community 78 - "Document links smoke test on real data (2026-10-21)"
Cohesion: 0.40
Nodes (4): Document links smoke test on real data (2026-10-21), Reading, Result, Setup

### Community 79 - "O"
Cohesion: 0.20
Nodes (8): Eh(), O(), Oh(), Ph(), Rh(), Uh(), xo(), Xy()

### Community 80 - "EsSource"
Cohesion: 0.22
Nodes (7): ElasticsearchConfig, ReaderConfig, _build_client(), EsSource, retry(), test_reconnect_builds_new_client(), test_resolve_and_list_indices()

### Community 81 - "time"
Cohesion: 0.16
Nodes (4): client(), test_password_never_returned(), test_pipeline_stale_flag(), test_selection_round_trip_and_command()

### Community 82 - "A"
Cohesion: 0.15
Nodes (16): cA(), da(), Dr(), fR(), iv(), s(), Jo(), Ke() (+8 more)

### Community 83 - "xu"
Cohesion: 0.20
Nodes (11): au(), BG(), bu(), cu(), du(), fg(), fu(), jg() (+3 more)

### Community 84 - "test_incident_test.py"
Cohesion: 0.26
Nodes (10): doc_row(), sample(), test_alert_onsets(), test_doc_row_and_doc_key_state(), test_match_template_fails_fast_on_none_or_several(), test_onset_carried_over_is_not_counted(), test_onset_inside_phase_and_check1_timing(), test_row_key() (+2 more)

### Community 85 - ".page"
Cohesion: 0.18
Nodes (5): 4.1 Elasticsearch document, 4.2 Query, paging and the millisecond re-read, 4.3 Index selection and connection, 4.4 Documentation seed, 4. External input contract

### Community 86 - "5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive)"
Cohesion: 0.22
Nodes (9): 5.1 The Baseline Matrix & Visual Band Inspector (`/#baseline`), 5.2 The Pipeline Data Stream Topology (`/#pipeline`), 5.3 Datadog Watchdog Insights on Alerting & Templates (Phase 3 Deep Dive), 5. Detailed UI/UX Specifications, Pillar 1: Watchdog Insights Banner & Outlier Classification, Pillar 2: Log Pattern Volume Delta (% Change vs. Baseline), Pillar 3: 1-Click Incident Marking & Baseline Exclusion Loop, Tier 1: Weekly Baseline Health Matrix (24 Hours $\times$ 7 Committed Days) (+1 more)

### Community 87 - "te"
Cohesion: 0.28
Nodes (9): ce(), ee(), er(), n(), he(), le(), te(), ue() (+1 more)

### Community 88 - "json"
Cohesion: 0.20
Nodes (3): read_jsonl(), client(), test_profiles_crud_key_is_write_only_and_one_active()

### Community 90 - "Xd"
Cohesion: 0.22
Nodes (9): Gd(), Hd(), ii(), it(), me(), ri(), uk(), Wd() (+1 more)

### Community 91 - "mw"
Cohesion: 0.20
Nodes (10): Im(), Kr(), Ks(), ls(), mh(), mm(), mw(), Tm() (+2 more)

### Community 92 - "level_class"
Cohesion: 0.33
Nodes (7): After the build, Global Constraints, Grouped key picker and combined series — Implementation Plan, Review Focus, Task 1: Engine counts `*warn` / `*info`, detector skips them, Task 3: Shared key picker on Baseline and Review, level_class()

### Community 93 - "Rw"
Cohesion: 0.18
Nodes (18): bi(), bw(), Cw(), di(), Dw(), Ew(), fi(), hi() (+10 more)

### Community 94 - "ug"
Cohesion: 0.20
Nodes (9): ag(), bp(), lg(), lw(), pg(), re(), sg(), ug() (+1 more)

### Community 95 - "commands.py"
Cohesion: 0.28
Nodes (3): finish(), take(), test_enqueue_take_finish()

### Community 96 - "eS"
Cohesion: 0.60
Nodes (5): eS(), Fw(), jw(), NS(), qw()

### Community 97 - "rt"
Cohesion: 0.33
Nodes (9): ac(), aR(), ec(), hc(), ic(), oc(), rt(), Yr() (+1 more)

### Community 98 - "aw"
Cohesion: 0.50
Nodes (4): Ab(), aw(), Gt(), oW()

### Community 99 - "B"
Cohesion: 0.50
Nodes (4): at(), B(), gB(), z()

### Community 100 - "Hu"
Cohesion: 0.50
Nodes (4): Dt(), Ht(), Hu(), zt()

### Community 101 - "Y"
Cohesion: 0.21
Nodes (12): cc(), fD(), hB(), hw(), mt(), og(), Pn(), ut() (+4 more)

### Community 102 - "bf"
Cohesion: 0.26
Nodes (12): bf(), df(), dg(), Ff(), hf(), nf(), of(), rf() (+4 more)

### Community 103 - "ia"
Cohesion: 0.67
Nodes (3): ia(), oA(), rA()

### Community 104 - "Fm"
Cohesion: 0.22
Nodes (9): Cr(), Cx(), dc(), eM(), Fm(), kx(), PC(), po() (+1 more)

### Community 105 - "logai2 Module Alert API Implementation Plan"
Cohesion: 0.25
Nodes (8): File map, Global Constraints, logai2 Module Alert API Implementation Plan, Out of scope (add when asked), Review Focus, Spec coverage check, Task 1: Module per service and alert-minute timing in the engine, Task 3: `GET /api/module-alerts`, docs and the end-to-end check

### Community 106 - "Wt"
Cohesion: 0.25
Nodes (11): be(), bh(), Fh(), Gn(), Ie(), Se(), vi(), we() (+3 more)

### Community 107 - "OpenMinutes"
Cohesion: 0.29
Nodes (7): OpenMinutes, test_close_includes_empty_minutes_and_service_keys(), test_first_start_backlog_is_linear(), test_late_count_is_dropped(), test_level_class_keys_add_up_to_total(), test_levels_error_keys_and_most_severe(), test_silent_key_forgotten_next_hour()

### Community 108 - "3B. Baseline page (`#baseline`, charts only)"
Cohesion: 0.25
Nodes (8): 3B.1 Header and cards, 3B.2 Actual vs expected (ECharts line), 3B.3 Possible incidents, 3B.4 Incident detail, 3B.5 Days × hours (read-only), 3B.6 The baseline itself (ECharts line), 3B.7 API, 3B. Baseline page (`#baseline`, charts only)

### Community 109 - "q"
Cohesion: 0.22
Nodes (10): Am(), ao(), Ix(), km(), ko(), lk(), lN(), Pm() (+2 more)

### Community 110 - "T"
Cohesion: 0.16
Nodes (13): E(), Eg(), et(), mB(), md(), Mg(), mo(), oS() (+5 more)

### Community 111 - "LLM resolver: every unlinked template, not only alerting or new ones"
Cohesion: 0.25
Nodes (7): Change, Deploy, Docs (with the code, since ARCHITECTURE describes what is implemented), How a template that was never seen before is handled (for context), LLM resolver: every unlinked template, not only alerting or new ones, Problem, Tests (`tests/test_resolver.py`)

### Community 112 - "`GET /api/pipeline`"
Cohesion: 0.29
Nodes (7): 10.5 `worker_status`, Task 10: Pipeline page UI, Documentation, `GET /api/pipeline`, LLM profiles, stale(), warnings()

### Community 113 - "4. Pipeline page"
Cohesion: 0.29
Nodes (7): 4.1 Status line, 4.2 Topology (ECharts `graph`, fixed layout), 4.3 Lag and throughput, last 60 minutes (ECharts line), 4.4 Volume vs expected, per service (ECharts line, small multiples), 4.5 Services table (HTML), 4.6 Engine and API changes, 4. Pipeline page

### Community 114 - "vo"
Cohesion: 0.50
Nodes (5): fa(), ga(), gp(), vo(), Zy()

### Community 115 - "logai2 Core Engine + Web v1 Implementation Plan"
Cohesion: 0.50
Nodes (4): Global Constraints, logai2 Core Engine + Web v1 Implementation Plan, Review Focus, Spec coverage check

### Community 116 - "cg"
Cohesion: 0.67
Nodes (3): Cf(), cg(), Pf()

### Community 117 - "co"
Cohesion: 0.67
Nodes (3): co(), dx(), ho()

## Knowledge Gaps
- **243 isolated node(s):** `DatabaseConfig`, `FlushConfig`, `StartConfig`, `BaselineConfig`, `DropConfig` (+238 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 590 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **9 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `db()` connect `i` to `create_app`, `a`, `Owner`, `logai2 Architecture`, `echarts.min.js`, `Reader`?**
  _High betweenness centrality (0.245) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `E()` (e.g. with `I()` and `T()`) actually correct?**
  _`E()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `DatabaseConfig`, `FlushConfig`, `StartConfig` to the rest of the system?**
  _243 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `test_engine.py` be split into smaller, more focused modules?**
  _Cohesion score 0.13506012950971322 - nodes in this community are weakly interconnected._
- **Why does `Task 9: Owner and the flush transaction (exactly once)` connect `Owner` to `Baseline`, `OpenMinutes`, `Miners`, `i`, `Reader`, `Task 5: Drain3 per service`, `engine.py`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `create_app()` (e.g. with `AppConfig` and `Baseline`) actually correct?**
  _`create_app()` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `linker.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08788159111933395 - nodes in this community are weakly interconnected._