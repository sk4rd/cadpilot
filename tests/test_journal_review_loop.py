"""The review loop's pure half: what the journal does with the user's own edits."""

import ast
import asyncio
import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import step_journal as sj  # noqa: E402


def test_a_named_constraint_binding_is_found_by_name():
    exprs = {".Constraints.Width": "Params.Wd", "Constraints[3]": "Params.Ht"}
    assert sj.constraint_expression(exprs, 2, "Width") == "Params.Wd"
    assert sj.constraint_expression(exprs, 3, "") == "Params.Ht"
    assert sj.constraint_expression(exprs, 1, "") is None
    assert sj.constraint_expression(exprs, 2, "") is None


def test_a_named_binding_survives_an_edit_of_another_constraint():
    exprs = {".Constraints.Width": "Params.Wd"}
    expr = sj.constraint_expression(exprs, 2, "Width")
    assert sj.map_constraint_value("distance", "=Params.Wd", 80.0, expr) == "=Params.Wd"


def _engine_func(name):
    tree = ast.parse((_ADDON / "rpc_server" / "step_engine.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_observer_ignores_a_document_being_restored():
    for name in ("slotChangedObject", "sync_sketch"):
        attrs = {n.attr for n in ast.walk(_engine_func(name)) if isinstance(n, ast.Attribute)}
        assert "Restoring" in attrs, f"{name} must skip a document that is being opened"


# --- what the user changed ---------------------------------------------------


def _edit(old, new, what="Pad.Length", step=3):
    return {
        "kind": "edit",
        "step": step,
        "label": "plate pad",
        "what": what,
        "old": old,
        "new": new,
    }


def test_repeated_edits_of_one_value_fold_into_one():
    log = []
    sj.append_change(log, _edit(8, 10))
    sj.append_change(log, {"kind": "added", "objects": ["Box"], "types": ["Part::Box"]})
    sj.append_change(log, _edit(10, 12))
    assert [e["kind"] for e in log] == ["edit", "added"]
    assert (log[0]["old"], log[0]["new"]) == (8, 12)


def test_an_edit_and_its_own_undo_cancel_out():
    log = []
    sj.append_change(log, _edit(8, 12))
    sj.append_change(log, _edit(12, 8))
    assert log == []


def test_an_undo_is_a_boundary_edits_do_not_fold_across():
    log = []
    sj.append_change(log, _edit(8, 12))
    sj.append_change(log, {"kind": "undo", "transaction": "CADPilot: #4 pad", "step": 4})
    sj.append_change(log, _edit(12, 14))
    assert [e["kind"] for e in log] == ["edit", "undo", "edit"]


def test_an_object_added_and_deleted_again_cancels_out():
    log = []
    sj.append_change(log, {"kind": "added", "objects": ["A", "B"], "types": ["T", "U"]})
    sj.append_change(log, {"kind": "removed", "objects": ["A", "Pocket"]})
    assert log == [
        {"kind": "added", "objects": ["B"], "types": ["U"]},
        {"kind": "removed", "objects": ["Pocket"]},
    ]


def test_consecutive_deletions_merge():
    log = []
    sj.append_change(log, {"kind": "removed", "objects": ["Pocket"]})
    sj.append_change(log, {"kind": "removed", "objects": ["HoleSk"]})
    assert log == [{"kind": "removed", "objects": ["Pocket", "HoleSk"]}]


def test_panel_actions_read_as_what_the_user_did():
    assert sj.panel_action_text({"operation": "rollback_to", "index": 3}) == (
        "rolled back to step 3"
    )
    assert sj.panel_action_text({"operation": "reject", "index": 5}) == (
        "rejected step 5 and everything after it"
    )
    line = sj.describe_change({"kind": "panel", "action": "replayed the journal", "ok": False})
    assert line == "Steps panel: replayed the journal (failed)"


def test_the_user_changes_block_is_compact_and_names_each_change():
    from cadpilot.operations.core import USER_CHANGES_HEADING, format_user_changes

    text = format_user_changes(
        {
            "Plate": [
                _edit(8, 12),
                {"kind": "undo", "transaction": "CADPilot: #7 pocket", "step": 5, "label": "hole"},
                {"kind": "added", "objects": ["UserChamfer"], "types": ["PartDesign::Chamfer"]},
            ],
            "Empty": [],
        }
    )
    lines = text.splitlines()
    assert lines[0] == f"## {USER_CHANGES_HEADING}"
    assert lines[1] == "Document 'Plate':"
    assert lines[2] == "- step 3 (plate pad): Pad.Length 8 -> 12"
    assert lines[3].startswith("- Ctrl+Z undid step 5 (hole)") and "drop 5" in lines[3]
    assert lines[4] == "- added UserChamfer (PartDesign::Chamfer)"
    assert "Empty" not in text
    assert format_user_changes({}) == ""


def test_every_freecad_tool_reply_leads_with_the_user_changes(fake_freecad, monkeypatch):
    from cadpilot import server

    monkeypatch.setattr(server.state, "freecad_connection", fake_freecad)
    fake_freecad.documents = ["Plate"]
    fake_freecad.pending_user_changes = {"Plate": [_edit(8, 12)]}
    content = asyncio.run(server.mcp.call_tool("list_documents", {}))
    blocks = content[0] if isinstance(content, tuple) else content
    texts = [b.text for b in blocks]
    assert len(texts) == 1 + 1
    assert texts[0].startswith("## User changes since your last call")
    assert "Pad.Length 8 -> 12" in texts[0]
    # Delivered once: the next call is quiet.
    content = asyncio.run(server.mcp.call_tool("list_documents", {}))
    blocks = content[0] if isinstance(content, tuple) else content
    assert not blocks[0].text.startswith("## User changes")
