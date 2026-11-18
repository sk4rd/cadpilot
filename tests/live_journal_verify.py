"""Live verification of the review loop: the user edits, Claude continues.

Each scenario interleaves a simulated GUI edit with Claude's calls and checks
what the journal, the model and the reply Claude reads make of it. A GUI edit
is scheduled with ``QtCore.QTimer.singleShot`` from ``execute_code``, so it runs
after the RPC returned, outside its quiet window, the way a real property-panel
or sketcher edit arrives; the script then polls until it ran.

Needs a running FreeCAD with the add-on and ``CADPILOT_PORT`` set (there is no
default port on purpose: the script creates and closes documents).

Run: CADPILOT_PORT=<port> uv run --frozen python tests/live_journal_verify.py
"""

import json
import os
import sys
import tempfile
import textwrap
import time

from cadpilot import operations as ops
from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations.core import USER_CHANGES_HEADING

if not os.environ.get("CADPILOT_PORT"):
    sys.exit("set CADPILOT_PORT to the port of the FreeCAD to test against")
os.environ.setdefault("CADPILOT_HOME", tempfile.mkdtemp(prefix="cadpilot-live-journal-"))

c = FreeCADConnection()
FAILURES: list[str] = []
CHANGE_TEXTS: list[str] = []


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")
    return cond


def py(code, doc=None):
    res = c.execute_code(textwrap.dedent(code), doc_name=doc)
    if not res.get("success") and "modal dialog" in str(res.get("error")):
        # A FreeCAD that was killed with documents open greets the next start
        # with a Document Recovery dialog, which holds every queued call back.
        c.dismiss_blocking_dialog()
        time.sleep(1)
        res = c.execute_code(textwrap.dedent(code), doc_name=doc)
    if not res.get("success"):
        raise RuntimeError(f"snippet failed: {res.get('error')}")
    return str(res.get("message") or "").split("Output: ", 1)[-1].strip()


_EDITS = 0


def user(doc, body):
    """Run ``body`` the way a GUI edit runs: later, on the GUI thread, unmuted.

    ``d`` is the document inside ``body``. The body is bracketed like a GUI
    command (FreeCAD.closeActiveTransaction before and after): a read-only
    execute_code leaves its transaction name active in FreeCAD, and a change
    made without a transaction of its own would otherwise be filed under it.
    Waits until the edit has run and the observer's deferred reads settled.
    """
    global _EDITS
    _EDITS += 1
    token = f"edit{_EDITS}"
    code = (
        "import FreeCAD, Part, Sketcher\n"
        "from PySide import QtCore\n"
        "def _edit():\n"
        f"    d = FreeCAD.getDocument({doc!r})\n"
        "    FreeCAD.closeActiveTransaction()\n"
        "    try:\n" + textwrap.indent(textwrap.dedent(body), "        ") + "\n"
        "    finally:\n"
        "        FreeCAD.closeActiveTransaction()\n"
        f"        FreeCAD._cadpilot_live_edit = {token!r}\n"
        "QtCore.QTimer.singleShot(50, _edit)\n"
    )
    py(code, doc)
    for _ in range(100):
        time.sleep(0.1)
        done = py("import FreeCAD\nprint(getattr(FreeCAD, '_cadpilot_live_edit', ''))", doc)
        if done == token:
            time.sleep(0.3)
            return
    raise RuntimeError(f"the simulated GUI edit {token} never ran")


SHOTS = os.environ.get("CADPILOT_LIVE_SHOTS") or tempfile.mkdtemp(prefix="lj-shots-")


