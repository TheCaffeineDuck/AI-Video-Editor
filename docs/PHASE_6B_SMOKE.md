# Phase 6b smoke checklist (Path A — propose / apply / inspect)

Manual end-to-end verification through Claude Desktop. Targets the
six 6b-2 proposal-lifecycle tools (`propose_moves`,
`list_proposals`, `read_proposal`, `apply_proposal`,
`list_apply_results`, `read_apply_result`) plus the v3.1 schema
(`Document.edit_log` from 6b-1) and the apply-result chain-of-custody
hash.

Path A is the MCP-only, no-GUI flow: a model authors a proposal,
the human reviews it via `read_proposal`, the human picks a subset
to apply, and the apply-result file records the outcome. 6b-3 will
add a GUI proposal-review surface on top of this same persistence
layer.

The unit tests prove the proposal store is correct. This checklist
proves a remote agent (Claude Desktop) can drive the full lifecycle
end-to-end — that the tool descriptions are unambiguous enough to
pick the right tool at the right moment.

## Prerequisites

- Phase 6b-2 has been built and committed. `python main_mcp.py`
  boots cleanly (the README and 6a smoke checklist cover the
  handshake).
- Claude Desktop's `claude_desktop_config.json` has the `transcribe`
  MCP server configured, restarted since the 6b-2 build.
- A test `.transcribe.json` exists on disk with at least 3 clips —
  produced either by running the 6a smoke checklist through step 8
  (which leaves a 2-clip doc; you'll need to apply one more cut to
  get to 3), or by hand-editing a fresh transcript to add cuts. The
  doc should be source-monotonic (Path A doesn't require non-
  monotonic input — moves *produce* non-monotonic state, they don't
  require it).

## Steps

### 1. Confirm fourteen tools are registered

Prompt:

> "Which Transcribe tools are available?"

Expected: Claude lists fourteen tools. Eight from Phase 6a
(`transcribe`, `load_document`, `get_transcript`, `get_ranges`,
`get_timeline`, `apply_cuts`, `restore_ranges`, `render`) plus six
from Phase 6b-2 (`propose_moves`, `list_proposals`, `read_proposal`,
`apply_proposal`, `list_apply_results`, `read_apply_result`).

If the count is still 8, the new build hasn't been picked up by
Claude Desktop — quit and re-launch it.

### 2. Read the timeline (v3 playlist view)

Prompt:

> "Show me the timeline of `<absolute path to .transcribe.json>`."

Expected: Claude calls `get_timeline`. Response: a list of clips in
playlist order, each with `source_path`, `source_start_s`,
`source_end_s`, `reason`. `is_source_monotonic: true`. Note the
exact `source_start_s` and `source_end_s` values for at least the
first and last clips — the proposal anchors must match these
floats exactly.

### 3. Propose two moves against the timeline

Prompt:

> "Propose two moves: first, take the **first clip** and put it at
> the end of the playlist; second, take the **last clip** (in the
> *new* playlist order — i.e., the clip that was originally just
> before the one we moved) and put it before what's currently the
> first clip. Use reasons 'rearrange — try cold open up' and
> 'rearrange — bring outro forward'."

Note: each move's `span` is a *list of one anchor* for the
single-clip case (a span is always a list, even when it covers a
single clip — the list shape supports multi-clip spans without a
schema change). A two-clip span would be a list of two anchors;
the matched clips must be contiguous in the timeline.

Expected: Claude calls `propose_moves(json_path=..., moves=[...])`
with two `MoveRequest` entries. The anchors in each move's `span`
match the values from step 2's `get_timeline` output. Response:
`proposal_id` (timestamp-prefixed string like
`20260429T141530-a3b4c5d6`), `proposal_path` ending in
`.proposals/<proposal_id>.proposal.json`,
`parent_document_state_hash` matching the doc's content hash
(sha256 over the full Document JSON, including `edit_log`),
`move_ids` like `["m000", "m001"]`.

The on-disk `.transcribe.json` is unchanged at this point — the
proposal is *recorded*, not *applied*.

### 4. List proposals

Prompt:

> "What proposals exist for that document?"

Expected: Claude calls `list_proposals`. Response: one proposal
summary with the id from step 3, `move_count: 2`,
`latest_apply_result_id: null` (nothing applied yet),
`created_at` set to a recent ISO timestamp.

### 5. Read the proposal back

Prompt:

> "Read the full proposal — I want to see the moves before I
> approve."

Expected: Claude calls `read_proposal(json_path=..., proposal_id=...)`.
Response: full move list with `move_id`, `span`, `target`, `reason`.
Compare the printed reasons to step 3's prompt — they should match
verbatim.

### 6. Apply only the first move

Prompt:

> "Apply only the first move (m000) and keep the second one
> rejected for now."

