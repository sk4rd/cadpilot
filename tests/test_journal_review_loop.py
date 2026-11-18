"""The review loop's pure half: what the journal does with the user's own edits."""

import ast
import asyncio
import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from cadpilot_rpc import step_journal as sj  # noqa: E402


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
    tree = ast.parse((_ADDON / "cadpilot_rpc" / "step_engine.py").read_text(encoding="utf-8"))
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


def _blocks(result):
    """The content blocks of a call_tool result: a list or tuple on MCP 1.x, a CallToolResult on 2.x."""
    if hasattr(result, "content"):
        return result.content
    return result[0] if isinstance(result, tuple) else result


def test_every_freecad_tool_reply_leads_with_the_user_changes(fake_freecad, monkeypatch):
    from cadpilot import server

    monkeypatch.setattr(server.state, "freecad_connection", fake_freecad)
    fake_freecad.documents = ["Plate"]
    fake_freecad.pending_user_changes = {"Plate": [_edit(8, 12)]}
    content = asyncio.run(server.mcp.call_tool("list_documents", {}))
    blocks = _blocks(content)
    texts = [b.text for b in blocks]
    assert len(texts) == 1 + 1
    assert texts[0].startswith("## User changes since your last call")
    assert "Pad.Length 8 -> 12" in texts[0]
    # Delivered once: the next call is quiet.
    content = asyncio.run(server.mcp.call_tool("list_documents", {}))
    blocks = _blocks(content)
    assert not blocks[0].text.startswith("## User changes")


# --- feature sync is data-driven ---------------------------------------------


def _patch(op, prop, live, expr=None, base=""):
    return sj.sync_patch(sj.feature_rule(op, prop), live, expr, base)


def test_every_feature_rule_has_a_converter():
    kinds = {rule.kind for rules in sj.FEATURE_SYNC.values() for rule in rules.values()}
    known = {
        "value",
        "int",
        "spacings",
        "pattern_mode",
        "pad_mode",
        "link_sub",
        "names",
        "axis",
        "mirror_plane",
        "z_offset",
        "edge_size",
    }
    assert kinds <= known


def _mode(fc_type="Length", side="One side", midplane=False):
    return {"type": fc_type, "side": side, "midplane": midplane}


def test_pad_and_pocket_sync_their_mode_as_the_builder_reads_it():
    assert _patch("pocket", "Type", _mode("ThroughAll")) == {
        "midplane": sj.REMOVE,
        "length2": sj.REMOVE,
        "through_all": True,
        "pad_type": sj.REMOVE,
        "up_to_face": sj.REMOVE,
    }
    # FreeCAD 1.1 keeps two lengths in SideType, the builder in pad_type.
    assert _patch("pad", "SideType", _mode(side="Two sides")) == {
        "pad_type": "two_lengths",
        "through_all": sj.REMOVE,
        "midplane": sj.REMOVE,
        "up_to_face": sj.REMOVE,
    }
    assert _patch("pad", "Midplane", _mode(side="Symmetric", midplane=True))["midplane"] is True
    up = _patch("pocket", "Type", _mode("UpToFace"))
    assert up["pad_type"] == "up_to_face" and "up_to_face" not in up
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
    assert _patch("pattern", "Length", 30.0) == {"length": 30.0}
    assert _patch("pattern", "Mode", "Extent") == {"mode": "extent", "spacings": sj.REMOVE}
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
    assert sj.patch_value_matches(rule, "midplane", None, sj.REMOVE)


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


def test_merging_params_keeps_what_the_caller_did_not_mention():
    old = {"obj_name": "Profile", "obj_properties": {"length": 8, "reversed": True, "x": [1]}}
    merged, replaced = sj.merge_params(
        old, {"obj_properties": {"length": 12, "midplane": True, "x": [2], "reversed": None}}
    )
    assert merged == {
        "obj_name": "Profile",
        "obj_properties": {"length": 12, "midplane": True, "x": [2]},
    }
    assert replaced == ["obj_properties.length", "obj_properties.x", "obj_properties.reversed"]
    assert old["obj_properties"]["length"] == 8


