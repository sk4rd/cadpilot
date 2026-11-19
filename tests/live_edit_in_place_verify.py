"""Live regression for changing a part that already exists, the way a person does.

A session that converted nine printer parts rebuilt whole documents to change them, because the
tools could not: reading a two-body part cost 111 KB, a sheet value needed its cell address, a
feature could only go at the end of the history, and a screenshot showed the sketches and marked
the document changed. This builds a small plate and then edits it in place.

    CADPILOT_PORT=9879 uv run --frozen python tests/live_edit_in_place_verify.py
"""

import json
import math
import sys

from live_port import require_test_port

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import (
    cad_operation,
    get_objects_operation,
    get_view_operation,
    save_document_operation,
)

require_test_port()
c = FreeCADConnection()
FAILURES: list[str] = []
DOC = "EditInPlace"


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def text(res):
    return "\n".join(getattr(r, "text", str(r)) for r in res)


def cad(operation, obj_name=None, **props):
    return text(cad_operation(c, operation, DOC, obj_name=obj_name, obj_properties=props or None))


def run(code):
    res = c.execute_code(code, doc_name=DOC)
    if not res.get("success"):
        raise RuntimeError(res.get("error") or res)
    out = str(res.get("message") or res.get("output") or "")
    return out.split("Output:")[-1].strip() if "Output:" in out else out


def volume(body="Plate"):
    return float(run(f"print(App.getDocument({DOC!r}).getObject({body!r}).Shape.Volume)"))


def order(body="Plate"):
    code = (
        f"b = App.getDocument({DOC!r}).getObject({body!r})\n"
        "print(','.join(o.Name for o in b.Group if o.TypeId != 'App::Origin'), '|', b.Tip.Name)"
    )
    names, tip = run(code).split(" | ")
    return names.split(","), tip


def solids(names):
    return [n for n in names if not n.endswith("Sketch")]


