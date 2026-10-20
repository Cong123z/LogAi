"""One Drain3 template miner per service, state kept as blobs.

Template ids are per service (``lixi:T00001``), so one service's traffic never
reshapes another's tree. The owner persists ``dirty()`` blobs to the
``drain3_state`` table inside its flush transaction and passes them back to the
constructor on restart.
"""
from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from logai2.config import Drain3Config
from logai2.reader.normalize import PLACEHOLDER

# Drain3's similarity (Drain.get_seq_distance) skips template tokens equal to
# its own wildcard "<*>", so a message whose tokens are mostly normalizer
# placeholders could never reach sim_threshold against its own cluster and
# would create a new cluster on every occurrence. Normalizer placeholders are
# therefore handed to Drain3 as this literal token (counted as a match) and
# turned back into "<*>" in every template we expose.
DRAIN_LITERAL_PLACEHOLDER = "<#>"


def display_template(template: str) -> str:
    return template.replace(DRAIN_LITERAL_PLACEHOLDER, PLACEHOLDER)


class _BlobPersistence:
    """Drain3 persistence handler holding the state in memory."""

    def __init__(self, blob: Optional[bytes] = None):
        self.blob = blob

    def save_state(self, state: bytes) -> None:
        self.blob = state

    def load_state(self) -> Optional[bytes]:
        return self.blob


class Miners:
    def __init__(self, cfg: Drain3Config, blobs: Dict[str, bytes]):
        self._tm_config = TemplateMinerConfig()
        self._tm_config.drain_sim_th = cfg.sim_threshold
        self._tm_config.drain_depth = cfg.depth
        self._tm_config.drain_max_children = cfg.max_children
        self._tm_config.profiling_enabled = False
        self._miners: Dict[str, Tuple[TemplateMiner, _BlobPersistence]] = {}
        self._dirty: Set[str] = set()
        for service, blob in blobs.items():
            self._miner(service, blob)

    def _miner(self, service: str, blob: Optional[bytes] = None) -> TemplateMiner:
        entry = self._miners.get(service)
        if entry is None:
            persistence = _BlobPersistence(blob)
            # The handler must be set during construction so the miner restores
            # the blob; detach it afterwards so drain3 does not re-serialise the
            # whole tree on every template change (dirty() saves once per flush).
            miner = TemplateMiner(persistence, config=self._tm_config)
            miner.persistence_handler = None
            entry = self._miners[service] = (miner, persistence)
        return entry[0]

    def match(self, service: str, text: str) -> Tuple[str, str, bool]:
        """``text`` is already normalized; returns (template_id, display_text, is_new)."""
        result = self._miner(service).add_log_message(
            text.replace(PLACEHOLDER, DRAIN_LITERAL_PLACEHOLDER)
        )
        # change_type: "cluster_created" | "cluster_template_changed" | "none"
        # ponytail: a plain match (size++ only) does not mark dirty, so persisted
        # cluster sizes lag; mark every match dirty if sizes ever matter.
        if result["change_type"] != "none":
            self._dirty.add(service)
        template_id = f"{service}:T{result['cluster_id']:05d}"
        return template_id, display_template(result["template_mined"]), result["change_type"] == "cluster_created"

    def dirty(self) -> Dict[str, bytes]:
        """Serialized state of services changed since the last call."""
        out: Dict[str, bytes] = {}
        for service in self._dirty:
            miner, persistence = self._miners[service]
            miner.persistence_handler = persistence
            try:
                miner.save_state("flush")
            finally:
                miner.persistence_handler = None
            out[service] = persistence.blob
        self._dirty.clear()
        return out