def panel(doc, action, select=None, answer="No", shot=None):
    """Press a Steps-panel button as the user would; answer the dialog it opens.

    Returns the text of every dialog the button opened and the panel's last
    log lines. ``shot`` saves the dialog (and the panel next to it) as PNGs.
    """
    shot_path = os.path.join(SHOTS, shot) if shot else ""
    user(
        doc,
        f"""
        import json, FreeCADGui
        from PySide import QtCore, QtWidgets
        from rpc_server import step_panel
        FreeCADGui.setActiveDocument({doc!r})
        pn = step_panel.ensure_panel()
        pn.setVisible(True)
        pn._digest = None
        pn.refresh()
        tree = pn.tree
        for i in range(tree.topLevelItemCount()):
            if tree.topLevelItem(i).text(0) == str({select!r}):
                tree.setCurrentItem(tree.topLevelItem(i))
        seen = []
        pressed = []
        def answer():
            if pressed:
                return
            w = QtWidgets.QApplication.activeModalWidget()
            if w is None:
                QtCore.QTimer.singleShot(50, answer)
                return
            seen.append(w.text() if hasattr(w, "text") else type(w).__name__)
            if {shot_path!r}:
                w.grab().save({shot_path!r})
            for b in w.buttons():
                if b.text().replace("&", "") == {answer!r}:
                    b.click()
                    return
            w.reject()
        QtCore.QTimer.singleShot(100, answer)
        getattr(pn, {action!r}).trigger()
        pressed.append(True)
        if {shot_path!r}:
            pn.grab().save({shot_path!r}.replace(".png", "-panel.png"))
        FreeCAD._cadpilot_live_panel = json.dumps(
            {{"dialogs": seen, "log": [e["text"] for e in pn._entries[-6:]]}}
        )
        """,
    )
    return json.loads(py("import FreeCAD\nprint(FreeCAD._cadpilot_live_panel)", doc))


def changes(doc):
    """The "User changes since your last call" block Claude's next reply leads with."""
    c.begin_tool_call()
    text = ops.user_changes_text(c, doc)
    if text:
        CHANGE_TEXTS.append(text)
    return text


def records(doc):
    return c.get_step_journal(doc).get("records") or []


def step_params(doc, op, nth=0):
    recs = [r for r in records(doc) if r["operation"] == op]
    return recs[nth]["params"].get("obj_properties") or {}


def cad(op, doc, name=None, desc="", obj_type=None, **props):
    text = "\n".join(
        x.text
        for x in ops.cad_operation(
            c,
            op,
            doc,
            obj_type=obj_type,
            obj_name=name,
            obj_properties=props,
            description=desc,
            auto_audit=False,
        )
    )
    try:
        return json.loads(text)
    except ValueError:
        return {"summary": text}


def control(doc, action, **kw):
    text = "\n".join(x.text for x in ops.step_control_operation(c, doc, action, **kw))
    try:
        return json.loads(text)
    except ValueError:
        return {"success": False, "error": text}


def fresh(doc):
    py(
        f"import FreeCAD\nif {doc!r} in FreeCAD.listDocuments(): FreeCAD.closeDocument({doc!r})\n"
        f"FreeCAD.newDocument({doc!r})\nFreeCAD.setActiveDocument({doc!r})"
    )
    changes(doc)  # drain whatever the setup itself produced


def sketch_state(doc, name):
    out = py(
        f"""
        import FreeCAD, json
        s = FreeCAD.getDocument({doc!r}).getObject({name!r})
        print(json.dumps({{
            "cons": [[k.Type, k.Name, round(float(k.Value), 4)] for k in s.Constraints],
            "geo": len(s.Geometry),
            "expr": sorted([str(a), str(b)] for a, b in s.ExpressionEngine),
        }}))
        """,
        doc,
    )
    return json.loads(out)


RECT = [
    {"type": "line", "from": [0, 0], "to": [60, 0]},
    {"type": "line", "from": [0, 5], "to": [0, 30]},
]


# --- A1: a constraint value edit keeps an unrelated NAMED binding -------------
D = "LJ_Cons"
fresh(D)
cad("variables", D, "Params", cells={"A1": ["Wd", 60], "A2": ["Ht", 30]})
cad(
    "sketch",
    D,
    "S",
    geometry=RECT,
    constraints=[
        {"type": "horizontal", "items": [0]},
        {"type": "vertical", "items": [1]},
        {"type": "distance", "items": [0], "value": "=Params.Wd", "name": "Width"},
        {"type": "distance", "items": [1], "value": 25},
    ],
)
changes(D)
user(D, "s = d.getObject('S'); s.setDatum(3, FreeCAD.Units.Quantity('35 mm')); d.recompute()")
cons = step_params(D, "sketch")["constraints"]
check("A1 the edited value syncs", cons[3]["value"] == 35, str(cons))
check("A1 the named binding stays an expression", cons[2]["value"] == "=Params.Wd", str(cons))
text = changes(D)
check(
    "A1 Claude is told about the constraint edit",
    USER_CHANGES_HEADING in text and "25 -> 35" in text,
    text,
)
check("A1 the change is delivered once", changes(D) == "")

