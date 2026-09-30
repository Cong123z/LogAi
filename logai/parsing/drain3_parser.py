"""Drain3-based log parsing (plan section 3.2 / 4.2).

Drain3 maintains its own persisted tree state (we point it at a file under
data/) which already gives us template_id stability across restarts. We
wrap it to produce our `ParsedEvent` contract and to know whether a message
matched an existing template ("Known Template") or created a new one
("Unknown Template", plan section 4.3/4.4).
"""
from __future__ import annotations

import re
from typing import List, Pattern

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from logai.config import Drain3Config
from logai.models import ParsedEvent, RawLog


class Drain3Parser:
    def __init__(self, config: Drain3Config):
        self.config = config
        tm_config = TemplateMinerConfig()
        tm_config.drain_sim_th = config.sim_threshold
        tm_config.drain_depth = config.depth
        tm_config.drain_max_children = config.max_children
        tm_config.profiling_enabled = False
        self._extra_delimiters: List[str] = list(getattr(config, "extra_delimiters", []))
        tm_config.drain_extra_delimiters = self._extra_delimiters

        masking_rules = getattr(config, "masking_rules", [])
        # Rules with mask_with == "" mean "delete this span outright" (e.g. the
        # non-semantic "<date> <time> <LEVEL> [<thread>] " prefix). Drain3's
        # own MaskingInstruction always wraps mask_with in its mask_prefix/
        # suffix (default "<"/">"), so an empty mask_with there would still
        # leave a literal "<>" behind. Apply those ourselves via plain
        # re.sub before drain3 ever sees the message; only pass the
        # placeholder-producing rules into drain3's own masking pipeline.
        self._strip_patterns: List[Pattern[str]] = [
            re.compile(rule["pattern"])
            for rule in masking_rules
            if rule.get("mask_with", "*") == ""
        ]
        wildcard_rules = [rule for rule in masking_rules if rule.get("mask_with", "*") != ""]
        if wildcard_rules:
            tm_config.masking_instructions = [
                MaskingInstruction(pattern=rule["pattern"], mask_with=rule.get("mask_with", "*"))
                for rule in wildcard_rules
            ]

        persistence = FilePersistence(config.persistence_path)
        self.miner = TemplateMiner(persistence, config=tm_config)
    def parse(self, raw: RawLog) -> ParsedEvent:
        message = raw.message
        for pattern in self._strip_patterns:
            message = pattern.sub("", message)
        result = self.miner.add_log_message(message)
        # drain3 result keys: cluster_id, cluster_size, template_mined,
        # change_type ("cluster_created" | "cluster_template_changed" | "none")
        template_id = f"T{result['cluster_id']:05d}"
        template = result["template_mined"]
        is_new = result["change_type"] == "cluster_created"
        for delimiter in self._extra_delimiters:
            message = message.replace(delimiter, " ")
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
