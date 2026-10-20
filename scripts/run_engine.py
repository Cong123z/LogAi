"""Run the logai2 engine: python scripts/run_engine.py [config.yaml]"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai2.config import load_config  # noqa: E402
from logai2.db import connect  # noqa: E402
from logai2.engine import Engine  # noqa: E402
from logai2.metrics import Metrics  # noqa: E402
from logai2.reader.es_source import EsSource  # noqa: E402


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    engine = Engine(cfg, connect(cfg.database.dsn), EsSource(cfg.elasticsearch, cfg.reader),
                    metrics=Metrics(cfg.metrics.port))
    engine.run_forever()


if __name__ == "__main__":
    main()
