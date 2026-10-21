"""Run the logai2 linker: python scripts/run_linker.py [config.yaml]

Embeds templates and documents' ERROR lines (bge-m3), keeps inherited and corpus links current,
and sends the rest to the LLM of the active profile (confirm a candidate, or suggest a document).
The engine creates the schema; the linker waits for it.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai2.config import load_config  # noqa: E402
from logai2.db import connect  # noqa: E402
from logai2.linker import Linker  # noqa: E402
from logai2.resolver import Resolver  # noqa: E402

logger = logging.getLogger("logai2.linker")


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    while True:
        try:
            conn = connect(cfg.database.dsn)
            if conn.execute("SELECT to_regclass('document_errors') AS t").fetchone()["t"]:
                conn.rollback()
                break
            conn.close()
            logger.info("waiting for the engine to create the schema")
        except Exception as exc:  # noqa: BLE001
            logger.warning("database unreachable: %s", exc)
        time.sleep(10)
    Linker(cfg, conn, resolver=Resolver(cfg, conn)).run_forever()


if __name__ == "__main__":
    main()
