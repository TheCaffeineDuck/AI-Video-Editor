"""Edit-event records, clip anchors, and the reason validator.

Phase 6b-1 introduces an append-only :class:`Document.edit_log` that
records every structural change a Document has gone through (cuts,
restores, moves). The previous v3 ``Clip.reason`` field stays as a
denormalized convenience for clients that walk the playlist; the
authoritative rationale lives on the matching :class:`EditEvent`.

This module provides:

- :class:`ClipAnchor` — identifies a Clip on the playlist by its
  source identity (path + start/end). Used by :class:`MoveClipSpan`
  so moves keep referring to the same clip even after other moves
  shift indices.
- :class:`EditEvent` — one entry in the edit log. Three discriminated
  ``kind`` values cover the operations 6b-1 understands:
  ``"cut"`` / ``"restore"`` carry ``start`` / ``end`` / ``source_id``;
  ``"move"`` carries ``span`` / ``target``. Fields not used by a
  given kind are ``None`` (or the empty tuple for ``span``).
- :func:`is_valid_reason` — a standalone validator that checks
  ``reason`` against a small set of canonical category prefixes
  (``manual`` / ``filler`` / ``silence`` / ``rearrange`` / ``highlight``
  / ``narrative`` / ``trim`` / ``best-take`` / ``undo:``). Reasons that
  don't start with one of those prefixes pass only when they're at
  least :data:`_MIN_FREEFORM_LEN` characters of free-form text — the
  threshold discourages drive-by ``"x"`` rationales without forcing
  every caller into the prefix set. The validator is consulted by
  :class:`MoveClipSpan` at construction; existing v2/v3 commands
  (``AddCut``, ``RestoreRange``, ``CutWordRange``) keep accepting
  arbitrary reason strings since their tests predate the rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Reason validator
# ---------------------------------------------------------------------------


REASON_CATEGORIES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^manual\b", re.IGNORECASE),
    re.compile(r"^filler\b", re.IGNORECASE),
    re.compile(r"^silence\b", re.IGNORECASE),
    re.compile(r"^rearrange\b", re.IGNORECASE),
    re.compile(r"^highlight\b", re.IGNORECASE),
    re.compile(r"^narrative\b", re.IGNORECASE),
    re.compile(r"^trim\b", re.IGNORECASE),
    re.compile(r"^best[-\s_]take\b", re.IGNORECASE),
    re.compile(r"^undo:\s*", re.IGNORECASE),
)
"""Category prefixes that always pass :func:`is_valid_reason`.

Order is *display order* — when an MCP client surfaces example reasons
to a user, walking this tuple in order is a reasonable starting point.
The patterns themselves are anchored to the start of the string and
match a word boundary, so ``"manual override"`` and ``"manualxyz"``
behave differently (the latter does not match ``manual\\b``).
"""

_MIN_FREEFORM_LEN = 8
"""Minimum length for free-form (non-prefixed) reasons.

