# Document links smoke test on real data (2026-10-21)

Branch `embeddings-doc-links`, Phase A (layers 1–2), before deploy. Not an
acceptance run: the ground truth is only 3 manual links.

## Setup

- A throwaway schema `smoke_links` in the test database `logai_test`, with
  the live `logai` deploy's 316 templates (service `recharge`), its 12
  seeded documents and its 3 manual links copied in (the live database was
  only read).
- The 12 documents split into ERROR + Handling with
  [`2026-10-21-corpus-error-split.yaml`](../plans/2026-10-21-corpus-error-split.yaml)
  (9 split, DOC-016/017/018 kept without an ERROR line).
- One `Linker.cycle()` against the stack's real bge-m3 (`localhost:8080`,
  TEI 1.9.4), default thresholds (ERROR 0.85, WARN 0.88, INFO 0.92, margin
  0.05).

## Result

One cycle, 45 s: 316 templates and 12 documents embedded, nothing pending.

**Automatic corpus links (layer 2): 5, all correct on reading.**

| Template | Level | Document | Similarity |
|---|---|---|---|
| `TransferHandler: <*> request transfer to themselt` | ERROR | DOC-007 Transfer to own number | 0.940 |
| `RequestTransferHandler: <*> sent request to themselt` | ERROR | DOC-007 | 0.884 |
| `TopupUtil: sender is not valid for transaction SubInfo isdn <*> subId <*> paidType …` | ERROR | DOC-008 Topup sender not eligible | 0.902 |
| `BccsUtil: No tag value SUB_ID_BCCS<*>` | ERROR | DOC-009 BCCS subscriber id missing | 0.944 |
| `NavigationSync: NavigationSync files is empty - skip` | WARN | DOC-010 Navigation sync found no files | 0.985 |

**Inherited links (layer 1): none.** The 3 manual templates have no sibling
above the thresholds.

**Best template per document ERROR line** ("next document" = that
template's best ERROR line of any other document):

| Document | Best template (level) | Similarity | Next document | Linked? |
|---|---|---|---|---|
| DOC-001 | `LixiHandler: <*> enterd Receiving subscriber incorrect: <*>` (ERROR) | 0.937 | 0.565 | manual already |
| DOC-003 | `DBAdapter: Time invoke <*> CHAM_DB_LEVEL<*>: <*> ms` (WARN) | 0.803 | 0.644 | manual already |
| DOC-004 | `DBAdapter: Input length must be multiple of <*> when decrypting with padded ciph…` (WARN) | 0.850 | 0.522 | **no: under WARN 0.88** |
| DOC-005 | `TransactionManager: Transaction timeout: TransactionInfo reqID <*> transID <*> …` (INFO) | 0.840 | 0.623 | **no: under INFO 0.92** |
| DOC-007 | `TransferHandler: <*> request transfer to themselt` (ERROR) | 0.940 | 0.491 | corpus |
| DOC-008 | `TopupUtil: sender is not valid …` (ERROR) | 0.902 | 0.600 | corpus |
| DOC-009 | `BccsUtil: No tag value SUB_ID_BCCS<*>` (ERROR) | 0.944 | 0.561 | corpus |
| DOC-010 | `NavigationSync: NavigationSync files is empty - skip` (WARN) | 0.985 | 0.434 | corpus |
| DOC-011 | `con-<*>-Receiver: receive message` (INFO) | 0.769 | 0.535 | no |

## Reading

- Matching on the ERROR field only works: each document's best template is
  the line it was written for, and the next document is 0.20–0.55 lower,
  far above the 0.05 margin. (The 2026-10-19 whole-document test had a
  median best score of 0.598.)
- The per-class corpus thresholds cost two right links (DOC-004 at 0.850
  WARN, DOC-005 at 0.840 INFO). With this margin, a flat 0.80–0.84 would
  have linked them and nothing wrong. Thresholds were kept as configured
  (user decision: calibrate later); recalibrate with `scripts/link_eval.py`
  once more manual links exist.
