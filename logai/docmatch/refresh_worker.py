"""Background documentation refresh for the realtime pipeline."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.storage.documentation import DocumentationCorpusStore, group_fingerprint
from logai.storage.registries import GroupRegistry, TemplateRegistry

logger = logging.getLogger("logai.documentation.refresh")


class DocumentationRefreshWorker:
    def __init__(
        self,
        store: DocumentationCorpusStore,
        matcher: DocumentationMatcher,
        groups: GroupRegistry,
        templates: TemplateRegistry,
        interval_seconds: float = 5.0,
    ):
        self.store = store
        self.matcher = matcher
        self.groups = groups
        self.templates = templates
        self.interval_seconds = max(0.25, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_input: tuple[str, str] | None = None
        self._invalidation_lock = threading.RLock()
        self._invalidated_groups: set[str] = set()

    def invalidate_groups(self, group_ids: set[str]) -> None:
        with self._invalidation_lock:
            self._invalidated_groups.update(group_ids)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="documentation-refresh", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except FileNotFoundError:
                # The backing directory may be a short-lived test/development
                # directory. Stop instead of logging forever after it is removed.
                return
            except Exception:  # noqa: BLE001
                logger.exception("Documentation refresh failed unexpectedly")
            self._stop.wait(self.interval_seconds)

    def refresh_once(self) -> bool:
        corpus = self.store.load_corpus()
        overrides_snapshot = self.store.load_overrides()
        input_revision = (corpus["revision"], overrides_snapshot["revision"])
        with self._invalidation_lock:
            invalidated = set(self._invalidated_groups)
        if input_revision == self._last_input and not invalidated:
            return False

        attempt_at = time.time()
        previous_status = self.store.load_status()
        try:
            if not self.matcher.reload():
                raise RuntimeError(self.matcher.last_error or "Unable to load documentation corpus")

            entries = {entry.doc_id: entry for entry in self.matcher.entries}
            overrides = overrides_snapshot["overrides"]
            cleared_groups = overrides_snapshot.get("cleared_groups", {})
            membership_generation, group_snapshot, centroids = (
                self.groups.membership_snapshot()
            )
            updates: Dict[str, Dict[str, Any]] = {}
            stale_groups: list[str] = []
            membership_changed_groups: list[str] = []

            for group in group_snapshot:
                if group.group_id in cleared_groups:
                    updates[group.group_id] = self._empty_update("none")
                    continue
                centroid = centroids.get(group.group_id)
                if centroid is None:
                    updates[group.group_id] = self._empty_update("none")
                    continue

                automatic = self.matcher.match(group.group_id, centroid)
                update = {
                    "documented": automatic.documented,
                    "documentation_id": automatic.documentation_id,
                    "confidence": automatic.similarity,
                    "error_code": automatic.error_code,
                    "documentation_source": "automatic" if automatic.documented else "none",
                }

                override = overrides.get(group.group_id)
                if isinstance(override, dict):
                    texts = [
                        template.template_text
                        for template_id in group.template_ids
                        if (template := self.templates.get(template_id)) is not None
                    ]
                    current_fingerprint = group_fingerprint(group.template_ids, texts)
                    doc_id = override.get("documentation_id")
                    if override.get("group_fingerprint") != current_fingerprint:
                        membership_changed_groups.append(group.group_id)
                    if doc_id not in entries:
                        update["documentation_source"] = "stale_override"
                        stale_groups.append(group.group_id)
                    else:
                        forced = self.matcher.match_document(group.group_id, str(doc_id), centroid)
                        update = {
                            "documented": True,
                            "documentation_id": forced.documentation_id,
                            "confidence": forced.similarity,
                            "error_code": forced.error_code,
                            "documentation_source": "manual",
                        }
                updates[group.group_id] = update

            # Do not activate work based on an obsolete edit.
            latest = (
                self.store.load_corpus()["revision"],
                self.store.load_overrides()["revision"],
            )
            if latest != input_revision:
                return False

            if not self.groups.apply_documentation_updates(
                updates, expected_generation=membership_generation
            ):
                return False
            applied_at = time.time()
            self.store.write_status({
                "applied_corpus_revision": input_revision[0],
                "applied_override_revision": input_revision[1],
                "attempted_corpus_revision": input_revision[0],
                "attempted_override_revision": input_revision[1],
                "last_attempt_at": attempt_at,
                "last_applied_at": applied_at,
                "stale_group_ids": sorted(stale_groups),
                "membership_changed_group_ids": sorted(membership_changed_groups),
                "error": None,
            })
            self._last_input = input_revision
            with self._invalidation_lock:
                self._invalidated_groups.difference_update(invalidated)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Documentation revision could not be applied: %s", exc)
            self.store.write_status({
                "applied_corpus_revision": previous_status.get("applied_corpus_revision"),
                "applied_override_revision": previous_status.get("applied_override_revision"),
                "attempted_corpus_revision": input_revision[0],
                "attempted_override_revision": input_revision[1],
                "last_attempt_at": attempt_at,
                "last_applied_at": previous_status.get("last_applied_at"),
                "stale_group_ids": previous_status.get("stale_group_ids", []),
                "membership_changed_group_ids": previous_status.get(
                    "membership_changed_group_ids", []
                ),
                "error": str(exc),
            })
            return False

    @staticmethod
    def _empty_update(source: str) -> Dict[str, Any]:
        return {
            "documented": False,
            "documentation_id": None,
            "confidence": 0.0,
            "error_code": "",
            "documentation_source": source,
        }
