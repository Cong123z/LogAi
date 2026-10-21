-- Several active LLM profiles, tried in priority order (lowest first); a dead one fails over to the next.
DROP INDEX llm_profiles_one_active;
ALTER TABLE llm_profiles ADD COLUMN priority int NOT NULL DEFAULT 100;