# --- A2: deleting a constraint keeps every binding on its own constraint -----
D = "LJ_Del"
fresh(D)
cad("variables", D, "Params", cells={"A1": ["Wd", 60], "A2": ["Ht", 30]})
cad(
    "sketch",
    D,
    "S",
    geometry=RECT,
    constraints=[
        {"type": "horizontal", "items": [0]},
        {"type": "vertical", "items": [1]},
        {"type": "distance", "items": [0], "value": "=Params.Wd"},
        {"type": "distance", "items": [1], "value": "=Params.Ht"},
    ],
)
user(D, "s = d.getObject('S'); s.delConstraint(0); d.recompute()")
cons = step_params(D, "sketch")["constraints"]
values = [k.get("value") for k in cons if "value" in k]
check(
    "A2 no binding was rewritten by the deletion",
    "=Params.Wd" in values and "=Params.Ht" in values and 60 not in values and 30 not in values,
    str(cons),
)

check("A2 the deletion itself is in the step", len(cons) == 3, str(cons))
res = control(D, "replay", index=0, force=True)
live = sketch_state(D, "S")
check(
    "A2 replay keeps the deletion and drives each line by its own alias",
    res.get("success")
    and [k[0] for k in live["cons"]] == ["Vertical", "Distance", "Distance"]
    and sorted(live["expr"]) == [["Constraints[1]", "Params.Wd"], ["Constraints[2]", "Params.Ht"]],
    f"{res.get('error')} {live}",
)
changes(D)

# --- A3: a spreadsheet cell edit ---------------------------------------------
user(D, "v = d.getObject('Params'); v.set('A1', '70'); d.recompute()")
cells = step_params(D, "variables")["cells"]
check("A3 the cell edit syncs", cells["A1"] == ["Wd", 70], str(cells))
text = changes(D)
check("A3 Claude is told about the cell", "Params.A1 (Wd) 60 -> 70" in text, text)

# --- G: the user's own constraints survive a replay ---------------------------
D = "LJ_Sk"
fresh(D)
cad("variables", D, "Params", cells={"A1": ["Wd", 60]})
cad(
    "sketch",
    D,
    "S",
    geometry=RECT,
    constraints=[
        {"type": "horizontal", "items": [0]},
        {"type": "vertical", "items": [1]},
        {"type": "distance", "items": [0], "value": "=Params.Wd", "name": "Width"},
    ],
)
changes(D)
user(
    D,
    """
    s = d.getObject('S')
    d.openTransaction('user height')
    i = s.addConstraint(Sketcher.Constraint('Distance', 1, 25.0))
    s.renameConstraint(i, 'UserHeight')
    d.commitTransaction(); d.recompute()
    """,
)
rec = next(r for r in records(D) if r["operation"] == "sketch")
names = [k.get("name") for k in rec["params"]["obj_properties"]["constraints"]]
check("G the user's constraint is in the step", "UserHeight" in names, str(names))
check("G the step is flagged as edited by hand", rec["params"].get("user_edited") is True)
text = changes(D)
check("G Claude is told about the sketch edit", "+UserHeight" in text, text)
summary = control(D, "status", params={"summary": True})
check(
    "G status shows the edit is in the journal, not drift",
    summary.get("drift") is False and "edited by hand" in summary["steps"][1]["label"],
    str(summary.get("steps")),
)
control(D, "replay", index=0, force=True)
live = sketch_state(D, "S")
check(
    "G replay reproduces the user's sketch",
    ["Distance", "UserHeight", 25.0] in live["cons"]
    and [".Constraints.Width", "Params.Wd"] in live["expr"],
    str(live),
)
user(
    D,
    """
    s = d.getObject('S')
    d.openTransaction('point on line')
    s.addGeometry(Part.Point(FreeCAD.Vector(20, 0, 0)))
    s.addConstraint(Sketcher.Constraint('PointOnObject', 2, 1, 0))
    d.commitTransaction(); d.recompute()
    """,
)
rec = next(r for r in records(D) if r["operation"] == "sketch")
check(
    "G a constraint the spec cannot hold keeps FreeCAD's data",
    bool(rec["params"].get("live_sketch")),
)
control(D, "replay", index=0, force=True)
live = sketch_state(D, "S")
check(
    "G replay restores it exactly",
    live["geo"] == 3 and "PointOnObject" in [k[0] for k in live["cons"]],
    str(live),
)
changes(D)
user(D, "s = d.getObject('S'); s.touch(); d.recompute()")
check("G a recompute of the kept sketch is no user change", changes(D) == "", CHANGE_TEXTS[-1:])