Eight characters is short enough to allow phrases like ``"too slow"``
(8 chars) or ``"better take"`` (11 chars) but rejects single-word
labels like ``"todo"`` / ``"test"`` / ``"foo"`` that don't carry
operational intent. A future audit may tighten this back toward 12+
once we see what reasons clients actually emit.
"""


def is_valid_reason(reason: object) -> bool:
    """Return ``True`` iff ``reason`` is a non-empty rationale string
    that either starts with a known category prefix or is free-form
    text of at least :data:`_MIN_FREEFORM_LEN` characters.

    ``None`` and non-strings are always invalid. Whitespace-only
    strings are invalid. The check is whitespace-trimmed at the
    boundary; internal whitespace is preserved.
    """
    if not isinstance(reason, str):
        return False
    s = reason.strip()
    if not s:
        return False
    for pat in REASON_CATEGORIES:
        if pat.match(s):
            return True
    return len(s) >= _MIN_FREEFORM_LEN


# ---------------------------------------------------------------------------
# Clip anchors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipAnchor:
    """A handle that identifies a :class:`~core.timeline.Clip` by source identity.

    Anchors are how :class:`~core.editing.MoveClipSpan` refers to clips:
    by what they sample, not by index. This survives interleaved moves
    that change index but not source identity.

    Equality is exact across all three fields. The renderer-side
    ``Clip`` shape carries the same ``source_path`` / ``source_start`` /
    ``source_end`` triple, so ``ClipAnchor.from_clip(clip) == anchor``
    iff the anchor was constructed off that clip's identity.
    """

    source_path: Path
    source_start: float
    source_end: float

    @classmethod
    def from_clip(cls, clip: object) -> ClipAnchor:
        """Build an anchor off a :class:`~core.timeline.Clip` (or duck-typed equiv)."""
        return cls(
            source_path=clip.source_path,  # type: ignore[attr-defined]
            source_start=float(clip.source_start),  # type: ignore[attr-defined]
            source_end=float(clip.source_end),  # type: ignore[attr-defined]
        )

    def matches(self, clip: object) -> bool:
        """Return True iff ``clip`` has this anchor's exact identity.

        Exact float equality is the contract. Phase 6a's renderer
        rounds to 1ms for smartcut bookkeeping; in practice anchors
        constructed from the on-disk JSON don't drift relative to
        clips loaded from the same JSON. If a future caller mints
        anchors from snapped/padded values, they need to use the
        unsnapped originals.
        """
        return (
            getattr(clip, "source_path", None) == self.source_path
            and float(getattr(clip, "source_start", 0.0)) == self.source_start
            and float(getattr(clip, "source_end", 0.0)) == self.source_end
        )

    def to_json(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path),
            "source_start": float(self.source_start),
            "source_end": float(self.source_end),
        }

    @classmethod
    def from_json(cls, data: dict) -> ClipAnchor:
        return cls(
            source_path=Path(data["source_path"]),
            source_start=float(data["source_start"]),
            source_end=float(data["source_end"]),
        )


# ---------------------------------------------------------------------------
# Edit log entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditEvent:
    """One append-only entry in :attr:`~core.document.Document.edit_log`.

    ``kind`` is one of:

    - ``"cut"`` — interval ``[start, end]`` removed on ``source_id``
    - ``"restore"`` — interval ``[start, end]`` re-inserted on ``source_id``
    - ``"move"`` — clip ``span`` moved to before ``target`` (or to end
      when ``target is None``)

    Cut/restore events leave ``span`` as the empty tuple and ``target``
    as ``None``; move events leave ``start`` / ``end`` / ``source_id``
    as ``None``. The same dataclass covers all three kinds rather than
    a discriminated union, because the surface area is small (four
    optional fields total) and the JSON shape is already
    kind-discriminated.

    ``timestamp`` should be a UTC :class:`datetime`. ``reason`` is a
    free-form string; it isn't re-validated on load (whatever the
    original author committed is what shows up here). Phase 6b-1's
    only writer that *does* validate is :class:`MoveClipSpan`.
    """

    kind: str
    timestamp: datetime
    reason: str
    start: float | None = None
    end: float | None = None
    source_id: str | None = None
    span: tuple[ClipAnchor, ...] = field(default_factory=tuple)
    target: ClipAnchor | None = None

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "kind": self.kind,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
        }
        if self.kind in ("cut", "restore"):
            if self.start is None or self.end is None or self.source_id is None:
                raise ValueError(
                    f"EditEvent kind={self.kind!r} requires start/end/source_id; "
                    f"got start={self.start!r} end={self.end!r} source_id={self.source_id!r}"
                )
            out["start"] = float(self.start)
            out["end"] = float(self.end)
            out["source_id"] = str(self.source_id)
        elif self.kind == "move":
            out["span"] = [a.to_json() for a in self.span]
            out["target"] = self.target.to_json() if self.target is not None else None
        else:
            raise ValueError(f"Unknown EditEvent kind: {self.kind!r}")
        return out

    @classmethod
    def from_json(cls, data: dict) -> EditEvent:
        kind = data["kind"]
        ts = datetime.fromisoformat(data["timestamp"])
        reason = str(data.get("reason", ""))
        if kind in ("cut", "restore"):
            return cls(
                kind=kind,
                timestamp=ts,
                reason=reason,
                start=float(data["start"]),
                end=float(data["end"]),
                source_id=str(data["source_id"]),
            )
        if kind == "move":
            span = tuple(ClipAnchor.from_json(a) for a in data.get("span", []))
            target_data = data.get("target")
            target = (
                None if target_data is None else ClipAnchor.from_json(target_data)
            )
            return cls(
                kind=kind,
                timestamp=ts,
                reason=reason,
                span=span,
                target=target,
            )
        raise ValueError(f"Unknown edit_log event kind: {kind!r}")
