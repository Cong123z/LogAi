# Baseline and Review: grouped key picker and combined series

Date: 2026-10-22. Status: approved design, not built. Mockup (approved layout):
session scratchpad `mockup/index.html`, real `recharge` data, fake chart.

## Goal

The **Show** control on Baseline (`#baseline`) and Review (`#review`) is hard
to use. Baseline offers All events, Errors and every template in one long
`<select>`, with no documents. Review offers All events, Errors and every
document, and makes you **type** a template id. Neither lets you look at a
level or a kind of document as a whole.

The new picker works in two steps: first a **group** (a level or a document
verdict), then **what to show** in it: the group's combined series, one
document, or one template. Both pages use the same picker. Three groups are
new combined series: all WARN lines, all INFO lines, and all templates of
ERROR (`action`) documents or SAFE documents.

Success: an operator picks "WARN", sees all WARN lines against their band, and
reaches any WARN template or related document in two clicks or a few typed
characters, on either page, without knowing an id.

**View only.** The new series are drawn and can be reviewed. They never raise
alerts (no `alert_state` rows, nothing on Alerting). Alerting on them is a
later decision (see Later).

## Keys

| Group | Key | Kind | What it counts | Exists today |
|---|---|---|---|---|
| All events | `*total` | stored | every line | yes |
| ERROR | `*error` | stored | lines of level ERROR, FATAL or CRITICAL | yes |
| WARN | `*warn` | stored | lines of level WARN or WARNING | **new** |
| INFO | `*info` | stored | every other line (INFO, DEBUG, TRACE, NOTICE, unknown) | **new** |
| ERROR docs | `verdict:action` | derived | templates linked to an active document with verdict `action` | **new** |
| SAFE docs | `verdict:safe` | derived | templates linked to an active document with verdict `safe` | **new** |

`*error + *warn + *info = *total` for every minute.

"Linked" means a link whose source is in `ALERTING_SOURCES` (`manual`,
`inherited`, `corpus`, `llm`), the same rule as `doc:` keys and the Templates
page's Known.

The verdict keys use a `verdict:` prefix rather than `*`. Code that sees `*`
treats a key as a stored service key (`alerts.py`, `incident()`), and these
keys are never stored. The mockup showed `*doc-error` / `*doc-safe`; this spec
replaces those names.

### Stored keys: `*warn`, `*info`

Counted exactly like `*error`:

- `owner/counts.py` `OpenMinutes.add`: every line adds to one level-class key,
  `*error`, `*warn` or `*info`, by `links.level_class(level)` (the same
  classes as `class_sql` and the Templates page). No new level sets.
- `close_before` zero-fills `*warn` and `*info` for each service like
  `*total` and `*error`, so every service has all four keys every minute.
- The owner's hourly flush already writes every key in `cm.keys` to
  `hourly_counts` (`sum`, `sumsq`), and the nightly commit
  (`jobs/commit.py`) already builds `baseline` rows for every key there. So no
  schema change, no migration, and the band is exact (true per-minute σ).
- History starts at deploy. No backfill: `sumsq` of a sum cannot be rebuilt
  from template rows. The first night's commit gives a young baseline; it is
  full after `full_after` committed days (5 weekdays, 4 weekend days). Until
  the first commit, `baseline_state` is `none` and the chart shows the rate
  with no band, as for a new template today.

### Derived keys: `verdict:action`, `verdict:safe`

Computed like `doc:` keys, nothing stored:

- `web/baseline_view.resolve_key` maps `verdict:<v>` (v in `action`, `safe`)
  to kind `doc` with the ids of the service's templates linked to an active
  document of that verdict. Unknown verdict, or no linked template →
  `UnknownKey` (404, as today).
- Everything that handles kind `doc` then works unchanged: `_cells` sums the
  templates' `hourly_counts`, `lookup_doc` gives the band Σmean ± k·Σσ,
  `series` / `matrix` / `inspect` / `incident` read the same as for a document.
