import json
import subprocess
import sys

from tests.conftest import TEST_DSN


def test_mem_probe_small(schema):
    out = subprocess.run([sys.executable, "scripts/mem_probe.py", "--dsn", f"{TEST_DSN}?options=-csearch_path%3D{schema},public",
                          "--templates", "500", "--minutes", "90"], capture_output=True, text=True, check=True)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["templates"] == 500 and r["baseline_rows"] == 500 * 48 and r["blob_bytes"] > 0
    assert r["load_peak_mib"] >= r["load_steady_mib"] > 0
    assert r["flush_peak_mib"] >= r["load_peak_mib"]
