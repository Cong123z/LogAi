-- Lab (baseline bake-off) tables. No partitions here: db.ensure_partitions creates lab_minute_counts' daily ones.
CREATE TABLE lab_minute_counts (
  service text NOT NULL, key text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
  counts int[] NOT NULL CHECK (array_length(counts, 1) = 60),
  PRIMARY KEY (service, key, day, hour)) PARTITION BY RANGE (day);

CREATE TABLE lab_runs (
  id bigserial PRIMARY KEY, site text NOT NULL CHECK (site IN ('laptop', 'company')),
  test text NOT NULL CHECK (test IN ('A', 'B', 'C')), status text NOT NULL DEFAULT 'running',
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  code_version text NOT NULL, image_tag text, data_schema text NOT NULL DEFAULT 'public',
  window_from date NOT NULL, window_to date NOT NULL, tune_to date NOT NULL, seed int NOT NULL,
  params jsonb NOT NULL,          -- methods, knob grids, scenario, budgets, split mode
  kinds jsonb,                    -- key count per kind, and the assignment hash
  error text);

CREATE TABLE lab_results (        -- one row per run x method x kind x scope ('all' or a service)
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, kind text, scope text,
  knob double precision, status text NOT NULL,   -- ok | skipped (not enough history) | unavailable | failed
  metrics jsonb NOT NULL, verdict text, strength text,
  PRIMARY KEY (run_id, method, kind, scope));

CREATE TABLE lab_episodes (
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, service text, key text,
  start_minute int, end_minute int, peak_score double precision, alerted_minutes int,
  PRIMARY KEY (run_id, method, service, key, start_minute));

CREATE TABLE lab_bands (          -- drawn series; step 5 for overview, 1 around episodes
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, service text, key text,
  step smallint, minute int, count int, expected real, low real, high real, high_flag boolean,
  PRIMARY KEY (run_id, method, service, key, step, minute));

CREATE TABLE lab_sweeps (
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, kind text, knob double precision,
  part text CHECK (part IN ('tune', 'test')), metrics jsonb NOT NULL,
  PRIMARY KEY (run_id, method, kind, knob, part));

CREATE TABLE lab_choices (
  kind text PRIMARY KEY, method text NOT NULL, knob double precision, run_id bigint,
  chosen_at timestamptz NOT NULL DEFAULT now(), note text);

CREATE TABLE lab_incidents (
  id bigserial PRIMARY KEY, source text NOT NULL CHECK (source IN ('synthetic', 'test-a', 'test-c')),
  data_schema text NOT NULL DEFAULT 'public', run_id bigint, service text NOT NULL, key text,
  type text NOT NULL, size double precision, start_minute int NOT NULL, end_minute int NOT NULL, meta jsonb);