# --- H: a feature the user deleted is stale, and drop accepts it ---------------
PLATE = {
    "geometry": [
        {"type": "line", "from": [0, 0], "to": [60, 0]},
        {"type": "line", "from": [60, 0], "to": [60, 30]},
        {"type": "line", "from": [60, 30], "to": [0, 30]},
        {"type": "line", "from": [0, 30], "to": [0, 0]},
    ],
    "constraints": [
        {"type": "coincident", "items": [[0, "end"], [1, "start"]]},
        {"type": "coincident", "items": [[1, "end"], [2, "start"]]},
        {"type": "coincident", "items": [[2, "end"], [3, "start"]]},
        {"type": "coincident", "items": [[3, "end"], [0, "start"]]},
    ],
}
HOLE = {
    "plane": {"face": ["Pad", "+Z"]},
    "geometry": [{"type": "circle", "center": [15, 15], "radius": 4}],
}


def plate(doc):
    fresh(doc)
    cad("variables", doc, "Vars", cells={"A1": ["thick", 8]})
    cad("sketch", doc, "Profile", desc="base profile", **PLATE)
    cad("pad", doc, "Profile", desc="plate pad", length="=Vars.thick")
    cad("sketch", doc, "HoleSk", desc="hole sketch", **HOLE)
    cad("pocket", doc, "HoleSk", desc="hole", through_all=True)
    changes(doc)


D = "LJ_Gone"
plate(D)
user(
    D,
    "d.openTransaction('Delete'); d.removeObject('Pocket'); d.commitTransaction(); d.recompute()",
)
status = control(D, "status", params={"summary": True})
check(
    "H a deleted feature makes its step stale",
    [x["index"] for x in status.get("stale") or []] == [5],
    str(status.get("stale")),
)
check("H ... and that is drift", status.get("drift") is True)
text = changes(D)
check(
    "H Claude is told the step is gone and how to accept it",
    "deleted Pocket" in text and "drop 5" in text,
    text,
)
res = control(D, "rollback_to", index=5)
check(
    "H a rollback refuses to re-create it silently",
    not res.get("success") and "drop" in str(res.get("error")),
    str(res),
)
count = len(records(D))
res = control(D, "drop", index=5)
check(
    "H drop forgets the step",
    res.get("success") and res["journal"]["count"] == count - 1 and len(records(D)) == count - 1,
    str(res),
)
status = control(D, "status", params={"summary": True})
check(
    "H no drift once the change is accepted", status.get("drift") is False, str(status.get("stale"))
)

