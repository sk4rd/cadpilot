"""FreeCAD-facing half of the step journal.

Owns the document property, per-step execution, and undo. Everything here
runs on the GUI thread.

``apply_op`` is the single entry point, shared by the RPC handler
(``FreeCADRPC.journal_op``) and the GUI panel — so the panel keeps working
with the RPC server stopped.

Journal writes happen INSIDE each step's FreeCAD transaction, so a step's log
entry and the model change it describes are committed together and cannot
diverge on failure.

They are NOT, however, restored by ``doc.undo()``: FreeCAD tracks undo for
document OBJECTS, and a document-level dynamic property like this journal is
not part of that. Verified on 1.1.4 — after ``doc.undo()`` the created object
was gone while the journal still reported the step as ``done``. Rollback and
re-execution therefore reconcile the log explicitly (``sj.rewind``); that
reconciliation is the primary mechanism, not a safety net. The ``drift`` flag
in :func:`_status` surfaces the case where someone undid work by hand.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from typing import Any

import FreeCAD

from rpc_server import dbglog
from rpc_server import step_journal as sj
from rpc_server.feature_ops import FEATURE_TYPES, create_feature_gui
from rpc_server.object_factory import (
    create_object_gui,
    delete_object_gui,
    edit_object_gui,
    repair_body_tips,
)
from rpc_server.property_mapper import Object

logger = dbglog.get_logger("journal")

EXECUTABLE_OPS = (
    {"create_object", "edit_object", "delete_object", "batch"}
    | set(FEATURE_TYPES)
    | {
        # The assembly toolchain used to journal with params={} and
        # executable=False: rollback undid them (they own transactions), but a
        # replay/rebuild SKIPPED them and the model came back unassembled, and
        # their unrecoverable flag degraded every rebuild across them to
        # "partial". They carry their full payload now and re-run for real.
        "assemble",
        "align_shapes",
        "set_anchors",
        "assembly",
    }
)


# --- document property -------------------------------------------------------


def read_journal(doc) -> list[sj.StepRecord]:
    try:
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
    except Exception:
        text = ""
    return sj.from_json(text)


def read_meta(doc) -> dict[str, Any]:
    try:
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
    except Exception:
        text = ""
    return sj.meta_from_json(text)


def write_journal(doc, records: list[sj.StepRecord], meta: dict[str, Any] | None = None) -> None:
    sj.reindex(records)
    if sj.JOURNAL_PROP not in list(doc.PropertiesList):
        doc.addProperty("App::PropertyString", sj.JOURNAL_PROP, "CADPilot", "CADPilot step journal")
    if meta is None:
        meta = read_meta(doc)
    setattr(doc, sj.JOURNAL_PROP, sj.to_json(records, meta))


# --- small helpers -----------------------------------------------------------


def _undo_names(doc) -> list[str]:
    try:
        return list(getattr(doc, "UndoNames", []) or [])
    except Exception:
        return []


def _redo_names(doc) -> list[str]:
    try:
        return list(getattr(doc, "RedoNames", []) or [])
    except Exception:
        return []


def _is_ghost_entry(name) -> bool:
    """A transaction belonging to ANOTHER document, parked on this stack.

    FreeCAD 1.1.4 attributes an undo entry to the document that is ACTIVE when
    the transaction is recorded, not to the one it was opened on: committing on
    a non-active document leaves a "-> <name>" entry on the active document's
    stack (live-verified with plain FreeCAD, no CADPilot involved). A ghost is
    not a step of this document; popping it via ``doc.undo()`` is a no-op here,
    so it must never consume one of a rollback's ``n`` slots.
    """
    return str(name).startswith("->")


def _real_undo_names(doc) -> list[str]:
    """Undo-stack names minus other documents' ghosts (see _is_ghost_entry)."""
    return [n for n in _undo_names(doc) if not _is_ghost_entry(n)]


def _undo_cap() -> int:
    """FreeCAD's ``MaxUndoSize``: how many transactions the undo stack keeps.

    Once the stack is full every commit evicts the oldest entry, so a journal
    longer than the cap can never be undone natively back to its start.
    """
    with contextlib.suppress(Exception):
        return int(
            FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Document").GetInt(
                "MaxUndoSize", 20
            )
        )
    return 20


def _undo_trust(
    doc,
    records: list[sj.StepRecord],
    upto_index: int,
    undo_count: int,
    skip: set[int] | frozenset[int] = frozenset(),
) -> tuple[int, str]:
    """How much of this span the undo stack can revert, and why it falls short.

    The stack is shared with the GUI and does not survive a reopen, while manual
    edits interleave on it in every session, so its own names are the check: the
    entries about to be popped must be exactly the journal's transactions for
    these steps, most recent first (UndoNames[0] is the next entry undo() would
    take). Same-named entries are indistinguishable — every execute_code step
    commits as "CADPilot: execute_code" — and a foreign name in the way stops the
    matching prefix, because popping past it would undo the WRONG transaction.

    The answer is a SPAN, not a yes/no. FreeCAD caps the undo stack
    (``MaxUndoSize``, 20 by default), so a journal longer than the cap has its
    oldest entries evicted: the stack can never hold the whole span again, and
    judging it "does not hold the journal's transactions" (the reopened-document
    verdict) popped nothing and forced a full rebuild on every deep rollback
    while every entry it DID hold matched, in order. ``why`` is empty when the
    whole span is covered, and otherwise names the real cause instead of
    guessing between a reopen and an off-journal edit.
    """
    names = _real_undo_names(doc)
    span = sj.undo_trust_span(names, records, upto_index, undo_count, skip)
    if span >= undo_count:
        return span, ""
    if span == 0:
        why = (
            "the undo stack holds none of the journal's transactions for these steps "
            "(the document was reopened, or edited outside the journal)"
        )
    elif len(names) < undo_count:
        why = (
            f"the undo stack holds only {span} of the {undo_count} transactions this range "
            f"needs: FreeCAD keeps just the last {_undo_cap()} undo entries, so the oldest "
            "step(s) can no longer be undone natively"
        )
    else:
        why = (
            f"the undo stack holds only {span} of the {undo_count} transactions this range "
            "needs: a foreign (off-journal) transaction sits in the way"
        )
    return span, why


def _object_names(doc) -> list[str]:
    try:
        return sorted(o.Name for o in doc.Objects)
    except Exception:
        return []


def stale_steps(doc, records: list[sj.StepRecord] | None = None) -> list[dict[str, Any]]:
    """Done steps whose work is gone from the model (see sj.stale_steps)."""
    return sj.stale_steps(
        read_journal(doc) if records is None else records,
        _real_undo_names(doc),
        _real_redo_names(doc),
        _object_names(doc),
    )


def _stale_refusal(stale: list[dict[str, Any]], upto: int, verb: str) -> str:
    """Why ``verb`` must not silently re-create steps the user took out, or ""."""
    hit = [s for s in stale if s["index"] <= upto]
    if not hit:
        return ""
    what = "; ".join(f"step {s['index']}: {s['reason']}" for s in hit)
    first = hit[0]["index"]
    return (
        f"{verb} would re-create work the user took out of the model ({what}). Accept the "
        f"user's change with drop (step_control drop {first}), or pass force=true to "
        "re-create it"
    )


def _transaction_name(rec: sj.StepRecord) -> str:
    name = rec.params.get("obj_name") or ""
    return f"CADPilot: {rec.operation} {name}".strip()


# Per-document sequence so every undo entry gets a distinct name. Keyed by
# document NAME and never reset: a closed-and-reopened document simply keeps
# counting, which is harmless (names only need to be unique while they are on a
# stack, and a reopen throws the stack away).
_TX_SEQ: dict[str, int] = {}

# Every CADPilot transaction starts with this; "which entries are ours" is how
# a caller with its own log (the MCP session) refuses to eat a manual edit.
TX_PREFIX = "CADPilot: "


def transaction_name(doc, label: str) -> str:
    """Unique FreeCAD undo-entry name: ``CADPilot: #<seq> <label>``.

    Uniqueness is load-bearing, not cosmetics. The journal verifies a rollback
    by comparing the undo stack's names against its own records, so two steps
    sharing a name are indistinguishable — and before the sequence number every
    ``edit_object Box`` committed as the identical ``"CADPilot: edit_object
    Box"``. A manual Ctrl+Z of the newest step then left an older same-named
    entry on the stack, the drift check (a membership test) still answered
    "present", and a rollback popped the WRONG transaction while every
    object-set verification stayed silent, because only properties had changed.
    A record stores the name at commit time and never recomputes it, so
    journals written before this (and their stacks) keep matching themselves.
    """
    key = str(getattr(doc, "Name", "") or "")
    seq = _TX_SEQ.get(key, 0) + 1
    _TX_SEQ[key] = seq
    rest = str(label or "op")
    if rest.startswith(TX_PREFIX):
        rest = rest[len(TX_PREFIX) :]
    return f"{TX_PREFIX}#{seq} {rest}"


def _real_redo_names(doc) -> list[str]:
    """Redo-stack names minus other documents' ghosts (see _is_ghost_entry)."""
    return [n for n in _redo_names(doc) if not _is_ghost_entry(n)]


def foreign_undo_entries(doc, n: int, undo: bool = True) -> list[str]:
    """Names among the ``n`` entries an undo/redo would pop that are NOT ours.

    The journal path verifies the exact step names before popping; a caller
    keeping its own log (the MCP session) cannot, but it can still refuse to eat
    a transaction that was never one of its steps — a manual GUI edit, another
    tool's write. Without this check the session's count-based rollback popped
    the manual edit instead and reported success while every session step stayed
    applied: only properties had moved, so the object-set verification saw
    nothing (live-analyzed; same class as the journal's name check).
    """
    if n <= 0:
        return []
    names = _real_undo_names(doc) if undo else _real_redo_names(doc)
    return [str(name) for name in names[:n] if not str(name).startswith(TX_PREFIX)]


# --- engine-echo mute -----------------------------------------------------------
#
# The manual-edit observer (further down) mirrors property-panel edits into
# the journal. Undo/redo and step re-runs fire the SAME property-change
# notifications, but they write restored or already-recorded values: syncing
# those would undo a user's correction after the fact (the undo restores
# Height 6, the observer "manual-edits" the journal back to 6, the re-run
# rebuilds at 6 — the correction silently reverts). Every window where the
# ENGINE drives the document mutes the observer; a genuine GUI edit never
# happens inside one.

_ENGINE_ACTIVE = 0


class _EngineQuiet:
    """Context manager: while active, the manual-edit observer stays silent."""

    def __enter__(self) -> _EngineQuiet:
        global _ENGINE_ACTIVE
        if _ENGINE_ACTIVE == 0:
            # A user edit still waiting for its deferred read must be recorded
            # against the state the user left, not the one this window writes.
            flush_deferred_syncs()
        _ENGINE_ACTIVE += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        global _ENGINE_ACTIVE
        # CLAMPED at zero: a window left open across a hot reload still runs its
        # __exit__ against the freshly re-executed (zeroed) counter, and that
        # unbalanced decrement used to leave the counter at -1 — permanently, so
        # every later `engine_quiet()` brought it to 0 instead of 1 and the mute
        # silently STOPPED WORKING for the rest of the session. The observer then
        # synced machine writes into the owning step's params (live: after one
        # restart-from-execute_code, execute_code's own writes rewrote the batch
        # step's obj_properties — the exact bleed this window exists to prevent).
        _ENGINE_ACTIVE = max(0, _ENGINE_ACTIVE - 1)
        return False


_SKETCH_PROPS = ("Constraints", "ExpressionEngine", "Geometry")
_DEFERRED: dict[tuple[str, str], bool] = {}
_FLUSH_SCHEDULED = False


def _defer_sketch_sync(doc_name: str, obj_name: str) -> None:
    global _FLUSH_SCHEDULED
    _DEFERRED[(doc_name, obj_name)] = True
    if _FLUSH_SCHEDULED:
        return
    try:
        from PySide import QtCore
    except ImportError:
        flush_deferred_syncs()
        return
    _FLUSH_SCHEDULED = True
    QtCore.QTimer.singleShot(0, _flush_from_timer)


