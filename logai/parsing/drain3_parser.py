"""Drain3-based log parsing (plan section 3.2 / 4.2).

Drain3 maintains its own persisted tree state (we point it at a file under
data/) which already gives us template_id stability across restarts. We
wrap it to produce our `ParsedEvent` contract and to know whether a message
matched an existing template ("Known Template") or created a new one
("Unknown Template", plan section 4.3/4.4).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from logai.config import Drain3Config
from logai.models import ParsedEvent, RawLog
from logai.parsing.preprocessor import PLACEHOLDER, normalize

# Drain3's similarity (Drain.get_seq_distance) skips template tokens equal to
# its own wildcard "<*>", so a message whose tokens are mostly preprocessor
# placeholders could never reach sim_threshold against its own cluster and
# would create a new cluster on every occurrence. Preprocessor placeholders
# are therefore handed to Drain3 as this literal token (counted as a match)
# and turned back into "<*>" in every template we expose.
DRAIN_LITERAL_PLACEHOLDER = "<#>"


def display_template(template: str) -> str:
    return template.replace(DRAIN_LITERAL_PLACEHOLDER, PLACEHOLDER)


class AtomicFilePersistence:
    """Drain3 persistence handler that never leaves a half-written state file.

    drain3's own FilePersistence writes the file in place, so a crash or
    OOM-kill during a multi-megabyte write corrupts the whole template tree.
    Write to a temp file, fsync, then atomically rename over the old state.
    """

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.last_saved_bytes = 0

    def save_state(self, state: bytes) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_name(self.file_path.name + ".tmp")
        with open(tmp_path, "wb") as stream:
            stream.write(state)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, self.file_path)
        self.last_saved_bytes = len(state)

    def load_state(self) -> Optional[bytes]:
        if not self.file_path.exists():
            return None
        return self.file_path.read_bytes()


class Drain3Parser:
    def __init__(self, config: Drain3Config, autosave: bool = True):
        """``autosave=True`` keeps drain3's default of re-serialising the
        *whole* state after every template change. That is O(clusters) per
        change and dominates bulk parsing once the tree is large, so the
        training pipeline passes ``autosave=False`` and calls ``save_state()``
        once per batch instead."""
        self.config = config
        tm_config = TemplateMinerConfig()
        tm_config.drain_sim_th = config.sim_threshold
        tm_config.drain_depth = config.depth
        tm_config.drain_max_children = config.max_children
        tm_config.profiling_enabled = False
        # Wording-only normalisation (logai/parsing/preprocessor.py) runs
        # before Drain3; config masking_rules are optional extra Drain3 rules
        # applied after it.
        masking_rules: List[dict] = getattr(config, "masking_rules", [])
        if masking_rules:
            tm_config.masking_instructions = [
                MaskingInstruction(pattern=rule["pattern"], mask_with=rule.get("mask_with", "*"))
                for rule in masking_rules
            ]

        self._autosave = autosave
        self._persistence = AtomicFilePersistence(config.persistence_path)
        # The handler must be set during construction so the miner restores
        # any existing state; detach it afterwards to disable per-change saves.
        self.miner = TemplateMiner(self._persistence, config=tm_config)
        if not autosave:
            self.miner.persistence_handler = None

    def save_state(self, reason: str) -> int:
        """Persist the full Drain3 state now; returns the serialised size."""
        self.miner.persistence_handler = self._persistence
        try:
            self.miner.save_state(reason)
        finally:
            if not self._autosave:
                self.miner.persistence_handler = None
        return self._persistence.last_saved_bytes

    @property
    def persistence_path(self) -> Path:
        return self._persistence.file_path

    def cluster_count(self) -> int:
        return len(self.miner.drain.clusters)

    def message_count(self) -> int:
        return int(self.miner.drain.get_total_cluster_size())

    def preprocess(self, message: str) -> str:
        if not getattr(self.config, "preprocess", True):
            return message
        return normalize(
            message,
            max_chars=self.config.max_chars,
            max_tokens=self.config.max_tokens,
            level_search_tokens=self.config.level_search_tokens,
        )

    def parse(self, raw: RawLog) -> ParsedEvent:
        message = self.preprocess(raw.message)
        result = self.miner.add_log_message(
            message.replace(PLACEHOLDER, DRAIN_LITERAL_PLACEHOLDER)
        )
        # drain3 result keys: cluster_id, cluster_size, template_mined,
        # change_type ("cluster_created" | "cluster_template_changed" | "none")
        template_id = f"T{result['cluster_id']:05d}"
        template = display_template(result["template_mined"])
        is_new = result["change_type"] == "cluster_created"
        parameters = self._extract_parameters(template, message)
        return ParsedEvent(
            raw=raw,
            template_id=template_id,
            template=template,
            parameters=parameters,
            is_new_template=is_new,
        )

    @staticmethod
    def _extract_parameters(template: str, message: str) -> List[str]:
        """Best-effort extraction of the `<*>` slots' actual values by
        aligning template tokens against the raw message tokens."""
        t_tokens = template.split()
        m_tokens = message.split()
        params: List[str] = []
        if len(t_tokens) != len(m_tokens):
            return params
        for t_tok, m_tok in zip(t_tokens, m_tokens):
            if t_tok == "<*>":
                params.append(m_tok)
            elif "<*>" in t_tok:
                # Handle prefixes/suffixes attached to <*>, e.g. "<*>:Exception"
                parts = t_tok.split("<*>")
                val = m_tok
                if parts[0] and val.startswith(parts[0]):
                    val = val[len(parts[0]):]
                if len(parts) > 1 and parts[1] and val.endswith(parts[1]):
                    val = val[:-len(parts[1])]
                params.append(val)
        return params