# --- I: a user Ctrl+Z stays visible after Claude's next commit -----------------
D = "LJ_Undo"
plate(D)
user(D, "d.undo(); d.undo(); d.recompute()")  # Ctrl+Z twice: the pocket, then its sketch
text = changes(D)
check(
    "I Claude is told which steps the user undid",
    "Ctrl+Z undid step 5 (hole)" in text and "Ctrl+Z undid step 4" in text,
    text,
)
user(D, "d.redo(); d.recompute()")  # Ctrl+Y brings the sketch back
text = changes(D)
check("I Claude is told about the redo", "Ctrl+Y redid step 4" in text, text)
status = control(D, "status", params={"summary": True})
check(
    "I a redo makes the step whole again",
    [x["index"] for x in status.get("stale") or []] == [5],
    str(status.get("stale")),
)
cad("create_object", D, "Fixture", obj_type="Part::Box")
status = control(D, "status", params={"summary": True})
check(
    "I Claude's next commit does not clear it",
    status.get("drift") is True and [x["index"] for x in status.get("stale") or []] == [5],
    str(status.get("stale")),
)
res = control(D, "rollback_to", index=5)
check(
    "I a rollback across it refuses without force",
    not res.get("success") and "drop 5" in str(res.get("error")),
    str(res),
)
res = control(D, "rollback_to", index=5, force=True)
objects = py(
    "import FreeCAD\nprint(sorted(o.Name for o in FreeCAD.getDocument('LJ_Undo').Objects))", D
)
check(
    "I ... and re-creates it when forced",
    res.get("success") and "Pocket" in objects,
    f"{res} {objects}",
)

# --- J: rollback and reject never break the user's own feature silently -------
D = "LJ_Own"
plate(D)
user(
    D,
    """
    body = d.getObject('Body'); pk = d.getObject('Pocket')
    edges = [f'Edge{i + 1}' for i, e in enumerate(pk.Shape.Edges)
             if abs(e.BoundBox.ZMin - 8) < 1e-6 and abs(e.BoundBox.ZMax - 8) < 1e-6
             and e.Curve.TypeId == 'Part::GeomLine']
    d.openTransaction('Chamfer')
    ch = body.newObject('PartDesign::Chamfer', 'UserChamfer')
    ch.Base = (pk, edges)
    ch.Size = 1
    d.commitTransaction(); d.recompute()
    """,
)
text = changes(D)
check(
    "J Claude is told about the user's chamfer",
    "added UserChamfer (PartDesign::Chamfer)" in text,
    text,
)
cad("edit_object", D, "Vars", desc="rename vars", Label="Params")
before = py(
    "import FreeCAD\nprint(sorted(o.Name for o in FreeCAD.getDocument('LJ_Own').Objects))", D
)
dry = control(D, "rollback_to", index=3, params={"dry_run": True})
check(
    "J the dry run names the user's object it would break",
    dry.get("dry_run")
    and dry.get("user_dependents") == ["UserChamfer"]
    and "UserChamfer" in dry.get("summary", ""),
    str({k: dry.get(k) for k in ("mode", "remove", "user_dependents", "summary", "error")}),
)
res = control(D, "rollback_to", index=3)
check(
    "J a rollback under the user's feature refuses without force",
    not res.get("success") and "UserChamfer" in str(res.get("error")),
    str(res),
)
res = control(D, "reject", index=4)
check(
    "J a reject under it refuses too",
    not res.get("success") and "UserChamfer" in str(res.get("error")),
    str(res),
)
out = panel(D, "act_reject", select=4, answer="No", shot="reject-dialog.png")
check(
    "J the panel's reject dialog names the document, the dry run and the user's object",
    out["dialogs"] and "'LJ_Own'" in out["dialogs"][0] and "UserChamfer" in out["dialogs"][0],
    str(out),
)
out = panel(D, "act_rollback", select=5, answer="No", shot="rollback-dialog.png")
check(
    "J the rollback dialog shows the dry run instead of a transaction count",
    out["dialogs"] and "transaction(s)" in out["dialogs"][0] and "UserChamfer" in out["dialogs"][0],
    str(out),
)
panel(D, "act_accept", select=3)
text = changes(D)
check(
    "J Claude is told what the user pressed in the panel",
    "Steps panel: accepted step 3" in text,
    text,
)
user(
    D,
    """
    import FreeCADGui
    from rpc_server import step_panel
    FreeCADGui.setActiveDocument('LJ_Gone')
    pn = step_panel.ensure_panel()
    pn.refresh()
    FreeCAD._cadpilot_live_bound = (pn._doc_name, pn._selected_index(), len(pn._entries))
    FreeCADGui.setActiveDocument('LJ_Own')
    pn.refresh()
    FreeCAD._cadpilot_live_back = (pn._doc_name, [e["text"] for e in pn._entries][-1:])
    """,
)
bound = py("import FreeCAD\nprint(FreeCAD._cadpilot_live_bound, FreeCAD._cadpilot_live_back)", D)
check(
    "J the panel follows the shown document, with no row carried over and its own log",
    bound.startswith("('LJ_Gone', 0,") and "('LJ_Own', ['accept step 3']" in bound,
    bound,
)
control(D, "accept", index=3, params={"on": False})
after = py(
    "import FreeCAD\nprint(sorted(o.Name for o in FreeCAD.getDocument('LJ_Own').Objects))", D
)
valid = py(
    "import FreeCAD\nprint(FreeCAD.getDocument('LJ_Own').getObject('UserChamfer').isValid())", D
)
check(
    "J nothing was removed and the chamfer is intact",
    before == after and valid == "True",
    f"{before} {after} {valid}",
)
changes(D)