Expected: Claude calls
`apply_proposal(json_path=..., proposal_id=..., move_ids=["m000"])`.
Response: `apply_result_id`, `applied_count: 1`,
`skipped_count: 1`, `failed_count: 0`. The outcomes list has two
entries — m000 with `applied: true`, m001 with `skipped: true`.

The on-disk `.transcribe.json` now reflects the move:
`get_timeline` (call it again to confirm) shows the first clip
moved to the end, `is_source_monotonic: false`.

### 7. List apply-results

Prompt:

> "What apply-results have been recorded for that document?"

Expected: Claude calls `list_apply_results`. Response: one summary
with `apply_result_id` from step 6, `proposal_id` from step 3,
`applied_count: 1`, `skipped_count: 1`, `failed_count: 0`.

Bonus check: prompt "Now show me apply-results for proposal
`<id>`." — Claude should call `list_apply_results` with the
`proposal_id` parameter and return the same single entry.

### 8. Read the apply-result in full

Prompt:

> "Read the full outcomes from that apply-result, including the
> chain-of-custody hashes."

Expected: Claude calls `read_apply_result`. Response:
`document_pre_hash` (sha256 of the doc state before any move),
`document_post_hash` (sha256 of the doc state after the applied
moves), per-outcome `post_state_hash` for the applied move (null
for the skipped one), `move_ids_filter: ["m000"]`,
`human_rejection_reason: null` for both outcomes (6b-2 has no
place to author rejections; 6b-3 will).

### 9. List proposals again — latest_apply_result_id linkage

Prompt:

> "List proposals one more time."

Expected: Claude calls `list_proposals`. Now the proposal's
`latest_apply_result_id` is the id from step 6 (no longer null).
This linkage is what 6b-3's GUI will use to jump from a proposal
listing straight to its most-recent outcome.

### 10. Stale-proposal refusal (intra-doc edit drifts the hash)

Prompt:

> "Now apply the second move (m001) from the original proposal."

Expected: Claude calls `apply_proposal` with `move_ids=["m001"]`
and gets back `STALE_PROPOSAL: …`. That's the load-bearing safety
check: the proposal's `parent_document_state_hash` was captured at
step 3, before step 6 mutated the document. Step 6's apply
appended an `EditEvent` to `edit_log` and rewrote the timeline,
which flips `document_state_hash(doc)` — so the live content hash
no longer matches what the proposal was authored against.

This fires for *any* intra-doc edit between authoring and apply,
not just `source_hash` drift. The hash is content-based (sha256
over the full Document JSON including `edit_log`); a re-applied
proposal against a doc that's been touched will refuse cleanly.

Pre-cleanup proposals on disk (schema v1) have a different shape
and are forever-stale-uncheckable: their stale check is suppressed
because the legacy hash isn't comparable to a v2 content hash.
Production smokes from the current build always emit v2 and exercise
the strict path.

### 11. Reasoned-reject placeholder

Prompt:

> "Read the latest apply-result and tell me whether any move had
> a `human_rejection_reason`."

Expected: Claude calls `read_apply_result`, observes that every
outcome has `human_rejection_reason: null`. This is the 6b-3 hook —
the field exists in the wire shape and on disk, but the MCP-driven
flow has no place to author it.

### 12. Render the post-apply doc

Prompt:

> "Render the post-apply timeline to `~/Desktop/transcribe-6b-smoke.mp4`."

Expected: Claude calls `render`. Response: `output_path`,
`duration_s` matching the sum of clip durations,
`file_size_bytes > 0`. Verify the rendered file plays and the
clip order matches the post-step-6 playlist.

This step is the same as the Phase 6a smoke step 10 — proving the
6a renderer handles the non-monotonic timeline 6b-2 just produced.

## What success looks like

All steps land their expected outputs. Claude's tool selection
should feel obvious — describing a move in natural language
(step 3) lands on `propose_moves` rather than the now-also-named
`apply_proposal`, and "apply only m000" (step 6) lands on
`apply_proposal` with the `move_ids` filter rather than re-
proposing.

If Claude consistently picks the wrong tool, the description
needs tightening — this is the real-world feedback the unit tests
can't substitute for. Note any phrasing that confuses Claude in
6b-3's review notes.

## Out of scope for this checklist

- The GUI proposal-review surface (Phase 6b-3).
- Reasoned-reject capture (`human_rejection_reason` populated by
  the GUI flow — Phase 6b-3).
- Multi-doc proposals (a single proposal targeting two
  `.transcribe.json` files — not supported, and not planned).
- Programmatic proposal authoring from third-party LLMs over a
  non-Claude MCP client. The wire shape is fixed; this is a
  Claude-Desktop walk-through but the same prompts work over
  any MCP client.
