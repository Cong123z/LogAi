"""Launch the Template Explorer web UI."""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai.web.app import create_app
from logai.config import load_config


def _storage_path(base: Path, filename: str) -> str:
    """Resolve a StorageConfig filename exactly as the engine does."""
    return str(base / Path(filename))


def main():
    parser = argparse.ArgumentParser(description="LogAI Template Explorer")
    parser.add_argument("--host", default=os.environ.get("LOGAI_WEB_HOST", "0.0.0.0"), help="Bind address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOGAI_WEB_PORT", "5555")), help="Port")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get(
            "LOGAI_WEB_DATA_DIR",
            os.environ.get("LOGAI_STORAGE_BASE_DIR", "data"),
        ),
        help="Directory containing template_registry.json and group_registry.json",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    config = load_config(args.config)
    web_base = Path(args.data_dir)

    def web_path(value: str, default_name: str) -> str:
        path = Path(value)
        if path.is_absolute():
            return str(path)
        if path.parent == Path("data"):
            return str(web_base / default_name)
        return str(path)

    app = create_app(
        data_dir=args.data_dir,
        corpus_path=web_path(config.doc_matcher.corpus_path, "documentation_corpus.json"),
        overrides_path=web_path(config.doc_matcher.overrides_path, "documentation_overrides.json"),
        status_path=web_path(config.doc_matcher.status_path, "documentation_status.json"),
        seed_path=config.doc_matcher.seed_corpus_path,
        grouping_overrides_path=_storage_path(
            web_base, config.storage.grouping_overrides_file
        ),
        grouping_status_path=_storage_path(
            web_base, config.storage.grouping_status_file
        ),
        grouping_stale_seconds=config.grouping.engine_status_stale_seconds,
        service_analysis_path=_storage_path(
            web_base, config.storage.service_analysis_file
        ),
        analysis_requests_path=_storage_path(
            web_base, config.storage.analysis_requests_file
        ),
        llm_profiles_path=_storage_path(
            web_base, config.storage.llm_profiles_file
        ),
    )
    print(f"Template Explorer running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
