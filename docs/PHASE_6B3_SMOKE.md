# Phase 6b-3 smoke checklist (Path B — GUI review + reasoned-reject)

Manual end-to-end verification of the **human-in-the-loop** half of
the iterative editing loop. Path A (`docs/PHASE_6B_SMOKE.md`)
proved the LLM-only round trip. Path B closes the loop on the GUI
side: the human reads a proposal in the Qt editor, accepts/rejects
each move with rationale on rejections, applies via Path B
(`apply_proposal_with_human_decisions`), and the resulting
apply-result file is read back by the LLM via the existing MCP
`read_apply_result` tool — *with the human's rejection reason
flowing through verbatim*.

That last sentence is the load-bearing one. If the LLM doesn't see
the human's rejection reason on its next turn, iterative editing
collapses into "the human silently overrules the LLM and the LLM
re-proposes the same thing." This checklist exists to catch that
regression.

## Prerequisites

- Phase 6b-3 has been built. `python main_qt.py` boots cleanly
  (the 6a smoke checklist covers the editor handshake).
- Claude Desktop's `claude_desktop_config.json` has the
  `transcribe` MCP server configured, restarted since the 6b-3
  build.
- A test `.transcribe.json` exists on disk with at least 3 clips,
  source-monotonic. Easiest path: re-use the doc from Phase 6b
  smoke, before its step 6 apply (or just run a fresh
  `transcribe` on a short clip and add 2-3 manual cuts in the
  GUI).

## Steps

### 1. Author a proposal via Path A (MCP)

In Claude Desktop, prompt:

> "Propose three moves against `<absolute path to the .transcribe.json>`:
> first, swap clip 1 and clip 3 (move clip 3 to before clip 1);
> second, push clip 2 to the end of the playlist; third, take the
> result of move 2 and put it back at position 2. Use reasons
> 'rearrange — try cold open', 'rearrange — try outro hook', and
> 'rearrange — undo the outro try'."

Expected: Claude calls `propose_moves` once with three
`MoveRequest` entries. Response: a `proposal_id`, a
`proposal_path` ending in
`.proposals/<proposal_id>.proposal.json`, `move_ids`
`["m000", "m001", "m002"]`. The on-disk doc is unchanged.

### 2. Open the doc in the Qt editor

Run `python main_qt.py` (or open the .app bundle). File → Open…
the `.transcribe.json` from step 1.

Expected: the editor pane appears with the transcript rendered.
Edit menu now has a "Review Proposal" entry (look near the
bottom of the menu, under Cut / Restore Cuts).

### 3. Open the proposal review dock

Edit → Review Proposal.

Expected: a dock appears on the right side of the window
(~360px wide). Header reads "Proposal Review". Below it: the
proposal's id + creation time + move count, and three "move
cards" with Accept (green ✓) / Reject (red ✗) buttons.
Apply button at the bottom is *disabled*.

If only one proposal exists for the doc, the picker dropdown is
hidden. With multiple proposals, the dropdown shows them in
chronological order with the most recent one selected by
default.

### 4. Inspect a move card

Click into the third move card (the "undo" one). Expected:

- Header line: "Move 1 clip → before clip at <T>s (position N)"
- Reason line: "Reason: rearrange — undo the outro try"
- Clip row: "↓/↑ moving from position N to position M",
  source-time span (e.g., "5.0s — 10.0s"), and a transcript
  snippet (first 6 words "…" last 6 words, or all words if ≤12).

If any move's anchors don't resolve in the current doc (e.g. you
edited the doc since proposal authoring), the card shows a "⚠
Span anchors don't resolve in the current document" warning.
That's expected for stale proposals.

### 5. Accept moves 1 and 3, reject move 2 with reason

Click ✓ Accept on the first card. Expected: the button turns
solid green, status line below the controls reads "Accepted."
Apply summary updates: "Apply: 1/3 moves decided (1 accept, 0
reject)". Apply button still disabled.

Click ✗ Reject on the *second* card. Expected: the button turns
solid red, an inline reason input field appears below with
placeholder text "Why reject? (≥8 chars or use a category
prefix)". Submit button below it, disabled (text is empty).

Type "the segue doesn't work" in the input. Expected: Submit
button enables when text length passes 8 characters.

Click Submit (or press Enter in the field). Expected: status
line below the card reads "Rejected: the segue doesn't work".
Apply summary updates: "Apply: 2/3 moves decided (1 accept, 1
reject)".

Click ✓ Accept on the *third* card. Expected: Apply button
*enables*; summary reads "Apply: 2 accepted, 1 rejected".

### 6. Submit Apply

Click Apply.

Expected within ~1s:

- The proposal review dock dismisses (becomes hidden).
- The transcript view re-renders with the post-apply state
  (move 1 and move 3 applied; move 2 was rejected so not
  applied — the doc reflects only two of the three moves).
- A new file appears in `<doc>.proposals/`:
  `<apply_result_id>.apply-result.json`. The id is timestamp-
  prefixed (e.g. `20260429T141530-deadbeef`).

(If you see "Apply failed: stale proposal", the doc was modified
between step 1 and step 6 — re-do step 1 against the now-current
doc.)

### 7. Verify the apply-result file shape on disk

In a shell:

```
cat <doc>.proposals/<apply_result_id>.apply-result.json | python3 -m json.tool
```

