#!/usr/bin/env python
"""Run the LogAI training pipeline against historical Elasticsearch logs.

Usage:
    python scripts/run_training.py --lookback-hours 168
    python scripts/run_training.py --config custom_config.yaml
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai.config import load_config
from logai.training.train_pipeline import run_training_from_elasticsearch


def main() -> None:
    parser = argparse.ArgumentParser(description="LogAI training pipeline")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--lookback-hours", type=float, default=None,
        help="Override training.lookback_seconds from config (hours)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    config.embedding.validate()
    lookback_seconds = (
        args.lookback_hours * 3600 if args.lookback_hours is not None else None
    )
    run_training_from_elasticsearch(config, lookback_seconds=lookback_seconds)


if __name__ == "__main__":
    main()