# --- K: a rebuild starts at the first wrong step, not at step 1 ----------------
D = "LJ_Rebuild"
fresh(D)
cad("variables", D, "Vars", cells={"A1": ["w", 10]})
cad("create_object", D, "Box", obj_type="Part::Box", Length=10)
cad("create_object", D, "Cyl", obj_type="Part::Cylinder")
cad("edit_object", D, "Box", Length=30)
user(
    D,
    "d.getObject('Vars').Label2 = 'kept'\n"
    "d.openTransaction('mine'); d.addObject('Part::Sphere', 'Mine'); d.commitTransaction()",
)
res = control(D, "rollback_to", index=3)
state = py(
    "import FreeCAD\nd = FreeCAD.getDocument('LJ_Rebuild')\n"
    "print(d.getObject('Vars').Label2, d.getObject('Box').Length.Value, "
    "sorted(o.Name for o in d.Objects))",
    D,
)
check(
    "K the edit the undo could not reach is rebuilt from the Box's own step",
    res.get("success") and res.get("restored") == "rebuild" and res.get("rebuild_from") == 2,
    str({k: res.get(k) for k in ("success", "restored", "rebuild_from", "removed", "error")}),
)
check(
    "K steps before it and the user's own object are untouched",
    state == "kept 10.0 ['Box', 'Cyl', 'Mine', 'Vars']",
    state,
)

# --- E2: a re-run with the user's own edit on top of the undo stack ------------
D = "LJ_Rerun"
fresh(D)
cad("create_object", D, "Lid", obj_type="Part::Box", Length=30, Width=20, Height=4)
user(D, "d.openTransaction('wider'); d.getObject('Lid').Width = 25; d.commitTransaction()")
res = control(D, "reexecute", index=1, params={"obj_properties": {"Height": 8}})
dims = py(
    "import FreeCAD\nb = FreeCAD.getDocument('LJ_Rerun').getObject('Lid')\n"
    "print(b.Length.Value, b.Width.Value, b.Height.Value)",
    D,
)
check(
    "E2 a re-run rebuilds past the user's edit and keeps it",
    res.get("success") and dims == "30.0 25.0 8.0",
    f"{dims} {res.get('error')} {res.get('restored')}",
)
changes(D)

# --- B: a session rollback goes through the journal ----------------------------
D = "LJ_Sess"
fresh(D)


def session(action, **kw):
    text = "\n".join(x.text for x in ops.session_action_operation(c, action, doc_name=D, **kw))
    try:
        return json.loads(text)
    except ValueError:
        return {"success": False, "error": text}


session("start", name="live session")
cad("create_object", D, "Base", obj_type="Part::Box")
cad("create_object", D, "Post", obj_type="Part::Cylinder")
steps = session("get_steps")["steps"]
check(
    "B every session step knows its journal step and undo entry",
    all(s["journal_index"] and s["transaction"].startswith("CADPilot: #") for s in steps),
    str(steps),
)
ops.step_plan_operation(
    c, D, [{"operation": "create_object", "obj_type": "Part::Sphere", "obj_name": "Knob"}]
)
control(D, "run_next")  # a step the session never saw
user(D, "d.openTransaction('wider'); d.getObject('Base').Width = 25; d.commitTransaction()")
res = session("rollback", to_step=1)
objects = py(
    "import FreeCAD\nprint(sorted(o.Name for o in FreeCAD.getDocument('LJ_Sess').Objects))", D
)
width = py("import FreeCAD\nprint(FreeCAD.getDocument('LJ_Sess').getObject('Base').Width.Value)", D)
check(
    "B rolling back to session step 1 removes the session's and step_control's later steps",
    res.get("success") and objects == "['Base']",
    f"{objects} {res}",
)
check("B ... and keeps the user's own edit", width == "25.0", width)
status = session("status")
check(
    "B session status counts the journal's steps and stays small",
    status.get("journal", {}).get("session_steps") == 1 and len(json.dumps(status)) < 2500,
    str(status.get("journal")),
)
changes(D)
session("complete")