def _flush_from_timer() -> None:
    global _FLUSH_SCHEDULED
    _FLUSH_SCHEDULED = False
    if _ENGINE_ACTIVE > 0:
        # A nested event loop inside an engine window; the window's exit
        # leaves the edit pending and the next window (or tick) settles it.
        if _DEFERRED:
            _defer_sketch_sync(*next(iter(_DEFERRED)))
        return
    flush_deferred_syncs()


def flush_deferred_syncs() -> None:
    """Run the sketch syncs that user edits queued (see _defer_sketch_sync)."""
    if not _DEFERRED:
        return
    pending = list(_DEFERRED)
    _DEFERRED.clear()
    observer = getattr(FreeCAD, _OBSERVER_ATTR, None)
    if observer is None:
        return
    for doc_name, obj_name in pending:
        with contextlib.suppress(Exception):
            doc = FreeCAD.getDocument(doc_name)
            obj = doc.getObject(obj_name)
            if obj is not None:
                observer.sync_sketch(doc, obj)


def engine_quiet() -> _EngineQuiet:
    """Mute the manual-edit observer for machine-driven document writes.

    The RPC layer wraps every mutation in this: a tool-driven change is
    recorded as its OWN step, and letting it also bleed into an earlier step's
    params through the observer made reject/replay inconsistent (reject the
    edit step and the earlier step still carries its value). Only human edits
    — the property panel, the sketcher, FreeCAD's Python console — may sync.
    """
    return _EngineQuiet()


@contextlib.contextmanager
def active_document(doc):
    """Make ``doc`` the App-active document while a transaction is recorded.

    FreeCAD 1.1.4 attributes an undo entry to the document that is ACTIVE at
    commit time, not to the one the transaction was opened on: committing on a
    non-active document leaves a ghost entry ("-> <name>") on the active
    document's stack, where a later ``undo()`` pops the ghost instead of its
    own step while this document's objects stay untouched — a rollback then
    reports success and truncates its log while the model never moved
    (live-verified, and reproduced with plain FreeCAD, no CADPilot involved).
    Activating the target for the open/commit window keeps every entry on its
    real stack; the previously active document is restored afterwards.
    """
    if doc is None:
        yield
        return
    previous = None
    with contextlib.suppress(Exception):
        active = FreeCAD.ActiveDocument
        previous = active.Name if active is not None else None
    if previous != doc.Name:
        with contextlib.suppress(Exception):
            FreeCAD.setActiveDocument(doc.Name)
    try:
        yield
    finally:
        if previous is not None and previous != doc.Name:
            with contextlib.suppress(Exception):
                FreeCAD.setActiveDocument(previous)


def undo_n(doc, n: int) -> dict[str, Any]:
    """Undo up to ``n`` transactions; report how many actually went.

    Never call undo() blind past the end of the stack (the raised exceptions
    are noise), and stop at the first failure so the reported count matches
    reality — both the session log and the journal are truncated by it.
    """
    return _stack_op(doc, n, undo=True)


def redo_n(doc, n: int) -> dict[str, Any]:
    return _stack_op(doc, n, undo=False)


def _stack_op(doc, n: int, undo: bool) -> dict[str, Any]:
    """Undo/redo up to ``n`` of THIS document's transactions.

    Ghost entries (``-> name``: another document's transaction that was
    committed while this one was active) are popped out of the way but do NOT
    consume a slot — they are no-ops here, and counting them is exactly how a
    rollback reported "undone N" while its own steps were still applied.
    """
    stack = [str(x) for x in (getattr(doc, "UndoNames" if undo else "RedoNames", []) or [])]
    if n <= 0 or not stack:
        return {"success": True, "count": 0, "ghosts_skipped": 0, "objects": _object_names(doc)}
    with _EngineQuiet():
        done = 0
        ghosts = 0
        idx = 0
        while done < n and idx < len(stack):
            ghost = _is_ghost_entry(stack[idx])
            idx += 1
            try:
                doc.undo() if undo else doc.redo()
            except Exception as e:
                FreeCAD.Console.PrintWarning(
                    f"CADPilot: {'undo' if undo else 'redo'} stopped: {e}\n"
                )
                break
            if ghost:
                ghosts += 1
            else:
                done += 1
        with contextlib.suppress(Exception):
            doc.recompute()
    if ghosts:
        dbglog.get_logger("tx").info(
            "stack op skipped %d ghost entry(ies) owned by other documents", ghosts
        )
    return {
        "success": True,
        "count": done,
        "ghosts_skipped": ghosts,
        "objects": _object_names(doc),
    }


def commit_transaction(doc) -> None:
    """Commit, and close FreeCAD's application-wide active transaction too.

    A transaction that recorded no change commits nothing and leaves its name
    active, so the user's next change made without a transaction of its own
    (a console edit, a script) was filed under CADPilot's name and left open.
    """
    doc.commitTransaction()
    with contextlib.suppress(Exception):
        active = FreeCAD.getActiveTransaction()
        # Only our own: the user may be inside a sketch edit in another
        # document, whose transaction must not be committed under them.
        if active and str(active[0]).startswith(TX_PREFIX):
            FreeCAD.closeActiveTransaction(False, int(active[1]))


# --- recording ---------------------------------------------------------------


def record_commit(
    doc,
    *,
    operation: str,
    label: str,
    params: dict[str, Any] | None = None,
    transaction: str = "",
    atomic: bool = True,
    executable: bool | None = None,
    objects_before: list[str] | None = None,
    objects_after: list[str] | None = None,
) -> sj.StepRecord | None:
    """Append a completed step.

    MUST be called inside the step's transaction, so the log entry commits with
    the model change (an abort must leave neither). Undo will not take it back
    out again — see the module docstring.
    """
    try:
        records = read_journal(doc)
        # A new commit invalidates the not-yet-executed plan: it was authored
        # against a document state that no longer exists. The plan is not
        # necessarily a trailing run (inspections append behind it, a snapshot
        # marker may sit there too), so the removal keys on STATE, not
        # position — slicing from planned_tail_start would remove nothing and
        # a stale plan would survive the commit.
        sj.drop_planned(records)
        params = dict(params or {})
        if operation == "sketch" or operation in sj.SKETCH_EDIT_OPS:
            _note_sketch_signature(doc, params, objects_before or [], objects_after or [])
        rec = sj.StepRecord(
            index=len(records) + 1,
            state=sj.STATE_DONE,
            operation=operation,
            label=label or operation,
            params=params,
            transaction=transaction,
            atomic=atomic,
            executable=operation in EXECUTABLE_OPS if executable is None else executable,
            objects_before=list(objects_before or []),
            objects_after=list(objects_after or []),
            timestamp=sj.stamp(),
        )
        records.append(rec)
        write_journal(doc, records)
        return rec
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: step journal write failed: {e}\n")
        return None


def object_names(doc) -> list[str]:
    """Public fingerprint of the document's object set (sorted names)."""
    return _object_names(doc)


def undo_token(doc) -> tuple:
    """FreeCAD's "did the last commit land" signal, without the undo-cap blind spot.

    ``UndoCount`` alone is NOT that signal: FreeCAD caps the undo stack (the
    ``MaxUndoSize`` preference — 20 by default), so once the stack is full a
    REAL commit leaves the count pinned. Using the count as the probe made the
    journal classify every mutation after the ~20th as read-only (no
    transaction, not re-executable), and rollback/replay then skipped real
    steps. Verified live on 1.1.4: at ``UndoCount == 20`` a real commit still
    moves ``UndoNames`` (oldest evicted, newest appended) while an EMPTY commit
    moves nothing — so the pair is decisive where the count alone is not.
    """
    return (int(getattr(doc, "UndoCount", 0)), tuple(getattr(doc, "UndoNames", None) or ()))


def document_tokens() -> dict[str, tuple]:
    """{document name: change token} for every open document.

    ``execute_code`` needs this: the snippet is free to switch documents (and
    to close one), so "which document did it change?" can only be answered by
    comparing every document before and after the run.
    """
    tokens: dict[str, tuple] = {}
    for doc in FreeCAD.listDocuments().values():
        with contextlib.suppress(Exception):
            tokens[doc.Name] = (*undo_token(doc), tuple(_object_names(doc)))
    return tokens


def changed_documents(before: dict[str, tuple]) -> list[str]:
    """Names of the documents whose token moved since ``before`` (sorted).

    A document that disappeared is not listed (there is nothing to reconcile);
    one that appeared counts as changed.
    """
    after = document_tokens()
    return sorted(name for name, token in after.items() if token != before.get(name))


def append_execute_code(
    doc,
    *,
    code: str,
    changed: bool,
    objects_before: list[str] | None = None,
    transaction: str = "",
) -> None:
    """Record an execute_code step.

    ``changed`` says whether the snippet produced an undo entry, i.e. whether it
    mutated the document inside the wrapper transaction the RPC handler opened
    around it:

    * changed -> ATOMIC and replayable. The code is stored in ``params`` so
      ``reexecute``/``replay`` can re-run it, and rollback treats it like any
      other transaction-bearing step.
    * unchanged -> a read-only inspection. Non-atomic (rollback must not stop at
      it) and non-executable (replay must not re-run it).
    """
    try:
        records = read_journal(doc)
        after = _object_names(doc)
        # A read-only execute_code — inspecting the model, the common case —
        # must NOT destroy a pending plan. The snippet's own undo entry is the
        # test: comparing object sets against the journal's last snapshot also
        # counted every object the USER had added since, and dropped the plan.
        if changed:
            sj.drop_planned(records)
        # The inspection is APPENDED at the very end, even behind a pending
        # plan. Inserting it before the tail shifted every planned step's
        # index, so an MCP client that read "pocket = step 11" and then called
        # reexecute(11) hit the inspection instead (live: JTest). A done
        # record behind the plan is harmless — the plan cursor walks by STATE
        # and run_to's upto check is cursor-based — while stable indices are
        # what index-addressed verbs (run_to/reexecute/rollback_to) run on.
        records.append(
            sj.StepRecord(
                index=len(records) + 1,
                state=sj.STATE_DONE,
                operation="execute_code",
                # One label, wherever it is read (panel row, status, the reply's
                # journal snapshot): the snippet's leading comment when it wrote
                # one, then what it DID. See sj.execute_code_label.
                label=sj.execute_code_label(code, changed, list(objects_before or []), after),
                # The code is ALWAYS kept, read-only or not: the panel shows it
                # as the step's detail, and without it a row is an opaque "{}".
                params={"code": code},
                transaction=(transaction or "CADPilot: execute_code") if changed else "",
                atomic=changed,
                mutated=changed,
                executable=changed,
                objects_before=list(objects_before or []),
                objects_after=after,
                timestamp=sj.stamp(),
            )
        )
        write_journal(doc, records)  # reindexes: list position == step number
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: step journal write failed: {e}\n")


# --- executing a record ------------------------------------------------------


def downgrade_if_no_undo(doc, rec: sj.StepRecord | None, token_before) -> bool:
    """Reconcile a just-committed step with the undo stack.

    An empty commit adds no undo entry — the op changed nothing (a read-only
    assembly ``verify``, an edit that set the values the object already had).
    A record that claims a transaction the stack does not have makes
    ``plan_rollback`` pop an EARLIER step's transaction, so the record is
    downgraded to read-only: no transaction, no mutation, no re-execution.

    ``token_before`` is :func:`undo_token` taken before the op; comparing it to
    the post-commit token is what makes this work at FreeCAD's undo cap, where
    the plain count stops moving (see :func:`undo_token`).
    """
    if rec is None:
        return False
    produced = True
    with contextlib.suppress(Exception):
        produced = undo_token(doc) != token_before
    if produced:
        return False
    records = read_journal(doc)
    target = next((r for r in records if r.index == rec.index), None)
    if target is None:
        return False
    target.atomic = False
    target.mutated = False
    target.executable = False
    target.transaction = ""
    with contextlib.suppress(Exception):
        write_journal(doc, records)
    logger.info(
        "step %d (%s): commit produced no undo entry, marked read-only",
        target.index,
        target.operation,
    )
    return True


def _normalize(res) -> dict[str, Any]:
    if res is True:
        return {"success": True}
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