- History is available at once (the templates' own history). The membership is
  the links as they are now, applied to the whole period. That matches how a
  `doc:` key reads today.

### View only

- `owner/detect.py` `Detector.run` skips `*warn` and `*info`
  (`VIEW_ONLY_KEYS`). Without this they would be judged with level INFO and
  never cross a floor, but the skip states the rule outright and keeps them
  out of `alert_state`.
- The verdict keys never reach the detector (it only builds `doc:` keys).
- `KEY_LEVEL` in `owner/baseline.py` is not extended.

## Picker (both pages)

One module, `logai2/web/static/js/keypicker.js`, replaces the `bl-key` select
on Baseline and the `rv-key` select plus `rv-tpl` text field on Review.
Layout as in the approved mockup.

### Row 1: group buttons

```
All events 316 │ LEVEL  ● ERROR 11  ● WARN 4  ● INFO 301 │ DOCUMENTATION  ● ERROR docs 16  ● SAFE docs 58
```

- `.toggle-btn` buttons with `aria-pressed` in a `role="group"`
  (`aria-label="Group"`), split by small LEVEL and DOCUMENTATION labels.
  They wrap on narrow screens.
- Colour dots: ERROR red, WARN yellow, INFO blue, SAFE green. The text label
  is always there, so colour is never the only cue.
- Counts: templates of the service in the group for All events and levels;
  active documents of that verdict for the documentation groups.
- A click selects the group **and** shows its combined series right away.

### Row 2: Show

A button showing the current choice (truncated with an ellipsis, full text
in `title`). It opens a popover:

- a search field on top (`role="combobox"`, focus moves there on open). It
  filters by template id, template text, document id and title,
  case-insensitive substring;
- a `role="listbox"` with three sections, each with a count:
  1. **Combined**: the group's key (`All WARN lines combined`, `*warn`);
  2. **Documents**: for a documentation group, the active documents of that
     verdict (`DOC-021 · SAFE · title · N templates`); for a level group,
     the documents linked to the group's templates; for All events, every
     active document of the service (documents with no service included);
  3. **Templates**: the service's templates in the group (`id · level · text`),
     newest `last_seen` first.
- Keys: ↑/↓ move, Enter picks, Esc closes and returns focus to the button.
  A click outside closes it. The current key is marked.
- Empty result: "Nothing in this group matches. Try another group or clear
  the filter."

Template membership in a level group uses the template's stored `level`
(its worst level seen). A template's lines can be counted in `*warn` while
the template itself is listed under ERROR if it once logged ERROR. That is
acceptable and is noted in the docs.

### Line under the picker

`Showing <group> › <choice>  <key>  <how it is counted>`, where "how" is
`stored key`, `new stored key: young baseline, full after 5 weekdays + 4
weekend days` (only while `baseline_state` is `none` or `young`),
`derived: sum of linked templates`, or `single template`.

### Routes and data

- Routes are unchanged: `#baseline/<service>/<key>/<days>` and
  `#review/<service>/<key>/<days>[/…]`. The group is derived from the key:
  `*total` → All events, `*error|*warn|*info` → that level,
  `verdict:<v>` → that verdict, `doc:<id>` → its document's verdict,
  template id → its level. Old links and bookmarks keep working.
- Data: the pages already load `/api/templates?service=…&size=500` and
  `/api/documents`. The picker needs only fields they already return:
  `level`, `doc_id`, `doc_verdict`, `link_source`; `verdict`, `service`,
  `linked_templates`. No new endpoint. The cap of 500 templates per service
  stays as today (largest service now: 316).
- Changing the service resets the group to All events (`*total`), as today.

## Review page

- Review decisions (`review_exclusions`) are per service, day and hours, not
  per key, so excluding or keeping a period while looking at `*warn` works
  with no backend change, and applies to every key of the service as today.
- The days × hours grid and inspector work for every new key through
  `matrix` / `inspect`.
- "Needs review" suggestions come from alert history. View-only keys have
  none, so for them the grid shows out-of-band cells but no "review"
  suggestions. The page says so in the empty state: "This series does not
  alert, so nothing is suggested. Out-of-band hours are still marked."

## `incident()` for the new keys

- `*warn` / `*info`: modules come from the templates of that level class
  (today `*total` and `*error` use every template of the service). Sample
  lines exist only for ERROR templates (`error_samples`), so `*warn` /
  `*info` show modules only.
- `verdict:*`: as for a `doc:` key (its linked templates).

## Tests

- `counts`: a minute with ERROR, WARN, WARNING, INFO and DEBUG lines gives
  `*error`, `*warn`, `*info` that add up to `*total`; a silent minute gives
  all four keys at 0.
- `detect`: a large `*warn` count never creates an `alert_state` row.
- `baseline_view`: `resolve_key("verdict:safe")` returns the linked
  templates of safe documents only (not drafts, not rejected, not other
  services); unknown verdict → `UnknownKey`; `series` and `matrix` answer
  for `*warn` (stored, with a baseline row) and `verdict:action` (derived).
- API: `/api/baseline/series?key=*warn` and `?key=verdict:safe` are 200;
  `?key=verdict:nope` is 404.
- Front end: checked in Chrome on the real database (groups, filter,
  keyboard, old route still opens the right group). No JS test framework is
  added.

## Docs

`docs/WEB_UI.md` (Baseline, Review, picker), `ARCHITECTURE.md` (the four
service keys and the derived `verdict:` keys), `agents.md` entry.

## Deploy

Engine (new counting, detector skip) and web. The linker is unchanged.
Rebuild and restart `logai2-engine` and `logai2-web`. The new stored keys
start counting at the engine restart.

## Out of scope

- Alerting on any new series (later, see below).
- An "Undocumented" group, or Known / Unknown as groups.
- Backfilling `*warn` / `*info` history.
- Lifting the 500-templates-per-service cap.

## Later

To make a new series alert: add the key to `KEY_LEVEL` (`*warn` → WARN uses
`floors.WARN`), drop it from `VIEW_ONLY_KEYS`, and teach `alerts.py` its
title. Verdict keys would need the detector to build them like `doc:` keys,
and would overlap the per-document alerts.