# --- C: opening a saved file is not a burst of manual edits -------------------
D = "LJ_Open"
fresh(D)
cad("variables", D, "Vars", cells={"A1": ["thick", 8]})
cad(
    "sketch",
    D,
    "Profile",
    geometry=[
        {"type": "line", "from": [0, 0], "to": [40, 0]},
        {"type": "line", "from": [40, 0], "to": [40, 20]},
        {"type": "line", "from": [40, 20], "to": [0, 20]},
        {"type": "line", "from": [0, 20], "to": [0, 0]},
    ],
    constraints=[
        {"type": "coincident", "items": [[0, "end"], [1, "start"]]},
        {"type": "coincident", "items": [[1, "end"], [2, "start"]]},
        {"type": "coincident", "items": [[2, "end"], [3, "start"]]},
        {"type": "coincident", "items": [[3, "end"], [0, "start"]]},
    ],
)
cad("pad", D, "Profile", length="=Vars.thick")
before = records(D)
path = os.path.join(tempfile.mkdtemp(prefix="lj-"), "LJ_Open.FCStd")
py(
    f"""
    import FreeCAD
    FreeCAD.getDocument({D!r}).saveAs({path!r})
    FreeCAD.closeDocument({D!r})
    FreeCAD.openDocument({path!r})
    """,
    None,
)
time.sleep(0.5)
after = records(D)
check(
    "C reopening leaves every step's params as saved",
    [r["params"] for r in after] == [r["params"] for r in before],
    json.dumps([r["params"].get("obj_properties", {}).get("length") for r in after]),
)
check("C reopening is not reported as user changes", changes(D) == "", CHANGE_TEXTS[-1:])

# --- D: a read-only execute_code keeps a pending plan --------------------------
D = "LJ_Plan"
fresh(D)
cad("create_object", D, "Base", obj_type="Part::Box")
ops.step_plan_operation(
    c,
    D,
    [
        {"operation": "create_object", "obj_type": "Part::Cylinder", "obj_name": "Pin"},
        {"operation": "create_object", "obj_type": "Part::Sphere", "obj_name": "Knob"},
    ],
)
user(D, "d.openTransaction('user box'); d.addObject('Part::Box', 'UserBox'); d.commitTransaction()")
py("import FreeCAD\nprint(len(FreeCAD.getDocument('LJ_Plan').Objects))", D)
planned = [r for r in records(D) if r["state"] == "planned"]
check("D the plan survives a read-only execute_code", len(planned) == 2, str(planned))
text = changes(D)
check("D Claude is told the user added an object", "added UserBox (Part::Box)" in text, text)