def _replay_ready_code(doc, code: str) -> str:
    """Point a recorded snippet's document references at this document.

    The journal rides ON the document, but its snippets capture the document
    NAME they were written against. Save the file under a new name (or move it)
    and reopening hands FreeCAD a document named after the FILE — every re-run
    then dies with "Unknown document" before touching a shape, which silently
    broke rollback's rebuild path (live: DeskFan.FCStd, ex-MideaDeskFan, where
    every rebuild step failed on the rename). A reference that no longer
    resolves to an open document is rewritten to the host; one that DOES
    resolve is left alone, because the snippet may genuinely mean that other
    document.
    """
    if "getDocument" not in code:
        return code
    open_names = set(FreeCAD.listDocuments())
    stale = {
        name: doc.Name
        for name in sj.document_refs(code)
        if name != doc.Name and name not in open_names
    }
    if not stale:
        return code
    logger.warning(
        "journal replay: getDocument(%s) no longer resolves; rewrote to %r",
        ", ".join(repr(n) for n in sorted(stale)),
        doc.Name,
    )
    return sj.rewrite_document_refs(code, stale)


def _execute_one(doc, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one non-batch operation against ``doc`` (no transaction, no journal)."""
    props = params.get("obj_properties") or {}

    if operation == "create_object":
        if not params.get("obj_type"):
            return {"success": False, "error": "missing obj_type"}
        return _normalize(
            create_object_gui(
                doc.Name,
                Object(
                    name=params.get("obj_name") or "New_Object",
                    type=params["obj_type"],
                    properties=props,
                ),
            )
        )
    if operation == "edit_object":
        return _normalize(
            edit_object_gui(doc.Name, Object(name=params.get("obj_name") or "", properties=props))
        )
    if operation == "delete_object":
        return _normalize(delete_object_gui(doc.Name, params.get("obj_name") or ""))
    if operation in FEATURE_TYPES:
        spec = {"type": operation, "base": params.get("obj_name"), **props}
        try:
            feat = create_feature_gui(doc, spec)
            if params.get("live_sketch"):
                target = doc.getObject(str(params.get("obj_name") or ""))
                if getattr(feat, "TypeId", "") == "Sketcher::SketchObject":
                    target = feat
                _restore_live_sketch(doc, target, params["live_sketch"])
            return {"success": True, "object_name": feat.Name}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if operation == "set_anchors":
        from rpc_server.assembly_ops import set_anchors

        return _normalize(
            set_anchors(
                doc.Name,
                str(params.get("obj_name") or ""),
                params.get("anchors") or {},
                bool(params.get("replace", False)),
                str(params.get("coord_frame") or "local"),
            )
        )
    if operation == "align_shapes":
        from rpc_server.geometry_query import align_shapes

        return _normalize(
            align_shapes(
                doc.Name,
                params.get("obj_name"),
                params.get("element"),
                params.get("element_index"),
                params.get("target_obj_name"),
                params.get("target_element"),
                params.get("target_element_index"),
                params.get("mode", "touch"),
                params.get("offset", 0.0),
            )
        )
    if operation == "assemble":
        from rpc_server.assembly_ops import assemble

        # Strict on re-run: the original call may have committed a PARTIAL
        # mate set (commit_if=passed>0), but replay must reproduce all of it —
        # a silently half-assembled rebuild is worse than a failed step.
        res = assemble(
            doc.Name,
            params.get("mates") or [],
            float(params.get("tolerance", 0.1)),
            bool(params.get("stop_on_error", True)),
        )
        if isinstance(res, dict) and res.get("success"):
            return {"success": True}
        return {
            "success": False,
            "error": (res.get("error") if isinstance(res, dict) else str(res)) or "assemble failed",
        }
    if operation == "assembly":
        spec = params.get("spec") or {}
        if not spec.get("operation"):
            return {"success": False, "error": "assembly step has no recorded spec"}
        from rpc_server.joint_ops import assembly_op

        try:
            res = assembly_op(doc, spec)
            return {
                "success": True,
                "object_name": str(
                    res.get("joint") or res.get("link") or res.get("assembly") or ""
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if operation == "execute_code":
        # Re-run a recorded snippet (reexecute/replay). It runs inside the step's
        # own transaction (opened by run_record) and through the SAME executor
        # the RPC handler uses, so it sees the namespace it was written against.
        code = str(params.get("code") or "")
        if not code:
            return {"success": False, "error": "execute_code step has no recorded code"}
        code = _replay_ready_code(doc, code)
        from rpc_server import rpc_server as _rs

        try:
            _rs.exec_snippet(code)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    return {"success": False, "error": f"operation '{operation}' is not re-executable"}


def execute_record(doc, rec: sj.StepRecord) -> dict[str, Any]:
    """Run one record's operation against ``doc`` (no transaction, no journal)."""
    if rec.operation == "batch":
        ops = (rec.params or {}).get("ops") or []
        if not ops:
            return {"success": False, "error": "batch step has no ops"}
        for i, sub in enumerate(ops, start=1):
            op_name = sj.sub_operation(sub)
            res = _execute_one(doc, op_name, sj.params_for(sub))
            if not res.get("success"):
                return {
                    "success": False,
                    "error": f"batch op {i} ({op_name}): {res.get('error')}",
                }
        return {"success": True}
    return _execute_one(doc, rec.operation, rec.params or {})


def run_record(doc, records: list[sj.StepRecord], rec: sj.StepRecord) -> dict[str, Any]:
    """Execute ``rec`` in its own transaction, journal written inside it."""
    with active_document(doc):
        return _run_record_locked(doc, records, rec)


def _run_record_locked(doc, records: list[sj.StepRecord], rec: sj.StepRecord) -> dict[str, Any]:
    tx = transaction_name(doc, _transaction_name(rec))
    before = _object_names(doc)
    token_before = None
    with contextlib.suppress(Exception):
        token_before = undo_token(doc)
    doc.openTransaction(tx)
    logger.info("open transaction %r (%d objects)", tx, len(before))
    started = time.monotonic()
    with _EngineQuiet():
        try:
            res = execute_record(doc, rec)
        except Exception as e:
            res = {"success": False, "error": f"{type(e).__name__}: {e}"}
    rec.duration_ms = int((time.monotonic() - started) * 1000)

    if not res.get("success"):
        # abortTransaction restores the pre-step state: an undo in disguise,
        # and restoring writes the OLD values that the observer must not
        # mirror back over (possibly synced) params.
        with _EngineQuiet(), contextlib.suppress(Exception):
            doc.abortTransaction()
        rec.state = sj.STATE_FAILED
        rec.error = str(res.get("error") or "unknown error")
        # The abort destroyed the transaction, so the record owns no undo entry
        # any more. Keeping the OLD name (a re-run of a step that previously
        # succeeded) put a name in the journal that is not on the stack, which
        # broke the rollback trust check at that position and turned a healthy
        # rollback into a full rebuild.
        rec.transaction = ""
        logger.warning("aborted transaction %r: %s", tx, rec.error)
        # The transaction is gone, so this write is deliberately outside one.
        with contextlib.suppress(Exception):
            write_journal(doc, records)
        return res

    rec.state = sj.STATE_DONE
    rec.stale = ""
    rec.transaction = tx
    rec.objects_before = before
    rec.objects_after = _object_names(doc)
    rec.error = ""
    rec.result = str(res.get("object_name") or "")[:200]
    if rec.operation == "sketch" or rec.operation in sj.SKETCH_EDIT_OPS:
        _note_sketch_signature(doc, rec.params, rec.objects_before, rec.objects_after)
    write_journal(doc, records)
    logger.info(
        "committed transaction %r (%d objects, %d journal records)",
        tx,
        len(rec.objects_after),
        len(records),
    )
    commit_transaction(doc)
    with _EngineQuiet(), contextlib.suppress(Exception):
        doc.recompute()
    # Self-correction for a re-run whose op ended up changing nothing (an
    # edited execute_code snippet, an edit that re-applied the same values):
    # the commit produced no undo entry, so the record must stop claiming one —
    # otherwise a later rollback would pop an EARLIER step's transaction off
    # the plain stack. (There is no path that upgrades in the other direction.)
    produced = True
    with contextlib.suppress(Exception):
        produced = undo_token(doc) != token_before
    if not produced and (rec.atomic or rec.transaction):
        rec.atomic = False
        rec.mutated = False
        rec.executable = False
        rec.transaction = ""
        logger.info("step %d (%s): re-run changed nothing, downgraded", rec.index, rec.operation)
        with contextlib.suppress(Exception):
            write_journal(doc, records)
    return res


# --- op dispatch -------------------------------------------------------------


def apply_op(doc, spec: dict[str, Any], deliver: bool = True) -> dict[str, Any]:
    """Single entry point for the panel and the RPC handler.

    Wraps :func:`_apply_op` so every journal operation — however it returns —
    lands in the log. The panel calls this straight from the GUI thread, so a
    button press that does nothing is otherwise invisible.

    Every mutating result carries a compact ``journal`` snapshot, so a caller
    (the MCP client especially) sees the resulting state in the same call and
    does not need a follow-up status round-trip.

    ``deliver`` hands the caller what the user changed since the last delivery
    (``user_changes``, every document). The RPC handler is Claude; the panel
    passes False, because its own button presses must not swallow the record
    Claude is owed.
    """
    operation = str(spec.get("operation") or "")
    started = time.monotonic()
    flush_deferred_syncs()
    try:
        result = _apply_op(doc, spec)
    finally:
        # INFO: a panel button press is user-visible intent, so "what was
        # clicked and what came of it" belongs in the default trace.
        logger.info("journal %s finished in %.1fms", operation, (time.monotonic() - started) * 1000)
    if not result.get("success"):
        logger.warning("journal %s failed: %s", operation, result.get("error"))
    if operation not in ("status", "user_changes"):
        with contextlib.suppress(Exception):
            result["journal"] = _compact_journal(doc)
    if deliver:
        changes = take_user_changes()
        if changes:
            result["user_changes"] = changes
    return result


def note_panel_action(doc, spec: dict[str, Any], result: dict[str, Any] | None) -> None:
    """Tell Claude about a Steps-panel button the user pressed."""
    res = result or {}
    detail = ""
    if res.get("restored"):
        detail = f"restored: {res['restored']}"
    removed = res.get("removed") or []
    if removed:
        detail = (detail + "; " if detail else "") + "removed " + ", ".join(removed)
    if not res.get("success", False) and res.get("error"):
        detail = str(res["error"])[:200]
    note_user_change(
        doc.Name,
        {
            "kind": "panel",
            "action": sj.panel_action_text(spec),
            "ok": bool(res.get("success")),
            "detail": detail,
        },
        panel=False,
    )


def _compact_journal(doc) -> dict[str, Any]:
    """The steps list without params — small enough to attach to every reply."""
    records = read_journal(doc)
    stale = {s["index"]: s["reason"] for s in stale_steps(doc, records)}
    return {
        "count": len(records),
        "done": sj.done_count(records),
        "planned": sj.pending_count(records),
        "accepted": sum(1 for r in records if r.accepted),
        "drift": bool(stale),
        "steps": [
            {
                "index": r.index,
                "state": r.state,
                "operation": r.operation,
                "label": r.label,
                "accepted": r.accepted,
                **({"error": r.error} if r.error else {}),
                **({"user_edited": True} if (r.params or {}).get("user_edited") else {}),
                **({"stale": stale[r.index]} if r.index in stale else {}),
            }
            for r in records
        ],
    }


def _apply_op(doc, spec: dict[str, Any]) -> dict[str, Any]:
    operation = str(spec.get("operation") or "")
    records = read_journal(doc)

    if operation == "status":
        return _status(doc, records, spec)
    if operation == "user_changes":
        # Nothing to do here: apply_op attaches the changes to every reply.
        # The last record is what a session step needs to know which journal
        # step (and which undo entry) the call it just made became.
        last = records[-1] if records else None
        return {
            "success": True,
            "document": doc.Name,
            "count": len(records),
            "last_record": (
                {
                    "index": last.index,
                    "state": last.state,
                    "operation": last.operation,
                    "transaction": last.transaction,
                }
                if last is not None
                else None
            ),
        }
    if operation == "set_plan":
        steps = list(spec.get("steps") or [])
        # A planned snippet MUST carry its code: op name alone would be accepted
        # and then skipped at run time, so the refusal has to happen here.
        bare = [
            i
            for i, s in enumerate(steps, 1)
            if sj.sub_operation(s) == "execute_code" and not s.get("code")
        ]
        if bare:
            return {
                "success": False,
                "error": (
                    f"plan step(s) {bare} are execute_code without a 'code' key: a planned "
                    "snippet needs the code it will run, e.g. {'operation': 'execute_code', "
                    "'code': 'import FreeCAD; ...'}"
                ),
            }
        added = sj.set_plan(records, steps, EXECUTABLE_OPS)
        meta = read_meta(doc)
        if spec.get("description"):
            meta["description"] = str(spec["description"])
        write_journal(doc, records, meta)
        return {"success": True, "planned": len(added), "count": len(records)}
    if operation == "clear_plan":
        sj.drop_planned(records)
        write_journal(doc, records)
        return {"success": True, "count": len(records)}
    if operation == "run_next":
        return _run_steps(doc, records, limit=1, upto=None)
    if operation == "run_all":
        return _run_steps(doc, records, limit=None, upto=None)
    if operation == "run_to":
        return _run_steps(doc, records, limit=None, upto=int(spec.get("index") or 0))
    if operation == "rollback_to":
        return _rollback(
            doc,
            records,
            int(spec.get("index") or 0),
            bool(spec.get("force")),
            bool((spec.get("params") or {}).get("dry_run") or spec.get("dry_run")),
        )
    if operation == "reexecute":
        return _reexecute(
            doc,
            records,
            int(spec.get("index") or 0),
            spec.get("params") or {},
            bool(spec.get("force")),
            bool(spec.get("replace")),
        )
    if operation == "drop":
        index = int(spec.get("index") or 0)
        stale = {s["index"]: s for s in stale_steps(doc, records)}
        rec = next((r for r in records if r.index == index and r.state == sj.STATE_DONE), None)
        if rec is None:
            return {"success": False, "error": f"no done step {index} to drop"}
        if index not in stale and not spec.get("force"):
            return {
                "success": False,
                "error": (
                    f"step {index} is still in the model; drop only forgets a step the user "
                    "undid or deleted (pass force=true to forget it anyway, or reject it to "
                    "undo it)"
                ),
            }
        sj.drop_step(records, index)
        write_journal(doc, records)
        logger.info("dropped step %d (%s)", index, rec.label)
        return {
            "success": True,
            "dropped": index,
            "label": rec.label,
            "reason": stale.get(index, {}).get("reason", "forced"),
            "count": len(records),
        }
    if operation == "accept":
        rec = sj.set_accepted(
            records, int(spec.get("index") or 0), bool((spec.get("params") or {}).get("on", True))
        )
        if rec is None:
            return {
                "success": False,
                "error": f"no done step {spec.get('index')} to accept/unaccept",
            }
        write_journal(doc, records)
        return {"success": True, "index": rec.index, "accepted": rec.accepted}
    if operation == "snapshot":
        p = spec.get("params") or {}
        return _snapshot(doc, records, str(p.get("note") or ""), bool(p.get("accept_done")))
    if operation == "reject":
        return _reject(
            doc,
            records,
            int(spec.get("index") or 0),
            bool(spec.get("force")),
            str((spec.get("params") or {}).get("reason") or ""),
            bool((spec.get("params") or {}).get("dry_run") or spec.get("dry_run")),
        )
    if operation == "update":
        params = dict(spec.get("params") or {})
        label = str(params.pop("label", "") or "")
        updated = sj.update_planned(
            records, int(spec.get("index") or 0), params, label, bool(spec.get("replace"))
        )
        rec, replaced = updated if updated is not None else (None, [])
        if rec is None and label and not params:
            # A label is presentation, not history: renaming a DONE step is a
            # legitimate fix of a wrong row, and it cannot invalidate the step's
            # transaction. params changes still require planned/failed (they
            # only take effect on a re-run).
            rec = sj.set_label(records, int(spec.get("index") or 0), label)
        if rec is None:
            return {
                "success": False,
                "error": (
                    f"step {spec.get('index')} is not planned/failed — done steps take "
                    "reexecute for params (update accepts params.label alone to rename one)"
                ),
            }
        write_journal(doc, records)
        return {"success": True, "index": rec.index, "label": rec.label, "replaced": replaced}
    if operation == "insert":
        steps = list(spec.get("steps") or (spec.get("params") or {}).get("steps") or [])
        if not steps:
            return {
                "success": False,
                "error": "insert needs a non-empty steps list (a single step dict or "
                "{'steps': [...]}); the MCP step_control wraps params automatically",
            }
        added = sj.insert_steps(records, int(spec.get("index") or 0), steps, EXECUTABLE_OPS)
        if added is None:
            return {
                "success": False,
                "error": f"insert point {spec.get('index')} is inside executed history — "
                "the plan tail is append/insert-only",
            }
        write_journal(doc, records)
        return {"success": True, "inserted": [r.index for r in added]}
    if operation == "replay":
        from_index = int(spec.get("index") or 0)
        res = _rollback(doc, records, from_index, bool(spec.get("force")))
        if not res.get("success"):
            return res
        out = _run_steps(doc, read_journal(doc), limit=None, upto=None)
        out["replayed_from"] = from_index
        if not out.get("success"):
            # Replay rolls back to 0 FIRST, so a re-run failure leaves the
            # document holding only the steps that re-ran before it. The old
            # reply named the failing step and nothing else; the caller found an
            # emptied model on its next call (live: a failed batch step left 0
            # objects while the error text said nothing about it).
            out["document_objects"] = [o.Name for o in doc.Objects]
            out["warning"] = (
                "replay rolled the document back to step 0 before re-running, so it now "
                f"holds only what re-ran successfully ({len(out.get('executed', []))} "
                "step(s)); the model is NOT the pre-replay state. Fix the failing step "
                "(step_control update/reject) and replay again."
            )
        return out
    if operation == "reset":
        # confirm arrives top-level from the RPC spec and, for callers that
        # pass it in the generic params bag, nested under "params".
        confirm = bool(spec.get("confirm") or (spec.get("params") or {}).get("confirm"))
        if not confirm:
            return {"success": False, "error": "reset requires confirm=true"}
        write_journal(doc, [], meta={})
        return {"success": True, "count": 0}
    return {"success": False, "error": f"unknown journal operation '{operation}'"}


def _status(
    doc, records: list[sj.StepRecord], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Every done step is checked, not just the last one: a deleted Pocket under
    # a later step went unnoticed, and a check anchored on the newest step
    # cleared itself the moment Claude committed anything after a Ctrl+Z.
    stale = stale_steps(doc, records)
    out: dict[str, Any] = {
        "success": True,
        "document": doc.Name,
        "count": len(records),
        "done": sj.done_count(records),
        "planned": sj.pending_count(records),
        "drift": bool(stale),
        "stale": stale,
        "meta": read_meta(doc),
    }
    # Read-only options, so the tool signature (and its docstring budget) stays
    # as it is. A long session's full records — every inspection step carries
    # its whole snippet in params — ran to tens of KB, which the caller pays for
    # as context just to learn "which step is next".
    p = dict((spec or {}).get("params") or {})
    if p.get("summary"):
        out["summary"] = True
        out["steps"] = [
            {
                "index": r.index,
                "state": r.state,
                "operation": r.operation,
                "label": sj.row_text(r),
                "accepted": r.accepted,
                "executable": r.executable,
                "result": r.result,
                "transaction": r.transaction,
            }
            for r in records
        ]
        return out
    offset = max(0, int(p.get("offset") or 0))
    limit = int(p.get("limit") or 0)
    page = (
        records[offset : offset + limit] if limit > 0 else (records[offset:] if offset else records)
    )
    if limit > 0:
        out["offset"] = offset
        out["limit"] = limit
        out["has_more"] = offset + len(page) < len(records)
    out["records"] = [r.to_dict() for r in page]
    return out


def _run_steps(doc, records, limit: int | None, upto: int | None) -> dict[str, Any]:
    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    while True:
        rec = sj.next_runnable(records)
        if rec is None:
            break
        # The cursor decides the upto bound, never a done-count: done records
        # may legitimately sit BEHIND the plan (appended inspections, a
        # snapshot marker), and a done_count >= upto comparison then broke
        # early with planned steps still waiting.
        if upto is not None and rec.index > upto:
            break
        if limit is not None and len(executed) >= limit:
            break
        if not rec.executable:
            # execute_code & co. own no transaction and cannot be re-run, and a
            # read-only inspection is a normal journal citizen — run_all and
            # replay must not die on one. Mark it done (a rollback never undid
            # its effects, if any) and continue past it.
            rec.state = sj.STATE_DONE
            rec.error = ""
            rec.result = (
                "skipped: no recorded code"
                if rec.operation == "execute_code"
                else "skipped: not re-executable"
            )
            # A skipped step is a no-op by definition, so it must stop claiming
            # a mutation: build_record defaults atomic/mutated True, and a
            # record that keeps them while owning no transaction satisfies
            # sj.steps_without_undo — which diverts an otherwise clean native
            # rollback to the (destructive) rebuild path over a step that
            # changed nothing. This mirrors append_execute_code's read-only
            # record, and the rule AGENTS.md states: a record that provably
            # changed nothing must NOT stand in rollback's way.
            rec.atomic = False
            rec.mutated = False
            rec.transaction = ""
            skipped.append({"index": rec.index, "operation": rec.operation})
            logger.info("step %d (%s): skipped, not re-executable", rec.index, rec.operation)
            continue
        res = run_record(doc, records, rec)
        executed.append(
            {
                "index": rec.index,
                "operation": rec.operation,
                "success": bool(res.get("success")),
                "error": rec.error,
            }
        )
        if not res.get("success"):
            break
    if skipped:
        # Skips mutate no Shape, so there is no transaction to write inside of.
        write_journal(doc, records)
    return {
        "success": all(e["success"] for e in executed),
        "executed": executed,
        "skipped": skipped,
        "count": len(records),
        "done": sj.done_count(records),
        # Name the failing step: this text lands in the addon log and in MCP
        # replies, where a bare "ValueError: ..." is undebuggable.
        "error": next(
            (
                f"step {e['index']} ({e['operation']}): {e['error']}"
                for e in executed
                if not e["success"]
            ),
            "",
        ),
    }


def _snapshot(doc, records, note: str, accept_done: bool = False) -> dict[str, Any]:
    """Bookmark the current model state as the accepted baseline.

    For the "the user modeled outside the journal" flow: review the good steps,
    do the complex part by hand in the GUI, snapshot, and let the model continue
    from there over MCP. The marker is done + accepted, so rollback/replay
    refuses to cross it without force — that soft lock transitively protects
    the manual work, whose transactions the journal cannot count.
    objects_before (what the journal last knew) vs objects_after (the world
    now) names exactly what happened off-journal. The planned tail is KEPT: a
    snapshot is a marker, not a commit. ``accept_done`` accepts every done step
    in the same call, which bundles "everything so far is correct" into the
    baseline instead of ten separate accept clicks.
    """
    # Anchor on the last DONE record's snapshot — the journal's last known
    # state. The physical last record may be a planned tail entry, which never
    # ran and carries no snapshot; anchoring there would claim the world before
    # step one was empty and mark the user's whole document as "new".
    prev = next(
        (
            list(r.objects_after)
            for r in reversed(records)
            if r.state == sj.STATE_DONE and r.objects_after
        ),
        [],
    )
    accepted = 0
    if accept_done:
        for r in records:
            if r.state == sj.STATE_DONE and not r.accepted:
                r.accepted = True
                accepted += 1
    rec = sj.StepRecord(
        index=len(records) + 1,
        state=sj.STATE_DONE,
        operation="snapshot",
        label=note or "manual baseline",
        params={"note": note, "accept_done": accept_done},
        atomic=False,
        # A snapshot is a MARKER: it changes no geometry, so it must not claim a
        # mutation. `mutated=True` made it a rollback blocker (blocking =
        # not atomic AND mutated), which meant a force-less rollback across it
        # reported the non-atomic guard instead of the accepted soft-lock the
        # step exists to impose.
        mutated=False,
        executable=False,
        accepted=True,
        objects_before=prev,
        objects_after=_object_names(doc),
        timestamp=sj.stamp(),
    )
    records.append(rec)
    write_journal(doc, records)
    added = [n for n in rec.objects_after if n not in set(prev)]
    logger.info(
        "journal snapshot at step %d (%d new object(s), %d step(s) accepted)",
        rec.index,
        len(added),
        accepted,
    )
    return {"success": True, "index": rec.index, "added": added, "accepted": accepted}


def _remove_objects(doc, names: list[str]) -> list[str]:
    """Delete objects by name in one transaction; returns the names removed."""
    with active_document(doc):
        return _remove_objects_locked(doc, names)


def _remove_objects_locked(doc, names: list[str]) -> list[str]:
    removed: list[str] = []
    if not names:
        return removed
    present = set(_object_names(doc))
    targets = [n for n in names if n in present]
    if not targets:
        return removed
    doc.openTransaction(transaction_name(doc, "rollback cleanup"))
    try:
        # Engine-driven by definition: the recompute after a removal can fire
        # changed-object events (a dependant feature's AttachmentOffset, a
        # Body's tip), and mirroring those back into the journal would rewrite a
        # step's params from a state this very operation is tearing down.
        with _EngineQuiet():
            for name in targets:
                obj = doc.getObject(name)
                if obj is None:
                    continue
                try:
                    doc.removeObject(name)
                    removed.append(name)
                except Exception as exc:  # still referenced, or not removable
                    FreeCAD.Console.PrintWarning(f"CADPilot: could not remove '{name}': {exc}\n")
            doc.recompute()
            # Removing the feature that WAS a Body's Tip leaves the Body Invalid
            # until the tip is re-pointed; a rollback that drops a tip feature must
            # leave a usable document behind.
            if repair_body_tips(doc):
                doc.recompute()
        commit_transaction(doc)
    except Exception:
        with contextlib.suppress(Exception):
            doc.abortTransaction()
        raise
    return removed


def _is_container(obj) -> bool:
    # A Body or group lists its members in Group: removing a member shrinks
    # it, nothing breaks, so it is no dependent.
    with contextlib.suppress(Exception):
        return bool(obj.hasExtension("App::GroupExtension"))
    return False


def user_dependents(doc, names: list[str], built: set[str]) -> list[str]:
    """Objects the journal did not build that depend on any of ``names``.

    Removing or undoing their base leaves them broken (a user's chamfer left
    outside its Body with Base=None, Invalid), so a rollback or reject that
    would do it must say so and wait for force.
    """
    targets = set(names)
    out: set[str] = set()
    for name in names:
        obj = doc.getObject(name)
        if obj is None:
            continue
        for dep in getattr(obj, "InListRecursive", None) or []:
            if dep.Name in targets or dep.Name in built or _is_container(dep):
                continue
            out.add(dep.Name)
    return sorted(out)


def rollback_plan(doc, records: list[sj.StepRecord], to_index: int) -> dict[str, Any]:
    """The dry run of ``rollback_to(to_index)``: how, and what goes away.

    ``mode`` (native / rebuild / partial), the transactions undone, the step
    a rebuild starts from and the steps it re-runs, the objects removed, and
    ``user_dependents``: the user's own objects that would break. The panel's
    confirm dialog shows ``summary``; the RPC returns all of it for
    ``params.dry_run``.
    """
    stale = stale_steps(doc, records)
    stale_ids = {s["index"] for s in stale}
    gone = {i for i in stale_ids if i > to_index}
    plan = sj.plan_rollback(records, to_index, gone)
    span, why = _undo_trust(doc, records, to_index, plan["undo_count"], gone)
    restore = sj.plan_restore(records, to_index, span, _object_names(doc), stale_ids)
    built = set(sj.created_since(records, 0))
    deps = user_dependents(doc, restore["remove"] + restore["undone_objects"], built)
    if why:
        restore["reasons"].insert(0, why)
    return {
        **restore,
        "undo_count": plan["undo_count"],
        "blocking": plan["blocking"],
        "accepted": plan["accepted"],
        "stale": stale,
        "user_dependents": deps,
        "summary": sj.restore_summary(restore, deps),
    }


def _dependents_refusal(deps: list[str], what: str) -> str:
    return (
        f"{what} would break the user's own object(s) {deps}: they depend on objects it "
        "removes. Pass force=true to go ahead anyway, or let the user move or delete them "
        "first"
    )


def _rollback(doc, records, to_index: int, force: bool, dry_run: bool = False) -> dict[str, Any]:
    if to_index < 0 or to_index > len(records):
        return {"success": False, "error": f"step {to_index} out of range 0-{len(records)}"}
    plan = rollback_plan(doc, records, to_index)
    if dry_run:
        return {"success": True, "dry_run": True, **plan}
    refusal = _stale_refusal(plan["stale"], to_index, f"rolling back to step {to_index}")
    if refusal and not force:
        return {"success": False, "error": refusal, "stale": plan["stale"]}
    if plan["blocking"] and not force:
        return {
            "success": False,
            "error": (
                f"cannot roll back across non-atomic step(s) "
                f"{sj.blocking_text(records, plan['blocking'])}; pass force=true"
            ),
        }
    if plan["accepted"] and not force:
        return {
            "success": False,
            "error": (
                f"step(s) {plan['accepted']} are accepted (reviewed); "
                "pass force=true to roll back across them"
            ),
        }
    if plan["user_dependents"] and not force:
        return {
            "success": False,
            "error": _dependents_refusal(
                plan["user_dependents"], f"rolling back to step {to_index}"
            ),
            "dry_run": plan,
        }
    # rollback_plan checked the span name by name (_undo_trust): the entries
    # popped here are exactly the journal's own, newest first.
    res = undo_n(doc, plan["undo"])
    # doc.undo() does NOT rewind the journal — the property is document-level and
    # FreeCAD only tracks undo for objects. This reconciliation is what makes
    # the log match the model again, so it runs on every rollback, not just as a
    # fallback.
    records = read_journal(doc)
    stale_ids = {s["index"] for s in plan["stale"]}
    present = set(_object_names(doc))
    # Verify what the undo did instead of trusting the count: whatever the
    # rolled-back steps created must be gone (or be among the leftovers the
    # plan removes), whatever the target needs must be present (or be rebuilt).
    leftover = sorted(set(sj.created_since(records, to_index)) & present)
    built = set(sj.created_since(records, 0))
    expected = set(sj.objects_after_index(records, to_index)) & built
    missing = sorted(n for n in expected if n not in present)
    remove = sorted(set(plan["remove"]) | set(leftover))
    rebuild_from = plan["rebuild_from"]
    surprise = sorted(set(missing) - set(plan["missing"]) - set(plan["remove"]))
    for name in surprise:
        owner = sj.creator_of(records, name)
        if owner is not None and owner.index <= to_index:
            rebuild_from = min(rebuild_from or owner.index, owner.index)
    mode = plan["mode"]
    if rebuild_from is not None and mode != "rebuild":
        if sj.unrecoverable_steps(records, to_index):
            mode = "partial"
            rebuild_from = None
        else:
            mode = "rebuild"
    if rebuild_from is not None:
        remove = sorted(set(remove) | (set(sj.created_since(records, rebuild_from - 1)) & present))
    if res["count"] < plan["undo"]:
        plan["reasons"].append(
            f"the undo stack held only {res['count']}/{plan['undo']} of the journal's transactions"
        )
    late_deps = sorted(set(user_dependents(doc, remove, built)) - set(plan["user_dependents"]))
    if late_deps and not force:
        _rewind_after(doc, records, to_index)
        return {
            "success": False,
            "error": (
                f"the undo took {res['count']} transaction(s), but finishing the rollback "
                + _dependents_refusal(late_deps, "")[1:]
            ),
            "undone": res["count"],
        }
    removed = _remove_objects(doc, remove)
    records = read_journal(doc)
    notes: list[str] = []
    warnings: list[str] = []
    reason = "; ".join(plan["reasons"])
    if mode == "rebuild":
        for rec in records:
            if rebuild_from <= rec.index <= to_index and rec.state in (
                sj.STATE_DONE,
                sj.STATE_FAILED,
            ):
                # Failed records belong in the re-run too: their transaction
                # aborted, so re-running them is safe, and skipping them would
                # run a later step against a missing dependency.
                rec.state = sj.STATE_PLANNED
                rec.transaction = ""
                rec.error = ""
                rec.stale = ""
        write_journal(doc, records)
        run = _run_steps(doc, records, limit=None, upto=to_index)
        records = read_journal(doc)
        notes.append(
            f"{reason}. Rebuilt from step {rebuild_from}: {len(removed)} object(s) removed "
            f"and steps {rebuild_from}..{to_index} re-run."
        )
        if not run.get("success"):
            _rewind_after(doc, records, to_index)
            return {
                "success": False,
                "error": f"the rebuild stopped early: {run.get('error') or 'unknown error'}",
                "undone": res["count"],
                "restored": mode,
                "rebuild_from": rebuild_from,
                "removed": removed,
                "done": sj.done_count(read_journal(doc)),
                "count": len(records),
                "notes": notes,
            }
    elif mode == "removed":
        notes.append(
            f"{reason}. Those steps only created objects, so removing them restores step "
            f"{to_index} exactly: {', '.join(removed) or 'nothing left to remove'}."
        )
    elif removed or mode == "partial":
        warnings.append(
            f"{reason}. Their objects were removed directly ({len(removed)} object(s)), but "
            "property changes they made (placements, dimensions) cannot be restored. "
            "Replay the journal if you need an exact state."
        )
    still = sorted(set(remove) & set(_object_names(doc)) - set(removed))
    if still:
        warnings.append(f"object(s) {still} could not be removed and are still in the document")
    if plan["user_dependents"]:
        warnings.append(
            f"forced: the user's own object(s) {plan['user_dependents']} depended on what was "
            "removed and may now be broken"
        )
    replanned = _rewind_after(doc, records, to_index)
    return {
        "success": True,
        "undone": res["count"],
        "restored": mode,
        "rebuild_from": rebuild_from,
        "removed": removed,
        "stranded": [] if mode == "rebuild" else plan["unreached"],
        "replanned": replanned,
        "skipped_stale": sorted(i for i in stale_ids if i > to_index),
        "done": sj.done_count(records),
        "count": len(records),
        "notes": notes,
        "warnings": warnings,
    }


def _rewind_after(doc, records, to_index: int) -> list[int]:
    """Put every record after ``to_index`` back to planned; returns the failed ones.

    Counts the DONE records the rollback affected — NOT done_count - to_index,
    which assumes 1..to_index are all done and so under-rewinds whenever a
    FAILED or PLANNED record sits in between. A failed record was never
    applied, so it goes back to planned too (see sj.replan_failed): otherwise
    the cursor steps over it and runs the rest of the plan against a
    dependency that was never created.
    """
    extra = sj.done_after_index(records, to_index)
    if extra > 0:
        sj.rewind(records, extra)
    replanned = sj.replan_failed(records, to_index)
    if extra > 0 or replanned:
        with contextlib.suppress(Exception):
            write_journal(doc, records)
    return replanned


def reject_plan(doc, records: list[sj.StepRecord], index: int) -> dict[str, Any] | None:
    """The dry run of ``reject(index)``: what is undone, removed, and who breaks."""
    gone = {s["index"] for s in stale_steps(doc, records) if s["index"] >= index}
    plan = sj.plan_reject(records, index, gone)
    if plan is None:
        return None
    span, why = _undo_trust(doc, records, index - 1, plan["undo_count"], gone)
    present = set(_object_names(doc))
    doomed = sorted(set(sj.created_since(records, index - 1)) & present)
    deps = user_dependents(doc, doomed, set(sj.created_since(records, 0)))
    removed_directly = len(doomed) if span < plan["undo_count"] else 0
    summary = (
        f"Undo {span} transaction(s) and forget {len(plan['drop'])} step(s); "
        f"{len(doomed)} object(s) go away"
        + (f", {removed_directly} of them removed directly" if removed_directly else "")
        + "."
        + (f" Your own object(s) {', '.join(deps)} depend on them and would break." if deps else "")
    )
    return {
        **plan,
        "undo": span,
        "undo_why": why,
        "gone": sorted(gone),
        "remove": doomed,
        "user_dependents": deps,
        "summary": summary,
    }


def _reject(
    doc, records, index: int, force: bool, reason: str, dry_run: bool = False
) -> dict[str, Any]:
    """Undo and forget everything from ``index`` onward (see sj.plan_reject).

    Reject is the deliberate act of destruction, so — unlike rollback/replay —
    it does not stop at accepted steps; it refuses to cross non-atomic ones
    without force, where undo itself is unreliable, and to break the user's
    own objects that depend on what it removes.
    """
    plan = reject_plan(doc, records, index)
    if plan is None:
        return {"success": False, "error": f"no step {index}"}
    if dry_run:
        return {"success": True, "dry_run": True, **plan}
    if plan["user_dependents"] and not force:
        return {
            "success": False,
            "error": _dependents_refusal(plan["user_dependents"], f"rejecting step {index}"),
            "dry_run": plan,
        }
    if plan["blocking"] and not force:
        return {
            "success": False,
            "error": (
                f"cannot reject across non-atomic step(s) "
                f"{sj.blocking_text(records, plan['blocking'])}; pass force=true"
            ),
        }
    span, undo_why = plan["undo"], plan["undo_why"]
    res = undo_n(doc, span)
    records = read_journal(doc)
    warnings: list[str] = []
    if undo_why:
        warnings.append(undo_why)
    # The same undo-stack problems as rollback: a rejected step with no undo
    # entry leaves its objects behind, and a manual edit interleaved on the
    # stack pops under this reject's name. The records are about to be DROPPED,
    # so there is no rebuild option — remove what the doomed steps introduced
    # (a no-op when the undo was clean), then say what could not be fixed.
    removal = sj.created_since(records, index - 1)
    removed = _remove_objects(doc, removal)
    records = read_journal(doc)
    present = set(_object_names(doc))
    still = sorted(set(removal) & present)
    if still:
        warnings.append(
            f"object(s) {still} from the rejected steps could not be removed and are "
            "still in the document"
        )
    kept_expected = set(sj.objects_after_index(records, index - 1))
    gone = sorted(
        n for n in kept_expected if n not in present and n in set(sj.created_since(records, 0))
    )
    if gone:
        warnings.append(
            f"object(s) {gone} that the kept steps created are missing (off-journal "
            "edits on the undo stack?); replay the journal to rebuild them"
        )
    if res["count"] < span:
        warnings.append(
            f"only {res['count']}/{plan['undo_count']} transactions could be undone "
            "(the FreeCAD undo stack was shorter than the journal)"
        )
    doomed = sj.steps_without_undo(records, index - 1)
    if doomed:
        warnings.append(
            f"rejected step(s) {doomed} owned no undo entry, so their objects were removed "
            f"directly ({len(removed)} object(s)); property changes they made cannot be "
            "restored"
        )
    del records[index - 1 :]
    write_journal(doc, records)
    logger.info(
        "rejected step %d onward (dropped %s)%s",
        index,
        plan["drop"],
        f": {reason}" if reason else "",
    )
    return {
        "success": True,
        "rejected": plan["drop"],
        "undone": res["count"],
        "removed": removed,
        "warnings": warnings,
    }


def _reexecute(
    doc, records, index: int, params: dict[str, Any], force: bool = False, replace: bool = False
) -> dict[str, Any]:
    params = dict(params or {})
    # A label is presentation, so it is applied to the record and never merged
    # into the op's params (where a stray "label" key would do nothing).
    label = str(params.pop("label", "") or "")
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return {"success": False, "error": f"no step {index}"}
    if not rec.executable:
        return {"success": False, "error": f"step {index} ('{rec.operation}') is not re-executable"}
    # A re-run needs the model exactly at step index-1: the rollback's own
    # verification (and its rebuild from the first wrong step) provides it,
    # including when the user's edits sit on the undo stack. A partial restore
    # is no base, so it is refused before anything is touched.
    plan = rollback_plan(doc, records, index - 1)
    if plan["mode"] == "partial":
        return {
            "success": False,
            "error": (
                f"cannot re-run step {index}: {'; '.join(plan['reasons'])}, and step(s) before "
                "it cannot be re-run to rebuild the model; replay the journal or reject the step"
            ),
        }
    after = sj.done_after_index(records, index)
    back = _rollback(doc, records, index - 1, force)
    if not back.get("success"):
        error = str(back.get("error") or "")
        if error.startswith(f"rolling back to step {index - 1}"):
            error = f"re-running step {index}" + error[len(f"rolling back to step {index - 1}") :]
        return {**back, "error": error or f"cannot re-run step {index}"}
    records = read_journal(doc)
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return {"success": False, "error": f"step {index} disappeared during rollback"}
    rec.state = sj.STATE_PLANNED
    # Its transaction was just undone, so the record owns no undo entry until
    # the re-run commits one: a stale name would sit in the journal as a
    # transaction the stack cannot supply, which breaks the rollback trust
    # check at that position.
    rec.transaction = ""
    # Merged, not swapped: the step's params carry the user's synced edits,
    # and a caller passing {"obj_properties": {"reversed": true}} used to drop
    # every other property with them.
    if replace and params:
        replaced = sorted(k for k in rec.params or {} if (rec.params or {}).get(k) != params.get(k))
        rec.params = dict(params)
    else:
        rec.params, replaced = sj.merge_params(rec.params or {}, params or {})
    if label:
        rec.label = label
    write_journal(doc, records)
    res = run_record(doc, records, rec)
    out = {
        **res,
        "index": index,
        "count": len(records),
        "done": sj.done_count(records),
        "replaced": replaced,
        "restored": back.get("restored"),
        "removed": back.get("removed"),
        "notes": back.get("notes") or [],
        "warnings": back.get("warnings") or [],
    }
    if after > 0:
        # A re-run happens on a clean base, so the steps after it are undone and
        # returned to `planned`. That is by design, but it used to happen
        # silently: the reply carried a done-count and nothing else, so a caller
        # watched its later model disappear (live: "the model was down to the
        # first step"). The step itself is not one of them.
        out["rewound"] = after
        note = (
            f"re-running step {index} rolled the {after} step(s) after it back to "
            "planned: their work is undone and they must be released again "
            "(step_control run_all or run_to)."
        )
        out["warning"] = f"{out['warning']} {note}".strip() if out.get("warning") else note
    return out


# --- manual-edit sync ----------------------------------------------------------
#
# Human/machine collaboration needs the journal to tell the truth about the
# model: when the user corrects a dimension (or moves a part, edits a
# spreadsheet cell, rebinds an expression) in FreeCAD's GUI, a later
# reexecute/replay must not silently revert that correction. This observer
# mirrors GUI edits on objects a done step produced back into that step's
# params. What syncs: scalars the spec already carries (create/edit_object),
# the builder-known keys of every feature op (sj.FEATURE_SYNC — including
# ones left at default, or replay reverts the edit), spreadsheet cells and
# aliases (variables), dimensional constraint values and their expression
# bindings (sketch), datum/sketch attachment offsets, the Placement of
# create/edit steps, and the final pose of move-targeted objects (folded into
# the last move step as an absolute placement). Machine-driven writes never
# sync: engine windows (undo/redo/re-run) and every RPC-layer mutation run
# under _EngineQuiet — a tool's change is its own step, and echoing it into
# an earlier step's params made reject/replay inconsistent.

# Per document, two copies of what the user did (see sj.append_change for the
# event kinds): Claude's, drained by the next RPC reply, and the panel's,
# drained by its log. The RPC thread may read Claude's copy, hence the lock.
_CHANGES_LOCK = threading.Lock()
_USER_CHANGES: dict[str, list[dict[str, Any]]] = {}
_PANEL_EVENTS: dict[str, list[dict[str, Any]]] = {}
_CHANGES_CAP = 200


def note_user_change(doc_name: str, event: dict[str, Any], panel: bool = True) -> None:
    """Record something the user did to ``doc_name`` for Claude (and the panel)."""
    event = {**event, "time": time.strftime("%H:%M:%S")}
    with _CHANGES_LOCK:
        stores = (_USER_CHANGES, _PANEL_EVENTS) if panel else (_USER_CHANGES,)
        for store in stores:
            log = store.setdefault(doc_name, [])
            sj.append_change(log, dict(event))
            del log[:-_CHANGES_CAP]


def take_user_changes() -> dict[str, list[dict[str, Any]]]:
    """Every document's changes since the last call, and forget them."""
    with _CHANGES_LOCK:
        out = {name: list(events) for name, events in _USER_CHANGES.items() if events}
        _USER_CHANGES.clear()
    return out


def pop_panel_events(doc_name: str) -> list[dict[str, Any]]:
    """The panel's copy of one document's changes since its last refresh."""
    with _CHANGES_LOCK:
        return _PANEL_EVENTS.pop(doc_name, [])


def _record_for_transaction(doc, name: str) -> sj.StepRecord | None:
    if not name.startswith(TX_PREFIX):
        return None
    return next((r for r in read_journal(doc) if r.transaction == name), None)


def _num(value: float) -> int | float:
    f = float(value)
    return int(f) if f.is_integer() else f


def _json_scalar(value) -> int | float | str | bool | None:
    """A FreeCAD property value reduced to a JSON scalar, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, str):
        return value
    q = getattr(value, "Value", None)  # Base.Quantity
    if isinstance(q, (int, float)):
        return _num(q)
    return None


def _same_scalar(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    return a == b


# Feature-op property -> spec-key mapping lives in step_journal (pure,
# unit-tested): sj.FEATURE_SYNC, sj.tracked_objects, sj.map_cell_value,
# sj.map_constraint_value. The engine keeps only the FreeCAD reads.


def _uniform_edge_size(edges) -> int | float | None:
    """Uniform (index, start, end) edge-size list -> the single size, else None.

    The journal spec carries one scalar radius/size, so only a uniform edit
    maps back — per-edge sizes have no spec representation.
    """
    try:
        sizes = {round(float(t[1]), 9) for t in edges} | {round(float(t[2]), 9) for t in edges}
    except (TypeError, IndexError, ValueError):
        return None
    if len(sizes) == 1:
        return _num(sizes.pop())
    return None


def _placement_json(pl) -> dict:
    """FreeCAD.Placement -> the dict shape property_mapper accepts.

    Rotation.Angle reads back in RADIANS while the mapper constructs with
    DEGREES (FreeCAD.Rotation(axis, deg)) — convert here or a round-trip
    turns 90° into 1.57°.
    """
    rot = pl.Rotation
    return {
        "Base": {"x": _num(pl.Base.x), "y": _num(pl.Base.y), "z": _num(pl.Base.z)},
        "Rotation": {
            "Axis": {"x": _num(rot.Axis.x), "y": _num(rot.Axis.y), "z": _num(rot.Axis.z)},
            "Angle": _num(math.degrees(rot.Angle)),
        },
    }


def _flat_placement(d: dict) -> tuple:
    base = d.get("Base") or d.get("Position") or {}
    rot = d.get("Rotation") or {}
    axis = rot.get("Axis") or {}
    return (
        float(base.get("x", 0)),
        float(base.get("y", 0)),
        float(base.get("z", 0)),
        float(axis.get("x", 0)),
        float(axis.get("y", 0)),
        float(axis.get("z", 1)),
        float(rot.get("Angle", 0)),
    )


def _same_placement(a, b) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    fa, fb = _flat_placement(a), _flat_placement(b)
    return all(abs(x - y) <= 1e-9 for x, y in zip(fa, fb, strict=True))


def _placement_brief(d) -> str:
    if not isinstance(d, dict):
        return "-"
    base = d.get("Base") or {}
    rot = d.get("Rotation") or {}
    axis = rot.get("Axis") or {}
    pos = ",".join(str(_num(base.get(k, 0))) for k in "xyz")
    ax = ",".join(str(_num(axis.get(k, 0))) for k in "xyz")
    return f"pos({pos}) rot {_num(rot.get('Angle', 0))}deg@({ax})"


def _link_json(value) -> dict[str, Any] | None:
    """A PropertyLinkSub value as sj.sync_patch reads it."""
    if not value:
        return None
    target, subs = (value[0], value[1]) if isinstance(value, tuple) else (value, [])
    if target is None:
        return None
    if isinstance(subs, str):
        subs = [subs]
    return {
        "name": target.Name,
        "subs": [str(x) for x in subs or []] or [""],
        "role": str(getattr(target, "Role", "") or ""),
        "type": str(getattr(target, "TypeId", "") or ""),
    }


def _read_live(obj, prop: str, kind: str):
    """A feature property's live value, in the shape its sync rule's kind takes."""
    value = getattr(obj, prop, None)
    if kind in ("link_sub", "axis", "mirror_plane"):
        return _link_json(value)
    if kind == "names":
        return [o.Name for o in value or [] if o is not None]
    if kind == "spacings":
        return list(value) if value is not None else None
    if kind == "pad_mode":
        return {
            "type": str(getattr(obj, "Type", "") or ""),
            "side": str(getattr(obj, "SideType", "") or ""),
            "midplane": bool(getattr(obj, "Midplane", False)),
        }
    if kind == "pattern_mode":
        return str(value) if value is not None else None
    if kind == "edge_size":
        return _uniform_edge_size(value)
    if kind == "z_offset":
        if value is None:
            return None
        return (value.Base.x, value.Base.y, value.Base.z, value.Rotation.Angle)
    return _json_scalar(value)


def _live_sketch(obj) -> tuple[list[dict], list[bool], list[dict], list[list[str]]]:
    """A sketch's geometry, construction flags, constraints and external refs, as data."""
    geometry: list[dict[str, Any]] = []
    for g in obj.Geometry:
        kind = type(g).__name__
        item: dict[str, Any] = {"type": kind}
        if kind == "LineSegment":
            item.update(start=[g.StartPoint.x, g.StartPoint.y], end=[g.EndPoint.x, g.EndPoint.y])
        elif kind in ("Circle", "ArcOfCircle"):
            item.update(center=[g.Center.x, g.Center.y], radius=g.Radius, axis_z=g.Axis.z)
            if kind == "ArcOfCircle":
                item.update(first=g.FirstParameter, last=g.LastParameter)
        elif kind == "Point":
            item.update(at=[g.X, g.Y])
        geometry.append(item)
    construction = [bool(obj.getConstruction(i)) for i in range(len(geometry))]
    constraints = [
        {
            "type": k.Type,
            "first": int(k.First),
            "first_pos": int(k.FirstPos),
            "second": int(k.Second),
            "second_pos": int(k.SecondPos),
            "third": int(k.Third),
            "third_pos": int(k.ThirdPos),
            "value": float(k.Value),
            "name": str(k.Name or ""),
            "driving": bool(getattr(k, "Driving", True)),
            "active": bool(getattr(k, "IsActive", True)),
        }
        for k in obj.Constraints
    ]
    external = []
    for ref in getattr(obj, "ExternalGeometry", None) or []:
        target, subs = ref if isinstance(ref, tuple) else (ref, "")
        subs = [subs] if isinstance(subs, str) else list(subs or [])
        external.append([getattr(target, "Name", ""), *[str(x) for x in subs]])
    return geometry, construction, constraints, external


def _sketch_object(doc, params: dict[str, Any], before: list[str], after: list[str]):
    for name in [n for n in after if n not in set(before)] + [str(params.get("obj_name") or "")]:
        obj = doc.getObject(name) if name else None
        if obj is not None and obj.TypeId == "Sketcher::SketchObject":
            return obj
    return None


def _note_sketch_signature(doc, params: dict[str, Any], before, after) -> None:
    """Remember the structure a sketch step built, so a later edit can be told apart."""
    with contextlib.suppress(Exception):
        obj = _sketch_object(doc, params, list(before or []), list(after or []))
        if obj is not None:
            params["sketch_signature"] = sj.sketch_signature(*_live_sketch(obj))


def _sketch_blob(obj) -> dict[str, Any]:
    """The sketch's own serialization, for a sketch the spec cannot express.

    ``digest`` covers the content only: the dumps are zip files stamped with
    the time they were written, so two dumps of one sketch never compare equal.
    """
    import base64
    import hashlib
    import io
    import zipfile

    blob: dict[str, Any] = {}
    digest = hashlib.sha1()
    for prop in ("Geometry", "Constraints", "ExternalGeometry"):
        data = bytes(obj.dumpPropertyContent(prop))
        blob[prop] = base64.b64encode(data).decode("ascii")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in sorted(archive.namelist()):
                digest.update(archive.read(name))
    blob["expressions"] = [
        [str(path), str(expr)]
        for path, expr in getattr(obj, "ExpressionEngine", None) or []
        if "Constraints" in str(path)
    ]
    digest.update(json.dumps(blob["expressions"]).encode("utf-8"))
    blob["digest"] = digest.hexdigest()[:16]
    return blob


def _restore_live_sketch(doc, sketch, blob: dict[str, Any]) -> None:
    """Put a user-edited sketch back exactly, after the builder made its base."""
    import base64

    for path, _expr in list(getattr(sketch, "ExpressionEngine", None) or []):
        if "Constraints" in str(path):
            sketch.setExpression(str(path), None)
    for prop in ("ExternalGeometry", "Geometry", "Constraints"):
        if blob.get(prop):
            sketch.restorePropertyContent(prop, base64.b64decode(blob[prop]))
    for path, expr in blob.get("expressions") or []:
        sketch.setExpression(path, expr)
    doc.recompute()
    if sketch.solve() != 0:
        raise RuntimeError(f"the user's edited sketch '{sketch.Name}' no longer solves")


def _sync_props_target(rec: sj.StepRecord, sub: int | None) -> dict[str, Any] | None:
    """The obj_properties dict a manual-edit sync write lands in.

    ``sub`` routes the write into a batch record's sub-op (tracked_objects
    claims create/edit/move sub-ops); None is a top-level step.
    """
    if sub is None:
        return rec.params.setdefault("obj_properties", {})
    ops = (rec.params or {}).get("ops") or []
    if isinstance(sub, int) and 0 <= sub < len(ops) and isinstance(ops[sub], dict):
        return ops[sub].setdefault("obj_properties", {})
    return None


def _user_thread() -> bool:
    # execute_code_async runs Claude's snippet on a worker thread outside any
    # engine window; only the GUI thread is the user.
    return threading.current_thread() is threading.main_thread()


class _JournalSyncObserver:
    def __init__(self):
        self._cache: dict[str, tuple[str, dict]] = {}
        self._writing = False
        # Changes FreeCAD makes while undoing/redoing (doc.Transacting), held
        # until the undo is over and its transaction name says whose it was.
        self._undoing: dict[str, dict[str, Any]] = {}

    def _muted(self, doc) -> bool:
        return (
            self._writing
            or _ENGINE_ACTIVE > 0
            or not _user_thread()
            or bool(getattr(doc, "Restoring", False))
        )

    def _undo_buffer(self, doc) -> dict[str, Any]:
        return self._undoing.setdefault(doc.Name, {"props": [], "added": [], "removed": []})

    def slotCreatedObject(self, obj):
        with contextlib.suppress(Exception):
            doc = obj.Document
            if self._muted(doc):
                return
            if doc.Transacting:
                self._undo_buffer(doc)["added"].append(obj.Name)
                return
            note_user_change(
                doc.Name, {"kind": "added", "objects": [obj.Name], "types": [obj.TypeId]}
            )

    def slotDeletedObject(self, obj):
        with contextlib.suppress(Exception):
            doc = obj.Document
            if self._muted(doc):
                return
            if doc.Transacting:
                self._undo_buffer(doc)["removed"].append(obj.Name)
                return
            note_user_change(doc.Name, {"kind": "removed", "objects": [obj.Name]})
            owner = sj.creator_of(read_journal(doc), obj.Name)
            if owner is not None:
                note_user_change(
                    doc.Name,
                    {
                        "kind": "stale",
                        "step": owner.index,
                        "label": owner.label,
                        "detail": f"the user deleted {obj.Name}",
                    },
                )

    def slotUndoDocument(self, doc):
        with contextlib.suppress(Exception):
            self._after_stack_op(doc, "undo")

    def slotRedoDocument(self, doc):
        with contextlib.suppress(Exception):
            self._after_stack_op(doc, "redo")

    def _after_stack_op(self, doc, kind: str) -> None:
        held = self._undoing.pop(doc.Name, None) or {"props": [], "added": [], "removed": []}
        if self._muted(doc):
            return
        stack = _real_redo_names(doc) if kind == "undo" else _real_undo_names(doc)
        name = stack[0] if stack else ""
        rec = _record_for_transaction(doc, name)
        if rec is not None:
            # Remembered on the record: a later commit clears the redo stack,
            # and with it the only other trace that the step was undone.
            records = read_journal(doc)
            target = next(r for r in records if r.index == rec.index)
            target.stale = "undone by the user (Ctrl+Z)" if kind == "undo" else ""
            self._writing = True
            try:
                write_journal(doc, records)
            finally:
                self._writing = False
        if not name.startswith(TX_PREFIX):
            # The user undid (or redid) their OWN edit: the values FreeCAD
            # restored are the user's again, so the journal follows them. A
            # CADPilot step's undo is not mirrored: the step keeps its params
            # and goes stale instead, so Claude can restore it or drop it.
            for obj_name, prop in held["props"]:
                obj = doc.getObject(obj_name)
                if obj is not None:
                    self._sync(obj, prop)
        note_user_change(
            doc.Name,
            {
                "kind": kind,
                "transaction": name,
                "step": rec.index if rec is not None else None,
                "label": rec.label if rec is not None else "",
                "added": held["added"],
                "removed": held["removed"],
            },
        )

    def slotChangedObject(self, obj, prop):
        # _writing guards the observer's own journal write; _ENGINE_ACTIVE
        # mutes machine-driven windows (engine undo/redo/re-run, and every
        # RPC-layer mutation — a tool's change is its own step, and letting it
        # bleed into an earlier step's params made reject/replay inconsistent).
        if self._writing or _ENGINE_ACTIVE > 0:
            return
        # An observer must never break the host's edit.
        with contextlib.suppress(Exception):
            # Opening a file sets every property once, in file order (Length
            # before the ExpressionEngine that binds it), which read as a
            # burst of manual edits and rewrote bound params to numbers.
            if obj.Document.Restoring or not _user_thread():
                return
            if obj.Document.Transacting:
                self._undo_buffer(obj.Document)["props"].append((obj.Name, prop))
                return
            self._sync(obj, prop)

    def _entry(self, doc, obj) -> tuple[str, dict[str, Any] | None]:
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
        if not text:
            return "", None
        cached = self._cache.get(doc.Name)
        if cached is None or cached[0] != text:
            cached = (text, sj.tracked_objects(sj.from_json(text)))
            self._cache[doc.Name] = cached
        return text, cached[1].get(getattr(obj, "Name", ""))

    def sync_sketch(self, doc, obj) -> None:
        """The deferred half of a sketch edit (see _defer_sketch_sync)."""
        if self._writing or doc.Restoring:
            return
        text, entry = self._entry(doc, obj)
        if entry is None or entry.get("sketch") is None:
            return
        records = sj.from_json(text)
        if self._sync_sketch_structure(obj, doc, records, *entry["sketch"]):
            return
        self._sync_constraints(obj, doc, records, *entry["sketch"])

    def _sync(self, obj, prop) -> None:
        doc = getattr(obj, "Document", None)
        if doc is None:
            return
        text, entry = self._entry(doc, obj)
        if entry is None:
            return
        if entry.get("sketch") is not None and prop in _SKETCH_PROPS:
            # Deleting a constraint fires ExpressionEngine while Constraints
            # still has the old length but the expression paths are already
            # renumbered, so a sync at event time paired bindings with the
            # wrong constraints. The sketch is read once the edit settled.
            _defer_sketch_sync(doc.Name, obj.Name)
            if prop != "ExpressionEngine":
                return
        if prop == "ExpressionEngine":
            # Binding or unbinding an expression rewrites ExpressionEngine and
            # need not fire the bound property itself, so every value this
            # object is tracked for is re-read here. Live-verified on 1.1.4:
            # binding Pad.Length to =Vars.plate_thk in the GUI left the step's
            # params at the last number (and unbinding left "=Vars.plate_thk"
            # in them), even though the code below reads the live binding
            # correctly whenever it runs.
            records = sj.from_json(text)
            if entry.get("sheet") is not None:
                self._sync_cells(obj, doc, records, *entry["sheet"])
            # Only what may have been bound or unbound: a property that is
            # bound now, or whose step value is an expression. Re-reading every
            # claim wrote each builder default the caller had left out.
            bound = {
                str(path).lstrip(".").split(".")[0]
                for path, _expr in getattr(obj, "ExpressionEngine", None) or []
            }
            by_index = {r.index: r for r in records}
            for claimed, (index, key, sub) in list(entry["props"].items()):
                rec = by_index.get(index)
                target = (_sync_props_target(rec, sub) or {}) if rec is not None else {}
                if claimed in bound or str(target.get(key, "")).startswith("="):
                    self._sync(obj, claimed)
            if entry.get("move") is not None:
                self._sync_move_fold(obj, doc, records, *entry["move"])
            return
        # The value compare inside each handler makes repeat firings (a cell
        # edit fires both 'cells' and the address) no-ops, so trigger wide.
        if entry.get("sheet") is not None:
            self._sync_cells(obj, doc, sj.from_json(text), *entry["sheet"])
            return
        hit = entry["props"].get(prop)
        if hit is None:
            # A move-owned object's pose belongs to its LAST move step.
            if prop == "Placement" and entry.get("move") is not None:
                self._sync_move_fold(obj, doc, sj.from_json(text), *entry["move"])
            return
        records = sj.from_json(text)
        rec = next((r for r in records if r.index == hit[0]), None)
        if rec is None:
            return
        rule = sj.feature_rule(rec.operation, prop) if hit[2] is None else None
        if rule is not None:
            self._sync_rule(obj, doc, records, rec, prop, rule)
            return
        if prop == "Placement":
            value, same, brief = (
                _placement_json(obj.Placement),
                _same_placement,
                _placement_brief,
            )
        else:
            # An expression binding wins over the literal value, in both
            # directions: binding in the GUI stores "=expr" (the builder
            # re-binds it on re-run), unbinding stores the number it fell
            # back to.
            exprs = dict(getattr(obj, "ExpressionEngine", None) or [])
            expr = exprs.get(prop)
            if expr:
                value = "=" + "".join(str(expr).split())
            else:
                value = _json_scalar(getattr(obj, prop, None))
                if value is None:
                    return
            same, brief = _same_scalar, None
        props = _sync_props_target(rec, hit[2])
        if props is None:
            return
        current = props.get(hit[1])
        if same(current, value):
            return
        props[hit[1]] = value
        shown = (brief(current), brief(value)) if brief else (current, value)
        self._commit_sync(doc, records, rec, [(f"{obj.Name}.{prop}", *shown)])

    def _sync_rule(self, obj, doc, records, rec, prop: str, rule: sj.SyncRule) -> None:
        """Mirror one feature property through its FEATURE_SYNC rule."""
        if rule.only and getattr(obj, "TypeId", "") not in rule.only:
            return
        if rule.when is not None and str(getattr(obj, rule.when[0], "")) != str(rule.when[1]):
            return
        exprs = dict(getattr(obj, "ExpressionEngine", None) or [])
        expr = exprs.get(prop)
        if rule.kind == "z_offset":
            expr = exprs.get(".AttachmentOffset.Base.z") or exprs.get("AttachmentOffset.Base.z")
        base = str((rec.params or {}).get("obj_name") or "")
        patch = sj.sync_patch(rule, _read_live(obj, prop, rule.kind), expr, base)
        if not patch:
            return
        target = rec.params.setdefault("obj_properties", {})
        edits: list[tuple[str, Any, Any]] = []
        for key, value in patch.items():
            current = target.get(key)
            if sj.patch_value_matches(rule, key, current, value):
                continue
            if value is sj.REMOVE:
                target.pop(key, None)
                value = None
            else:
                target[key] = value
            what = f"{obj.Name}.{prop}" + ("" if key == rule.key else f" ({key})")
            edits.append((what, current, value))
        if edits:
            self._commit_sync(doc, records, rec, edits)

    def _sync_cells(self, obj, doc, records, index: int, sub: int | None = None) -> None:
        """Mirror spreadsheet cell edits (values, formulas, aliases) into the
        owning step's ``cells`` spec (``sub`` routes into a batch sub-op)."""
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        cells = (_sync_props_target(rec, sub) or {}).get("cells")
        if not isinstance(cells, dict) or not cells:
            return
        edits: list[tuple[str, Any, Any]] = []
        for cell, cell_entry in cells.items():
            if not (isinstance(cell_entry, list) and len(cell_entry) == 2):
                continue
            alias, old = cell_entry
            try:
                live_contents = obj.getContents(cell)
            except Exception:
                continue  # the cell was deleted by hand — leave the spec as-is
            new = sj.map_cell_value(old, live_contents)
            if new is not sj.UNREADABLE and not _same_scalar(old, new):
                cell_entry[1] = new
                edits.append((f"{obj.Name}.{cell} ({alias})", old, new))
            try:
                live_alias = obj.getAlias(cell) or ""
            except Exception:
                live_alias = alias
            if live_alias and live_alias != alias:
                cell_entry[0] = live_alias
                edits.append((f"{obj.Name}.{cell} alias", alias, live_alias))
        if edits:
            self._commit_sync(doc, records, rec, edits)

    def _sync_sketch_structure(self, obj, doc, records, index: int, sub: int | None) -> bool:
        """Snapshot a sketch the user changed structurally into its step.

        Geometry or constraints added or deleted by hand used to leave the
        step's spec as it was (the value sync maps by index and bails), so a
        replay silently rebuilt the original sketch. The live sketch goes into
        the spec when the spec can express it, otherwise the sketch's own
        serialization rides along (``live_sketch``), which replay restores
        after the builder made the base. True = handled, the values included.
        """
        rec = next((r for r in records if r.index == index), None)
        if rec is None or sub is not None:
            return False
        live = _live_sketch(obj)
        signature = sj.sketch_signature(*live)
        stored = rec.params.get("sketch_signature")
        if rec.operation != "sketch":
            # A sketch_edit-style step has no full spec to rewrite: whatever the
            # user changed after it, structure or values, rides on it as the
            # sketch's own data, restored after it re-runs.
            blob = _sketch_blob(obj)
            old = rec.params.get("live_sketch") or {}
            if stored == signature and (not old or old.get("digest") == blob["digest"]):
                return True
            rec.params["live_sketch"] = blob
            rec.params["sketch_signature"] = signature
            rec.params["user_edited"] = True
            detail = "edited by hand after this step; kept as FreeCAD's own sketch data"
            self._commit_sync(doc, records, rec, [], sketch=(obj.Name, detail))
            return True
        spec = rec.params.setdefault("obj_properties", {})
        if stored is None:
            # A journal from before signatures: adopt the live structure as the
            # baseline unless it plainly differs from the spec.
            if len(spec.get("constraints") or []) == len(live[2]) and len(
                spec.get("geometry") or []
            ) == len(live[0]):
                rec.params["sketch_signature"] = signature
                return False
        elif stored == signature:
            if not rec.params.get("live_sketch"):
                return False
            blob = _sketch_blob(obj)
            if blob["digest"] == rec.params["live_sketch"].get("digest"):
                return True
            rec.params["live_sketch"] = blob
            self._commit_sync(doc, records, rec, [], sketch=(obj.Name, "dimensions changed"))
            return True
        new_spec, unsupported = sj.sketch_spec_from_live(
            live[0], live[1], live[2], dict(getattr(obj, "ExpressionEngine", None) or [])
        )
        if len(live[3]) != len(spec.get("external") or []):
            unsupported.append("external geometry")
        detail = sj.describe_sketch_edit(spec, new_spec)
        if unsupported:
            rec.params["live_sketch"] = _sketch_blob(obj)
            detail += (
                f"; kept as FreeCAD's own sketch data because the spec cannot express "
                f"{', '.join(unsupported[:3])}"
            )
        else:
            rec.params.pop("live_sketch", None)
            spec.update(new_spec)
            if not spec["construction"]:
                spec.pop("construction")
        rec.params["sketch_signature"] = signature
        rec.params["user_edited"] = True
        self._commit_sync(doc, records, rec, [], sketch=(obj.Name, detail))
        return True

    def _sync_constraints(self, obj, doc, records, index: int, sub: int | None = None) -> None:
        """Mirror dimensional-constraint edits into the owning sketch step.

        The spec's constraint list maps to the live sketch BY INDEX, so the
        whole sketch is skipped the moment the sequences diverge (a constraint
        added or removed by hand shifts every later index — there is no safe
        partial mapping).
        """
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        pcons = (_sync_props_target(rec, sub) or {}).get("constraints")
        if not isinstance(pcons, list) or not pcons:
            return
        live = list(getattr(obj, "Constraints", None) or [])
        if len(live) != len(pcons):
            return
        exprs = dict(getattr(obj, "ExpressionEngine", None) or [])
        updates = []
        for i, pc in enumerate(pcons):
            if not isinstance(pc, dict):
                return
            ctype = str(pc.get("type") or "")
            if sj.CONSTRAINT_TYPE_MAP.get(ctype) != getattr(live[i], "Type", None):
                return
            if ctype not in sj.DIMENSIONAL_CONSTRAINTS or "value" not in pc:
                continue
            expr = sj.constraint_expression(exprs, i, str(getattr(live[i], "Name", "") or ""))
            new = sj.map_constraint_value(
                ctype, pc.get("value"), getattr(live[i], "Value", None), expr
            )
            if new is sj.UNREADABLE:
                continue
            if not _same_scalar(pc.get("value"), new):
                updates.append((i, new))
        if not updates:
            return
        edits = []
        for i, new in updates:
            name = pcons[i].get("name") or getattr(live[i], "Name", "") or f"#{i}"
            edits.append((f"{obj.Name} constraint {name}", pcons[i]["value"], new))
            pcons[i]["value"] = new
        self._commit_sync(doc, records, rec, edits)

    def _sync_move_fold(self, obj, doc, records, index: int, sub: int | None) -> None:
        """Fold a manual drag of a move-owned object into its last move step.

        A move is RELATIVE, so the drag cannot be expressed against it — the
        owning obj_properties become an absolute placement override instead
        (the builder's placement wins over translate/rotate on re-run), which
        reproduces the current pose no matter what came before the move.
        ``sub`` routes the fold into a batch record's move sub-op.
        """
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        if sub is None:
            if rec.operation != "move":
                return
            parent = rec.params
        else:
            ops = (rec.params or {}).get("ops") or []
            if not (isinstance(sub, int) and 0 <= sub < len(ops) and isinstance(ops[sub], dict)):
                return
            if sj.sub_operation(ops[sub]) != "move":
                return
            parent = ops[sub]
        value = _placement_json(obj.Placement)
        current = (parent.get("obj_properties") or {}).get("placement")
        if _same_placement(current, value):
            return
        parent["obj_properties"] = {"placement": value}
        self._commit_sync(
            doc,
            records,
            rec,
            [(f"{obj.Name}.Placement", _placement_brief(current), _placement_brief(value))],
        )

    def _commit_sync(
        self,
        doc,
        records,
        rec,
        edits: list[tuple[str, Any, Any]],
        sketch: tuple[str, str] | None = None,
    ) -> None:
        self._writing = True
        try:
            write_journal(doc, records)
        finally:
            self._writing = False
        if sketch is not None:
            logger.info("journal sync: step %d sketch %s %s (manual edit)", rec.index, *sketch)
            note_user_change(
                doc.Name,
                {
                    "kind": "sketch",
                    "step": rec.index,
                    "label": rec.label,
                    "object": sketch[0],
                    "detail": sketch[1],
                },
            )
        for what, old, new in edits:
            logger.info("journal sync: step %d %s = %s (manual edit)", rec.index, what, new)
            note_user_change(
                doc.Name,
                {
                    "kind": "edit",
                    "step": rec.index,
                    "label": rec.label,
                    "what": what,
                    "old": old,
                    "new": new,
                },
            )


_OBSERVER_ATTR = "_cadpilot_journal_sync"


def install_sync_observer() -> None:
    """(Re)install the singleton observer; safe across hot reloads."""
    old = getattr(FreeCAD, _OBSERVER_ATTR, None)
    if old is not None:
        with contextlib.suppress(Exception):
            FreeCAD.removeDocumentObserver(old)
    observer = _JournalSyncObserver()
    FreeCAD.addDocumentObserver(observer)
    setattr(FreeCAD, _OBSERVER_ATTR, observer)


install_sync_observer()
