# Graph Report - logai2  (2026-10-21)

## Corpus Check
- 107 files · ~107,464 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .css 1, .ini 1)

## Summary
- 1401 nodes · 3374 edges · 66 communities (60 shown, 6 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 329 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `9b3d3f69`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- test_links.py
- test_engine.py
- AppConfig
- pytest
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
- datetime
- test_incident_test.py
- normalize.py
- Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture
- TestUniversalMaskingRules
- renderTemplates
- normalize
- app.py
- baseline.js
- Templates page redesign
- Task 5: Drain3 per service
- logai2 Module Alert API Implementation Plan
- connect
- Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance
- core.js
- config.py
- main.js
- HTTP API
- Metrics
- ui.js
- alerting.js
- test_web_docs_baseline.py
- File map
- engine.py
- db.py
- logai2 Embeddings, Document Links and LLM Resolution Implementation Plan
- Phase A: embeddings and automatic links (no LLM)
- pipeline.js
- logai2
- logai2 Architecture
- Per-service templates, statistical baselines, document-centric alerts
- llm.js
- Incident test, 2026-10-20: smoke test
- Web UI and HTTP API
- .reload_links
- Baseline and Pipeline pages: what they show (sub-project 9, phases 1–2)
- test_owner.py
- 3. Baseline page
- loadTemplates
- logai2 — instructions for Claude sessions
- Phase B: LLM confirm or suggest (absorbs sub-project 8)
- agy-task.sh
- Review Focus
- commit
- suggest
- day_hour
- LLM resolver: every unlinked template, not only alerting or new ones
- 10. Engine loop

## God Nodes (most connected - your core abstractions)
1. `create_app()` - 75 edges
2. `normalize()` - 46 edges
3. `FakeSource` - 41 edges
4. `hit()` - 37 edges
5. `tmpl()` - 33 edges
6. `Engine` - 30 edges
7. `vec()` - 30 edges
8. `conn()` - 29 edges
9. `AppConfig` - 27 edges
10. `Baseline` - 27 edges

## Surprising Connections (you probably didn't know these)
- `Task 1: Remove the alerting / 24 h gate` --references--> `LlmConfig`  [INFERRED]
  docs/plans/2026-10-21-llm-resolve-all-templates.md → logai2/config.py
- `10.1 Start` --references--> `migrate()`  [INFERRED]
  ARCHITECTURE.md → logai2/db.py
- `API` --references--> `class_sql()`  [INFERRED]
  docs/specs/2026-10-21-templates-page-redesign.md → logai2/links.py
- `5. Pipeline: Elasticsearch → engine, one process first` --references--> `normalize()`  [INFERRED]
  docs/design/per-service-baseline.md → logai2/reader/normalize.py
- `Task 2: Copied text processing (normalize) and its tests` --references--> `normalize()`  [INFERRED]
  docs/plans/2026-10-20-core-engine-v1.md → logai2/reader/normalize.py

## Import Cycles
- None detected.

## Communities (66 total, 6 thin omitted)

### Community 0 - "test_links.py"
Cohesion: 0.05
Nodes (79): 5.1 Document links, Task 4: Links SQL (`links.py`), the core, Task 5: Linker process (`linker.py`, `scripts/run_linker.py`), Task 7: Web API, Global Constraints, LLM Resolver for Every Unlinked Template — Implementation Plan, Review Focus, Task 1: Remove the alerting / 24 h gate (+71 more)

### Community 1 - "test_engine.py"
Cohesion: 0.11
Nodes (38): 4.1 Status line, 4.2 Topology (ECharts `graph`, fixed layout), 4.3 Lag and throughput, last 60 minutes (ECharts line), 4.4 Volume vs expected, per service (ECharts line, small multiples), 4.5 Services table (HTML), 4.6 Engine and API changes, 4. Pipeline page, put_setting() (+30 more)

### Community 2 - "AppConfig"
Cohesion: 0.07
Nodes (51): 5. Core data contracts, 7.2 Open and closed minutes, 7.3 The flush transaction, 7. Owner and exactly-once flush, 8.1 Keys per closed minute, 8.2 Floors and levels, 8.3 Band, 8.4 Historic minutes (+43 more)

### Community 3 - "pytest"
Cohesion: 0.05
Nodes (37): Document links smoke test on real data (2026-10-21), Reading, Result, Setup, embed(), EmbeddingError, literal(), _unit() (+29 more)

### Community 4 - "create_app"
Cohesion: 0.06
Nodes (64): 11. Postgres schema, 1. Templates per service, 2. Detection: statistical baseline, no training, 3. From alert to document: templates link directly (option D, confirmed), 4. Writing documentation: by hand, with an AI chat alongside, 4a. LLM confirm or suggest, reviewed by a person, 5. Pipeline: Elasticsearch → engine, one process first, 6. Storage: PostgreSQL only (+56 more)

### Community 5 - "Engine"
Cohesion: 0.20
Nodes (3): get_setting(), _day(), Engine

### Community 6 - "test_web_alerts_templates.py"
Cohesion: 0.25
Nodes (11): Task 1: API — `link_source=review` and `review_total`, add_doc(), add_state(), add_template(), app(), client(), _review_fixture(), test_link_unknown_doc_404_and_link_enqueues_reload() (+3 more)

### Community 7 - "templates.js"
Cohesion: 0.10
Nodes (21): AUTOMATIC_SOURCES, getPageRange(), linkUrl(), offerSimilar(), renderTplPagination(), $similarModal, tpl, TPL_LEVELS (+13 more)

### Community 8 - "incident_test.py"
Cohesion: 0.09
Nodes (19): alert_onsets(), check1_deadline(), doc_key_state(), doc_row_state(), http(), main(), match_template(), onset() (+11 more)

### Community 9 - "PR: task/<id>-<slug>"
Cohesion: 0.06
Nodes (30): Branches and worktrees, Gemini 3.8 Flash (Antigravity CLI), How the tech lead runs each implementer, Ledger, Life of a task, Questions go up, never sideways, Review gates, Roles (+22 more)

### Community 10 - "documentation.js"
Cohesion: 0.16
Nodes (31): decide(), deleteDocument(), $docList, docMatches(), $docPane, docs, $docSearch, $docService (+23 more)

### Community 11 - "test_es_source.py"
Cohesion: 0.05
Nodes (46): 6. Reader, Task 3: Elasticsearch source (search, paging, hit → Line), ElasticsearchConfig, ReaderConfig, _build_client(), _coerce_message(), EsSource, _extract_level() (+38 more)

### Community 12 - "Miners"
Cohesion: 0.14
Nodes (9): Drain3Config, Miners, _match(), TestLogMasking, test_ids_are_per_service(), test_placeholder_display(), test_state_round_trip(), test_generalized_template_survives_restore() (+1 more)

### Community 13 - "sources.js"
Cohesion: 0.12
Nodes (24): conn, connCluster(), $connError, $connForm, $connHosts, connMovesCluster(), $connPassword, connSave() (+16 more)

### Community 14 - "datetime"
Cohesion: 0.22
Nodes (6): OpenMinutes, test_close_includes_empty_minutes_and_service_keys(), test_first_start_backlog_is_linear(), test_late_count_is_dropped(), test_levels_error_keys_and_most_severe(), test_silent_key_forgotten_next_hour()

### Community 15 - "test_incident_test.py"
Cohesion: 0.26
Nodes (10): doc_row(), sample(), test_alert_onsets(), test_doc_row_and_doc_key_state(), test_match_template_fails_fast_on_none_or_several(), test_onset_carried_over_is_not_counted(), test_onset_inside_phase_and_check1_timing(), test_row_key() (+2 more)

### Community 16 - "normalize.py"
Cohesion: 0.12
Nodes (8): _collapse(), find_level(), _is_prefix_token(), _mask_hex(), _mask_long_id(), _strip_leading_timestamp(), _xml_tag(), TestFindLevel

### Community 17 - "Antigravity Proposal: Datadog Watchdog-Inspired Visual Observability, Interactive Baseline Heatmap, and Pipeline Flow Architecture"
Cohesion: 0.08
Nodes (24): 1.1 The Current Problem: Visual Disconnect and Operator Friction, 1.2 The Proposed Solution: Strategy A (Unified Apache ECharts), 1. Executive Summary & Problem Statement, 2. Core Architectural & Design Principles, 3.1 Why Apache ECharts Over Disjointed Micro-Libraries?, 3. Technology Decision: Strategy A (Unified Apache ECharts Engine), 4. End-to-End Visual Workflow & User Experience, 5.1 The Baseline Matrix & Visual Band Inspector (`/#baseline`) (+16 more)

### Community 19 - "renderTemplates"
Cohesion: 0.29
Nodes (12): Task 2: Toolbar mode and the 5-column table, highlightWildcards(), levelBadgeClass(), linkedDocument(), linkSourceBadge(), openTemplateDrawer(), renderTemplates(), tplActionButton() (+4 more)

### Community 20 - "normalize"
Cohesion: 0.20
Nodes (3): normalize(), TestNormalize, TestNormalizeMissingParts

### Community 21 - "app.py"
Cohesion: 0.14
Nodes (3): test_password_never_returned(), test_pipeline_stale_flag(), test_selection_round_trip_and_command()

### Community 22 - "baseline.js"
Cohesion: 0.18
Nodes (16): bl, blCard(), $blDays, blDecide(), $blDecisions, $blFrom, blHour(), blHours() (+8 more)

### Community 23 - "Templates page redesign"
Cohesion: 0.17
Nodes (11): Accessibility (UI UX Pro Max checks), API, Docs, Drawer, Goal, Layout, Not changed, Out of scope (+3 more)

### Community 24 - "Task 5: Drain3 per service"
Cohesion: 0.15
Nodes (4): 7.1 Templates, Task 5: Drain3 per service, _BlobPersistence, display_template()

### Community 25 - "logai2 Module Alert API Implementation Plan"
Cohesion: 0.40
Nodes (5): Global Constraints, logai2 Module Alert API Implementation Plan, Out of scope (add when asked), Review Focus, Spec coverage check

### Community 26 - "connect"
Cohesion: 0.19
Nodes (9): load_config(), connect(), main(), main(), main(), apply_split(), main(), test_env_overrides_yaml() (+1 more)

### Community 27 - "Antigravity Proposal: Autonomous Runbook & Documentation Generation with Human-in-the-Loop Governance"
Cohesion: 0.14
Nodes (14): 1. Executive Summary & Problem Statement, 2. Core Architectural Principles, 3. End-to-End Processing Workflow, 4. Trigger Criteria & Anti-Spam Filtering, 5.1 Context Payload, 5.2 Structured Prompt, 5. Context Assembly & Prompt Engineering, 6. Database Schema Evolution (+6 more)

### Community 28 - "core.js"
Cohesion: 0.24
Nodes (6): Task 3: Decision-first drawer and Next item, Table, normal(), formatFullTs(), formatTs(), state

### Community 29 - "config.py"
Cohesion: 0.10
Nodes (15): Task 1: Skeleton, config, database and schema, BaselineConfig, DatabaseConfig, DocumentationConfig, DropConfig, EmbeddingConfig, FlushConfig, LlmConfig (+7 more)

### Community 30 - "main.js"
Cohesion: 0.16
Nodes (10): currentTheme(), loadData(), mainContent, POLL_MS, setView(), $sidebar, $sidebarCollapse, updateThemeToggle() (+2 more)

### Community 31 - "HTTP API"
Cohesion: 0.14
Nodes (13): Commands, Documents, Error bodies, `GET /api/alerts`, `GET /api/baseline?service=<service>`, `GET /api/sources`, `GET /api/templates`, HTTP API (+5 more)

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

### Community 36 - "File map"
Cohesion: 0.07
Nodes (28): File map, Global Constraints, logai2 Core Engine + Web v1 Implementation Plan, Review Focus, Spec coverage check, Task 10: Engine loop, bootstrap, commands, metrics, Task 12: Alerting and Templates pages, Task 13: Documentation and Baseline pages (+20 more)

### Community 37 - "engine.py"
Cohesion: 0.12
Nodes (4): finish(), take(), _ts(), test_enqueue_take_finish()

### Community 38 - "db.py"
Cohesion: 0.13
Nodes (10): 14. Known limits, Task 7: Baseline band and daily commit, Run the UI, drop_partitions_before(), ensure_partitions(), migrate(), StaleRevision, test_migrate_is_idempotent() (+2 more)

### Community 39 - "logai2 Embeddings, Document Links and LLM Resolution Implementation Plan"
Cohesion: 0.18
Nodes (11): Decisions (confirmed 2026-10-21 unless marked), Edge cases (each has a test), File map, Global Constraints, How it works (what the user confirms), logai2 Embeddings, Document Links and LLM Resolution Implementation Plan, Other behavior, Out of scope (+3 more)

### Community 40 - "Phase A: embeddings and automatic links (no LLM)"
Cohesion: 0.29
Nodes (7): Phase A: embeddings and automatic links (no LLM), Task 1: Schema and config, Task 2: Engine touch points, Task 3: Embeddings client (`embed.py`), Task 6: Alert clustering (`alerts.py`), Task 8: Web views (Gemini-suited), Task 9: Compose, docs, Phase A validation

### Community 41 - "pipeline.js"
Cohesion: 0.36
Nodes (8): pl, plCard(), plDuration(), plLinkerCard(), plPercent(), plRecordSample(), plSparkline(), renderPipeline()

### Community 42 - "logai2"
Cohesion: 0.20
Nodes (10): Configuration, First start, How it works, Incident test, logai2, Repo layout, Requirements, Run with Docker Compose (+2 more)

### Community 44 - "logai2 Architecture"
Cohesion: 0.17
Nodes (12): 13. Failure handling, 15. Where the code departs from the spec, 16. Reading the code, 17. When to update this document, 2. System context, 4.1 Elasticsearch document, 4.2 Query, paging and the millisecond re-read, 4.3 Index selection and connection (+4 more)

### Community 45 - "Per-service templates, statistical baselines, document-centric alerts"
Cohesion: 0.22
Nodes (9): Goal, Known weak points (accepted for v1), Open questions, Per-service templates, statistical baselines, document-centric alerts, Pipeline risks and fixes, Results (2026-10-19, live recharge stack), Test: template → document vs group centroid → document, Validation (after building) (+1 more)

### Community 46 - "llm.js"
Cohesion: 0.31
Nodes (7): llm, llmBanner(), $llmModal, llmStats(), llmStatusBadge(), $llmTbody, loadLlmPage()

### Community 47 - "Incident test, 2026-10-20: smoke test"
Cohesion: 0.25
Nodes (7): Checks, Incident test, 2026-10-20: smoke test, Inject errors and anomalies, Recovery, Targets, What is meaningful, Why B3 and B5 failed

### Community 48 - "Web UI and HTTP API"
Cohesion: 0.18
Nodes (11): Alerting, Baseline, Data sources, Documentation, `GET /api/pipeline`, Implementation map, LLM profiles, Pipeline (+3 more)

### Community 49 - ".reload_links"
Cohesion: 0.29
Nodes (6): 12.1 Settings keys, 12.2 Commands, 12. Web ↔ engine contract, 1. Purpose and scope, `PUT /api/templates/<service>/<template_id>/link`, Templates

### Community 50 - "Baseline and Pipeline pages: what they show (sub-project 9, phases 1–2)"
Cohesion: 0.29
Nodes (6): 1. Questions each page answers, 2. ECharts in this console, 5. Build order, 6. Out of scope, 7. Open questions, Baseline and Pipeline pages: what they show (sub-project 9, phases 1–2)

### Community 51 - "test_owner.py"
Cohesion: 0.08
Nodes (46): 10.2 Tick, 3. Configuration, Task 4: Reader (cursors, dedup, cache, MinuteBatch), KnownLines, Count, MinuteBatch, Reader, hit() (+38 more)

### Community 52 - "3. Baseline page"
Cohesion: 0.29
Nodes (7): 3.1 Header and cards, 3.2 Matrix: days × hours (ECharts heatmap), 3.3 Inspector: one Watchdog card for the selection, 3.4 Coverage: what the baseline is built from (ECharts heatmap), 3.5 Removed, 3.6 API, 3. Baseline page

### Community 53 - "loadTemplates"
Cohesion: 0.33
Nodes (6): fetchDocuments(), fetchServices(), fetchTemplates(), loadTemplates(), renderServiceFilter(), setTplMode()

### Community 54 - "logai2 — instructions for Claude sessions"
Cohesion: 0.50
Nodes (3): logai2 — instructions for Claude sessions, Project rules, Team workflow (when the user says to use it)

### Community 55 - "Phase B: LLM confirm or suggest (absorbs sub-project 8)"
Cohesion: 0.40
Nodes (5): Phase B: LLM confirm or suggest (absorbs sub-project 8), Task 10: LLM client (`llm.py`), Task 11: Resolver (`resolver.py`), Task 12: Web for Phase B, Task 13: Phase B validation and docs

### Community 60 - "Review Focus"
Cohesion: 0.40
Nodes (4): Global Constraints, Review Focus, Task 4: Docs and wrap-up, Templates Page Redesign Implementation Plan

### Community 63 - "commit"
Cohesion: 0.33
Nodes (9): commit(), baseline_rows(), seed(), test_commit_is_idempotent_and_catches_up_missed_days(), test_commit_keeps_last_5_weekdays_and_skips_today(), test_commit_mean_std_and_silent_minutes(), test_exclusion_removes_hour(), test_failed_commit_rolls_back_everything() (+1 more)

### Community 66 - "suggest"
Cohesion: 0.33
Nodes (6): suggest(), chg(), local(), test_suggest_merges_overlaps_and_keeps_open_period_to_now(), test_suggest_splits_at_midnight_and_skips_decided(), test_suggest_widens_to_whole_hours()

### Community 68 - "day_hour"
Cohesion: 0.20
Nodes (11): File map, Task 1: Module per service and alert-minute timing in the engine, Task 2: ALERTING periods and the module query, Task 3: `GET /api/module-alerts`, docs and the end-to-end check, day_hour(), daytype(), local(), minute_of() (+3 more)

### Community 70 - "LLM resolver: every unlinked template, not only alerting or new ones"
Cohesion: 0.25
Nodes (7): Change, Deploy, Docs (with the code, since ARCHITECTURE describes what is implemented), How a template that was never seen before is handled (for context), LLM resolver: every unlinked template, not only alerting or new ones, Problem, Tests (`tests/test_resolver.py`)

### Community 73 - "10. Engine loop"
Cohesion: 0.33
Nodes (6): 10.1 Start, 10.3 Commit timing, 10.4 Bootstrap and `days_back`, 10.5 `worker_status`, 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108), 10. Engine loop

## Knowledge Gaps
- **231 isolated node(s):** `DatabaseConfig`, `FlushConfig`, `StartConfig`, `BaselineConfig`, `DropConfig` (+226 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 473 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `create_app()` connect `create_app` to `test_links.py`, `AppConfig`, `suggest`, `File map`, `day_hour`, `test_web_alerts_templates.py`, `test_web_docs_baseline.py`, `incident_test.py`, `Web UI and HTTP API`, `app.py`, `connect`?**
  _High betweenness centrality (0.086) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `create_app()` (e.g. with `AppConfig` and `Baseline`) actually correct?**
  _`create_app()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `DatabaseConfig`, `FlushConfig`, `StartConfig` to the rest of the system?**
  _231 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `test_links.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05044291338582677 - nodes in this community are weakly interconnected._
- **Why does `Review Focus` connect `Review Focus` to `renderTemplates`, `core.js`, `loadTemplates`, `test_web_alerts_templates.py`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `normalize()` (e.g. with `5.1 Document links` and `5. Pipeline: Elasticsearch → engine, one process first`) actually correct?**
  _`normalize()` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Should `test_engine.py` be split into smaller, more focused modules?**
  _Cohesion score 0.10522496371552975 - nodes in this community are weakly interconnected._