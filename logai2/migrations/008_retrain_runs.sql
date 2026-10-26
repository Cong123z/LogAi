CREATE TABLE retrain_runs (id bigserial PRIMARY KEY, kind text NOT NULL, trigger text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}', state text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, pos_ms bigint, steps jsonb NOT NULL DEFAULT '[]', result jsonb NOT NULL DEFAULT '{}',
  error text, error_type text, failed_step text, log jsonb NOT NULL DEFAULT '[]');
CREATE INDEX ON retrain_runs (started_at DESC);
