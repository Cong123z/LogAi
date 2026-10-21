# Templates page redesign

Date: 2026-10-21. Status: built (branch `templates-redesign`, merged into `master` as `be5d7ee`); docs/WEB_UI.md describes the page as built.

## Goal

The Templates page (`logai2/web/static/js/views/templates.js`, markup in
`index.html` `#view-templates`) is hard to read: nine columns, a Document
cell that holds the document, a link badge and the action button at once,
automatic links waiting for review mixed in with every other template, and
rate and band shown as two unrelated numbers.

The redesign shows the same information more clearly and makes **reviewing
automatic links** the page's main job. Charts are out: they belong on the
Baseline page.

Success: an operator opens Templates, sees how many links wait for review,
and works through them one after another from the drawer without returning
to the table; in "All templates" each row reads in one glance (state,
template, rate against normal, document, last seen).

## Layout

Option B of the brainstorm: keep the table and the drawer, slim the table,
add a review mode. No list + detail split, no bulk confirm.

### Toolbar

- A `.toggle-group` on the left: **To review (N)** | **All templates**.
  N is `review_total` from the API (below). The page opens on To review
  when N > 0, else on All templates. The choice is not persisted.
- **To review** lists templates whose link source is in
  `AUTOMATIC_SOURCES` (`inherited`, `corpus`, `llm`, `draft`).
- Search, service and level filters stay in both modes. The **link**
  filter is shown only in All templates (in To review it would overlap the
  mode); switching to To review clears it.
- Auto-refresh, page size and pagination stay as they are.

### Table

Five data columns and one action column (was nine):

| Column | Content |
|---|---|
| State | The alert-state badge with its text for WARMING, ALERTING, COOLING; NORMAL as muted text `normal`, so only real states stand out |
| Template | Line 1: template id (`.tmpl-id`, mono) and the level badge. Line 2: the full text, wrapped, `<*>` highlighted (as today) |
| Rate | Line 1: `14/min`. Above the band: red **and** `▲ above normal` in text (never color alone). Line 2, muted: the band as `normal ≤ 5/min` when `band_low ≤ 0`, else `normal 2–5/min`. No rate or no baseline: `—` with today's tooltips |
| Document | Line 1: `DOC-007 · title`, or `Not linked` (plus `not embedded yet` when not embedded), or `No document (set by hand)`. Line 2: the link source in words (today's `linkSourceBadge` labels) with similarity, and the SAFE badge |
| Last seen | `formatTs` (already relative: `2m ago`), `formatFullTs` as the title. First seen moves to the drawer |
| (action) | Right-aligned button: **Review** (primary) for an automatic link, else **Link…** (no document) or **Change** |

A click anywhere on a row opens the drawer too; the button stays the
keyboard path (rows do not become focusable). Empty state per mode:
To review with nothing left says "Nothing to review" and offers a button
to All templates.

### Drawer

Same element (`#tpl-drawer`), reordered so the decision comes first:

1. **Header**: template id, service, level, state, rate against normal
   (same rendering as the table), first and last seen.
2. **Proposed document** (or **Document**, for a manual link): the
   template text, then directly under it the document's id, title and
   ERROR line, so the two lines can be compared. Then **Why** in one
   sentence (today's `how` text) and the LLM's reason when there is one.
   Handling in a `<details>` (closed by default). Not linked: this section
   says so and nothing more.
3. **Actions**, pinned at the bottom of the drawer (sticky footer) for an
   automatic link: **Confirm link** / **Approve suggestion** (draft) and
   **Reject** (still behind the confirm dialog).
4. **Link by hand**: unchanged (select, Save link, hint), followed by the
   unchanged Apply-to-similar dialog.

**Next item.** In To review, after Confirm or Reject succeeds the drawer
opens the next row of the current page (the one after the decided row in
table order) instead of closing; the table reloads behind it. On the last
row of the page the drawer closes. The toast still confirms each action.
In All templates the drawer closes as today. Link by hand always closes
the drawer (it may open Apply to similar).

## API

`GET /api/templates` (`logai2/web/app.py`):

- `link_source=review` is accepted besides `none` and `LINK_SOURCES`; it
  filters `l.source = ANY(AUTOMATIC_SOURCES)` and orders by
  `a.state = 'ALERTING'` first, then level class ERROR > WARN > INFO
  (`links.class_sql`), then `last_seen DESC, template_id`. Other values
  keep today's order.
- Every response adds `review_total`: the count of templates with an
  automatic link that match the request's **other** filters (service,
  level, search; not `link_source`), so the toggle count follows the
  filters in both modes.

No schema change, no new route. The link mutation routes are unchanged.

## Not changed

Link semantics (ARCHITECTURE §5.1), the mutation routes, Apply to similar,
the Documentation page, pagination limits, and the rate and band
computation.

## Out of scope

Charts (Baseline page), bulk confirm, new keyboard shortcuts, a list +
detail split layout, persisting the mode.

## Accessibility (UI UX Pro Max checks)

- State and "above normal" carry text, not only color.
- The toggle is a tablist like the Documentation mode toggle
  (`aria-selected`); the count is part of the tab's label.
- Every action is a button reachable by keyboard; focus moves into the
  drawer when it opens and to the next item's heading after Next item.
- Toasts stay in the existing polite live region.
- Light and dark theme through the existing tokens only (no raw colors).
- Narrow widths: the table keeps its horizontal scroll wrapper.

## Testing

- `tests/test_web_alerts_templates.py`: `link_source=review` returns only
  automatic sources in the specified order; `review_total` with and
  without service / level / search filters; an unknown `link_source`
  still returns 400.
- Manual: run the web against the test data, check both modes, the Next
  item flow to the end of a page, empty To review, light and dark themes
  and a narrow window.

## Docs

Rewrite the Templates section of `docs/WEB_UI.md` (toolbar, columns,
drawer, Next item) and add `link_source=review` and `review_total` to its
API table.
