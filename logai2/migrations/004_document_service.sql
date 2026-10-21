-- Documents belong to a service (NULL = shared: applies to every service). Automatic matching and the
-- LLM's candidates stay within the template's service plus shared documents.
ALTER TABLE documents ADD COLUMN service text;
-- Existing documents: the one service whose templates use them, else shared.
UPDATE documents d SET service = s.service
FROM (SELECT doc_id, min(service) AS service FROM template_doc_links
      WHERE doc_id IS NOT NULL AND source IN ('manual', 'inherited', 'corpus', 'llm', 'draft')
      GROUP BY doc_id HAVING count(DISTINCT service) = 1) s
WHERE d.doc_id = s.doc_id;
CREATE INDEX documents_service ON documents (service);
-- An LLM draft written from other services' documents names them (shown with the suggestion).
ALTER TABLE documents ADD COLUMN based_on text[] NOT NULL DEFAULT '{}';
