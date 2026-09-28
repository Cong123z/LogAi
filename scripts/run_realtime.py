#!/usr/bin/env python
"""Run the LogAI realtime pipeline: poll Elasticsearch, process events,
serve Prometheus metrics at /metrics.

Usage:
    python scripts/run_realtime.py
    python scripts/run_realtime.py --config custom_config.yaml
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai.config import load_config
from logai.realtime.realtime_pipeline import RealtimePipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="LogAI realtime pipeline")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    config.embedding.validate()
    pipeline = RealtimePipeline(config)
    pipeline.run_forever()


if __name__ == "__main__":
    main()