# --- the user's own sketch edits ---------------------------------------------


def _con(ctype, first, first_pos=0, second=sj.GEO_UNDEF, second_pos=0, value=0.0, name=""):
    return {
        "type": ctype,
        "first": first,
        "first_pos": first_pos,
        "second": second,
        "second_pos": second_pos,
        "third": sj.GEO_UNDEF,
        "third_pos": 0,
        "value": value,
        "name": name,
        "driving": True,
        "active": True,
    }


LINES = [
    {"type": "LineSegment", "start": [0, 0], "end": [60.0000001, 0]},
    {"type": "LineSegment", "start": [0, 5], "end": [0, 30]},
]


def test_a_live_sketch_becomes_its_spec_again():
    cons = [
        _con("Horizontal", 0),
        _con("Vertical", 1),
        _con("Distance", 0, value=60.0, name="Width"),
        _con("Distance", 1, value=25.0, name="UserHeight"),
        _con("Coincident", 0, 1, -1, 1),
        _con("DistanceX", 1, 1, value=0.0),
    ]
    spec, unsupported = sj.sketch_spec_from_live(
        LINES, [False, True], cons, {".Constraints.Width": "Params.Wd"}
    )
    assert unsupported == []
    assert spec["geometry"][0] == {"type": "line", "from": [0, 0], "to": [60, 0]}
    assert spec["construction"] == [1]
    assert spec["constraints"] == [
        {"type": "horizontal", "items": [0]},
        {"type": "vertical", "items": [1]},
        {"type": "distance", "items": [0], "value": "=Params.Wd", "name": "Width"},
        {"type": "distance", "items": [1], "value": 25, "name": "UserHeight"},
        {"type": "coincident", "items": [[0, "start"], [-1, "start"]]},
        {"type": "distance_x", "items": [[1, "start"]], "value": 0},
    ]


def test_what_the_spec_cannot_express_is_named():
    cons = [_con("PointOnObject", 1, 1, 0), {**_con("Distance", 0, value=5.0), "driving": False}]
    _spec, unsupported = sj.sketch_spec_from_live(
        [*LINES, {"type": "Ellipse"}], [False] * 3, cons, {}
    )
    assert unsupported == [
        "geometry 2 (Ellipse)",
        "constraint #0 (PointOnObject)",
        "constraint #1 (Distance)",
    ]


def test_the_structure_signature_ignores_dimensions_but_not_references():
    base = [_con("Distance", 0, value=60.0)]
    sig = sj.sketch_signature(LINES, [False, False], base, [])
    assert sj.sketch_signature(LINES, [False, False], [_con("Distance", 0, value=80.0)], []) == sig
    assert sj.sketch_signature(LINES, [False, False], [_con("Distance", 1, value=60.0)], []) != sig
    assert sj.sketch_signature(LINES, [False, True], base, []) != sig


def test_a_sketch_edit_names_what_changed():
    old = {"geometry": [1, 2], "constraints": [{"type": "horizontal"}]}
    new = {"geometry": [1, 2], "constraints": [{"type": "horizontal"}, {"name": "UserHeight"}]}
    assert sj.describe_sketch_edit(old, new) == "constraints 1 -> 2 (+UserHeight)"


def test_a_hand_edited_step_says_so_in_its_row():
    rec = sj.StepRecord(index=2, operation="sketch", label="base profile")
    rec.params = {"user_edited": True}
    assert sj.row_text(rec) == "base profile (edited by hand)"


# --- stale steps --------------------------------------------------------------


def _done(index, op, before, after, tx=None, **kw):
    return sj.StepRecord(
        index=index,
        state=sj.STATE_DONE,
        operation=op,
        transaction=tx if tx is not None else f"CADPilot: #{index} {op}",
        objects_before=before,
        objects_after=after,
        **kw,
    )


