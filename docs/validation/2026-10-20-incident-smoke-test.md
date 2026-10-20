# Incident test, 2026-10-20: smoke test

**This is a smoke test. The baseline was one partial day.** Do not read it as an acceptance result.

- **Commit:** de93019, deployed on the recharge host (logai2-engine :9118, logai2-web :5565).
- **Run:** `scripts/incident_test.py run`, full scenario (not `--quick`). Run directory: `reports/incident-20261020-2351` (not committed).
- **Replay:** `full_node97.log` from source 00:00 at 1x, started 2026-10-20 23:34:03 local.
- **Run window:** 2026-10-20 23:51:02 to 2026-10-21 02:56 local (Asia/Ho_Chi_Minh).
- **Baseline at start:** one committed weekday, 2026-10-19, with k = 4 (young).
  - Real traffic only in local hours 02–04 and 09–10.
  - Hours 0 and 1 have no baseline row, so only the level floor applies (ERROR 5/min).
  - The 02:00 commit (19:00 UTC) during the run marked 2026-10-20 committed. It did not change the hour-2/3 rows the test used.

## Targets

| target | template | document |
|---|---|---|
| lixi | recharge:T00032 `LixiHandler: <*> enterd Receiving subscriber incorrect: <*>` | DOC-001 |
| slowdb | recharge:T00042 `DBAdapter: Time invoke <*> CHAM_DB_LEVEL<*>: <*> ms` | DOC-003 |
| sqltimeout | recharge:T00311 `DBAdapter: pGetNumberTopupInvalid hava exception java.sql.SQLTimeoutException ...` | DOC-003 |

T00311 first appeared at 23:50:33, 16.5 min into the replay. It was linked to DOC-003 just before the run started.

## Checks

| burst | local time | target | 1: ALERTING after (deadline 298 s) | 2: document row ALERTING | 4: document shown |
|---|---|---|---|---|---|
| B1-x50 (150/min) | 00:51 | lixi | 210 s **PASS** | 210 s **PASS** | DOC-001 **PASS** |
| B3-x20 (60/min) | 01:41 | lixi | never **FAIL** | never **FAIL** | – **FAIL** |
| B4-rare (30/min) | 02:06 | sqltimeout | 210 s **PASS** | 210 s **PASS** | DOC-003 **PASS** |
| B5-x10 (30/min) | 02:31 | lixi | never **FAIL** | never **FAIL** | – **FAIL** |

**Check 3, spread (B2, 01:16, slowdb 10/min + sqltimeout 10/min → DOC-003):**
- doc:DOC-003 reached ALERTING after 210 s, so the report says **PASS**.
- Both templates also reached ALERTING on their own, so the sum did not carry the alert.
- With zero bands in hour 1 and a floor-only rule, this check proves nothing.

**Check 5, control (00:21–00:51):**
- 12 ALERTING onsets in 0.5 h = **24.0 false alerts/h**.
- They were 6 on the DOC-001 document row and 6 on the recharge service row. Both are the normal lixi traffic flapping around the 5/min ERROR floor in hour 0, which has no baseline.
- Comparison: logai-engine's only run is `~/Documents/logai-engine/reports/incident-20261013-0822`. It had 14/h in control (28.6/h over the whole run), but in a daytime window with a trained model. There is no logai-engine run in a comparable window, so the two figures are not comparable.

## Recovery

Every recovery ended NORMAL: B1, B2 (slowdb and sqltimeout), B3, B4 and B5 all ended NORMAL for both the template and the document row. Every injection phase therefore started clean, and no result is "n/a: carried over".

## Inject errors and anomalies

- **Inject errors:** none. All 7 replayer injections returned 200. The replayer's injected counter rose by 1,500, 100+6, 600, 300 and 300 lines.
- **Lag:** `logai_lag_seconds` stayed between 30.0 and 31.0 s across all 2,220 samples. 30 s is the ES upper bound, so there were no spikes.
- **Engine:** no ERROR or Traceback lines in the logs and no restarts.
- **Sampling gaps:** none.

## Why B3 and B5 failed

Both are the young k = 4 band built from the single noisy hour-02/03 day of 10-19 (lixi weekday: hour 2 mean 9.8, std 14.5, upper about 68/min; hour 3 mean 35.3, std 9.6, upper about 74/min).

- **B3 (01:41, hour 1):** hour 1 has no row, so the band is the wider of hours 1 and 2, which is hour 2's ~68/min. Lixi ran at 60 injected + ~8 background = ~68/min, just at the band, so it never crossed it. Hour 1's total was 1,079 lines.
- **B5 (02:31):** 30 injected + ~5 background = ~35/min, against the wider of the hour-2 and hour-3 bands (~74/min).
- **B1 and B4** passed only because they fell where the band was 0, so the floor decided. That says nothing about the anomaly band, which was not exercised.

## What is meaningful

These results are meaningful:
- End-to-end plumbing: inject → ES → engine → alert row in 210 s (3 minute checks).
- Document linking: document rows and the `doc_id` shown.
- Recovery to NORMAL within 15 min.
- Stable 30 s lag with no engine errors.

These results are not meaningful:
- The PASS/FAIL of checks 1, 2 and 4 depends on whether a burst landed in an hour with a zero band or with the noisy 10-19 band.
- Check 3 passed through the per-template floor, not through the sum.
- The control rate of 24/h is floor flapping in an hour with no baseline.

A real run needs at least 5 committed weekdays covering the test hours.
