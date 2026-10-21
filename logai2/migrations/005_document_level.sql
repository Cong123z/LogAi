-- The level class of a document's ERROR line (ERROR | WARN | INFO; NULL = any): automatic matching only
-- links templates of the same class, and the LLM is shown it as evidence.
ALTER TABLE documents ADD COLUMN level text;
-- From the templates people linked (or the LLM wrote it for), when they agree on one class.
UPDATE documents d SET level = s.level
FROM (SELECT l.doc_id, min(c.cls) AS level
      FROM template_doc_links l JOIN templates t USING (service, template_id)
      CROSS JOIN LATERAL (SELECT CASE WHEN upper(t.level) IN ('ERROR', 'FATAL', 'CRITICAL') THEN 'ERROR'
                                      WHEN upper(t.level) IN ('WARN', 'WARNING') THEN 'WARN' ELSE 'INFO' END AS cls) c
      WHERE l.source IN ('manual', 'draft') AND l.doc_id IS NOT NULL
      GROUP BY l.doc_id HAVING count(DISTINCT c.cls) = 1) s
WHERE d.doc_id = s.doc_id;
