-- Layered document links (plan docs/plans/2026-10-21-embeddings-doc-links.md).
-- pgvector lives in public so a schema-per-test run never drops it; search_path must include public.
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

ALTER TABLE templates ADD COLUMN embedding vector(1024);   -- cleared by the engine when the text changes

ALTER TABLE documents RENAME COLUMN body TO handling;
ALTER TABLE documents ADD COLUMN error text NOT NULL DEFAULT '',             -- log line(s) it is for, one per line
                      ADD COLUMN status text NOT NULL DEFAULT 'active',      -- active | draft | rejected
                      ADD COLUMN source text NOT NULL DEFAULT 'manual',      -- manual | llm
                      ADD COLUMN verdict text NOT NULL DEFAULT 'action',     -- action | safe
                      ADD COLUMN confidence real,
                      ADD COLUMN embedded_revision text;                     -- revision document_errors was built from
CREATE TABLE document_errors (doc_id text NOT NULL REFERENCES documents ON DELETE CASCADE,
                              n smallint NOT NULL, text text NOT NULL, embedding vector(1024) NOT NULL,
                              PRIMARY KEY (doc_id, n));

-- source: manual | inherited | corpus | llm | draft | rejected; manual with doc_id NULL = "no document"
ALTER TABLE template_doc_links ALTER COLUMN doc_id DROP NOT NULL,
                               ADD COLUMN similarity real,
                               ADD COLUMN inherited_from text,
                               ADD COLUMN note text;

CREATE TABLE llm_profiles (name text PRIMARY KEY, base_url text NOT NULL, model text NOT NULL,
                           api_key text NOT NULL DEFAULT '', timeout_seconds int NOT NULL DEFAULT 120,
                           active boolean NOT NULL DEFAULT false, revision text NOT NULL,
                           updated_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX llm_profiles_one_active ON llm_profiles (active) WHERE active;
