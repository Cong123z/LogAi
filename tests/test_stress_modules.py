from logai2.db import connect
from scripts.stress_modules import TARGETS, main
from tests.conftest import TEST_DSN


def test_stress_tiny_runs(capsys):
    assert main(["--dsn", TEST_DSN, "--scale", "tiny"]) == 0
    rows = [line.split("|")[1:-1] for line in capsys.readouterr().out.splitlines() if line.startswith("| ")]
    measures = {r[0].strip(): float(r[1]) for r in rows[1:]}   # skip the header
    assert set(measures) == {*TARGETS, "size of 7 days"}
    with connect(TEST_DSN) as c:   # the throwaway schemas are gone
        assert not c.execute("SELECT 1 FROM pg_namespace WHERE nspname LIKE 'stress%%'").fetchall()


def test_stress_refuses_a_load_over_the_disk_limit(capsys):
    assert main(["--dsn", TEST_DSN, "--max-bytes", "1000"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_stress_refuses_a_database_with_a_migrated_public_schema(capsys, monkeypatch):
    monkeypatch.setattr("scripts.stress_modules.public_is_migrated", lambda dsn: True)
    assert main(["--dsn", TEST_DSN, "--scale", "tiny"]) == 3
    assert "migrated public schema" in capsys.readouterr().err
