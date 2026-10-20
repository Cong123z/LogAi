CREATE TABLE schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE services (service text PRIMARY KEY, partition int NOT NULL);
CREATE TABLE modules (module_code text PRIMARY KEY, service text NOT NULL REFERENCES services);
CREATE TABLE es_cursors (index text PRIMARY KEY, since_ms bigint NOT NULL, floor_ms bigint NOT NULL,
                         updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE templates (service text NOT NULL, template_id text NOT NULL, level text NOT NULL,
                        text text NOT NULL, first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
                        PRIMARY KEY (service, template_id));
CREATE TABLE drain3_state (service text PRIMARY KEY, blob bytea NOT NULL, version int NOT NULL DEFAULT 1);
CREATE TABLE service_hours (service text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
                            n_minutes int NOT NULL, PRIMARY KEY (service, day, hour)) PARTITION BY RANGE (day);
CREATE TABLE hourly_counts (service text NOT NULL, key text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
                            sum bigint NOT NULL, sumsq bigint NOT NULL,
                            PRIMARY KEY (service, key, day, hour)) PARTITION BY RANGE (day);
CREATE TABLE alert_minutes (service text NOT NULL, key text NOT NULL, module_code text NOT NULL,
                            day date NOT NULL, minute timestamptz NOT NULL, count int NOT NULL,
                            PRIMARY KEY (service, key, module_code, minute, day)) PARTITION BY RANGE (day);
CREATE TABLE day_status (service text NOT NULL, day date NOT NULL, committed boolean NOT NULL DEFAULT false,
                         daytype text NOT NULL, PRIMARY KEY (service, day));
CREATE TABLE review_exclusions (id bigserial PRIMARY KEY, service text NOT NULL, day date NOT NULL,
                                hour_from smallint NOT NULL, hour_to smallint NOT NULL,   -- [from, to)
                                decision text NOT NULL CHECK (decision IN ('exclude', 'keep')),
                                created_by text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE baseline (service text NOT NULL, key text NOT NULL, daytype text NOT NULL, hour smallint NOT NULL,
                       mean double precision NOT NULL, std double precision NOT NULL, days int NOT NULL,
                       PRIMARY KEY (service, key, daytype, hour));
CREATE TABLE alert_state (service text NOT NULL, key text NOT NULL, state text NOT NULL, since timestamptz NOT NULL,
                          streak int NOT NULL, score double precision NOT NULL, count int NOT NULL,
                          band_low double precision, band_high double precision, PRIMARY KEY (service, key));
CREATE TABLE alert_changes (service text NOT NULL, key text NOT NULL, at timestamptz NOT NULL,
                            from_state text NOT NULL, to_state text NOT NULL, count int NOT NULL,
                            band_low double precision, band_high double precision, PRIMARY KEY (service, key, at));
CREATE TABLE documents (doc_id text PRIMARY KEY, title text NOT NULL, error_code text NOT NULL DEFAULT '',
                        body text NOT NULL, revision text NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE template_doc_links (service text NOT NULL, template_id text NOT NULL,
                                 doc_id text NOT NULL REFERENCES documents,
                                 source text NOT NULL DEFAULT 'manual', linked_at timestamptz NOT NULL DEFAULT now(),
                                 PRIMARY KEY (service, template_id));
CREATE TABLE error_samples (service text NOT NULL, template_id text NOT NULL, at timestamptz NOT NULL, line text NOT NULL);
CREATE INDEX error_samples_key ON error_samples (service, template_id, at DESC);
CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL, revision text NOT NULL,
                       updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE commands (id bigserial PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
                       created_at timestamptz NOT NULL DEFAULT now(), done_at timestamptz, result jsonb);
CREATE TABLE worker_status (worker text PRIMARY KEY, status jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