def rect(x0, y0, x1, y1):
    return {"type": "polyline", "closed": True, "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def rect_constraints(x, y, w, h):
    return [
        {"type": "horizontal", "items": [0, 2]},
        {"type": "vertical", "items": [1, 3]},
        {"type": "distance_x", "items": [[-1, "start"], [0, "start"]], "value": x, "name": "X"},
        {"type": "distance_y", "items": [[-1, "start"], [0, "start"]], "value": y, "name": "Y"},
        {"type": "distance", "items": [0], "value": w, "name": "Width"},
        {"type": "distance", "items": [1], "value": h, "name": "Depth"},
    ]


SETUP = c.execute_code(
    f"if {DOC!r} in App.listDocuments():\n    App.closeDocument({DOC!r})\nApp.newDocument({DOC!r})"
)
if not SETUP.get("success"):
    sys.exit(f"could not start a document: {SETUP}")

# --- a small part: a plate, a hole through it, a second body beside it ---------------------
cad("variables", "Parameters", rows=[["Length", 60, "plate length"], ["Thick", 5, "plate"]])
cad(
    "sketch",
    "PlateSketch",
    body="Plate",
    plane="XY",
    geometry=[rect(0, 0, 60, 40)],
    constraints=rect_constraints(0, 0, "=Parameters.Length", 40),
)
cad("pad", "PlateSketch", name="Base", length="=Parameters.Thick")
cad(
    "sketch",
    "HoleSketch",
    body="Plate",
    plane="XY",
    geometry=[{"type": "circle", "center": [10, 10], "radius": 3}],
    constraints=[
        {"type": "radius", "items": [0], "value": 3, "name": "HoleR"},
        {"type": "distance_x", "items": [[-1, "start"], [0, "center"]], "value": 10, "name": "HX"},
        {"type": "distance_y", "items": [[-1, "start"], [0, "center"]], "value": 10, "name": "HY"},
    ],
)
cad("pocket", "HoleSketch", name="Hole", through_all=True)
cad(
    "sketch",
    "BlockSketch",
    body="Block",
    plane="XY",
    geometry=[rect(100, 0, 110, 10)],
    constraints=rect_constraints(100, 0, 10, 10),
)
cad("pad", "BlockSketch", name="BlockPad", length=10)
hole = math.pi * 9 * 5
check("the part is built", abs(volume() - (60 * 40 * 5 - hole)) < 0.1, str(volume()))

# --- reading: the tree, a sketch, the sheet -------------------------------------------------
tree = text(get_objects_operation(c, DOC))
check("the tree is a few hundred bytes, not a property dump", len(tree) < 2500, str(len(tree)))
for needle in (
    "Parameters (Spreadsheet, 2 aliases)",
    "Length = 60",
    "Plate (Body: 2 features, tip Hole",
    "Base",
    "Length 5 = Parameters.Thick",
    "Pocket Type ThroughAll",
    "selected: nothing",
):
    check(f"the tree shows {needle!r}", needle in tree, tree)
sketch = text(get_objects_operation(c, DOC, "PlateSketch"))
for needle in (
    "0 line (0, 0) -> (60, 0)",
    "Width: Distance 60 = Parameters.Length",
    "used by Base",
):
    check(f"the sketch shows {needle!r}", needle in sketch, sketch)
sheet = text(get_objects_operation(c, DOC, "Parameters"))
check("the sheet shows cells and notes", "B1 Length = 60  -- plate length" in sheet, sheet)
feature = text(get_objects_operation(c, DOC, "Hole"))
for needle in (
    "Hole (Pocket in Plate; follows Base; the tip)",
    "Type ThroughAll",
    "the body up to here",
):
    check(f"a feature shows {needle!r}", needle in feature, feature)
raw = text(get_objects_operation(c, DOC, "Base", raw=True))
check("raw still gives every property", json.loads(raw)["TypeId"] == "PartDesign::Pad", raw[:200])

# --- the sheet, by name -----------------------------------------------------------------------
res = cad("variables", "Parameters", set={"Length": 70})
check("set changes a value by its alias", '"Length":["60","70"]' in res, res)
check("the plate follows", abs(volume() - (70 * 40 * 5 - hole)) < 0.1, str(volume()))
res = cad("variables", "Parameters", set={"Lenght": 1})
check("an unknown alias is refused with the closest name", "Did you mean 'Length'" in res, res)
res = cad("variables", "Parameters", rows=[["RibT", 3, "rib thickness"], ["Spare", 1]])
check("rows go under the last row", '"RibT":"B3"' in res and '"Spare":"B4"' in res, res)
res = cad("variables", "Parameters", remove=["Thick"])
check("a parameter in use is not removed", "still used by Base.Length" in res, res)
res = cad("variables", "Parameters", remove=["Spare"])
sheet = text(get_objects_operation(c, DOC, "Parameters"))
check("an unused one is", '"removed":["Spare"]' in res and "Spare" not in sheet, res)
check("the others stay bound", "RibT = 3  -- rib thickness" in sheet, sheet)

# --- a feature in the middle of the history -------------------------------------------------
before = volume()
cad(
    "sketch",
    "RibSketch",
    after="Base",
    plane="XY",
    offset="=Parameters.Thick",
    geometry=[rect(20, 0, 30, 40)],
    constraints=rect_constraints(20, 0, 10, 40),
)
res = cad("pad", "RibSketch", after="Base", name="Rib", length="=Parameters.RibT")
names, tip = order()
# FreeCAD puts new objects just before the next feature, so that feature's own
# sketch stays ahead of them: the order of the features is what the history is
check("the pad sits right behind Base", solids(names) == ["Base", "Rib", "Hole"], str(names))
check("its sketch comes before it", names.index("RibSketch") < names.index("Rib"), str(names))
check("the tip is back on the last feature", tip == "Hole", tip)
check("the body gained exactly the rib", abs(volume() - before - 10 * 40 * 3) < 0.1, str(volume()))
check("the reply gives the whole body's volume", f'"volume_after":{volume():.1f}'[:18] in res, res)
links = run(
    f"d = App.getDocument({DOC!r})\n"
    "print(d.getObject('Rib').BaseFeature.Name, d.getObject('Hole').BaseFeature.Name)"
)
check("the hole now cuts what the rib added to", links == "Base Rib", links)

res = cad("chamfer", "Base", edges={"face": "-Y", "parallel": "X"}, size=1)
check("a dress-up mid-history names the way to do it", "after='Base'" in res, res)
before = volume()
res = cad("chamfer", "Base", after="Base", name="Edge", edges={"parallel": "Z"}, size=1)
names, tip = order()
check(
    "with after it lands behind its base",
    solids(names) == ["Base", "Edge", "Rib", "Hole"],
    str(names),
)
edge = text(get_objects_operation(c, DOC, "Edge"))
check("a dress-up shows where its edges are", "Base.Edge" in edge and "Line (" in edge, edge)
check("and the later features survive", tip == "Hole" and volume() < before, f"{tip} {volume()}")
res = cad("pocket", "HoleSketch", after="HoleSketch", length=1)
check("after refuses a sketch", "must be a feature that shapes" in res, res)
res = cad("variables", "Parameters", after="Base", set={"Length": 70})
check("after is refused where it means nothing", "does not take 'after'" in res, res)

# --- a feature out of the middle --------------------------------------------------------------
res = cad("delete_object", "Rib")
names, tip = order()
check("a mid-history feature is deleted", "Rib" not in names and tip == "Hole", f"{res} {names}")
check("its sketch is left for the caller", "RibSketch" in names, str(names))

# --- looking without touching -----------------------------------------------------------------
run(
    f"d = App.getDocument({DOC!r})\n"
    "d.getObject('PlateSketch').ViewObject.Visibility = True\n"
    "Gui.Selection.clearSelection()\n"
    f"Gui.Selection.addSelection({DOC!r}, 'Plate', 'Hole.Face1')"
)
run(f"Gui.getDocument({DOC!r}).Modified = False")
CAMERA = "import hashlib\nprint(hashlib.md5(Gui.getDocument(%r).activeView().getCamera().encode()).hexdigest())"
camera = run(CAMERA % DOC)
shot = text(get_view_operation(c, "Rear", 300, None, "Plate", DOC))
check("a view is captured, and Rear means Back", "Screenshot saved to" in shot, shot)
# the plate is 70 x 5 seen from behind: the picture is as wide as allowed for its height
check("the picture takes its subject's shape", "(300x100)" in shot, shot)
check("the user's camera is put back", run(CAMERA % DOC) == camera, "the camera moved")
pixels = run(
    "from PySide import QtGui\n"
    f"img = QtGui.QImage({shot.split('saved to ')[1].split(' (')[0]!r})\n"
    "bg = img.pixel(0, 0)\n"
    "cols = [x for x in range(img.width()) if any(img.pixel(x, y) != bg for y in range(img.height()))]\n"
    "print((max(cols) - min(cols) + 1) / img.width() if cols else 0)"
)
check("the subject fills the picture", float(pixels) > 0.85, pixels)
# a sketch is looked at on its part: magenta on glass, and nothing of that stays behind
STYLE = (
    f"d = App.getDocument({DOC!r})\n"
    "s, b = d.getObject('HoleSketch').ViewObject, d.getObject('Plate').ViewObject\n"
    "print(tuple(round(c, 2) for c in s.LineColor)[:3], s.LineWidth, s.Visibility, b.Transparency)"
)
style = run(STYLE)
shot = text(get_view_operation(c, "Top", 240, 240, "HoleSketch", DOC))
pixels = run(
    "from PySide import QtGui\n"
    f"img = QtGui.QImage({shot.split('saved to ')[1].split(' (')[0]!r})\n"
    "n = sum(1 for x in range(img.width()) for y in range(img.height())\n"
    "        if QtGui.QColor(img.pixel(x, y)).red() > 200 and QtGui.QColor(img.pixel(x, y)).green() < 90)\n"
    "print(n)"
)
check("a focused sketch is drawn to be seen", int(pixels) > 300, pixels)
check("its colour, width and the body's glass are put back", run(STYLE) == style, run(STYLE))

state = run(
    f"d = App.getDocument({DOC!r})\n"
    "sel = Gui.Selection.getSelectionEx('', 0)\n"
    "print(d.getObject('PlateSketch').ViewObject.Visibility, d.getObject('Block').ViewObject.Visibility,"
    f" Gui.getDocument({DOC!r}).Modified, len(sel), sel[0].SubElementNames[0].split('.')[-1])"
)
check(
    "sketch, other body, unsaved flag and selection are as before",
    state == "True True False 1 Face1",
    state,
)
tree = text(get_objects_operation(c, DOC))
check("the tree names the selected face", "selected: Hole (Pocket) Face1: " in tree, tree[-300:])

# --- a save leaves nothing unsaved ---------------------------------------------------------------
SAVED = run("import tempfile, os\nprint(os.path.join(tempfile.mkdtemp(), 'edit-in-place.FCStd'))")
saved = text(save_document_operation(c, DOC, SAVED))
tree = text(get_objects_operation(c, DOC))
check(
    "a saved document has no unsaved changes",
    "unsaved changes" not in tree,
    f"{saved} {tree[:200]}",
)

# --- a failing snippet says where -------------------------------------------------------------
res = c.execute_code("# bad\nx = 1\nApp.getDocument('Nope')\nprint(x)", doc_name=DOC)
check(
    "a snippet error names its line",
    "snippet line 3: App.getDocument('Nope')" in str(res.get("error")),
    str(res),
)
res = c.execute_code("# aliases\nprint(Gui is FreeCADGui, App is FreeCAD)", doc_name=DOC)
check("Gui and App are there", "True True" in str(res), str(res))

run(f"App.closeDocument({DOC!r})")
print(f"\n{len(FAILURES)} failure(s)")
sys.exit(1 if FAILURES else 0)