Expected: a JSON document with `apply_result_id`,
`proposal_id` (matching step 1's), `created_at` (recent ISO
timestamp), `document_pre_hash` and `document_post_hash` (sha256
strings), `move_ids_filter: null`, and an `outcomes` array of
three entries. The first and third have `applied: true`,
`error_code: null`, `human_rejection_reason: null`,
`post_state_hash` set. The second has `applied: false`,
`skipped: false`, `error_code: "REJECTED_HUMAN"`, and
`human_rejection_reason: "the segue doesn't work"`.

This is the *on-disk* shape that the MCP layer reads. If the
`human_rejection_reason` field is null here, the GUI didn't
authoring the rejection correctly — bail and debug Path B.

### 8. Round-trip the apply-result through MCP

Back in Claude Desktop, prompt:

> "What apply-results have been recorded for `<the same .transcribe.json
> path>`? Show me the latest one in full, including any human
> rejection reasons."

Expected: Claude calls `list_apply_results` (returns the new
apply-result summary), then `read_apply_result` with that id.

The `outcomes` it returns must have the *same* shape as step 7:
two `applied: true` outcomes, one `applied: false` /
`error_code: "REJECTED_HUMAN"` / `human_rejection_reason: "the
segue doesn't work"`. Claude should surface the rejection reason
in the conversation — typically by saying something like "The
human rejected move m001 because 'the segue doesn't work'."

This is the **loop closure** the smoke is designed to catch. If
Claude doesn't see the human reason here, iterative editing
breaks: the LLM re-proposes the same move next turn because it
doesn't know the human disliked the segue.

### 9. Re-propose against the post-apply state

Prompt:

> "OK, knowing that the human rejected the second move because
> the segue didn't work, propose an alternative: instead of
> moving clip 2 to the end, move it to before the *current*
> first clip. Use reason 'rearrange — softer transition into
> outro'."

Expected: Claude calls `propose_moves` with one move that
references the *post-apply* clip identities (which Claude
should re-fetch via `get_timeline` first if it hasn't already).
The proposal file lands; the dock would surface it on the
*next* Edit → Review Proposal click, with this new proposal
selected by default (most recent wins).

### 10. Re-open the dock to confirm picker behavior

Edit → Review Proposal again.

Expected: the dock re-appears. Now the picker dropdown shows
*two* entries — the original proposal (with "last apply: …"
showing the apply-result from step 6) and the new proposal
(with "never applied"). The new one is selected by default.

If the proposal you authored in step 9 isn't visible in the
picker, the dock didn't reload after the MCP wrote the new
proposal file — close and re-open the dock to force a re-scan.

### 11. Reject the whole new proposal

Click ✗ Reject on the new proposal's only move, type something
short and unsubstantial like "no" — Submit should *not* enable
(under the 8-char floor). Type "doesn't fit" — Submit enables.
Click Submit, then Apply.

Expected: the dock dismisses, no doc-state change (no move was
accepted), but a new `.apply-result.json` lands on disk with
one outcome: `applied: false`, `skipped: false`,
`error_code: "REJECTED_HUMAN"`,
`human_rejection_reason: "doesn't fit"`.

Round-trip the read via MCP one more time to confirm the
no-applied-moves apply-result still surfaces correctly.

### 12. Confirm doc-state-changed-during-review banner

Quit the editor. Hand-edit the `.transcribe.json` file: change
its top-level `source_hash` field to "MANUALLY_DRIFTED". Save.

Re-launch the editor, re-open the doc, Edit → Review Proposal.

Expected: a red staleness banner appears above the outline
reading "⚠ Proposal may be stale — the document has changed
since this proposal was authored. Apply will report per-move
outcomes, but rejected_system errors are likely."

Apply gating has two distinct gates that can both apply
simultaneously:

1. **"No decisions yet"** disables the Apply button until every
   move has been explicitly accepted or rejected. This is the
   standard gate.
2. **The staleness banner does NOT disable Apply.** Even with
   the banner showing, once every move has a decision the user
   can still proceed; per-move outcomes will report
   `SPAN_NOT_FOUND` (or similar) where anchors don't resolve,
   which is the useful signal Path B is designed to surface.

So a stale proposal with all decisions made → banner showing,
Apply enabled. A non-stale proposal with two of three moves
decided → no banner, Apply disabled. The two gates are
orthogonal.

## What success looks like

Steps 1–11 land their expected outputs. Step 8 is the
load-bearing one: the LLM must see the human's rejection reason
verbatim. If it does, the iterative-editing loop is closed. If it
doesn't, no amount of UI polish makes Path B useful — debug Path
B's outcome shape and the MCP read-back path before continuing.

Step 12 confirms the staleness banner surfaces but doesn't block.
The 6b-2 STALE_PROPOSAL error path is still the apply-time
guarantee; the banner is just an early warning.

## Out of scope for this checklist

- Drag-to-rearrange in the transcript view (deferred to Phase 7+).
- Editing a move's target before applying it (the workflow is
  reject + re-propose, not in-pane mutation).
- Highlights (Phase 6c).
- Multi-source compositions (Phase 6+).
- Undo of an apply. The apply is permanent; the human can
  manually run inverse moves through MCP if needed. Real undo
  is a future feature.