def _chain():
    return [
        _done(1, "variables", [], ["Vars"]),
        _done(2, "sketch", ["Vars"], ["Body", "Profile", "Vars"]),
        _done(3, "pad", ["Body", "Profile", "Vars"], ["Body", "Pad", "Profile", "Vars"]),
        _done(
            4,
            "pocket",
            ["Body", "Pad", "Profile", "Vars"],
            ["Body", "Pad", "Pocket", "Profile", "Vars"],
        ),
    ]


def test_every_done_step_is_checked_not_just_the_last():
    recs = _chain()
    undo = [r.transaction for r in reversed(recs)]
    present = ["Body", "Pad", "Profile", "Vars"]  # the user deleted the Pocket
    assert sj.stale_steps(recs, undo, [], present) == [
        {"index": 4, "reason": "deleted by the user: Pocket", "missing": ["Pocket"]}
    ]
    recs.append(_done(5, "create_object", present, [*present, "Fixture"]))
    assert [s["index"] for s in sj.stale_steps(recs, undo, [], [*present, "Fixture"])] == [4]


def test_an_undone_step_stays_stale_after_the_next_commit():
    recs = _chain()
    present = ["Body", "Profile", "Vars"]  # Ctrl+Z took the pad (and the pocket with it)
    redo = [recs[2].transaction]
    stale = sj.stale_steps(recs, [r.transaction for r in recs[:2]], redo, present)
    assert stale[0] == {"index": 3, "reason": "undone by the user (Ctrl+Z)", "missing": ["Pad"]}
    # A commit cleared the redo stack; the record still remembers the undo.
    recs[2].stale = "undone by the user (Ctrl+Z)"
    assert [s["index"] for s in sj.stale_steps(recs, [], [], present)] == [3, 4]


def test_an_object_a_later_step_deleted_is_not_stale():
    recs = _chain()[:3]
    recs.append(
        _done(4, "delete_object", ["Body", "Pad", "Profile", "Vars"], ["Body", "Profile", "Vars"])
    )
    assert sj.stale_steps(recs, [], [], ["Body", "Profile", "Vars"]) == []


def test_dropping_accepts_the_users_change():
    recs = _chain()
    assert sj.drop_step(recs, 3).operation == "pad"
    assert [(r.index, r.operation) for r in recs] == [
        (1, "variables"),
        (2, "sketch"),
        (3, "pocket"),
    ]
    assert sj.drop_step(recs, 9) is None


def test_dropping_a_planned_or_failed_step_takes_it_off_the_list():
    recs = _chain()
    recs.append(sj.StepRecord(index=5, state=sj.STATE_FAILED, operation="fillet"))
    recs.append(sj.StepRecord(index=6, state=sj.STATE_PLANNED, operation="chamfer"))
    assert sj.drop_step(recs, 5).operation == "fillet"
    assert sj.drop_step(recs, 5).operation == "chamfer"
    assert [r.operation for r in recs][-1] == "pocket"


def test_a_step_recorded_after_a_failure_goes_before_it():
    """The fix for a failed step (the Body it needed) must replay before it."""
    recs = [
        sj.StepRecord(index=1, state=sj.STATE_DONE, operation="variables"),
        sj.StepRecord(index=2, state=sj.STATE_FAILED, operation="sketch"),
        sj.StepRecord(index=3, state=sj.STATE_PLANNED, operation="pad"),
    ]
    body = sj.StepRecord(index=0, state=sj.STATE_DONE, operation="create_object")
    assert sj.insert_before_plan(recs, body) == 2
    sj.reindex(recs)
    assert [(r.operation, r.state) for r in recs] == [
        ("variables", "done"),
        ("create_object", "done"),
        ("sketch", "failed"),
        ("pad", "planned"),
    ]
    assert sj.next_runnable(recs).operation == "sketch"
    assert "steps 3-4" in sj.plan_shift_note(recs, body, 2)