# --- F: feature properties beyond length sync through their rules -------------
D = "LJ_Feat"
fresh(D)
SQUARE = {
    "geometry": [
        {"type": "line", "from": [0, 0], "to": [40, 0]},
        {"type": "line", "from": [40, 0], "to": [40, 40]},
        {"type": "line", "from": [40, 40], "to": [0, 40]},
        {"type": "line", "from": [0, 40], "to": [0, 0]},
    ],
    "constraints": [
        {"type": "coincident", "items": [[0, "end"], [1, "start"]]},
        {"type": "coincident", "items": [[1, "end"], [2, "start"]]},
        {"type": "coincident", "items": [[2, "end"], [3, "start"]]},
        {"type": "coincident", "items": [[3, "end"], [0, "start"]]},
    ],
}
cad("variables", D, "Vars", cells={"A1": ["gap", 3], "A2": ["n", 3]})
cad("sketch", D, "Base", **SQUARE)
cad("pad", D, "Base", length=8)
cad(
    "sketch",
    D,
    "Hole",
    plane={"face": ["Pad", "+Z"]},
    geometry=[{"type": "circle", "center": [8, 8], "radius": 2}],
)
cad("pocket", D, "Hole", length=3)
cad("pattern", D, "Pocket", pattern_type="linear", axis="X", spacing=10, count=2)
cad("datum_plane", D, "DP", plane="XY", offset=12)
changes(D)
user(
    D,
    """
    pad = d.getObject('Pad')
    d.openTransaction('two sides')
    pad.SideType = 'Two sides'
    pad.Length2 = 3
    d.commitTransaction(); d.recompute()
    """,
)
pad = step_params(D, "pad")
check(
    "F a two-sided pad syncs as the builder's two_lengths",
    pad.get("pad_type") == "two_lengths",
    str(pad),
)
check("F ... with the second length", pad.get("length2") == 3, str(pad))
user(D, "d.getObject('Pocket').Type = 'ThroughAll'; d.recompute()")
pocket = step_params(D, "pocket")
check("F a pocket switched to through-all syncs", pocket.get("through_all") is True, str(pocket))
pattern = [r for r in records(D) if r["operation"] == "pattern"]
if check("F the pattern step was built", pattern and pattern[0]["state"] == "done", str(pattern)):
    user(D, "d.getObject('LinearPattern').setExpression('Occurrences', 'Vars.n'); d.recompute()")
    params = step_params(D, "pattern")
    check(
        "F a bound pattern count syncs as an expression",
        params.get("count") == "=Vars.n"
        and set(params) == {"pattern_type", "axis", "spacing", "count"},
        str(params),
    )
user(
    D,
    "d.getObject('DP').setExpression('.AttachmentOffset.Base.z', 'Vars.gap * 4'); d.recompute()",
)
dp = step_params(D, "datum_plane")
check(
    "F a datum offset expression stays an expression",
    dp.get("offset") == "=Vars.gap*4",
    str(dp),
)
text = changes(D)
check(
    "F Claude is told about the feature edits",
    "Pad.SideType" in text and "Pocket.Type" in text and "=Vars.gap*4" in text,
    text,
)


# --- E: a partial update or re-run merges into the step ------------------------
D = "LJ_Merge"
fresh(D)
ops.step_plan_operation(
    c,
    D,
    [
        {
            "operation": "create_object",
            "obj_type": "Part::Box",
            "obj_name": "Lid",
            "obj_properties": {"Length": 30, "Width": 20, "Height": 4},
        }
    ],
)
res = control(D, "update", index=1, params={"obj_properties": {"Height": 6}})
check(
    "E update reports what it replaced", res.get("replaced") == ["obj_properties.Height"], str(res)
)
props = step_params(D, "create_object")
check(
    "E update keeps the properties it was not given",
    props == {"Length": 30, "Width": 20, "Height": 6},
    str(props),
)
control(D, "run_all")
res = control(D, "reexecute", index=1, params={"obj_properties": {"Height": 8}})
dims = py(
    "import FreeCAD\nb = FreeCAD.getDocument('LJ_Merge').getObject('Lid')\n"
    "print(b.Length.Value, b.Width.Value, b.Height.Value)",
    D,
)
check("E a partial re-run keeps the other properties", dims == "30.0 20.0 8.0", f"{dims} {res}")


py(
    "import FreeCAD\n"
    "for name in list(FreeCAD.listDocuments()):\n"
    "    if name.startswith(('LJ_', 'lj_')):\n"
    "        FreeCAD.closeDocument(name)\n"
)
print()
if CHANGE_TEXTS:
    print("Example of what Claude reads:\n" + max(CHANGE_TEXTS, key=lambda t: t.count("\n- ")))
with open(os.path.join(SHOTS, "user-changes.txt"), "w", encoding="utf-8") as out:
    out.write("\n\n".join(CHANGE_TEXTS))
print(f"\n{len(FAILURES)} failure(s)")
for failure in FAILURES:
    print(" -", failure)
sys.exit(1 if FAILURES else 0)
