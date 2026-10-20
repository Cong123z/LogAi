"""Live incident-injection test of logai2: detection, document rows and false alerts.

Needs a running stack: the replayer (:8001) replaying at 1x, the engine
(:9118/metrics) and the web (:5565). Stdlib only, runs on the host.

    python3 scripts/incident_test.py run [--quick]
    python3 scripts/incident_test.py report reports/incident-YYYYmmdd-HHMM

`run` resolves each target's template by text, injects bursts through the
replayer and stores /api/alerts and log_alert_state every 5 s.
`report` turns a run directory into report.md with the 5 checks of spec
section 8 (rerunnable on old runs).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SAMPLE_SECONDS = 5
CHECKS = 3  # check 1: the burst template must be ALERTING within this many minute checks
# A minute is checked once the ES upper bound (now - 30 s) has passed its end; the rest
# covers index refresh, flush and the 5 s sampling.
# ponytail: fixed allowance; derive it from logai_lag_seconds if runs flap on it.
CLOSE_DELAY_SECONDS = 60
MAX_PAGE_SIZE = 500  # the web's cap on /api/templates?size=

# (label, service, regex over the replayed line and the template text, expected document).
# Texts are the logai-engine 2026-10-13 targets; documents must be linked on /#templates first.
SERVICE = "recharge"
LIXI = ("lixi", SERVICE, r"enterd Receiving subscriber incorrect", "DOC-001")
SQL_TIMEOUT = ("sqltimeout", SERVICE, r"pGetNumberTopupInvalid hava exception", "DOC-003")
SLOW_DB = ("slowdb", SERVICE, r"CHAM_DB_LEVEL", "DOC-003")


def _phase(name: str, kind: str, injects: list) -> List[Dict[str, Any]]:
    """A 10-min injection phase and its 15-min recovery. injects: [(target, per_minute)]."""
    targets = [t for t, _ in injects]
    return [
        {"name": name, "kind": kind, "minutes": 10, "targets": targets,
         "injects": [{"label": f"{name}-{t[0]}", "pattern": t[2], "per_minute": rate} for t, rate in injects]},
        {"name": f"{name}-recovery", "kind": "recovery", "minutes": 15, "targets": targets, "injects": []},
    ]


SCENARIO: List[Dict[str, Any]] = [
    {"name": "warmup", "kind": "warmup", "minutes": 30, "targets": [], "injects": []},
    {"name": "control", "kind": "control", "minutes": 30, "targets": [], "injects": []},
    *_phase("B1-x50", "burst", [(LIXI, 150)]),
    *_phase("B2-x20", "burst", [(LIXI, 60)]),
    *_phase("B3-x10", "burst", [(LIXI, 30)]),
    *_phase("B4-rare", "burst", [(SQL_TIMEOUT, 30)]),
    # check 3: two templates of DOC-003 at a modest rate each; the doc key sums them
    *_phase("B5-spread", "spread", [(SLOW_DB, 10), (SQL_TIMEOUT, 10)]),
]


# --------------------------------------------------------------------- HTTP

def http(method: str, url: str, body: Any = None, timeout: float = 15) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(), exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


METRIC_RE = re.compile(r'^(log_alert_state|logai_lag_seconds)(?:\{([^}]*)\})? (\S+)$')
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_metrics(text: str) -> Dict[str, Any]:
    """Current alert state per "service|doc_id" series, and the engine lag."""
    out: Dict[str, Any] = {"states": {}, "lag": None}
    for line in text.splitlines():
        m = METRIC_RE.match(line)
        if not m:
            continue
        labels, value = dict(LABEL_RE.findall(m.group(2) or "")), float(m.group(3))
        if m.group(1) == "logai_lag_seconds":
            out["lag"] = value
        elif value == 1:
            out["states"][f"{labels['service']}|{labels['doc_id']}"] = labels["state"]
    return out


def match_template(items: List[Dict[str, Any]], regex: str) -> str:
    """The one template whose text matches `regex`; fail fast on none or several."""
    hits = [i for i in items if re.search(regex, i["text"])]
    if len(hits) != 1:
        found = ", ".join(f"{i['template_id']} {i['text'][:60]!r}" for i in hits)
        raise ValueError(f"/{regex}/: " + (f"{len(hits)} templates match: {found}" if hits else "no template matches"))
    return hits[0]["template_id"]


# ---------------------------------------------------------------------- run

class Runner:
    def __init__(self, args: argparse.Namespace, out: Path):
        self.args, self.out = args, out
        self.phase = "start"
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def write(self, name: str, record: Dict[str, Any]) -> None:
        with self.lock, open(self.out / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def scrape(self) -> Dict[str, Any]:
        with urllib.request.urlopen(f"{self.args.engine}/metrics", timeout=10) as resp:
            return parse_metrics(resp.read().decode())

    def sampler(self) -> None:
        while not self.stop.is_set():
            started = time.time()
            try:
                status, alerts = http("GET", f"{self.args.web}/api/alerts")
                if status != 200:
                    raise RuntimeError(f"/api/alerts {status}")
                self.write("samples.jsonl", {"ts": started, "phase": self.phase, "alerts": alerts, **self.scrape()})
            except Exception as exc:  # keep sampling through engine or web hiccups
                print(f"sample failed: {exc}", file=sys.stderr, flush=True)
            self.stop.wait(max(0.0, SAMPLE_SECONDS - (time.time() - started)))

    def templates(self, service: str) -> List[Dict[str, Any]]:
        items, page = [], 1
        while True:
            query = urllib.parse.urlencode({"service": service, "size": MAX_PAGE_SIZE, "page": page})
            status, data = http("GET", f"{self.args.web}/api/templates?{query}")
            if status != 200:
                sys.exit(f"/api/templates {status}: {data}")
            items += data["items"]
            if not data["items"] or len(items) >= data["total"]:
                return items
            page += 1

    def resolve(self) -> Dict[str, Dict[str, Any]]:
        """label -> {service, template_id, text, doc}. Exits on an unresolved or unlinked target."""
        targets = {t[0]: t for p in SCENARIO for t in p["targets"]}
        by_service = {svc: self.templates(svc) for svc in {t[1] for t in targets.values()}}
        resolved, problems = {}, []
        for label, (_, svc, regex, doc) in sorted(targets.items()):
            try:
                tid = match_template(by_service[svc], regex)
            except ValueError as exc:
                problems.append(f"{label}: {exc}")
                continue
            item = next(i for i in by_service[svc] if i["template_id"] == tid)
            if item.get("doc_id") != doc:
                problems.append(f"{label}: {tid} is linked to {item.get('doc_id')}, expected {doc} (link it on /#templates)")
            resolved[label] = {"service": svc, "template_id": tid, "text": item["text"], "doc": doc}
        if problems:
            sys.exit("target resolution failed:\n  - " + "\n  - ".join(problems))
        return resolved

    def preflight(self) -> None:
        problems = []
        _, rep = http("GET", f"{self.args.replayer}/status")
        if not (isinstance(rep, dict) and rep.get("replay", {}).get("is_running")):
            problems.append("replay is not running (POST :8001/replay/start)")
        _, pipe = http("GET", f"{self.args.web}/api/pipeline")
        if not isinstance(pipe, dict) or pipe.get("stale") is not False:
            problems.append("engine heartbeat is stale or missing (/api/pipeline)")
        lag = self.scrape()["lag"]
        if lag is None or lag > 60:
            problems.append(f"engine lag is {lag} s")
        if problems:
            sys.exit("preflight failed:\n  - " + "\n  - ".join(problems))

    def inject(self, phase: Dict[str, Any], minutes: float) -> None:
        for inj in phase["injects"]:
            status, body = http("POST", f"{self.args.replayer}/replay/inject",
                                {**inj, "minutes": minutes}, timeout=120)
            print(f"  inject {inj['label']} {inj['per_minute']}/min: {status} {body}", flush=True)
            self.write("phases.jsonl", {"event": "inject", "ts": time.time(), "status": status, **inj})

    def run(self) -> None:
        self.preflight()
        resolved = self.resolve()
        scale = 0.5 if self.args.quick else 1.0
        phases = [dict(p, minutes=p["minutes"] * scale) for p in SCENARIO]
        meta = {"started_at": time.time(), "quick": self.args.quick, "targets": resolved, "scenario": phases}
        (self.out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        threading.Thread(target=self.sampler, daemon=True).start()
        try:
            for phase in phases:
                start = time.time()
                self.phase = phase["name"]
                end = start + phase["minutes"] * 60
                print(f"{time.strftime('%H:%M:%S')} {phase['name']} ({phase['minutes']:g} min)", flush=True)
                self.write("phases.jsonl", {"event": "phase", "name": phase["name"], "kind": phase["kind"],
                                            "start": start, "end": end,
                                            "targets": [t[0] for t in phase["targets"]]})
                self.inject(phase, phase["minutes"])
                time.sleep(max(0.0, end - time.time()))
        finally:
            http("POST", f"{self.args.replayer}/replay/inject/stop")
            self.stop.set()
        print(f"done: {self.out}", flush=True)


# ------------------------------------------------------------------- report

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check1_deadline(burst_start: float) -> float:
    """Latest time the burst template may turn ALERTING: CHECKS minute checks counted from
    the first whole minute of the burst, plus the time it takes that minute to be checked."""
    first_minute = math.ceil(burst_start / 60) * 60
    return first_minute + CHECKS * 60 + CLOSE_DELAY_SECONDS


def first_template_state(samples: List[Dict[str, Any]], service: str, template_id: str,
                         state: str, start: float) -> Optional[Dict[str, Any]]:
    """First sample at or after `start` where the template is in `state`: {"ts", "row"}."""
    for s in samples:
        if s["ts"] < start:
            continue
        for row in s["alerts"]:
            if row["service"] == service and any(t["template_id"] == template_id and t["state"] == state
                                                 for t in row["templates"]):
                return {"ts": s["ts"], "row": row}
    return None


def row_key(row: Dict[str, Any]) -> str:
    if row["kind"] == "document":
        return f"{row['service']} doc {row['doc_id']}"
    if row["kind"] == "service":
        return f"{row['service']} service"
    return f"{row['service']} {row['keys'][0]}"


def alert_onsets(samples: List[Dict[str, Any]], prev: Optional[set] = None) -> Dict[str, int]:
    """ALERTING onsets per alert row. `prev` holds the rows ALERTING before the first
    sample, so an alert carried over from the previous phase is not counted again."""
    prev, onsets = set(prev or ()), {}
    for s in samples:
        now = {row_key(r) for r in s["alerts"] if r["state"] == "ALERTING"}
        for key in now - prev:
            onsets[key] = onsets.get(key, 0) + 1
        prev = now
    return onsets


def doc_key_state(sample: Dict[str, Any], service: str, doc: str) -> Optional[str]:
    for row in sample["alerts"]:
        for total in row.get("totals", []):
            if row["service"] == service and total["key"] == f"doc:{doc}":
                return total.get("state")
    return None


def summarize(run_dir: Path) -> str:
    meta = json.loads((run_dir / "meta.json").read_text())
    targets = meta["targets"]
    phases = [p for p in read_jsonl(run_dir / "phases.jsonl") if p["event"] == "phase"]
    samples = read_jsonl(run_dir / "samples.jsonl")
    tick = lambda ok: "PASS" if ok else "FAIL"
    after = lambda ts, start: "–" if ts is None else f"{ts - start:.0f} s"
    lines = [f"# Incident test {run_dir.name}", "", f"- quick: {meta.get('quick')}", "",
             "| target | template | text | document |", "|---|---|---|---|"]
    lines += [f"| {label} | {t['template_id']} | {t['text'][:70]} | {t['doc']} |" for label, t in sorted(targets.items())]
    lines += ["", "## Bursts: checks 1, 2 and 4", "",
              "| burst | target | 1: ALERTING after | deadline | 1 | 2: doc row ALERTING (API / metric) | 2 "
              "| 4: document shown | 4 |",
              "|---|---|---:|---:|---|---|---|---|---|"]
    for p in phases:
        if p["kind"] != "burst":
            continue
        inside = [s for s in samples if s["phase"] == p["name"]]
        for label in p["targets"]:
            t = targets[label]
            deadline = check1_deadline(p["start"])
            hit = first_template_state(inside, t["service"], t["template_id"], "ALERTING", p["start"])
            ok1 = hit is not None and hit["ts"] <= deadline
            api = any(r["kind"] == "document" and r["service"] == t["service"] and r["doc_id"] == t["doc"]
                      and r["state"] == "ALERTING" for s in inside for r in s["alerts"])
            metric = any(s["states"].get(f"{t['service']}|{t['doc']}") == "ALERTING" for s in inside)
            shown = hit["row"]["doc_id"] if hit else None
            lines.append(
                f"| {p['name']} | {label} | {after(hit and hit['ts'], p['start'])} "
                f"| {deadline - p['start']:.0f} s | {tick(ok1)} | {'yes' if api else 'no'} / {'yes' if metric else 'no'} "
                f"| {tick(api)} | {shown or '–'} | {tick(shown == t['doc'])} |")
    lines.append("")

    lines += ["## Spread: check 3", "", "| phase | document | doc key ALERTING after | 3 |", "|---|---|---:|---|"]
    for p in phases:
        if p["kind"] != "spread":
            continue
        inside = [s for s in samples if s["phase"] == p["name"]]
        for svc, doc in sorted({(targets[l]["service"], targets[l]["doc"]) for l in p["targets"]}):
            hit = next((s["ts"] for s in inside if doc_key_state(s, svc, doc) == "ALERTING"), None)
            lines.append(f"| {p['name']} | {doc} | {after(hit, p['start'])} | {tick(hit is not None)} |")
    lines.append("")

    control = [s for s in samples if s["phase"] == "control"]
    hours = len(control) * SAMPLE_SECONDS / 3600
    before = [s for s in samples if s["ts"] < (control[0]["ts"] if control else 0)]
    prev = {row_key(r) for r in before[-1]["alerts"] if r["state"] == "ALERTING"} if before else set()
    onsets = alert_onsets(control, prev)
    total = sum(onsets.values())
    rate = f"{total / hours:.1f}/h" if hours else "–"
    lines += ["## Control: check 5", "",
              f"{total} ALERTING onsets over {hours:.2f} h of control = **{rate}** "
              "(compare with logai-engine's Isolation Forest on the same window).", ""]
    if onsets:
        lines += ["| alert row | onsets |", "|---|---:|"]
        lines += [f"| {k} | {n} |" for k, n in sorted(onsets.items(), key=lambda kv: -kv[1])]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--quick", action="store_true", help="halve every phase")
    run.add_argument("--out", default="reports")
    run.add_argument("--engine", default=os.environ.get("LOGAI2_TEST_ENGINE", "http://localhost:9118"))
    run.add_argument("--web", default=os.environ.get("LOGAI2_TEST_WEB", "http://localhost:5565"))
    run.add_argument("--replayer", default=os.environ.get("LOGAI2_TEST_REPLAYER", "http://localhost:8001"))
    rep = sub.add_parser("report")
    rep.add_argument("run_dir")
    args = parser.parse_args()

    if args.cmd == "run":
        out = Path(args.out) / time.strftime("incident-%Y%m%d-%H%M")
        out.mkdir(parents=True, exist_ok=True)
        Runner(args, out).run()
        args.run_dir = str(out)
    report = summarize(Path(args.run_dir))
    (Path(args.run_dir) / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
