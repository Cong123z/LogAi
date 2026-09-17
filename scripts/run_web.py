"""Launch the Template Explorer web UI."""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai.web.app import create_app


def main():
    parser = argparse.ArgumentParser(description="LogAI Template Explorer")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=5555, help="Port")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LOGAI_WEB_DATA_DIR", "data"),
        help="Directory containing template_registry.json and group_registry.json",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    app = create_app(data_dir=args.data_dir)
    print(f"Template Explorer running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
