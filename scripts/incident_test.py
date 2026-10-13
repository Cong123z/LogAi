"""Live incident-injection test: false alerts, detection and LLM answers.

Needs a running stack: the replayer (:8001) replaying at 1x, the engine
(:9108/metrics) and the web (:5555). Stdlib only, runs on the host.

    python3 scripts/incident_test.py run [--quick] [--no-llm]
    python3 scripts/incident_test.py report reports/incident-YYYYmmdd-HHMM

`run` injects bursts through the replayer, scrapes alert state and score of
every group every 5 s, and asks the web for LLM analyses like a user would.
`report` turns a run directory into report.md (rerunnable on old runs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SAMPLE_SECONDS = 5
HELD_SAMPLES = 6  # >= 30 s above a threshold counts as an alert in the sweep
SWEEP = [0.58, 0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72]
PROBE_TIMEOUT = 480  # > llm.job_deadline_seconds (420), so a slow job is not logged as a timeout

# Targets are failure-text ERROR/WARN groups (agents.md 2026-10-12 LLM notes).
G0010 = ("lixi", r"enterd Receiving subscriber incorrect", "G0010", "DOC-001")
SQL_TIMEOUT = ("sqltimeout", r"pGetNumberTopupInvalid hava exception", "G_SINGLE_0051", "DOC-017")
CIPHER = ("cipher", r"Input length must be multiple of", "G_SINGLE_0043", "DOC-004")
SLOW_DB = ("slowdb", r"CHAM_DB_LEVEL", "G_SINGLE_0050", "DOC-003")

BURST_PROBES = [(3, "window"), (3, "service"), (9, "window")]


def _burst(name: str, injects: list) -> List[Dict[str, Any]]:
    """A 10-min burst phase and its 15-min recovery. injects: [(target, per_minute)]."""
    targets = {t[2]: t[3] for t, _ in injects}
    return [
        {"name": name, "kind": "burst", "minutes": 10, "targets": targets,
         "injects": [{"label": f"{name}-{t[0]}", "pattern": t[1], "per_minute": rate}
                     for t, rate in injects],
         "probes": BURST_PROBES},
        {"name": f"{name}-recovery", "kind": "recovery", "minutes": 15, "targets": targets,
         "injects": [], "probes": [(10, "service")]},
    ]


SCENARIO: List[Dict[str, Any]] = [
    {"name": "warmup", "kind": "warmup", "minutes": 30, "targets": {}, "injects": [], "probes": []},
    {"name": "control", "kind": "control", "minutes": 30, "targets": {}, "injects": [],
     "probes": [(15, "service"), (15, "window-top")]},
    *_burst("B1-x50", [(G0010, 150)]),
    *_burst("B2-x20", [(G0010, 60)]),
    *_burst("B3-x10", [(G0010, 30)]),
    *_burst("B4-rare", [(SQL_TIMEOUT, 30)]),
    *_burst("B5-multi", [(G0010, 150), (CIPHER, 30), (SLOW_DB, 30)]),
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


METRIC_RE = re.compile(r'^(log_anomaly_score|log_alert_state|log_alerts_total|'
                       r'logai_last_processed_event_timestamp_seconds)(?:\{([^}]*)\})? (\S+)$')
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_metrics(text: str) -> Dict[str, Any]:
    """Scores, current alert states and alert counters per group_id.
    ponytail: keyed by group_id only; one service in this demo, key by (service, group) if more."""
    out: Dict[str, Any] = {"scores": {}, "states": {}, "alerts": {}, "last_event": None}
    for line in text.splitlines():
        m = METRIC_RE.match(line)
        if not m:
            continue
        name, labels, value = m.group(1), dict(LABEL_RE.findall(m.group(2) or "")), float(m.group(3))
        group = labels.get("group_id")
        if name == "log_anomaly_score":
            out["scores"][group] = round(value, 4)
        elif name == "log_alert_state" and value == 1:
            out["states"][group] = labels["state"]
        elif name == "log_alerts_total":
            out["alerts"][group] = int(value)
        elif name == "logai_last_processed_event_timestamp_seconds":
            out["last_event"] = value
    return out


# ---------------------------------------------------------------------- run

class Runner:
    def __init__(self, args: argparse.Namespace, out: Path):
        self.args, self.out = args, out
        self.phase = "start"
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.probes: List[threading.Thread] = []

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
                sample = self.scrape()
                sample.update(ts=started, phase=self.phase)
                self.write("samples.jsonl", sample)
            except Exception as exc:  # keep sampling through engine hiccups
                print(f"sample failed: {exc}", file=sys.stderr, flush=True)
            self.stop.wait(max(0.0, SAMPLE_SECONDS - (time.time() - started)))

    def preflight(self) -> Dict[str, Any]:
        problems = []
        _, rep = http("GET", f"{self.args.replayer}/status")
        if not (isinstance(rep, dict) and rep.get("replay", {}).get("is_running")):
            problems.append("replay is not running (POST :8001/replay/start)")
        _, alerts = http("GET", f"{self.args.web}/api/alerts")
        llm = alerts.get("llm", {}) if isinstance(alerts, dict) else {}
        if not llm.get("alive"):
            problems.append("engine is not alive")
        if not self.args.no_llm and not llm.get("llm_enabled"):
            problems.append(f"LLM disabled: {llm.get('reason')}")
        _, retrain = http("GET", f"{self.args.web}/api/retrain")
        if isinstance(retrain, dict) and (retrain.get("run_pending")
                                          or retrain.get("engine_state") == "retraining"):
            problems.append("a retrain is pending or running")
        if isinstance(retrain, dict) and (retrain.get("schedule") or {}).get("enabled"):
            problems.append("scheduled retrain is enabled; disable it for the test")
        lag = time.time() - (self.scrape()["last_event"] or 0)
        if lag > 30:
            problems.append(f"engine is {lag:.0f} s behind")
        if problems:
            sys.exit("preflight failed:\n  - " + "\n  - ".join(problems))
        history = (retrain.get("status") or {}).get("history") or [{}]
        return {"llm_profile": llm.get("reason"), "last_retrain": history[-1]}

    def inject(self, phase: Dict[str, Any], minutes: float) -> None:
        for inj in phase["injects"]:
            status, body = http("POST", f"{self.args.replayer}/replay/inject",
                                {**inj, "minutes": minutes}, timeout=120)
            print(f"  inject {inj['label']} {inj['per_minute']}/min: {status} {body}", flush=True)
            self.write("phases.jsonl", {"event": "inject", "ts": time.time(), "status": status, **inj})

    def window_keys(self, groups: List[str]) -> List[str]:
        _, data = http("GET", f"{self.args.web}/api/insights")
        return [w["key"] for w in data.get("windows", []) if w["group_id"] in groups]

    def top_window(self, exclude: set) -> List[str]:
        sample = self.scrape()
        ranked = sorted((s, g) for g, s in sample["scores"].items() if g not in exclude)
        return self.window_keys([ranked[-1][1]]) if ranked else []

    def probe(self, phase: Dict[str, Any], kind: str) -> None:
        if kind == "service":
            jobs = [("service", self.args.service)]
        elif kind == "window":
            jobs = [("window", k) for k in self.window_keys(list(phase["targets"]))]
        else:
            jobs = [("window", k) for k in self.top_window(set())]
        for job in jobs:
            thread = threading.Thread(target=self.analyze, args=(phase["name"], *job), daemon=True)
            thread.start()
            self.probes.append(thread)

    def analyze(self, phase: str, kind: str, target: str) -> None:
        requested = time.time()
        deadline = requested + PROBE_TIMEOUT
        record: Dict[str, Any] = {"phase": phase, "kind": kind, "target": target,
                                  "requested_at": requested}
        while time.time() < deadline:  # 409 = previous analysis of this target still running
            status, body = http("POST", f"{self.args.web}/api/insights/analyze",
                                {"kind": kind, "id": target, "language": self.args.language})
            if status != 409:
                break
            time.sleep(5)
        if status != 202:
            self.write("llm.jsonl", {**record, "status": "request_failed", "error": f"{status} {body}"})
            return
        requested = body.get("requested_at", requested)
        result = None
        while time.time() < deadline:
            time.sleep(5)
            _, data = http("GET", f"{self.args.web}/api/insights")
            if kind == "service":
                found = [s["analysis"] for s in data.get("services", []) if s["service"] == target]
            else:
                found = [w["analysis"] for w in data.get("windows", []) if w["key"] == target]
            rec = found[0] if found else None
            if (isinstance(rec, dict) and rec.get("status") in {"done", "failed"}
                    and float(rec.get("requested_at") or 0) >= requested - 1):
                result = rec
                break
        if result is None:
            self.write("llm.jsonl", {**record, "status": "timeout"})
            return
        self.write("llm.jsonl", {
            **record,
            "finished_at": time.time(),
            "latency": round(float(result.get("analyzed_at") or time.time()) - requested, 1),
            "status": result.get("status"), "model": result.get("model"),
            "error": result.get("error"),
            "documentation_id": result.get("documentation_id"),
            "confidence": result.get("confidence"),
            "suggestion": result.get("suggestion"),
            "health": result.get("health"), "summary": result.get("summary"),
            "issue_groups": sorted({g for i in result.get("issues") or [] for g in i.get("group_ids") or []}),
            "reasoning": result.get("reasoning"),
        })

    def run(self) -> None:
        meta = {"started_at": time.time(), "quick": self.args.quick, "no_llm": self.args.no_llm,
                **self.preflight()}
        scale = 0.5 if self.args.quick else 1.0
        phases = [dict(p, minutes=p["minutes"] * scale,
                       probes=[(at * scale, kind) for at, kind in p["probes"]]) for p in SCENARIO]
        meta["scenario"] = phases
        (self.out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        threading.Thread(target=self.sampler, daemon=True).start()
        try:
            for phase in phases:
                start = time.time()
                self.phase = phase["name"]
                end = start + phase["minutes"] * 60
                print(f"{time.strftime('%H:%M:%S')} {phase['name']} ({phase['minutes']:g} min)", flush=True)
                self.write("phases.jsonl", {"event": "phase", "name": phase["name"], "kind": phase["kind"],
                                            "start": start, "end": end, "targets": phase["targets"]})
                self.inject(phase, phase["minutes"])
                for at, kind in sorted(phase["probes"]):
                    time.sleep(max(0.0, start + at * 60 - time.time()))
                    if not self.args.no_llm:
                        self.probe(phase, kind)
                time.sleep(max(0.0, end - time.time()))
            print("waiting for running LLM probes ...", flush=True)
            for thread in self.probes:
                thread.join(PROBE_TIMEOUT + 60)
        finally:
            http("POST", f"{self.args.replayer}/replay/inject/stop")
            self.stop.set()
        print(f"done: {self.out}", flush=True)


# ------------------------------------------------------------------- report

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def alert_onsets(samples: List[Dict[str, Any]], group: str, prev: Optional[str] = None) -> List[float]:
    """Timestamps where `group` enters ALERTING (from any other state). `prev` is the
    state before the first sample, so an alert carried over from the previous phase
    is not counted again."""
    onsets = []
    for s in samples:
        state = s["states"].get(group)
        if state == "ALERTING" and prev != "ALERTING":
            onsets.append(s["ts"])
        prev = state
    return onsets


def held_mask(scores: List[Optional[float]], threshold: float, held: int = HELD_SAMPLES) -> List[bool]:
    """True for samples inside a run of >= `held` consecutive scores >= threshold."""
    mask, run = [False] * len(scores), 0
    for i, score in enumerate(scores + [None]):
        if score is not None and score >= threshold:
            run += 1
            continue
        if run >= held:
            mask[i - run:i] = [True] * run
        run = 0
    return mask


def held_episodes(scores: List[Optional[float]], threshold: float, held: int = HELD_SAMPLES) -> int:
    mask = held_mask(scores, threshold, held)
    return sum(1 for i, on in enumerate(mask) if on and (i == 0 or not mask[i - 1]))


def first_state_after(samples: List[Dict[str, Any]], group: str, states: set, start: float) -> Optional[float]:
    for s in samples:
        if s["ts"] >= start and s["states"].get(group) in states:
            return round(s["ts"] - start)
    return None


def doc_tick(record: Dict[str, Any], expected: Dict[str, str], group: Optional[str], kind: str) -> str:
    """✔/✘ against the scenario: a burst window must pick its expected document, a burst
    service analysis must list every target, a control service analysis must say healthy."""
    if record.get("status") != "done":
        return "–"
    if record.get("kind") == "service":
        if kind == "control":
            return "✔" if record.get("health") == "healthy" else "✘"
        if kind == "burst":
            return "✔" if set(expected) <= set(record.get("issue_groups") or []) else "✘"
        return "–"
    want = expected.get(group or "")
    if not want:
        return "–"
    return "✔" if record.get("documentation_id") == want else "✘"


def summarize(run_dir: Path) -> str:
    meta = json.loads((run_dir / "meta.json").read_text())
    phases = [p for p in read_jsonl(run_dir / "phases.jsonl") if p["event"] == "phase"]
    samples = read_jsonl(run_dir / "samples.jsonl")
    llm = read_jsonl(run_dir / "llm.jsonl")
    by_phase = {p["name"]: p for p in phases}
    groups = sorted({g for s in samples for g in s["scores"]} | {g for s in samples for g in s["states"]})
    scored = [s for s in samples if by_phase.get(s["phase"], {}).get("kind") != "warmup"]

    def targets_at(s: Dict[str, Any]) -> Dict[str, str]:
        return by_phase.get(s["phase"], {}).get("targets", {})

    lines = [f"# Incident test {run_dir.name}", ""]
    retrain = meta.get("last_retrain") or {}
    lines += [f"- model: retrain started {time.strftime('%Y-%m-%d %H:%M', time.localtime(retrain.get('started_at', 0)))}"
              f", lookback {retrain.get('lookback_hours')} h, max_docs {retrain.get('max_docs')}",
              f"- LLM: {meta.get('llm_profile')}", f"- quick: {meta.get('quick')}", ""]

    # False alerts: ALERTING onsets of groups that are not a target of the current phase.
    hours = max(1e-9, len(scored) * SAMPLE_SECONDS / 3600)
    false: Dict[str, Dict[str, float]] = {}
    first_scored = samples.index(scored[0]) if scored else 0
    for g in groups:
        prev = samples[first_scored - 1]["states"].get(g) if first_scored else None
        for s in scored:
            state = s["states"].get(g)
            if g not in targets_at(s):
                row = false.setdefault(g, {"onsets": 0, "control": 0, "alert_min": 0.0})
                if state == "ALERTING":
                    row["alert_min"] += SAMPLE_SECONDS / 60
                    if prev != "ALERTING":
                        row["onsets"] += 1
                        row["control"] += by_phase[s["phase"]]["kind"] == "control"
            prev = state
    noisy = {g: r for g, r in false.items() if r["onsets"] or r["alert_min"]}
    total = sum(r["onsets"] for r in noisy.values())
    lines += ["## False alerts (warm-up excluded)", "",
              f"{total} ALERTING onsets in non-target groups over {hours:.2f} h = **{total / hours:.1f}/h**; "
              f"control phase: {sum(r['control'] for r in noisy.values()):g}", ""]
    if noisy:
        lines += ["| group | onsets | in control | minutes ALERTING |", "|---|---:|---:|---:|"]
        for g, r in sorted(noisy.items(), key=lambda kv: -kv[1]["onsets"]):
            lines.append(f"| {g} | {r['onsets']} | {r['control']:g} | {r['alert_min']:.1f} |")
        lines.append("")

    # Detection per burst target.
    lines += ["## Bursts", "",
              "| burst | group | to WARMING | to ALERTING | % ALERTING | peak | median | last-min median | re-alerts in recovery |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for p in phases:
        if p["kind"] != "burst":
            continue
        inside = [s for s in samples if s["phase"] == p["name"]]
        recovery = [s for s in samples if s["phase"] == f"{p['name']}-recovery"]
        before_recovery = inside[-1]["states"] if inside else {}
        for g in p["targets"]:
            scores = [s["scores"][g] for s in inside if g in s["scores"]]
            last = [s["scores"][g] for s in inside if g in s["scores"] and s["ts"] >= p["end"] - 60]
            alerting = sum(s["states"].get(g) == "ALERTING" for s in inside)
            fmt = lambda v: "–" if v is None else f"{v} s"
            lines.append(
                f"| {p['name']} | {g} | {fmt(first_state_after(inside, g, {'WARMING', 'ALERTING'}, p['start']))} "
                f"| {fmt(first_state_after(inside, g, {'ALERTING'}, p['start']))} "
                f"| {100 * alerting / max(1, len(inside)):.0f}% "
                f"| {max(scores, default=0):.3f} | {statistics.median(scores) if scores else 0:.3f} "
                f"| {statistics.median(last) if last else 0:.3f} | {len(alert_onsets(recovery, g, before_recovery.get(g)))} |")
    lines.append("")

    # Threshold sweep on the raw score, held >= 30 s.
    bursts = [p for p in phases if p["kind"] == "burst"]
    lines += [f"## Threshold sweep (score held >= {HELD_SAMPLES * SAMPLE_SECONDS} s)", "",
              "| score_high | false episodes | " + " | ".join(f"{p['name']} {g}" for p in bursts for g in p["targets"]) + " |",
              "|---:|---:|" + "---:|" * sum(len(p["targets"]) for p in bursts)]
    for thr in SWEEP:
        false_eps = 0
        for g in groups:
            # Only samples where g is not a target; a target's own burst is not a false alert.
            seq = [s["scores"].get(g) if g not in targets_at(s) else None for s in scored]
            false_eps += held_episodes(seq, thr)
        cells = []
        for p in bursts:
            inside = [s for s in samples if s["phase"] == p["name"]]
            for g in p["targets"]:
                mask = held_mask([s["scores"].get(g) for s in inside], thr)
                cells.append(f"{100 * sum(mask) / max(1, len(mask)):.0f}%")
        lines.append(f"| {thr:.2f} | {false_eps} | " + " | ".join(cells) + " |")
    lines.append("")

    # LLM probes.
    window_group = lambda key: (json.loads(key)[1] if key.startswith("[") else None)
    lines += ["## LLM probes", "",
              "| phase | kind | target | status | latency | model | doc / health | expected | ok | issue groups |",
              "|---|---|---|---|---:|---|---|---|---|---|"]
    for r in sorted(llm, key=lambda r: r["requested_at"]):
        expected = by_phase.get(r["phase"], {}).get("targets", {})
        group = window_group(r["target"]) if r["kind"] == "window" else None
        got = (f"{r.get('documentation_id') or ('suggestion' if r.get('suggestion') else '–')}"
               f" ({r.get('confidence')})") if r["kind"] == "window" else (r.get("health") or "–")
        want = expected.get(group, "–") if r["kind"] == "window" else (",".join(expected) or "healthy")
        lines.append(
            f"| {r['phase']} | {r['kind']} | {group or r['target']} | {r.get('status')} "
            f"| {r.get('latency', '–')} | {r.get('model') or '–'} | {got} | {want} "
            f"| {doc_tick(r, expected, group, by_phase.get(r['phase'], {}).get('kind', ''))} | {','.join(r.get('issue_groups') or []) or '–'} |")
    failed = [r for r in llm if r.get("status") != "done"]
    lines += ["", f"{len(llm)} probes, {len(failed)} not done"
              + "".join(f"\n- {r['phase']} {r['kind']} {r['target']}: {r.get('status')} {r.get('error')}" for r in failed), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--quick", action="store_true", help="halve every phase and probe time")
    run.add_argument("--no-llm", action="store_true", help="skip LLM probes")
    run.add_argument("--language", default="vi")
    run.add_argument("--service", default="recharge")
    run.add_argument("--out", default="reports")
    run.add_argument("--engine", default=os.environ.get("LOGAI_TEST_ENGINE", "http://localhost:9108"))
    run.add_argument("--web", default=os.environ.get("LOGAI_TEST_WEB", "http://localhost:5555"))
    run.add_argument("--replayer", default=os.environ.get("LOGAI_TEST_REPLAYER", "http://localhost:8001"))
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
