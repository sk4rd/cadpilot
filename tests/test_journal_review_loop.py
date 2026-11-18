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


# --- feature sync is data-driven ---------------------------------------------


def _patch(op, prop, live, expr=None, base=""):
    return sj.sync_patch(sj.feature_rule(op, prop), live, expr, base)


def test_every_feature_rule_has_a_converter():
    kinds = {rule.kind for rules in sj.FEATURE_SYNC.values() for rule in rules.values()}
    known = {
        "value",
        "int",
        "enum",
        "spacings",
        "extent_spacing",
        "pad_type",
        "link_sub",
        "names",
        "axis",
        "mirror_plane",
        "z_offset",
        "edge_size",
    }
    assert kinds <= known


def test_pad_and_pocket_sync_their_type_and_second_length():
    assert _patch("pocket", "Type", "ThroughAll") == {"through_all": True, "pad_type": sj.REMOVE}
    assert _patch("pad", "Type", "TwoLengths") == {
        "pad_type": "two_lengths",
        "through_all": sj.REMOVE,
    }
    assert _patch("pad", "Length2", 4.5) == {"length2": 4.5}
    assert _patch("pocket", "Offset", 1) == {"offset": 1}
    assert _patch("pocket", "UpToFace", {"name": "Pad", "subs": ["Face6"]}) == {
        "up_to_face": ["Pad", "Face6"]
    }


def test_pattern_and_mirror_properties_sync():
    assert _patch("pattern", "Occurrences", 6) == {"count": 6}
    assert _patch("pattern", "Occurrences", 6, expr="Vars.n") == {"count": "=Vars.n"}
    assert _patch("pattern", "Spacings", [10.0, 12.5]) == {"spacings": [10, 12.5]}
    assert _patch("pattern", "Spacings", [-1.0, -1.0]) == {"spacings": sj.REMOVE}
    assert _patch("pattern", "Length", (30.0, 4)) == {"spacing": 10}
    assert _patch("pad", "SideType", "Two sides") == {"side_type": "two_sides"}
    assert _patch("pattern", "Originals", ["Pocket", "Pocket001"], base="Pocket") == {
        "originals": ["Pocket001"]
    }
    axis = {"name": "Sketch", "subs": ["V_Axis"], "role": "", "type": "Sketcher::SketchObject"}
    assert _patch("pattern", "Direction", axis) == {"axis": "V"}
    origin = {"name": "Z_Axis", "subs": [""], "role": "Z_Axis", "type": "App::Line"}
    assert _patch("pattern", "Axis", origin) == {"axis": "Z"}
    plane = {"name": "YZ_Plane", "subs": [""], "role": "YZ_Plane", "type": "App::Plane"}
    assert _patch("mirror", "MirrorPlane", plane) == {"plane": "YZ", "face": sj.REMOVE}
    face = {"name": "Pad", "subs": ["Face3"], "role": "", "type": "PartDesign::Pad"}
    assert _patch("mirror", "MirrorPlane", face) == {"face": ["Pad", "Face3"], "plane": sj.REMOVE}


def test_rules_are_limited_to_the_object_they_describe():
    offset = sj.feature_rule("pattern", "Offset")
    assert offset.only == ("PartDesign::LinearPattern",) and offset.when == ("Mode", "Spacing")


def test_a_datum_offset_expression_stays_an_expression():
    assert _patch("datum_plane", "AttachmentOffset", (0, 0, 5, 0)) == {"offset": 5}
    assert _patch("datum_plane", "AttachmentOffset", (0, 0, 5, 0), expr="Vars.gap") == {
        "offset": "=Vars.gap"
    }
    assert _patch("datum_plane", "AttachmentOffset", (1, 0, 5, 0)) is None


def test_a_value_equal_to_the_builder_default_writes_nothing():
    rule = sj.feature_rule("pad", "Reversed")
    assert sj.patch_value_matches(rule, "reversed", None, False)
    assert not sj.patch_value_matches(rule, "reversed", None, True)
    rule = sj.feature_rule("pocket", "Type")
    assert sj.patch_value_matches(rule, "pad_type", None, sj.REMOVE)
    assert sj.patch_value_matches(rule, "pad_type", None, "length")
    assert not sj.patch_value_matches(rule, "through_all", None, True)


def test_feature_claims_follow_the_rules():
    rec = sj.StepRecord(
        index=4,
        operation="pattern",
        params={"obj_name": "Pocket", "obj_properties": {"count": 4}},
        objects_before=["Pocket"],
        objects_after=["Pocket", "LinearPattern"],
    )
    props = sj.tracked_objects([rec])["LinearPattern"]["props"]
    assert props["Occurrences"] == (4, "count", None)
    assert props["Originals"] == (4, "originals", None)
