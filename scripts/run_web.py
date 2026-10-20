"""Run the logai2 web console: python scripts/run_web.py [config.yaml]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai2.config import load_config  # noqa: E402
from logai2.web.app import create_app  # noqa: E402


def main() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    create_app(cfg).run(host=cfg.web.host, port=cfg.web.port, threaded=True)


if __name__ == "__main__":
    main()