def test_a_stale_step_claims_no_undo():
    recs = _chain()
    plan = sj.plan_rollback(recs, 2, skip={4})
    assert plan["undo_count"] == 1 and plan["affected"] == [3, 4]
    names = [recs[2].transaction]  # the pocket's entry already left the stack
    assert sj.undo_trust_span(names, recs, 2, 1, skip={4}) == 1
    assert sj.plan_reject(recs, 3, skip={4})["undo_count"] == 1


def test_creator_of_names_the_step_that_built_an_object():
    assert sj.creator_of(_chain(), "Pocket").index == 4
    assert sj.creator_of(_chain(), "Body").index == 2
    assert sj.creator_of(_chain(), "UserChamfer") is None


# --- what a rollback will do ---------------------------------------------------


def _plate_chain():
    recs = _chain()
    recs.append(
        _done(
            5,
            "edit_object",
            ["Body", "Pad", "Pocket", "Profile", "Vars"],
            ["Body", "Pad", "Pocket", "Profile", "Vars"],
            params={"obj_name": "Pad"},
        )
    )
    return recs


ALL = ["Body", "Pad", "Pocket", "Profile", "Vars"]


def test_a_clean_stack_rolls_back_natively():
    plan = sj.plan_restore(_plate_chain(), 3, 2, ALL)
    assert plan["mode"] == "native" and plan["undo"] == 2
    assert plan["remove"] == [] and plan["undone_objects"] == ["Pocket"]


def test_a_short_stack_rebuilds_from_the_first_wrong_step_not_from_one():
    # Only the edit's transaction can be undone; the pocket (step 4) cannot,
    # so its Pocket is a leftover and nothing before it needs re-running.
    plan = sj.plan_restore(_chain(), 3, 0, ALL)
    assert plan["mode"] == "removed" and plan["remove"] == ["Pocket"] and plan["unreached"] == [4]
    edited = _plate_chain()
    edited[4].params = {"code": "..."}
    edited[4].operation = "execute_code"
    assert sj.plan_restore(edited, 4, 0, ALL)["mode"] == "partial"
    # An edit the undo cannot reach changed the Pad: rebuild from the pad.
    plan = sj.plan_restore(_plate_chain(), 4, 0, ALL)
    assert plan["mode"] == "rebuild" and plan["rebuild_from"] == 3 and plan["rerun"] == [3, 4]
    assert plan["remove"] == ["Pad", "Pocket"]


def test_an_object_the_user_deleted_is_rebuilt_from_its_own_step():
    present = ["Body", "Pad", "Profile", "Vars", "Pocket"]
    present.remove("Pad")
    plan = sj.plan_restore(_chain(), 4, 0, present, stale={3})
    assert plan["mode"] == "rebuild" and plan["rebuild_from"] == 3
    assert "Body" not in plan["remove"] and "Vars" not in plan["remove"]


def test_the_dry_run_reads_as_what_will_happen():
    plan = sj.plan_restore(_plate_chain(), 4, 0, ALL)
    text = sj.restore_summary(plan, ["UserChamfer"])
    assert text.startswith("Then rebuild from step 3: remove 2 object(s) and re-run step(s) 3..4.")
    assert "UserChamfer" in text
    assert sj.restore_summary(sj.plan_restore(_chain(), 4, 0, ALL), []) == "Nothing to undo."


def test_a_later_sketch_edit_step_owns_the_sketch_structure():
    sketch = _done(2, "sketch", ["Vars"], ["Body", "Profile", "Vars"])
    sketch.params = {"obj_name": "Profile", "obj_properties": {"geometry": [], "constraints": []}}
    edit = _done(3, "sketch_edit", ["Body", "Profile", "Vars"], ["Body", "Profile", "Vars"])
    edit.params = {"obj_name": "Profile", "obj_properties": {"delete_constraints": [0]}}
    assert sj.tracked_objects([sketch])["Profile"]["sketch"] == (2, None)
    assert sj.tracked_objects([sketch, edit])["Profile"]["sketch"] == (3, None)
