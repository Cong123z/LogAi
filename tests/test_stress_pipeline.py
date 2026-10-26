from logai2.db import connect
from scripts.stress_pipeline import main, run
from tests.conftest import TEST_DSN


def test_stress_pipeline_tiny_runs():
    code, r = run(["--dsn", TEST_DSN, "--scale", "tiny", "--scenarios", "N1,I1,I5,I6-surrogate-module"])
    assert code == 0
    n1, i1, i5, i6 = r["N1"], r["I1"], r["I5"], r["I6-surrogate-module"]
    assert n1["counted"] == n1["truth"] > 0 and n1["late"] == 0 and n1["changes"] == []   # no false alert
    assert i1["template ALERTING"] == 3 and i1["false alerts"] == []
    assert i5["tick_failures"] > 0 and i5["counted"] == i5["truth"] and i5["template keys sum = *total"]
    assert i6["stalled"] and "UnicodeEncodeError" in i6["errors"][0]   # docs/BUGS.md B2; flip when fixed
    with connect(TEST_DSN) as c:   # the throwaway schemas are gone
        assert not c.execute("SELECT 1 FROM pg_namespace WHERE nspname LIKE 'stress_pipe%%'").fetchall()


def test_stress_pipeline_refuses_over_the_disk_limit(capsys):
    assert main(["--dsn", TEST_DSN, "--scale", "tiny", "--scenarios", "N1", "--max-bytes", "1000"]) == 2
    assert "refused" in capsys.readouterr().out


def test_stress_pipeline_reread_tiny():
    code, r = run(["--dsn", TEST_DSN, "--scale", "tiny", "--scenarios", "R1,R3,R4"])
    assert code == 0
    r1, r3, r4 = r["R1"], r["R3"], r["R4"]
    assert r1["mismatches"] == 0 and r1["rows compared"] > 0 and r1["reread state"] == "succeeded"
    assert r1["truth day 1"] == r1["counted day 1 (A)"] == r1["counted day 1 (B)"] > 0
    assert r3["mismatches"] == 0 and r3["state"] == "succeeded" and r3["kills"] == 2 and r3["pos never back"]
    assert r4["state"] == "succeeded" and r4["warning"] and r4["skipped hours"] > 0
    assert r4["zeros after range"] == 0 and r4["mismatches"] == 0 and r4["other services written"]
    with connect(TEST_DSN) as c:
        assert not c.execute("SELECT 1 FROM pg_namespace WHERE nspname LIKE 'stress_pipe%%'").fetchall()
