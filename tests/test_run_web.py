from pathlib import Path

from scripts.run_web import _storage_path


def test_storage_path_is_relative_to_web_data_directory():
    assert _storage_path(Path("/srv/logai"), "grouping_overrides.json") == (
        "/srv/logai/grouping_overrides.json"
    )
    assert _storage_path(Path("/srv/logai"), "state/grouping_status.json") == (
        "/srv/logai/state/grouping_status.json"
    )


def test_storage_path_preserves_absolute_filename():
    assert _storage_path(Path("/srv/logai"), "/tmp/grouping.json") == (
        "/tmp/grouping.json"
    )
