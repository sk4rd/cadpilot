"""Step journal — the per-document record behind the steps panel.

Pure data model and arithmetic: NO FreeCAD imports, so it is unit-testable
from ``tests/`` without a running FreeCAD. The FreeCAD-facing half (document
property persistence, step execution, undo) lives in ``step_engine.py``.

Why a journal at all: the MCP-side modeling session (``session_state.py``)
logs steps for the LLM, but it lives in the MCP process and only exists while
a session is active. The panel must work in FreeCAD's own process, with the
RPC server stopped, so the addon keeps its own copy on the document.

Manual-edit sync lives half here, half in the engine: this module owns the
pure mapping decisions (which object/step owns a property, what a live value
maps back to), the engine's document observer owns the FreeCAD reads.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

JOURNAL_PROP = "MCP_StepJournal"
JOURNAL_VERSION = 1

STATE_PLANNED = "planned"
STATE_DONE = "done"
STATE_FAILED = "failed"


@dataclass
class StepRecord:
    """One step: either planned (not yet applied) or executed."""

    index: int
    state: str = STATE_DONE
    operation: str = ""
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    transaction: str = ""
    atomic: bool = True
    # mutated=False: the step provably changed nothing (a read-only execute_code,
    # confirmed by the absence of an undo entry). It owns no transaction, so it
    # cannot make undo revert the wrong change — it neither blocks a rollback nor
    # counts for undo_count, and replay skips it.
    mutated: bool = True
    executable: bool = True
    objects_before: list[str] = field(default_factory=list)
    objects_after: list[str] = field(default_factory=list)
    error: str = ""
    timestamp: str = ""
    accepted: bool = False
    note: str = ""
    result: str = ""
    duration_ms: int = 0
    # Why a DONE step is no longer in the model ("" = it is): the user undid
    # it. The observer sets it when it sees the undo, because once a new
    # commit clears the redo stack nothing else remembers that it happened.
    stale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepRecord:
        return cls(
            index=int(data.get("index", 0)),
            state=data.get("state", STATE_DONE),
            operation=data.get("operation", ""),
            label=data.get("label", ""),
            params=dict(data.get("params") or {}),
            transaction=data.get("transaction", ""),
            atomic=bool(data.get("atomic", True)),
            mutated=bool(data.get("mutated", True)),
            executable=bool(data.get("executable", True)),
            objects_before=list(data.get("objects_before") or []),
            objects_after=list(data.get("objects_after") or []),
            error=data.get("error", ""),
            timestamp=data.get("timestamp", ""),
            accepted=bool(data.get("accepted", False)),
            note=data.get("note", ""),
            result=data.get("result", ""),
            duration_ms=int(data.get("duration_ms", 0)),
            stale=data.get("stale", ""),
        )


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_json(records: list[StepRecord], meta: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "version": JOURNAL_VERSION,
            "meta": dict(meta or {}),
            "records": [r.to_dict() for r in records],
        },
        ensure_ascii=False,
    )


def meta_from_json(text: str | None) -> dict[str, Any]:
    """The journal-level meta block (plan description); never raises."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    meta = data.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def from_json(text: str | None) -> list[StepRecord]:
    """Parse the journal property; never raises (a corrupt journal is empty)."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[StepRecord] = []
    for raw in data.get("records") or []:
        if not isinstance(raw, dict):
            continue
        try:
            rec = StepRecord.from_dict(raw)
        except (TypeError, ValueError):
            continue
        upgrade_legacy_label(rec)
        out.append(rec)
    return out


def reindex(records: list[StepRecord]) -> None:
    for i, rec in enumerate(records, start=1):
        rec.index = i


# --- one row-label grammar for every step ------------------------------------
#
# The panel's Step column answers one question: what IS this step? The caller's
# own ``description`` is the honest answer — it is the intent — and wins
# whenever it was written. With no description the label is derived from the
# op and the single parameter that identifies the step, so a row still reads
# as a step ("pad 'FlangeProfile' 6mm") rather than as a call ("pad on
# 'FlangeProfile'"). One grammar for every op, so a column of rows scans as a
# single document:
#
#     <verb> '<target>' <detail>
#
# The same builder feeds both halves of the journal — a committed step's label
# and a planned step's describe_step — so planned and done rows agree.

# The one scalar that names the step, with its unit. Key lookup is
# case-insensitive: cad() passes user params through verbatim, so a caller's
# spec may carry "Length" while the builders read "length".
_SCALAR_DETAIL: dict[str, tuple[str, str]] = {
    "pad": ("length", "mm"),
    "pocket": ("length", "mm"),
    "revolution": ("angle", "°"),
    "groove": ("angle", "°"),
    "fillet": ("radius", "mm"),
    "chamfer": ("size", "mm"),
    "thickness": ("value", "mm"),
    "draft": ("angle", "°"),
}

# Ops this grammar owns. Anything else (assemble, set_anchors, assembly, …)
# already writes a richer label of its own, and must not be "corrected" into
# the generic form.
_DERIVED_OPS = frozenset(
    {
        "create_object",
        "edit_object",
        "delete_object",
        "batch",
        "boolean",
        "fillet",
        "chamfer",
        "loft",
        "sweep",
        "mirror",
        "pattern",
        "move",
        "color",
        "variables",
        "sketch",
        "pad",
        "pocket",
        "revolution",
        "groove",
        "thickness",
        "draft",
        "datum_plane",
        "hull",
    }
)


def first_line(text: str | None) -> str:
    """A label is one line: the first non-empty line of a description block."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _num_text(value: Any) -> str:
    """A parameter as it should read in a label: 6.0 -> "6", "6 mm" as written."""
    if isinstance(value, str):
        return value.strip()
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _params_ci(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    return {str(k).lower(): v for k, v in params.items()}


def _color_text(value: Any) -> str:
    """A color parameter as it should read in a label: [0.8,.1,.1] -> "#cc1a1a".

    Local to this module on purpose: the label grammar is pure and must stay
    importable without FreeCAD, and property_mapper's parser imports it.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        try:
            channels = [float(c) for c in value]
        except (TypeError, ValueError):
            return str(value)
        if max(channels[:3]) > 1.0:
            channels = [c / 255.0 for c in channels]
        return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in channels[:3])
    return str(value)


def feature_detail(operation: str, params: Any) -> str:
    """The ONE parameter that identifies a step ("6mm", "polar ×8"), else "".

    The Op column already names the operation; this names the thing, which is
    what turns a recorded call into a readable step when the caller wrote no
    description of its own.
    """
    p = _params_ci(params)
    op = str(operation or "")
    if p.get("through_all") and op in ("pocket", "pad"):
        # No length to show: this is PartDesign's through-all (parametric), and
        # the row must say so rather than fall back to a bare op name.
        return "through"
    scalar = _SCALAR_DETAIL.get(op)
    if scalar is not None:
        key, unit = scalar
        value = p.get(key)
        if value is None and key == "angle" and op in ("revolution", "groove"):
            # FreeCAD's default is a full revolve, and the row must not depend
            # on whether the caller spelled it out: "revolution 'Prof'" and
            # "revolution 'Prof' 360°" are the same step.
            value = 360
        if value is None:
            return ""
        text = _num_text(value)
        # A string already carries its own unit ("6 mm") or is an expression
        # ("=Vars.Thickness"); only a bare number needs the unit appended.
        return text if isinstance(value, str) else f"{text}{unit}"
    if op == "pattern":
        kind = "polar" if str(p.get("pattern_type", "linear")).lower() == "polar" else "linear"
        count = p.get("count")
        return f"{kind} ×{_num_text(count)}" if count is not None else kind
    if op == "boolean":
        return str(p.get("op") or "")
    if op == "mirror":
        return f"across {str(p.get('plane') or 'XY').upper()}"
    if op == "sweep":
        return f"along '{p['path']}'" if p.get("path") else ""
    if op == "move":
        delta = p.get("translate")
        if isinstance(delta, dict):
            delta = [delta.get("x", 0), delta.get("y", 0), delta.get("z", 0)]
        if isinstance(delta, (list, tuple)) and len(delta) == 3:
            return "Δ(" + ", ".join(_num_text(v) for v in delta) + ")"
        rotate = p.get("rotate")
        if isinstance(rotate, dict) and rotate.get("angle") is not None:
            return f"rotate {_num_text(rotate['angle'])}°"
        return "placement" if p.get("placement") else ""
    if op == "variables":
        cells = p.get("cells")
        return f"{len(cells)} cell(s)" if isinstance(cells, dict) and cells else ""
    if op == "color":
        bits = []
        if p.get("color") is not None:
            bits.append(_color_text(p["color"]))
        if p.get("transparency") is not None:
            bits.append(f"{_num_text(p['transparency'])}%")
        if not bits:
            for key in ("line_color", "display_mode", "draw_style"):
                if p.get(key) is not None:
                    bits.append(_color_text(p[key]) if key == "line_color" else str(p[key]))
                    break
        if not bits and p.get("visible") is not None:
            bits.append("show" if p["visible"] else "hide")
        return " ".join(bits)
    if op == "sketch":
        bits = []
        for key, word in (("geometry", "geom"), ("constraints", "con")):
            count = len(p.get(key) or [])
            if count:
                bits.append(f"{count} {word}")
        return " / ".join(bits)
    if op == "loft":
        count = len(p.get("profiles") or [])
        return f"{count} profiles" if count else ""
    if op == "hull":
        views = p.get("sketches")
        count = len(views) if isinstance(views, (list, dict)) else 0
        return f"{count} views" if count else ""
    if op == "datum_plane":
        plane = p.get("plane")
        if isinstance(plane, dict):
            plane = plane.get("datum") or plane.get("plane")
        return str(plane) if plane else ""
    return ""


def batch_label(ops: Any) -> str:
    """ "batch ×5: pad, pocket, fillet" — what a batch row should say.

    "batch (5 ops)" described the container, not the step; the distinct
    sub-op verbs make the row scannable without opening it.
    """
    verbs: list[str] = []
    for sub in ops or []:
        if not isinstance(sub, dict):
            continue
        verb = str(sub.get("action") or sub.get("operation") or "")
        if verb and verb not in verbs:
            verbs.append(verb)
    head = f"batch ×{len(ops or [])}"
    if not verbs:
        return head
    tail = ", ".join(verbs[:4]) + (", …" if len(verbs) > 4 else "")
    return f"{head}: {tail}"


def step_label(
    operation: str,
    target: str | None = "",
    params: Any = None,
    description: str | None = "",
    detail: str | None = None,
) -> str:
    """The canonical one-line label for a step.

    The caller's description when it wrote one, else the derived
    ``<verb> '<target>' <detail>`` form. ``detail`` overrides the derived
    detail for the few ops whose identifying text is not a spec parameter
    (create_object's type).
    """
    note = first_line(description)
    if note:
        return note
    op = str(operation or "")
    if detail is None:
        detail = feature_detail(op, params)
    name = str(target or "")
    head = f"{op} '{name}'" if name else op
    return f"{head} {detail}".strip() if detail else head


def derived_label(rec: StepRecord) -> str:
    """A recorded step's mechanical label, recomputed from its stored params.

    Used by the hover tooltip: when the row label is the caller's description,
    this is the only place the operation and its parameters still surface.
    "" for the ops that write their own label (assemble, set_anchors, …).
    """
    op = rec.operation
    if op not in _DERIVED_OPS:
        return ""
    params = rec.params or {}
    if op == "batch":
        ops = params.get("ops")
        return batch_label(ops) if ops else ""
    if op == "create_object":
        name = params.get("obj_name")
        if not name:
            return ""
        obj_type = params.get("obj_type")
        return step_label("create", name, detail=f"({obj_type})" if obj_type else "")
    if op in ("edit_object", "delete_object"):
        name = params.get("obj_name")
        return step_label(op.split("_", 1)[0], name) if name else ""
    name = params.get("obj_name")
    if not name:
        return ""
    return step_label(op, name, params.get("obj_properties"))


def upgrade_legacy_label(rec: StepRecord) -> bool:
    """Re-label a step recorded before the label grammar existed.

    The old auto labels were "<op> on '<target>'", "batch (N ops)" and
    "create <type> '<name>'". They carry nothing the derived label does not,
    and a caller's description never looks like them, so upgrading is
    lossless: a model built before the change reads as a set of steps rather
    than a set of calls the moment it is opened. Returns whether it changed.
    """
    params = rec.params or {}
    name = params.get("obj_name")
    legacy = {
        f"{rec.operation} on '{name}'",
        f"batch ({len(params.get('ops') or [])} ops)",
        f"create {params.get('obj_type')} '{name}'",
    }
    if rec.label not in legacy:
        return False
    upgraded = derived_label(rec)
    if not upgraded or upgraded == rec.label:
        return False
    rec.label = upgraded
    return True


def describe_step(step: dict[str, Any]) -> str:
    """Derived label for a *planned* step (a cad() argument dict).

    Shares step_label with the committed path, so a planned row and the same
    step after it ran read identically instead of drifting apart.
    """
    op = str(step.get("operation") or step.get("action") or "")
    if op == "execute_code":
        # A planned snippet has no effect yet, so its row is its own leading
        # comment; without one there is nothing honest to show.
        return snippet_description(str(step.get("code") or "")) or "execute_code"
    if op not in _DERIVED_OPS:
        # Ops outside the grammar (align_shapes, assemble, …) keep the plain
        # "<op> '<target>'" form rather than losing their target entirely.
        name = step.get("obj_name")
        return f"{op} '{name}'" if name else op
    note = step.get("description")
    if op == "batch":
        return batch_label(step.get("ops"))
    if op == "create_object":
        obj_type = step.get("obj_type")
        return step_label(
            "create",
            step.get("obj_name"),
            description=note,
            detail=f"({obj_type})" if obj_type else "",
        )
    if op in ("edit_object", "delete_object"):
        return step_label(op.split("_", 1)[0], step.get("obj_name"), description=note)
    return step_label(op, step.get("obj_name"), step.get("obj_properties"), description=note)


def params_for(step: dict[str, Any]) -> dict[str, Any]:
    """The exact payload ``step_engine.execute_record`` needs to re-run a step.

    Everything except routing/presentation keys passes through: create/edit use
    ``obj_name``/``obj_type``/``obj_properties``, batches use ``ops``, and the
    assembly ops carry their own payloads (``mates``, ``anchors``, ``spec``, …).
    Whitelisting keys here used to silently strip an assemble step's mates, so
    a planned assemble could never run.
    """
    return {
        k: v for k, v in step.items() if k not in ("operation", "action", "description", "label")
    }


def sub_operation(sub: dict[str, Any]) -> str:
    """A batch sub-op's operation name, tolerating both key conventions.

    cad() batch ops arrive as {"action": ...} (the RPC batch schema) and are
    journaled verbatim, while journal-native steps use {"operation": ...} —
    without this, re-running a recorded batch step resolves "" and dies with
    "operation '' is not re-executable".
    """
    return str(sub.get("operation") or sub.get("action") or "")


def objects_after_index(records: list[StepRecord], index: int) -> list[str]:
    """The object list recorded for the last done step at or before ``index``."""
    names: list[str] = []
    for rec in records:
        if rec.index <= index and rec.state == STATE_DONE:
            names = list(rec.objects_after or [])
    return names


def created_since(records: list[StepRecord], index: int) -> list[str]:
    """Objects done steps after ``index`` introduced, from before/after diffs.

    Diffs, never whole snapshots: ``objects_after`` lists the ENTIRE document,
    so subtracting one step's snapshot from another's drags in objects that
    predate the journal. At ``index`` 0 the target snapshot is empty by
    definition, and a rollback must never delete what the journal did not
    build — the user's own objects sit in every snapshot. A record without a
    before-snapshot (journals written before ``objects_before`` existed)
    falls back to the previous done record's after-list, which is the same
    document one step earlier.
    """
    created: set[str] = set()
    prev_after: set[str] = set()
    for rec in records:
        if rec.state != STATE_DONE:
            continue
        if not getattr(rec, "mutated", True):
            # A record that provably changed nothing (a snapshot marker, a
            # skipped read-only step) carries whole-document snapshots as its
            # before/after span, so its "diff" names the user's own
            # pre-journal objects — feeding them to a removal set deleted
            # them (the v0.5.4 data-loss class re-entering via snapshot).
            # Contribute nothing; still advance the fallback chain.
            if rec.objects_after:
                prev_after = set(rec.objects_after)
            continue
        before = set(rec.objects_before) if rec.objects_before else prev_after
        if rec.index > index:
            created |= set(rec.objects_after or []) - before
        if rec.objects_after:
            prev_after = set(rec.objects_after)
    return sorted(created)


def steps_without_undo(records: list[StepRecord], index: int) -> list[int]:
    """Done steps after ``index`` that own no transaction.

    These are the ones a rollback cannot actually undo: FreeCAD's undo stack
    holds nothing for them, so whatever they changed survives the rollback. A
    journal that mixed transactional steps with older non-transactional ones is
    exactly how a rollback ends up reporting success while the model keeps the
    objects it was asked to drop.
    """
    return [
        rec.index
        for rec in records
        if rec.index > index and rec.state == STATE_DONE and rec.mutated and not rec.transaction
    ]


def undo_trust_span(
    stack_names: list[str],
    records: list[StepRecord],
    upto_index: int,
    undo_count: int,
    skip: set[int] | frozenset[int] = frozenset(),
) -> int:
    """How many of the rollback's newest transactions ``stack_names`` still holds.

    ``stack_names`` is FreeCAD's undo stack, newest first, with other documents'
    ghost entries removed. FreeCAD caps that stack (the ``MaxUndoSize``
    preference, 20 by default), so a journal longer than the cap can NEVER
    supply the whole span: the oldest entries were evicted by FreeCAD, not by an
    edit. Comparing the whole span then reads a capped stack as "does not hold
    the journal's transactions" (the reopened-document verdict) and forces the
    destructive rebuild path even though every entry the stack still has matches
    the journal, in order (live-caught: rollback to 0 on a 24-transaction
    journal reported exactly that, while the top 20 names were identical).

    Only DONE records count, exactly as in ``plan_rollback``: a FAILED record
    owns no undo entry (its transaction aborted), so letting its name into the
    expectation would break the prefix at a step the stack never held — and a
    failed record can still carry the transaction string of an EARLIER
    successful run of the same step, which is the stale name that made a
    perfectly healthy rollback look untrustworthy and rebuilt the model.

    Returns the length of the matching prefix, so a caller can undo what the
    stack really holds and say honestly what it could not reach. A foreign
    (off-journal) transaction in the way stops the prefix there, which is the
    case where popping further would undo the WRONG transaction.
    """
    expected = [
        rec.transaction
        for rec in reversed(records)
        if rec.index > upto_index
        and rec.state == STATE_DONE
        and rec.transaction
        and rec.index not in skip
    ]
    span = 0
    # strict=False: the stack is legitimately SHORTER than the span when FreeCAD
    # has already evicted entries, and that shortfall is the answer.
    for name, want in zip(stack_names, expected, strict=False):
        if name != want:
            break
        span += 1
    return span


def done_after_index(records: list[StepRecord], index: int) -> int:
    """How many DONE records sit after ``index``: the ones ``rewind`` must move.

    A rollback's rewind count is NOT ``done_count(records) - index``: that
    assumes records 1..index are all done, so any FAILED or PLANNED hole inside
    them makes the count too small and leaves later records marked ``done``
    while their transactions were already undone — the journal then claims work
    the model no longer has (live-caught: ``[failed, done]`` rolled back to 1
    rewound nothing). Counting the records the rollback actually affected is
    the same number ``plan_rollback`` reports as ``affected``.
    """
    return sum(1 for rec in records if rec.index > index and rec.state == STATE_DONE)


def unrecoverable_steps(records: list[StepRecord], index: int) -> list[int]:
    """Done steps up to ``index`` that nothing can put back.

    Such a step may have changed the document, yet it owns no transaction (so
    FreeCAD's undo cannot reach it) and is not re-executable (its operation or
    code was never recorded, as in journals written before execute_code became
    transactional). A rollback that hits one cannot restore the model exactly.
    """
    return [
        rec.index
        for rec in records
        if rec.index <= index and rec.state == STATE_DONE and rec.mutated and not rec.executable
    ]


def blocking_text(records: list[StepRecord], indices: list[int]) -> str:
    """Name the steps a rollback must be forced across, with their real ops.

    The message used to hardcode "execute_code", but a snapshot marker is also
    a blocking, non-transactional record — naming the wrong op sends the user
    looking in the wrong place.
    """
    ops: list[str] = []
    for index in indices:
        rec = next((r for r in records if r.index == index), None)
        op = rec.operation if rec is not None else "?"
        if op not in ops:
            ops.append(op)
    return f"{indices} ({'/'.join(ops)} without a transaction)"


def effect_label(changed: bool, before: list[str], after: list[str]) -> str:
    """Describe what a snippet DID — what an execute_code step row should say.

    The code's first line is usually boilerplate (``import FreeCAD`` /
    ``doc = FreeCAD.getDocument(...)``), so the object delta is the honest
    summary: "read-only", "+4 object(s): Hole1, …", or "changed properties" for
    a snippet that only edited existing ones.
    """
    if not changed:
        return "read-only"
    added = [n for n in after if n not in set(before)]
    removed = [n for n in before if n not in set(after)]
    if added:
        head = ", ".join(added[:3]) + (", …" if len(added) > 3 else "")
        return f"+{len(added)} object(s): {head}"
    if removed:
        return f"-{len(removed)} object(s)"
    return "changed properties"


# A PEP-263 coding cookie ("# -*- coding: utf-8 -*-", "# coding=latin-1") is
# encoding boilerplate, never a step description.
_CODING_RE = re.compile(r"^#.*\bcoding[:=]")


def snippet_description(code: str) -> str:
    """The execute_code description convention: the snippet's LEADING comment
    block is the step's human description ("# 步骤1: 琴身轮廓 + f孔").

    Leading blank lines, a shebang and the coding cookie are boilerplate and
    skipped; the block ends at its first blank or non-comment line — a later
    comment is an implementation note, not the description. "" = the snippet
    carries no description (callers fall back to the effect label).
    """
    lines: list[str] = []
    for raw in str(code or "").splitlines():
        line = raw.strip()
        if not lines and (not line or line.startswith("#!") or _CODING_RE.match(line)):
            continue
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return "\n".join(lines).strip()


def execute_code_label(code: str, changed: bool, before: list[str], after: list[str]) -> str:
    """The row label recorded for an execute_code step.

    One label, wherever it is read: the snippet's LEADING comment block says
    what the code IS (the documented convention), the effect says what it DID.
    The label used to be the effect alone, so the panel showed the description
    (composed in row_text) while step_control(status) and the MCP journal
    snapshot showed "execute_code: read-only" for the same step — two names for
    one row. Legacy labels of that old form are still rendered correctly by
    row_text, so reading a journal written by an older addon stays lossless.
    """
    effect = effect_label(changed, before, after)
    desc = snippet_description(code)
    if desc:
        return f"{desc.splitlines()[0]} · {effect}"
    return f"execute_code: {effect}"


# A recorded snippet captures the document NAME it ran against
# ("App.getDocument('MideaDeskFan')"). Save the file under a new name and
# reopening hands FreeCAD a document named after the FILE — every reference
# then dies with "Unknown document" and a rebuild cannot even start.
_DOC_REF_RE = re.compile(r"\bgetDocument\(\s*(['\"])([^'\"]+)\1\s*\)")


def document_refs(code: str) -> set[str]:
    """Document names a snippet resolves through ``getDocument(<literal>)``."""
    return {m.group(2) for m in _DOC_REF_RE.finditer(code or "")}


def rewrite_document_refs(code: str, mapping: dict[str, str]) -> str:
    """Re-point ``getDocument`` literals per ``mapping`` (old name -> new)."""

    def sub(m: re.Match) -> str:
        name = mapping.get(m.group(2), m.group(2))
        return f"getDocument({m.group(1)}{name}{m.group(1)})"

    return _DOC_REF_RE.sub(sub, code or "")


def step_description(rec: StepRecord) -> str:
    """A step's human description — one rule for every op, so the panel has a
    single canonical text to show (row, tooltip, spotlight).

    execute_code: the snippet's leading comment block (snippet_description) —
    the recorded label only says what the snippet DID, not what it IS. Every
    other op: the recorded label, which is already human text ("pad 'Pad'", a
    snapshot's note).
    """
    if rec.operation == "execute_code":
        return snippet_description(str((rec.params or {}).get("code") or ""))
    return rec.label


def row_text(rec: StepRecord) -> str:
    """The panel tree row for one step.

    An execute_code row leads with its description (what the snippet IS) and
    keeps the effect (what it DID) behind it. A record written by this addon
    already carries that composed label; a legacy record carries only the
    effect, so the description is composed here. The description is read off
    the CURRENT params, so editing the snippet's leading comment updates the row
    on the next refresh. Pure so the display rule is unit-testable without Qt.
    """
    label = rec.label
    desc = step_description(rec)
    if rec.operation == "execute_code" and desc:
        first = desc.splitlines()[0]
        if not label.startswith(first):
            if label.startswith("execute_code: "):
                # A legacy record carries the effect alone; compose on top.
                label = f"{first} · {label.removeprefix('execute_code: ')}"
            elif " · " in label:
                # This addon's label is "desc · effect" and the comment has
                # since been EDITED: replace the description segment, or the
                # row grows a stale "new · old · effect".
                label = f"{first} · {label.split(' · ', 1)[1]}"
            else:
                label = f"{first} · {label}"
    if (rec.params or {}).get("user_edited"):
        label += " (edited by hand)"
    if rec.error:
        label += f"  — {rec.error}"
    return label


def tooltip_text(rec: StepRecord) -> str:
    """Full row text for hover: the row label, then what the step actually did.

    When the label IS the caller's description, the derived text is the only
    place the operation and its parameters survive — so the tooltip keeps
    both. For a step with no description the two are equal and the label
    alone is the tooltip.
    """
    if rec.operation == "execute_code":
        desc = step_description(rec)
        if desc:
            first = desc.splitlines()[0]
            # The label is "description · effect" (this addon) or "execute_code:
            # effect" (legacy); either way the effect is what follows the
            # description, so the tooltip can show the full comment block.
            if rec.label.startswith(first):
                effect = rec.label[len(first) :].lstrip(" ·")
            else:
                effect = rec.label.removeprefix("execute_code: ")
            return f"{desc}\n{effect}" if effect else desc
        return rec.label
    derived = derived_label(rec)
    if derived and derived != rec.label:
        return f"{rec.label}\n{derived}"
    return rec.label


def build_record(
    step: dict[str, Any],
    index: int,
    state: str,
    executable_ops: set[str],
    label: str = "",
) -> StepRecord:
    op = str(step.get("operation", ""))
    # A planned execute_code step is executable when it CARRIES its snippet:
    # _execute_one re-runs recorded code through the same executor the RPC
    # handler uses, so the op name simply was not in EXECUTABLE_OPS — a plan
    # holding a snippet used to be accepted and then silently skipped at run
    # time ("skipped: not re-executable").
    executable = op in executable_ops or (op == "execute_code" and bool(step.get("code")))
    return StepRecord(
        index=index,
        state=state,
        operation=op,
        label=label or describe_step(step),
        params=params_for(step),
        executable=executable,
        timestamp=stamp(),
    )


def planned_tail_start(records: list[StepRecord]) -> int:
    """Index of the first record in the trailing run of ``planned`` records."""
    n = len(records)
    while n > 0 and records[n - 1].state == STATE_PLANNED:
        n -= 1
    return n


def drop_planned(records: list[StepRecord]) -> int:
    """Discard every ``planned`` record, wherever it sits; returns the count.

    A new commit invalidates the not-yet-executed plan, and the plan is NOT
    guaranteed to be a trailing run: read-only inspections are appended AFTER
    it (so index-addressed verbs see stable numbers), and a snapshot marker
    may already sit behind it. Slicing from ``planned_tail_start`` then removes
    nothing and a stale plan survives the commit — removal has to key on
    STATE, not position.
    """
    keep = [r for r in records if r.state != STATE_PLANNED]
    dropped = len(records) - len(keep)
    records[:] = keep
    return dropped


def insert_before_plan(records: list[StepRecord], rec: StepRecord) -> int:
    """Put a step that just ran ahead of the planned ones; returns how many follow it.

    The journal then lists steps in the order they happened, as FreeCAD's undo
    stack does: rollback trusts the stack only when the two agree. Dropping the
    plan instead threw away every planned step without telling anyone (live: a
    snippet that only called sketch.solve() wiped a 9-step plan).
    """
    first = next((i for i, r in enumerate(records) if r.state == STATE_PLANNED), None)
    if first is None:
        records.append(rec)
        return 0
    records.insert(first, rec)
    return sum(1 for r in records[first + 1 :] if r.state == STATE_PLANNED)


def plan_shift_note(records: list[StepRecord], rec: StepRecord, moved: int) -> str:
    """What the caller must know after insert_before_plan moved planned steps on."""
    if not moved:
        return ""
    planned = [i + 1 for i, r in enumerate(records) if r.state == STATE_PLANNED]
    where = next(i + 1 for i, r in enumerate(records) if r is rec)
    span = f"{planned[0]}-{planned[-1]}" if len(planned) > 1 else str(planned[0])
    return (
        f"step {where} ({rec.label or rec.operation}) was recorded before the {moved} planned "
        f"step(s), which kept their order and are now steps {span}: use the new numbers."
    )


def set_plan(
    records: list[StepRecord], steps: list[dict[str, Any]], executable_ops: set[str]
) -> list[StepRecord]:
    """Replace the not-yet-executed plan with a fresh plan."""
    drop_planned(records)
    added: list[StepRecord] = []
    for step in steps:
        rec = build_record(step, len(records) + 1, STATE_PLANNED, executable_ops)
        records.append(rec)
        added.append(rec)
    reindex(records)
    return added


def next_runnable(records: list[StepRecord]) -> StepRecord | None:
    """The next record a run action must apply: planned OR failed.

    A FAILED record is one whose transaction aborted, so its effect is not in
    the model: it is still to be applied, exactly like a planned one. Skipping
    it stepped the rest of the plan over a dependency that was never created
    (live: a failed pocket, then the polar pattern whose base it was, then a
    move that ran "successfully" on a part missing both). Re-running is the
    honest reading of "Next": retry the step that is not applied yet. A caller
    who wants to abandon a failed step rejects it or edits it, which is the
    deliberate act rather than a side effect of pressing Next.
    """
    for rec in records:
        if rec.state in (STATE_PLANNED, STATE_FAILED):
            return rec
    return None


def next_planned(records: list[StepRecord]) -> StepRecord | None:
    """The next PLANNED record only (kept for callers that want the plan tail)."""
    for rec in records:
        if rec.state == STATE_PLANNED:
            return rec
    return None


def replan_failed(records: list[StepRecord], to_index: int) -> list[int]:
    """Return FAILED records after ``to_index`` to ``planned``; returns their indices.

    A rollback puts the model back at ``to_index``, and a failed record was
    never applied in the first place, so keeping it marked failed left it out of
    reach of the cursor: the panel's Next skipped it and ran the steps BEHIND it
    against a dependency that does not exist (live: a failed pocket and pattern
    were skipped, and a move step then ran "successfully" on a part missing
    both). The rebuild path already reset failed records for exactly this
    reason; the native undo path did not, so the two rollback paths disagreed
    about what the journal means after a rollback.
    """
    replanned = [r for r in records if r.state == STATE_FAILED and r.index > to_index]
    for rec in replanned:
        rec.state = STATE_PLANNED
        rec.error = ""
    return [r.index for r in replanned]


def pending_count(records: list[StepRecord]) -> int:
    return sum(1 for r in records if r.state == STATE_PLANNED)


def done_count(records: list[StepRecord]) -> int:
    return sum(1 for r in records if r.state == STATE_DONE)


def plan_rollback(
    records: list[StepRecord], to_index: int, skip: set[int] | frozenset[int] = frozenset()
) -> dict[str, Any]:
    """What it takes to put the model back at ``to_index``.

    ``undo_count`` — how many FreeCAD transactions to undo. Only records that
    committed one count: a non-atomic execute_code/snapshot record carries no
    transaction, and counting it would undo a transaction that belongs to an
    EARLIER step (the undo stack is a plain stack).
    ``affected``   — the indices going back to ``planned``.
    ``blocking``   — non-atomic indices in that range that may have changed the
                     document without a transaction: undo would revert the wrong
                     change, so the caller must ask for force. A read-only
                     execute_code (mutated=False) is not one of them.
    ``accepted``   — reviewed indices in that range: rolling back across them
                     discards someone's approval, so the caller must force.
    ``skip`` are steps the user already undid or deleted (stale): they go
    back to planned with the rest, but their transactions are not on the undo
    stack any more, so they claim no undo.
    """
    affected = [r for r in records if r.state == STATE_DONE and r.index > to_index]
    return {
        "undo_count": sum(1 for r in affected if r.transaction and r.index not in skip),
        "affected": [r.index for r in affected],
        "blocking": [r.index for r in affected if not r.atomic and r.mutated],
        "accepted": [r.index for r in affected if r.accepted],
    }


# --- review semantics --------------------------------------------------------


def plan_reject(
    records: list[StepRecord], index: int, skip: set[int] | frozenset[int] = frozenset()
) -> dict[str, Any] | None:
    """What rejecting step ``index`` destroys: everything from it onward.

    Uniform semantics, mirroring "a new commit invalidates the planned tail":
    transaction-bearing done steps in the range are undone, planned ones
    dropped — the tail was authored against step ``index`` existing, so
    keeping it would replay steps against a world they were not designed
    for. ``skip`` are steps the user already undid (see plan_rollback). None =
    no such step.
    """
    if not any(r.index == index for r in records):
        return None
    drop = [r for r in records if r.index >= index]
    done = [r for r in drop if r.state == STATE_DONE]
    return {
        "undo_count": sum(1 for r in done if r.transaction and r.index not in skip),
        "drop": [r.index for r in drop],
        "blocking": [r.index for r in done if not r.atomic and r.mutated],
        "accepted": [r.index for r in done if r.accepted],
    }


def set_accepted(records: list[StepRecord], index: int, on: bool = True) -> StepRecord | None:
    """Toggle the review marker on a done step. None = no such done step."""
    rec = next((r for r in records if r.index == index), None)
    if rec is None or rec.state != STATE_DONE:
        return None
    rec.accepted = bool(on)
    return rec


def merge_params(
    old: dict[str, Any], new: dict[str, Any], prefix: str = ""
) -> tuple[dict[str, Any], list[str]]:
    """Deep-merge ``new`` into ``old``; also returns the dotted keys it replaced.

    Nested dicts merge key by key, so ``{"obj_properties": {"length": 12}}``
    changes the length and keeps every other property, including the ones the
    user's edits synced in. Any other value (a list, a number) replaces the old
    one, and ``None`` removes the key. A replaced key is one that held a
    different value before.
    """
    merged = dict(old or {})
    replaced: list[str] = []
    for key, value in (new or {}).items():
        path = f"{prefix}{key}"
        if value is None:
            if key in merged:
                merged.pop(key)
                replaced.append(path)
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key], inner = merge_params(current, value, f"{path}.")
            replaced.extend(inner)
            continue
        if key in merged and current != value:
            replaced.append(path)
        merged[key] = value
    return merged, replaced


def update_planned(
    records: list[StepRecord],
    index: int,
    params: dict[str, Any] | None = None,
    label: str = "",
    replace: bool = False,
) -> tuple[StepRecord, list[str]] | None:
    """Edit a not-yet-committed step in place. None = done or unknown step.

    ``params`` deep-merge into the step (see ``merge_params``), so a caller
    sends only what it changes; ``replace`` swaps them wholesale instead, for
    an editor that holds the complete dict (a key it deleted must go). Returns
    the record and the keys whose values were replaced.
    """
    rec = next((r for r in records if r.index == index), None)
    if rec is None or rec.state not in (STATE_PLANNED, STATE_FAILED):
        return None
    replaced: list[str] = []
    if params:
        if replace:
            replaced = sorted(
                k for k in rec.params or {} if (rec.params or {}).get(k) != params.get(k)
            )
            rec.params = dict(params)
        else:
            rec.params, replaced = merge_params(rec.params or {}, params)
    if label:
        rec.label = label
    return rec, replaced


def set_label(records: list[StepRecord], index: int, label: str) -> StepRecord | None:
    """Rename any recorded step (planned, failed or DONE).

    ``update`` edits planned/failed params, and a done step's params must not
    be edited out of the review loop (its transaction is history) — but its
    LABEL is presentation, and "the row says something wrong" is a reason to
    fix the row, not to re-run the step. None = unknown step or empty label.
    """
    if not label:
        return None
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return None
    rec.label = label
    return rec


def insert_steps(
    records: list[StepRecord],
    after_index: int,
    steps: list[dict[str, Any]],
    executable_ops: set[str],
) -> list[StepRecord] | None:
    """Insert planned steps after ``after_index`` (a count, like run_to).

    Planned steps form ONE contiguous run, but done records may sit behind it
    (appended inspections, a snapshot marker), so the insertion point must
    land at or inside the run — inserting after those done records would
    strand planned steps behind done ones. None = outside the run (or empty
    steps).
    """
    if not steps:
        return None
    planned = [r.index for r in records if r.state == STATE_PLANNED]
    if planned:
        # after_index == planned[0] - 1 lands at the run's head (the old
        # tail-start rule); anything before that is executed history.
        if not (planned[0] - 1 <= after_index <= planned[-1]):
            return None
    elif after_index < len(records):
        return None
    added = [build_record(s, 0, STATE_PLANNED, executable_ops) for s in steps]
    records[after_index:after_index] = added
    reindex(records)
    return added


def last_atomic_done(records: list[StepRecord]) -> StepRecord | None:
    """The most recent completed step that owns an undo transaction.

    Drift detection anchors on this, not on the last completed step outright:
    a non-atomic record (execute_code) carries no transaction name, so
    anchoring on it would compare ``""`` against the undo stack and report a
    clean state even after the user undid work by hand.
    """
    return next(
        (r for r in reversed(records) if r.state == STATE_DONE and r.transaction),
        None,
    )


def rewind(records: list[StepRecord], count: int) -> list[StepRecord]:
    """Move the ``count`` most recently completed records back to ``planned``."""
    completed = [r for r in records if r.state == STATE_DONE]
    back = completed[-count:] if count > 0 else []
    for rec in back:
        rec.state = STATE_PLANNED
        rec.transaction = ""
        rec.error = ""
        rec.stale = ""
    return back


def _record_diffs(records: list[StepRecord]) -> list[tuple[StepRecord, set[str], set[str]]]:
    """(record, objects it created, objects it removed) for every mutating done step."""
    out: list[tuple[StepRecord, set[str], set[str]]] = []
    prev_after: set[str] = set()
    for rec in records:
        if rec.state != STATE_DONE:
            continue
        after = set(rec.objects_after or [])
        if getattr(rec, "mutated", True):
            before = set(rec.objects_before) if rec.objects_before else prev_after
            out.append((rec, after - before, before - after))
        if rec.objects_after:
            prev_after = after
    return out


def creator_of(records: list[StepRecord], name: str) -> StepRecord | None:
    """The done step that created object ``name`` (the latest, if several did)."""
    owner = None
    for rec, created, _removed in _record_diffs(records):
        if name in created:
            owner = rec
    return owner


def stale_steps(
    records: list[StepRecord],
    undo_names: list[str],
    redo_names: list[str],
    present: list[str],
) -> list[dict[str, Any]]:
    """Done steps whose work the model no longer has: ``{index, reason, missing}``.

    A step is stale when the user undid it (its transaction sits on the redo
    stack, or the observer saw the undo and noted it on the record), or when
    objects it created are gone and no later journal step removed them (the
    user deleted them). Checked for EVERY done step: anchoring on the last
    step alone missed a deleted Pocket under a later step, and cleared itself
    as soon as Claude committed anything.
    """
    live = set(present)
    undo, redo = set(undo_names), set(redo_names)
    diffs = _record_diffs(records)
    out: list[dict[str, Any]] = []
    for pos, (rec, created, _removed) in enumerate(diffs):
        removed_later: set[str] = set()
        for _later, _c, gone in diffs[pos + 1 :]:
            removed_later |= gone
        missing = sorted(created - live - removed_later)
        reason = ""
        if rec.transaction and rec.transaction in redo and rec.transaction not in undo:
            reason = "undone by the user (Ctrl+Z)"
        elif rec.stale:
            reason = rec.stale
        elif missing:
            reason = "deleted by the user: " + ", ".join(missing)
        if reason:
            out.append({"index": rec.index, "reason": reason, "missing": missing})
    return out


def drop_step(records: list[StepRecord], index: int) -> StepRecord | None:
    """Forget one done step without touching the model; None = no such done step.

    For the user's undo or deletion of a step's work: the model already lost
    it, so the journal follows instead of re-creating it on the next rollback.
    """
    rec = next((r for r in records if r.index == index), None)
    if rec is None or rec.state != STATE_DONE:
        return None
    records.remove(rec)
    reindex(records)
    return rec


# --- manual-edit sync (pure half) ---------------------------------------------
#
# The engine's document observer mirrors GUI edits on objects a done step
# produced back into that step's params, so a later reexecute/replay does not
# silently revert a human correction. Everything decidable without FreeCAD
# lives here: which object/step owns which property (tracked_objects), and
# what a live value maps back to (map_cell_value / map_constraint_value).


@dataclass(frozen=True)
class SyncRule:
    """How one live feature property maps back onto the op's spec.

    ``kind`` names the converter (``sync_patch``) and the engine's reader:
    ``value`` (a scalar), ``int``, ``spacings`` (a float list, FreeCAD's all -1 meaning "uniform"),
    ``pattern_mode`` (a linear pattern's Spacing/Extent mode), ``pad_mode``
    (a pad/pocket's Type, SideType and Midplane together: ``pad_type``,
    ``through_all``, ``midplane``), ``link_sub`` (a ``[object, subelement]``
    reference),
    ``names`` (the extra originals beyond the step's own base), ``axis``,
    ``mirror_plane``, ``z_offset`` (a pure-z AttachmentOffset) and
    ``edge_size`` (a uniform Part fillet/chamfer size).
    ``default`` is what the builder does when the key is absent, so a live
    value equal to it writes nothing. ``only`` limits the rule to object
    TypeIds; ``when`` to objects whose (property, value) matches.
    """

    key: str
    kind: str = "value"
    default: Any = None
    only: tuple[str, ...] = ()
    when: tuple[str, Any] | None = None


#: A sync_patch value that removes the key from the spec.
REMOVE = object()

# Type, SideType and Midplane are one decision in the spec (pad_type plus the
# through_all/midplane flags, and the keys only one mode reads), so all three
# map through the same rule and its converter reads them together.
_PAD_MODE = SyncRule("pad_type", "pad_mode", default="length")
_PADLIKE = {
    "Length": SyncRule("length", default=10.0),
    "Length2": SyncRule("length2", when=("SideType", "Two sides")),
    "Offset": SyncRule("offset", default=0),
    "Reversed": SyncRule("reversed", default=False),
    "Midplane": _PAD_MODE,
    "SideType": _PAD_MODE,
    "Type": _PAD_MODE,
    "UpToFace": SyncRule("up_to_face", "link_sub", when=("Type", "UpToFace")),
}
_LINEAR = ("PartDesign::LinearPattern",)
_POLAR = ("PartDesign::PolarPattern",)

# Feature op -> {object property: rule}. Every property the op's builder reads
# back from its spec is listed, including keys the caller did not pass (a pad
# built with the default length must still pick up a GUI Length edit, or replay
# reverts it); a new spec key is one line here (plus a reader in the engine for
# a new kind). An expression bound to the property syncs as "=expr" whatever the
# kind. Boolean tool compounds and edge selectors stay unsynced.
# FreeCAD >= 1.1 moved Part-level fillet/chamfer sizes into per-edge Edges
# tuples, so both names are claimed (only the one that exists fires).
FEATURE_SYNC: dict[str, dict[str, SyncRule]] = {
    "fillet": {"Radius": SyncRule("radius"), "Edges": SyncRule("radius", "edge_size")},
    "chamfer": {"Size": SyncRule("size"), "Edges": SyncRule("size", "edge_size")},
    "pad": _PADLIKE,
    "pocket": _PADLIKE,
    "revolution": {
        "Angle": SyncRule("angle", default=360.0),
        "Reversed": SyncRule("reversed", default=False),
    },
    "groove": {
        "Angle": SyncRule("angle", default=360.0),
        "Reversed": SyncRule("reversed", default=False),
    },
    "thickness": {"Value": SyncRule("value"), "Reversed": SyncRule("reversed", default=False)},
    "draft": {"Angle": SyncRule("angle")},
    "loft": {"Solid": SyncRule("solid", default=True), "Ruled": SyncRule("ruled", default=False)},
    "sweep": {"Solid": SyncRule("solid", default=True)},
    "pattern": {
        "Occurrences": SyncRule("count", "int"),
        "Angle": SyncRule("angle", default=360.0, only=_POLAR),
        "Mode": SyncRule("mode", "pattern_mode", default="spacing", only=_LINEAR),
        "Offset": SyncRule("spacing", only=_LINEAR, when=("Mode", "Spacing")),
        "Length": SyncRule("length", only=_LINEAR, when=("Mode", "Extent")),
        "Spacings": SyncRule("spacings", "spacings", only=_LINEAR, when=("Mode", "Spacing")),
        "Reversed": SyncRule("reversed", default=False),
        "Originals": SyncRule("originals", "names", default=[]),
        "Direction": SyncRule("axis", "axis", only=_LINEAR),
        "Axis": SyncRule("axis", "axis", only=_POLAR),
    },
    "mirror": {
        "MirrorPlane": SyncRule("plane", "mirror_plane", default="XY"),
        "Originals": SyncRule("originals", "names", default=[]),
    },
    # A datum plane / attached sketch offset is a pure-z AttachmentOffset;
    # the converter refuses anything more general (rotation, x/y shift).
    "datum_plane": {"AttachmentOffset": SyncRule("offset", "z_offset", default=0)},
    "sketch": {"AttachmentOffset": SyncRule("offset", "z_offset", default=0)},
}


def feature_rule(op: str, prop: str) -> SyncRule | None:
    return FEATURE_SYNC.get(op, {}).get(prop)


# Type enum <-> the spec's pad_type; ThroughAll is the spec's through_all flag.
PAD_TYPES = {
    "Length": "length",
    "TwoLengths": "two_lengths",
    "UpToFirst": "up_to_first",
    "UpToLast": "up_to_last",
    "UpToFace": "up_to_face",
    "UpToShape": "up_to_shape",
}

_ORIGIN_TOKENS = {
    "X_Axis": "X",
    "Y_Axis": "Y",
    "Z_Axis": "Z",
    "H_Axis": "H",
    "V_Axis": "V",
    "N_Axis": "N",
    "XY_Plane": "XY",
    "XZ_Plane": "XZ",
    "YZ_Plane": "YZ",
}


def _link_token(link: Any) -> str | None:
    """("X_Axis" role of a body origin, or a sketch's "H_Axis" sub) -> "X"/"H"."""
    if not isinstance(link, dict):
        return None
    sub = (link.get("subs") or [""])[0]
    return _ORIGIN_TOKENS.get(sub) or _ORIGIN_TOKENS.get(str(link.get("role") or ""))


def sync_patch(
    rule: SyncRule, live: Any, expr: str | None = None, base: str = ""
) -> dict[str, Any] | None:
    """Spec keys (and values) that reproduce ``live``; None = no representation.

    ``live`` is what the engine read (a scalar, a list, or for links a dict
    ``{"name", "subs", "role", "type"}``); ``expr`` the property's binding,
    which wins in every kind, as ``"=expr"``. A value of ``REMOVE`` deletes the
    key (a pocket that is no longer through-all). ``base`` is the step's own
    object, which the spec names apart from its extra originals.
    """
    if expr:
        return {rule.key: "=" + _normalize_expression(str(expr))}
    kind = rule.kind
    if kind in ("value", "edge_size"):
        return None if live is None else {rule.key: live}
    if kind == "int":
        try:
            return {rule.key: int(live)}
        except (TypeError, ValueError):
            return None
    if kind == "spacings":
        if not isinstance(live, (list, tuple)):
            return None
        if all(float(v) == -1 for v in live):
            return {rule.key: REMOVE}
        return {rule.key: [_num(v) for v in live]}
    if kind == "pad_mode":
        return _pad_mode_patch(rule.key, live)
    if kind == "pattern_mode":
        if live not in ("Spacing", "Extent"):
            return None
        if live == "Extent":
            return {rule.key: "extent", "spacings": REMOVE}
        return {rule.key: "spacing"}
    if kind == "link_sub":
        if not live:
            return {rule.key: REMOVE}
        sub = (live.get("subs") or [""])[0]
        return {rule.key: [live.get("name"), sub] if sub else live.get("name")}
    if kind == "names":
        if not isinstance(live, (list, tuple)):
            return None
        return {rule.key: [n for n in live if n != base]}
    if kind == "axis":
        token = _link_token(live)
        return None if token is None else {rule.key: token}
    if kind == "mirror_plane":
        if not live:
            return None
        token = _link_token(live)
        if token in ("XY", "XZ", "YZ"):
            return {rule.key: token, "face": REMOVE}
        sub = (live.get("subs") or [""])[0]
        if sub.startswith("Face"):
            return {"face": [live.get("name"), sub], rule.key: REMOVE}
        if "Plane" in str(live.get("type") or ""):
            return {rule.key: {"datum": live.get("name")}, "face": REMOVE}
        return None
    if kind == "z_offset":
        if not isinstance(live, (list, tuple)) or len(live) != 4:
            return None
        x, y, z, angle = (float(v) for v in live)
        if abs(x) > 1e-9 or abs(y) > 1e-9 or abs(angle) > 1e-9:
            return None
        return {rule.key: _num(z)}
    return None


def _pad_mode_patch(key: str, live: Any) -> dict[str, Any] | None:
    """A pad/pocket's Type + SideType + Midplane as the spec's mode keys.

    ``live`` is ``{"type", "side", "midplane"}``. The spec reads each mode's
    own keys only (up_to_face with up_to_face, length2 with two_lengths), so a
    mode change removes the keys the new mode would refuse.
    """
    if not isinstance(live, dict):
        return None
    side = str(live.get("side") or "")
    fc_type = str(live.get("type") or "")
    if side == "Two sides" or fc_type == "TwoLengths":
        return {key: "two_lengths", "through_all": REMOVE, "midplane": REMOVE, "up_to_face": REMOVE}
    patch: dict[str, Any] = {
        "midplane": True if side == "Symmetric" or live.get("midplane") else REMOVE,
        "length2": REMOVE,
    }
    if fc_type == "ThroughAll":
        patch.update({"through_all": True, key: REMOVE, "up_to_face": REMOVE})
        return patch
    token = PAD_TYPES.get(fc_type)
    if token is None:
        return None
    patch.update({key: REMOVE if token == "length" else token, "through_all": REMOVE})
    if token != "up_to_face":
        patch["up_to_face"] = REMOVE
    return patch


def patch_value_matches(rule: SyncRule, key: str, current: Any, value: Any) -> bool:
    """Whether the spec's ``current`` already says ``value`` (absent = default)."""
    if current is None:
        if value is REMOVE:
            return True
        current = rule.default if key == rule.key else False
    if value is REMOVE:
        return False
    return _same_value(current, value)


# Ops that change an existing sketch's geometry or constraints in place.
SKETCH_EDIT_OPS = frozenset({"sketch_edit"})

# Ops whose obj_name names the object they CREATE (not a base), so the object
# is tracked by name even when the before/after diff is empty (an idempotent
# variables re-run creates nothing, and without this its sheet never syncs).
NEW_OBJECT_OPS = {"variables", "sketch", "datum_plane", "hull"}

# Spec constraint type -> live Sketcher.Constraint.Type. Used to verify that
# the live constraint sequence still lines up with the spec by index before
# any value is synced (a hand-added constraint shifts indices — then nothing
# is synced).
CONSTRAINT_TYPE_MAP = {
    "coincident": "Coincident",
    "horizontal": "Horizontal",
    "vertical": "Vertical",
    "tangent": "Tangent",
    "perpendicular": "Perpendicular",
    "parallel": "Parallel",
    "equal": "Equal",
    "symmetric": "Symmetric",
    "distance": "Distance",
    "distance_x": "DistanceX",
    "distance_y": "DistanceY",
    "radius": "Radius",
    "angle": "Angle",
}

DIMENSIONAL_CONSTRAINTS = ("distance", "distance_x", "distance_y", "radius", "angle")

#: map_cell_value / map_constraint_value return this when the live state has
#: no safe spec representation (the params are left untouched).
UNREADABLE = object()


def _num(value: float) -> int | float:
    f = float(value)
    return int(f) if f.is_integer() else f


def _normalize_expression(expr: str) -> str:
    """Whitespace-stripped formula, preserving string literals.

    Blanket ``"".join(expr.split())`` used to strip whitespace INSIDE quoted
    literals too, so a synced ``=IF(A1="a b",1,2)`` was re-applied as
    ``=IF(A1="ab",1,2)`` — a corrupted formula written back by
    reexecute/replay. FreeCAD formulas quote with double quotes and escape a
    quote by doubling, so toggling on every quote handles both.
    """
    out: list[str] = []
    in_quotes = False
    for ch in expr:
        if ch == '"':
            in_quotes = not in_quotes
            out.append(ch)
        elif in_quotes or not ch.isspace():
            out.append(ch)
    return "".join(out)


def _batch_sync_ops(rec: StepRecord) -> list[tuple[int, str, dict[str, Any]]]:
    """(position, op name, sub-op dict) of a batch record's syncable sub-ops.

    create/edit/move sub-ops carry the payload a manual edit maps back onto;
    feature sub-ops are skipped (see tracked_objects).
    """
    out: list[tuple[int, str, dict[str, Any]]] = []
    for i, sub in enumerate((rec.params or {}).get("ops") or []):
        if not isinstance(sub, dict):
            continue
        op = sub_operation(sub)
        if op in ("create_object", "edit_object", "move"):
            out.append((i, op, sub))
    return out


def _acted_name(params: dict[str, Any]) -> str:
    """The object a step actually acted on.

    ``moved_object`` is the op's own record of what it moved, and it differs
    from the request whenever FreeCAD redirects the op: a PartDesign feature
    does not own its frame, so the move builder moves the owning BODY and names
    it in the reply. Claiming the requested name instead left the Body
    untracked — a manual drag of it synced nowhere — while handing the claim to
    a feature whose Placement every recompute rewrites.
    """
    p = params or {}
    return str(p.get("moved_object") or p.get("obj_name") or "")


def _created_name(rec: StepRecord, sub: dict[str, Any]) -> str:
    """The name a create sub-op really created (FreeCAD de-duplicates).

    Recorded truth first — the batch annotates each sub-op with the object it
    created — then the single-creation case, where the record's own object diff
    names it unambiguously. A multi-create batch recorded before that
    annotation existed still falls back to the requested name.
    """
    name = str(sub.get("obj_name") or "")
    if sub_operation(sub) != "create_object":
        return name
    created = sorted(set(rec.objects_after) - set(rec.objects_before))
    return str(sub.get("created_object") or "") or (created[0] if len(created) == 1 else name)


def _claims_cells(op: str, props: dict[str, Any]) -> bool:
    """Does this step own the spreadsheet it names?

    ``variables`` builds one; a ``create_object``/``edit_object`` can create or
    fill one, and the spec's ``cells`` dict is exactly what ``_sync_cells``
    reads and writes back. Without the claim, a hand-edited cell in a sheet
    that a create_object built synced nowhere.
    """
    return op == "variables" or isinstance((props or {}).get("cells"), dict)


def tracked_objects(records: list[StepRecord]) -> dict[str, dict[str, Any]]:
    """object name -> sync handlers, from done steps.

    Entry keys:
      ``props``  — {object property: (step index, params key)}. Later done
                   steps win per property, so a sync lands on the step that
                   last decided it. create/edit_object also claim Placement —
                   unless a move step targets the object, because a move is
                   relative and an absolute create-time Placement plus the
                   move would double-apply on reexecute.
      ``sheet``  — (step index, batch sub-op) of the step owning the spreadsheet:
                   ``variables``, or any step whose spec carries a ``cells``
                   dict.
      ``sketch`` — (step index, sub) of the sketch step owning the sketch.
      ``move``   — (index, sub) of the LAST done move targeting the object
                   (top-level step or a move sub-op inside a batch). A manual
                   drag folds into it as an absolute placement override
                   (placement wins over translate/rotate on re-run), which
                   reproduces the pose no matter what came before it.

    Property claims are ``(step index, params key, batch sub-op position)``
    with sub None for top-level steps; the engine routes the sync write
    through it. Batch coverage: create/edit/move SUB-OPS sync (their payload
    names the object and carries the scalars a manual edit maps back onto);
    feature sub-ops do not — their created object cannot be attributed from
    the record-level object diff.

    Every claim follows the object the step really acted on: a re-directed
    move claims its Body (``moved_object``), a de-duplicated create claims the
    name FreeCAD assigned, not the one that was requested.
    """
    last_move: dict[str, tuple[int, int | None]] = {}
    for r in records:
        if r.state != STATE_DONE:
            continue
        if r.operation == "move":
            name = _acted_name(r.params or {})
            if name:
                last_move[name] = (r.index, None)
        elif r.operation == "batch":
            for sub_i, sub_op, sub in _batch_sync_ops(r):
                if sub_op == "move":
                    name = _acted_name(sub)
                    if name:
                        last_move[name] = (r.index, sub_i)

    tracked: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.state != STATE_DONE:
            continue
        params = rec.params or {}
        if rec.operation == "batch":
            for sub_i, sub_op, sub in _batch_sync_ops(rec):
                if sub_op == "move":
                    continue  # the fold handler owns move sub-ops
                props = sub.get("obj_properties")
                if not isinstance(props, dict):
                    # A malformed sub-op is journaled verbatim by design; if
                    # it reached .items() the whole document's manual-edit
                    # sync died silently (the exception is swallowed in
                    # slotChangedObject). Treat as "nothing claimable".
                    continue
                claims = {k: k for k, v in props.items() if isinstance(v, (int, float, str, bool))}
                name = _created_name(rec, sub)
                if not name:
                    continue
                entry = tracked.setdefault(
                    name, {"props": {}, "sheet": None, "sketch": None, "move": None}
                )
                for prop, key in claims.items():
                    entry["props"][prop] = (rec.index, key, sub_i)
                if sub_op == "create_object" and name not in last_move:
                    entry["props"]["Placement"] = (rec.index, "Placement", sub_i)
                if _claims_cells(sub_op, props):
                    entry["sheet"] = (rec.index, sub_i)
            continue
        props = params.get("obj_properties")
        if not isinstance(props, dict):
            props = {}
        op = rec.operation
        if op in ("create_object", "edit_object"):
            claims = {k: k for k, v in props.items() if isinstance(v, (int, float, str, bool))}
            if op == "create_object":
                names = set(rec.objects_after) - set(rec.objects_before)
            else:
                names = {str(params.get("obj_name") or "")}
        else:
            claims = {prop: rule.key for prop, rule in FEATURE_SYNC.get(op, {}).items()}
            names = set(rec.objects_after) - set(rec.objects_before)
            if op in NEW_OBJECT_OPS:
                names |= {str(params.get("obj_name") or "")}
        for name in names:
            if not name:
                continue
            entry = tracked.setdefault(
                name, {"props": {}, "sheet": None, "sketch": None, "move": None}
            )
            for prop, key in claims.items():
                entry["props"][prop] = (rec.index, key, None)
            if op in ("create_object", "edit_object") and name not in last_move:
                entry["props"]["Placement"] = (rec.index, "Placement", None)
            # The sheet/sketch handlers belong to the object the step NAMES —
            # the diff can also hold incidental creations (a sketch's Body),
            # which must not answer constraint/cell lookups. For create_object
            # the diff name IS the named object (FreeCAD may have de-duplicated
            # the request), so that is what counts.
            is_named = op == "create_object" or name == str(params.get("obj_name") or "")
            if is_named and _claims_cells(op, props):
                entry["sheet"] = (rec.index, None)
            elif is_named and op == "sketch":
                entry["sketch"] = (rec.index, None)

    for name, (idx, sub) in last_move.items():
        entry = tracked.setdefault(name, {"props": {}, "sheet": None, "sketch": None, "move": None})
        entry["move"] = (idx, sub)
        entry["props"].pop("Placement", None)
    # A later step that reshapes the sketch owns its structure from then on:
    # its replay runs on top of the sketch step's, so a hand edit made after it
    # belongs there, not in the sketch step's spec.
    for rec in records:
        if rec.state == STATE_DONE and rec.operation in SKETCH_EDIT_OPS:
            entry = tracked.get(str((rec.params or {}).get("obj_name") or ""))
            if entry is not None and entry["sketch"] is not None:
                entry["sketch"] = (rec.index, None)
    return tracked


def map_cell_value(old: Any, live_contents: Any) -> Any:
    """A variables-cell's synced value from the raw cell content.

    ``Sheet.get(cell)`` returns the COMPUTED value, which is stale when the
    change event fires (the recompute has not run yet) — live-verified: at
    event time get() still held the previous value while getContents() was
    already fresh. So only ``Sheet.getContents(cell)`` is read, whose 1.1.4
    conventions were measured live (ordinals, not reprs):

    * ``=A1 * 2``   — formula: stored whitespace-normalised (FreeCAD
                      pretty-prints on read, and without normalising every
                      read of a compactly written spec looks like an edit);
    * ``'...``      — text: a LEADING apostrophe marks a text cell
                      (Excel-style; a trailing one is optional). The builder
                      quotes text values, and the quotes persist in the cell
                      text, so one surrounding double-quote pair is stripped
                      too (the spec stores the bare text);
    * ``25``        — number.
    Returns UNREADABLE when the content cannot be read back safely.
    """
    if not isinstance(live_contents, str):
        return UNREADABLE
    c = live_contents.strip()
    if not c:
        return UNREADABLE  # a cleared cell has no spec representation
    if c.startswith("="):
        return "=" + _normalize_expression(c[1:])
    if c.startswith("'"):
        text = c[1:].removesuffix("'")
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text
    try:
        return _num(float(c))
    except ValueError:
        return UNREADABLE


def constraint_expression(exprs: dict[str, Any], index: int, name: str = "") -> str | None:
    """The expression bound to sketch constraint ``index``, if any.

    FreeCAD keys a NAMED constraint's binding by its name
    (``.Constraints.Width``) even when it was bound by index, and only an
    unnamed one by index (``Constraints[3]``). Looking up the index alone read
    a named binding as "unbound", so an edit to any other constraint of the
    sketch rewrote the step's ``=Params.Wd`` to its current number.
    """
    if name:
        expr = exprs.get(f".Constraints.{name}")
        if expr:
            return str(expr)
    expr = exprs.get(f"Constraints[{index}]") or exprs.get(f".Constraints[{index}]")
    return str(expr) if expr else None


def map_constraint_value(ctype: str, old: Any, live_value: Any, live_expr: str | None) -> Any:
    """A dimensional sketch constraint's synced value.

    ``live_value`` reads back in RADIANS for angle constraints (the spec takes
    degrees — same asymmetry as FreeCAD.Rotation). A live expression binding
    wins over the literal in both directions: binding in the GUI turns the
    spec value into ``=expr``, unbinding turns it back into the datum number.
    """
    if live_expr:
        return "=" + _normalize_expression(str(live_expr))
    try:
        value = float(live_value)
    except (TypeError, ValueError):
        return UNREADABLE
    if ctype == "angle":
        value = math.degrees(value)
    return _num(value)


# --- what the user changed ------------------------------------------------------
#
# The engine's observer turns the user's GUI work into events, one per document,
# for two readers: Claude (every RPC reply carries the events since its last call)
# and the steps panel's log. Each reader drains its own copy. Kinds:
#
#   edit    {step, label, what, old, new}   a value synced into a step's params
#   sketch  {step, label, object, detail}   geometry/constraints added or removed
#   undo    {transaction, step, label, added, removed}   Ctrl+Z (redo likewise)
#   added   {objects, types}                objects the user created
#   removed {objects}                       objects the user deleted
#   stale   {step, label, detail}           a done step the model no longer has
#   panel   {action, index, ok, detail}     a Steps-panel button the user pressed


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    return a == b


def append_change(log: list[dict[str, Any]], event: dict[str, Any]) -> None:
    """Append ``event``, folding it into the previous one where that reads better.

    A drag or a typed value fires one edit per keystroke, and an edit followed
    by its own Ctrl+Z is no change at all: repeated edits of one value keep the
    first ``old`` and the last ``new`` and vanish when the two agree. An undo,
    a panel action or a stale notice is a boundary nothing folds across.
    Consecutive additions or deletions become one entry, and an object added
    and then deleted again cancels out.
    """
    kind = event.get("kind")
    if kind == "edit":
        key = (event.get("step"), event.get("what"))
        for pos in range(len(log) - 1, -1, -1):
            prev = log[pos]
            if prev.get("kind") not in ("edit", "sketch", "added", "removed"):
                break
            if prev.get("kind") == "edit" and (prev.get("step"), prev.get("what")) == key:
                prev["new"] = event.get("new")
                if _same_value(prev.get("old"), prev.get("new")):
                    del log[pos]
                return
    last = log[-1] if log else None
    if last is not None and kind == "removed" and last.get("kind") == "added":
        gone = set(event.get("objects") or [])
        pairs = list(zip(last.get("objects") or [], last.get("types") or [], strict=False))
        kept = [(name, typ) for name, typ in pairs if name not in gone]
        if len(kept) < len(pairs):
            cancelled = {name for name, _ in pairs} & gone
            last["objects"] = [name for name, _ in kept]
            last["types"] = [typ for _, typ in kept]
            if not kept:
                log.pop()
            leftover = [n for n in event.get("objects") or [] if n not in cancelled]
            if leftover:
                append_change(log, {**event, "objects": leftover})
            return
    if last is not None and kind in ("added", "removed") and last.get("kind") == kind:
        last["objects"] = list(last.get("objects") or []) + list(event.get("objects") or [])
        if kind == "added":
            last["types"] = list(last.get("types") or []) + list(event.get("types") or [])
        return
    log.append(dict(event))


def _value_text(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe_change(event: dict[str, Any]) -> str:
    """One line for an event: the panel log shows it, tests read it."""
    kind = event.get("kind")
    step = event.get("step")
    where = f"step {step}" + (f" ({event['label']})" if event.get("label") else "") if step else ""
    if kind == "edit":
        old, new = _value_text(event.get("old")), _value_text(event.get("new"))
        return f"{where}: {event.get('what')} {old} -> {new}"
    if kind == "sketch":
        return f"{where}: sketch {event.get('object')} {event.get('detail')}"
    if kind in ("undo", "redo"):
        verb = "undid" if kind == "undo" else "redid"
        target = where or f"'{event.get('transaction')}'"
        bits = []
        if event.get("removed"):
            bits.append("removed " + ", ".join(event["removed"]))
        if event.get("added"):
            bits.append("restored " + ", ".join(event["added"]))
        return f"{verb} {target}" + (f" ({'; '.join(bits)})" if bits else "")
    if kind == "added":
        pairs = zip(event.get("objects") or [], event.get("types") or [], strict=False)
        return "added " + ", ".join(f"{n} ({t})" if t else n for n, t in pairs)
    if kind == "removed":
        return "deleted " + ", ".join(event.get("objects") or [])
    if kind == "stale":
        return f"{where} is no longer in the model: {event.get('detail')}"
    if kind == "panel":
        state = "" if event.get("ok", True) else " (failed)"
        detail = f": {event['detail']}" if event.get("detail") else ""
        return f"Steps panel: {event.get('action')}{state}{detail}"
    return str(event)


def panel_action_text(spec: dict[str, Any]) -> str:
    """What a Steps-panel button did, in words ("rolled back to step 3")."""
    op = str(spec.get("operation") or "")
    index = spec.get("index")
    params = spec.get("params") or {}
    if op == "rollback_to":
        return f"rolled back to step {index}" if index else "rolled back to the start"
    if op == "replay":
        return "replayed the journal from the start"
    if op == "reject":
        return f"rejected step {index} and everything after it"
    if op == "reexecute":
        return f"re-ran step {index} with edited parameters"
    if op == "accept":
        return f"{'accepted' if params.get('on', True) else 'unaccepted'} step {index}"
    if op == "update":
        return f"edited the parameters of step {index}"
    if op == "run_next":
        return "ran the next planned step"
    if op == "run_all":
        return "ran all planned steps"
    if op == "drop":
        return f"dropped step {index} (accepted the change to the model)"
    if op == "snapshot":
        note = params.get("note")
        return "took a snapshot" + (f" ({note})" if note else "")
    return op.replace("_", " ") + (f" step {index}" if index else "")


# --- the user's own sketch edits ------------------------------------------------
#
# A sketch step's spec maps onto the live sketch by index, so the moment the
# user adds or deletes geometry or constraints the two diverge and a replay
# would rebuild the ORIGINAL sketch. The engine reads the live sketch as plain
# data (geometry dicts, constraint dicts, construction flags, external refs)
# and the helpers below turn it back into the spec the builder takes, or say
# what it cannot express.

GEO_UNDEF = -2000
_POS_NAMES = {1: "start", 2: "end", 3: "center"}
_SPEC_TYPES = {v: k for k, v in CONSTRAINT_TYPE_MAP.items()}


def sketch_signature(
    geometry: list[dict[str, Any]],
    construction: list[bool],
    constraints: list[dict[str, Any]],
    external: list[Any],
) -> str:
    """A short fingerprint of a sketch's STRUCTURE (not its dimensions).

    Geometry kinds, construction flags, every constraint's type and the
    elements it references, and the external references: what a structural
    edit changes and a value edit or a solve does not.
    """
    import hashlib

    shape = [
        [g.get("type") for g in geometry],
        [bool(x) for x in construction],
        [
            [
                c.get("type"),
                c.get("first"),
                c.get("first_pos"),
                c.get("second"),
                c.get("second_pos"),
                c.get("third"),
                c.get("third_pos"),
                bool(c.get("driving", True)),
                bool(c.get("active", True)),
            ]
            for c in constraints
        ],
        [list(e) for e in external],
    ]
    return hashlib.sha1(json.dumps(shape).encode("utf-8")).hexdigest()[:16]


def _r(value: float) -> int | float:
    return _num(round(float(value), 6))


def _pt(p: Any) -> list[int | float]:
    return [_r(p[0]), _r(p[1])]


def _spec_geometry(g: dict[str, Any]) -> dict[str, Any] | None:
    kind = g.get("type")
    if kind == "LineSegment":
        return {"type": "line", "from": _pt(g["start"]), "to": _pt(g["end"])}
    if kind == "Circle":
        return {"type": "circle", "center": _pt(g["center"]), "radius": _r(g["radius"])}
    if kind == "ArcOfCircle" and float(g.get("axis_z", 1)) > 0:
        return {
            "type": "arc",
            "center": _pt(g["center"]),
            "radius": _r(g["radius"]),
            "start_angle": _r(math.degrees(g["first"])),
            "end_angle": _r(math.degrees(g["last"])),
        }
    if kind == "Point":
        return {"type": "point", "at": _pt(g["at"])}
    return None


def _point_item(geo: int, pos: int) -> list[Any] | None:
    if geo == -1:
        return [-1, "start"]
    name = _POS_NAMES.get(int(pos))
    return [geo, name] if name else None


def _spec_items(c: dict[str, Any]) -> list[Any] | None:
    """The builder's ``items`` for a live constraint, or None when it has none."""
    ctype = c.get("type")
    f, fp = int(c.get("first", GEO_UNDEF)), int(c.get("first_pos", 0))
    s, sp = int(c.get("second", GEO_UNDEF)), int(c.get("second_pos", 0))
    t, tp = int(c.get("third", GEO_UNDEF)), int(c.get("third_pos", 0))
    p1 = _point_item(f, fp) if fp else None
    p2 = _point_item(s, sp) if sp else None
    if ctype == "Coincident":
        return [p1, p2] if p1 and p2 else None
    if ctype in ("Horizontal", "Vertical"):
        return [f] if s == GEO_UNDEF and not fp else None
    if ctype in ("Tangent", "Perpendicular"):
        if t != GEO_UNDEF:
            return None
        if not fp and not sp:
            return [f, s]
        return [p1, p2] if p1 and p2 else None
    if ctype in ("Parallel", "Equal"):
        return [f, s] if not fp and not sp else None
    if ctype == "Symmetric":
        if not (p1 and p2):
            return None
        if tp:
            p3 = _point_item(t, tp)
            return [p1, p2, p3] if p3 else None
        return [p1, p2, t]
    if ctype == "Distance":
        if s == GEO_UNDEF:
            return [f] if not fp else None
        if p1 and p2:
            return [p1, p2]
        if p1 and not sp:
            return [p1, s]
        return None
    if ctype in ("DistanceX", "DistanceY"):
        if s == GEO_UNDEF:
            return [p1] if p1 else ([f] if not fp else None)
        return [p1, p2] if p1 and p2 else None
    if ctype == "Radius":
        return [f] if not fp else None
    if ctype == "Angle":
        if t != GEO_UNDEF or fp or sp:
            return None
        return [f] if s == GEO_UNDEF else [f, s]
    return None


def sketch_spec_from_live(
    geometry: list[dict[str, Any]],
    construction: list[bool],
    constraints: list[dict[str, Any]],
    exprs: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """The live sketch as the builder's spec, and what the spec cannot hold.

    Returns ``({"geometry", "constraints", "construction"}, unsupported)``.
    ``unsupported`` names each element with no spec form (a point-on-object
    constraint, a reference dimension, an ellipse, ...); when it is non-empty
    the spec is incomplete and the caller keeps the sketch some other way.
    Dimensions keep their expression bindings and names.
    """
    unsupported: list[str] = []
    spec_geo: list[dict[str, Any]] = []
    for i, g in enumerate(geometry):
        item = _spec_geometry(g)
        if item is None:
            unsupported.append(f"geometry {i} ({g.get('type')})")
            continue
        spec_geo.append(item)
    spec_cons: list[dict[str, Any]] = []
    for i, c in enumerate(constraints):
        ctype = _SPEC_TYPES.get(str(c.get("type")))
        items = _spec_items(c) if ctype else None
        if (
            ctype is None
            or items is None
            or not c.get("driving", True)
            or not c.get("active", True)
        ):
            label = c.get("name") or f"#{i}"
            unsupported.append(f"constraint {label} ({c.get('type')})")
            continue
        entry: dict[str, Any] = {"type": ctype, "items": items}
        if ctype in DIMENSIONAL_CONSTRAINTS:
            expr = constraint_expression(exprs, i, str(c.get("name") or ""))
            value = map_constraint_value(ctype, None, c.get("value"), expr)
            entry["value"] = None if value is UNREADABLE else value
        if c.get("name"):
            entry["name"] = str(c["name"])
        spec_cons.append(entry)
    spec = {
        "geometry": spec_geo,
        "constraints": spec_cons,
        "construction": [i for i, flag in enumerate(construction) if flag],
    }
    return spec, unsupported


def describe_sketch_edit(old: dict[str, Any], new: dict[str, Any]) -> str:
    """ "geometry 2 -> 3, constraints 4 -> 5 (+UserHeight)" for a change notice."""
    bits = []
    for key in ("geometry", "constraints"):
        before, after = len(old.get(key) or []), len(new.get(key) or [])
        if before != after:
            bits.append(f"{key} {before} -> {after}")
    old_names = {c.get("name") for c in old.get("constraints") or [] if c.get("name")}
    new_names = {c.get("name") for c in new.get("constraints") or [] if c.get("name")}
    names = [f"+{n}" for n in sorted(new_names - old_names)]
    names += [f"-{n}" for n in sorted(old_names - new_names)]
    if (old.get("construction") or []) != (new.get("construction") or []):
        bits.append("construction lines changed")
    text = ", ".join(bits) or "constraints rearranged"
    return text + (f" ({', '.join(names)})" if names else "")


# --- what a rollback will do (the dry run) -----------------------------------------


def _touched_objects(rec: StepRecord) -> set[str]:
    """Objects a step changed without creating them, as far as its params say."""
    params = rec.params or {}
    if rec.operation not in ("edit_object", "move", "color", "batch", "set_anchors"):
        return set()
    names = {str(params.get("moved_object") or params.get("obj_name") or "")}
    for sub in params.get("ops") or []:
        if isinstance(sub, dict) and sub_operation(sub) in ("edit_object", "move", "color"):
            names.add(str(sub.get("moved_object") or sub.get("obj_name") or ""))
    return {n for n in names if n}


# Ops whose whole effect is the objects they create: undoing one is removing
# those objects (the engine re-points a Body's Tip afterwards).
_CREATING_OPS = frozenset(
    {
        "create_object",
        "sketch",
        "pad",
        "pocket",
        "revolution",
        "groove",
        "fillet",
        "chamfer",
        "thickness",
        "draft",
        "loft",
        "sweep",
        "boolean",
        "mirror",
        "pattern",
        "datum_plane",
        "hull",
    }
)


def _only_creates(rec: StepRecord, created: set[str]) -> bool:
    params = rec.params or {}
    if rec.operation == "variables":
        return str(params.get("obj_name") or "") in created
    if rec.operation == "batch":
        return all(
            isinstance(sub, dict) and sub_operation(sub) in _CREATING_OPS
            for sub in params.get("ops") or []
        )
    return rec.operation in _CREATING_OPS


def plan_restore(
    records: list[StepRecord],
    to_index: int,
    span: int,
    present: list[str],
    stale: set[int] | frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """How ``rollback_to(to_index)`` puts the model back, decided before acting.

    ``span`` is how many of the journal's newest transactions the undo stack
    can pop (``undo_trust_span``), ``stale`` the steps the user already took
    out. Native undo pops ``span``; whatever it cannot reach is fixed from the
    FIRST step that is actually wrong, never from step 1:

    * objects created by steps after the target that the undo cannot reach
      are removed directly (``leftover``);
    * a journal object the target state needs that is gone (deleted by the
      user, or kept away by a step the undo cannot reach), an object such a
      step edited, and a stale step at or before the target make its creating
      step the first wrong one: from there the journal rebuilds (``remove``
      what those steps built, re-run them up to the target).

    ``mode``: ``native`` (the undo is enough), ``removed`` (the steps the
    undo cannot reach only created objects, and removing them is exact),
    ``rebuild``, or ``partial`` when what is left cannot be restored exactly
    (a step that edited objects in ways the journal cannot see, or a step in
    the rebuild range that cannot be re-run).
    """
    live = set(present)
    after = [
        r for r in records if r.state == STATE_DONE and r.index > to_index and r.index not in stale
    ]
    with_tx = [r for r in after if r.transaction]
    popped = {r.index for r in with_tx[len(with_tx) - span :]} if span else set()
    unreached = [r for r in after if r.index not in popped and (r.transaction or r.mutated)]
    diffs = {rec.index: (created, removed) for rec, created, removed in _record_diffs(records)}
    built = set(created_since(records, 0))
    leftover = sorted({n for r in unreached for n in diffs.get(r.index, (set(), set()))[0]} & live)
    expected = set(objects_after_index(records, to_index)) & built
    # Objects steps after the target removed come back only with their undo.
    restored_by_undo = {n for i in popped for n in diffs.get(i, (set(), set()))[1]}
    missing = sorted(n for n in expected if n not in live and n not in restored_by_undo)
    touched = sorted({n for r in unreached for n in _touched_objects(r)} & built - set(leftover))
    wrong: list[int] = []
    reasons: list[str] = []
    for name in missing + touched:
        owner = creator_of(records, name)
        if owner is not None and owner.index <= to_index:
            wrong.append(owner.index)
    wrong += [i for i in stale if i <= to_index]
    if unreached:
        reasons.append(f"the undo stack cannot reach step(s) {[r.index for r in unreached]}")
    if missing:
        reasons.append(f"object(s) {missing} that step {to_index} should have are gone")
    if touched:
        reasons.append(f"object(s) {touched} were edited by step(s) the undo cannot reach")
    rebuild_from = min(wrong) if wrong else None
    rerun = [
        r.index
        for r in records
        if rebuild_from is not None
        and rebuild_from <= r.index <= to_index
        and r.state in (STATE_DONE, STATE_FAILED)
    ]
    blocked = unrecoverable_steps(records, to_index)
    blocked = [i for i in blocked if rebuild_from is not None and i >= rebuild_from]
    if rebuild_from is None:
        exact = all(_only_creates(r, diffs.get(r.index, (set(), set()))[0]) for r in unreached)
        mode = "native" if not unreached else ("removed" if exact else "partial")
        remove = leftover
    elif blocked:
        mode = "partial"
        remove = leftover
        reasons.append(f"step(s) {blocked} cannot be re-run, so the model cannot be rebuilt")
    else:
        mode = "rebuild"
        remove = sorted((set(created_since(records, rebuild_from - 1)) & live) | set(leftover))
    return {
        "mode": mode,
        "undo": span,
        "rebuild_from": rebuild_from if mode == "rebuild" else None,
        "rerun": rerun if mode == "rebuild" else [],
        "remove": remove,
        "undone_objects": sorted(
            {n for i in popped for n in diffs.get(i, (set(), set()))[0]} & live
        ),
        "leftover": leftover,
        "missing": missing,
        "unreached": [r.index for r in unreached],
        "reasons": reasons,
    }


def restore_summary(plan: dict[str, Any], user_dependents: list[str]) -> str:
    """The dry run in words, for the panel's confirm dialog and the reply."""
    lines = []
    if plan.get("undo"):
        lines.append(f"Undo {plan['undo']} transaction(s) natively.")
    if plan["mode"] == "rebuild":
        steps = plan["rerun"]
        span = f"{steps[0]}..{steps[-1]}" if len(steps) > 1 else str(steps[0]) if steps else "-"
        lines.append(
            f"Then rebuild from step {plan['rebuild_from']}: remove "
            f"{len(plan['remove'])} object(s) and re-run step(s) {span}."
        )
    elif plan["remove"]:
        lines.append(
            f"Then remove {len(plan['remove'])} object(s) directly: {', '.join(plan['remove'])}."
        )
    if plan["mode"] == "partial":
        lines.append("Property changes those steps made cannot be restored.")
    if user_dependents:
        lines.append(
            f"Your own object(s) {', '.join(user_dependents)} depend on what goes away and "
            "would break."
        )
    if not lines:
        lines.append("Nothing to undo.")
    return " ".join(lines)
