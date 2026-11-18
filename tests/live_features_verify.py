"""Live regression for the Part Design feature ops (the 2026-11 audit).

Every check builds a small model through cadpilot.operations — the path the
MCP ``cad`` tool takes — and asserts exact volumes, counts and positions read
back from FreeCAD. One section per audit finding (the number in brackets).

Run against a FreeCAD whose CADPilot add-on listens on CADPILOT_PORT:
    CADPILOT_PORT=9891 uv run --frozen python tests/live_features_verify.py
The port is required, and it must be a test FreeCAD's (tests/live_port.py), so it
cannot touch a FreeCAD someone is working in.
"""

import ast
import json
import math
import sys

from live_port import require_test_port

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import cad_operation, measure_geometry_operation

require_test_port()


c = FreeCADConnection()
FAILURES: list[str] = []
TOL = 1e-3
HOLE_R3 = math.pi * 9  # area of an r=3 circle


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def near(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) <= tol


def cad(doc, op, name=None, **props):
    """(ok, summary text, parsed payload) of one cad() call."""
    res = cad_operation(c, op, doc, obj_name=name, obj_properties=props or None, auto_audit=False)
    text = "\n".join(getattr(r, "text", str(r)) for r in res)
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"summary": text}
    summary = str(payload.get("summary", text))
    return not summary.startswith(("Failed", "cad ")), summary, payload


def fc(doc, expr):
    """Evaluate a Python expression inside FreeCAD (``d`` is the document)."""
    code = f"import FreeCAD as App\nd = App.getDocument({doc!r})\nprint(repr({expr}))"
    res = c.execute_code(code, doc_name=doc)
    out = str(res.get("message") or res.get("output") or "")
    try:
        return ast.literal_eval(out.split("Output:")[-1].strip().splitlines()[-1])
    except (ValueError, SyntaxError, IndexError):
        return None


def newdoc(name):
    c.execute_code(
        "import FreeCAD as App\n"
        f"if {name!r} in App.listDocuments(): App.closeDocument({name!r})\n"
        f"App.newDocument({name!r})"
    )
    return name


def volume(doc, obj="Body"):
    return fc(doc, f"d.getObject({obj!r}).Shape.Volume")


def tip(doc, body="Body"):
    return fc(doc, f"d.getObject({body!r}).Tip.Name")


def rect(x0, y0, x1, y1):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    geo = [{"type": "line", "from": list(pts[i]), "to": list(pts[(i + 1) % 4])} for i in range(4)]
    cons = [{"type": "coincident", "items": [[i, "end"], [(i + 1) % 4, "start"]]} for i in range(4)]
    return geo, cons


def plate(doc, x1=100, y1=60, height=10, x0=0, y0=0):
    """A fresh document holding one padded rectangular plate."""
    newdoc(doc)
    geo, cons = rect(x0, y0, x1, y1)
    cad(doc, "sketch", "Base", geometry=geo, constraints=cons, plane="XY")
    cad(doc, "pad", "Base", length=height)


def holes(doc, name, centres, radius=3, offset=0, **pocket):
    geo = [{"type": "circle", "center": list(p), "radius": radius} for p in centres]
    cad(doc, "sketch", name, geometry=geo, plane="XY", offset=offset)
    return cad(doc, "pocket", name, **(pocket or {"through_all": True}))


def cylinder_centres(doc, obj="Body", max_area=1e9):
    return fc(
        doc,
        f"sorted((round(f.CenterOfMass.x, 3), round(f.CenterOfMass.y, 3)) for f in "
        f"d.getObject({obj!r}).Shape.Faces if f.Surface.TypeId == 'Part::GeomCylinder' "
        f"and f.Area < {max_area})",
    )


# --- [1] mirror inside a body -------------------------------------------------------
D = "LF_Mirror"
plate(D, 50, 60, x0=-50)
holes(D, "H1", [(20, 10)])
holes(D, "H2", [(20, 40)], radius=2)
ok, text, res = cad(D, "mirror", "Pocket", plane="YZ", originals=["Pocket001"])
expected = 60000 - 2 * HOLE_R3 * 10 - 2 * math.pi * 4 * 10
check("[1] PartDesign mirror changes the body", ok and near(volume(D), expected), text)
check("[1] the mirror becomes the Tip", tip(D) == "Mirrored", str(tip(D)))
check(
    "[1] a mirror is a PartDesign::Mirrored", fc(D, "d.Mirrored.TypeId") == "PartDesign::Mirrored"
)
ok, text, _ = cad(D, "mirror", "Pocket")
check("[1] a mirror that changes nothing is refused", not ok and "had no effect" in text, text)
ok, text, res = cad(D, "mirror", "Body", plane="YZ")
check("[1] mirroring a whole Body says it is a copy", ok and "whole Body" in str(res), text)

# --- [2] false success after a recompute ---------------------------------------------
D = "LF_Recompute"
newdoc(D)
bow = [
    {"type": "line", "from": [0, 0], "to": [40, 30]},
    {"type": "line", "from": [40, 30], "to": [40, 0]},
    {"type": "line", "from": [40, 0], "to": [0, 30]},
    {"type": "line", "from": [0, 30], "to": [0, 0]},
]
cad(D, "sketch", "Bow", geometry=bow, plane="XY")
ok, text, _ = cad(D, "pad", "Bow", length=10)
check("[2] a self-intersecting profile is refused", not ok and "crosses itself" in text, text)
check("[2] ...and nothing is padded", fc(D, "d.getObject('Pad') is None") is True)

D = "LF_Broken"
newdoc(D)
cad(D, "variables", "Vars", cells={"A1": ["Depth", 4], "A2": ["Thick", 10]})
geo, cons = rect(0, 0, 100, 60)
cad(D, "sketch", "Base", geometry=geo, constraints=cons, plane="XY")
cad(D, "pad", "Base", length="=Vars.Thick")
holes(D, "H", [(50, 30)], radius=5, offset=10, length="=Vars.Depth")
# Both rims: the floor's rim disappears once the hole goes through.
ok, text, res = cad(D, "fillet", "Pocket", edges={"type": "circle", "radius": 5}, radius=1)
check("[2] fillet on the hole rims", ok and res["resolved_edges"]["count"] == 2, text)
good = volume(D)
ok, text, res = cad(D, "variables", "Vars", cells={"A1": ["Depth", 12]})
check("[2] the op itself still applies", ok, text)
check(
    "[2] a dependent broken by the change is reported",
    "BROKE" in text and "STALE" in text and bool(res.get("broken")),
    text,
)
row = (res.get("model") or [{}])[0]
check(
    "[2] the reply names the tip and flags the stale volume",
    row.get("tip") and row.get("stale") is True,
    str(res.get("model")),
)
m = json.loads(measure_geometry_operation(c, D, "Body")[0].text)
check("[2] measure_geometry says the shape is stale", m.get("stale") is True, str(m)[:300])
check("[2] measure_geometry names the tip", m.get("tip") == row.get("tip"), str(m)[:300])
ok, text, res = cad(D, "variables", "Vars", cells={"A1": ["Depth", 4]})
check("[2] putting the value back heals the model", ok and near(volume(D), good), text)

D = "LF_Edit"
plate(D)
cad(D, "variables", "Vars", cells={"A1": ["Th", 10]})
fc(D, "d.Pad.setExpression('Length', 'Vars.Th')")
ok, text, _ = cad(D, "fillet", "Pad", edges={"parallel": "X", "at": "+Z"}, radius=2)
ok, text, _ = cad(D, "edit_object", "Fillet", Radius=50)
check("[2] an edit FreeCAD cannot recompute is refused", not ok, text)
check(
    "[2] ...and put back, recomputed",
    fc(D, "(d.Fillet.Radius.Value, 'Invalid' in d.Fillet.State)") == (2.0, False),
    str(fc(D, "(d.Fillet.Radius.Value, d.Fillet.State)")),
)
ok, text, _ = cad(D, "edit_object", "Pad", Length=20)
check("[2] a literal under an expression warns", ok and "bound to an expression" in text, text)

# --- [3] linear pattern keeps its pitch -----------------------------------------------
D = "LF_Pitch"
plate(D, 200)
cad(D, "variables", "Vars", cells={"A1": ["Count", 4], "A2": ["Pitch", 20]})
holes(D, "H", [(10, 10)])
ok, text, _ = cad(D, "pattern", "Pocket", count="=Vars.Count", spacing="=Vars.Pitch", axis="X")
check("[3] spacing mode", ok and fc(D, "d.LinearPattern.Mode") == "Spacing", text)
cad(D, "variables", "Vars", cells={"A1": ["Count", 6]})
xs = [x for x, _y in cylinder_centres(D) or []]
check("[3] 6 copies at pitch 20 after the count changed", xs == [10, 30, 50, 70, 90, 110], str(xs))
check("[3] exact volume", near(volume(D), 120000 - 6 * HOLE_R3 * 10), str(volume(D)))
cad(D, "variables", "Vars", cells={"A2": ["Pitch", 25]})
xs = [x for x, _y in cylinder_centres(D) or []]
check("[3] the pitch is bound too", xs == [10, 35, 60, 85, 110, 135], str(xs))

# --- [4] pattern axis, guard and causes -------------------------------------------------
D = "LF_Axis"
plate(D)
holes(D, "H1", [(10, 10)])
holes(D, "H2", [(80, 40)])
ok, text, res = cad(D, "pattern", "Pocket", count=3, spacing=20)
check("[4] linear defaults to the profile's H axis", ok and "H_Axis" in str(res), text)
check(
    "[4] an earlier (non-tip) original is patterned",
    near(volume(D), 60000 - 4 * HOLE_R3 * 10) and tip(D) == "LinearPattern",
    str(volume(D)),
)
ok, text, _ = cad(D, "pattern", "Pocket", count=3, spacing=5, axis="Z")
check("[4] along the hole's own axis: refused, cause named", not ok and "normal of" in text, text)
holes(D, "H3", [(50, 30)], radius=2)
ok, text, _ = cad(D, "pattern", "Pocket002", pattern_type="polar", count=6, center=[50, 30, 0])
check("[4] polar about the feature itself: cause named", not ok and "runs through" in text, text)
holes(D, "H4", [(70, 30)], radius=2)
before = volume(D)
ok, text, _ = cad(D, "pattern", "Pocket003", pattern_type="polar", count=6, center=[50, 30, 0])
check(
    "[4] polar about a centre",
    ok and near(volume(D), before - 5 * math.pi * 4 * 10) and tip(D) == "PolarPattern",
    text,
)

# --- [5] unknown keys --------------------------------------------------------------------
ok, text, _ = cad(D, "pattern", "Pocket", count=3, spacing=20, spacngs=[20, 20])
check(
    "[5] an unknown key is refused with the valid ones",
    not ok and "did you mean 'spacings'" in text and "Valid keys for pattern" in text,
    text,
)
ok, text, _ = cad(D, "pocket", "H1", lenght=5)
check("[5] ...for every op", not ok and "did you mean 'length'" in text, text)

# --- [6] values bound to expressions -------------------------------------------------------
D = "LF_Bind"
plate(D)
cad(D, "variables", "Vars", cells={"A1": ["Off", 5], "A2": ["Cx", 50], "A3": ["Ch", 1]})
ok, text, _ = cad(D, "datum_plane", "DP", plane="XY", offset="=Vars.Off")
check("[6] datum offset expression", ok and near(fc(D, "d.DP.Placement.Base.z"), 5), text)
holes(D, "H", [(70, 30)], radius=2)
ok, text, _ = cad(D, "pattern", "Pocket", pattern_type="polar", count=4, center=["=Vars.Cx", 30, 0])
check("[6] polar centre expression", ok and near(volume(D), 60000 - 4 * math.pi * 4 * 10), text)
cad(D, "variables", "Vars", cells={"A1": ["Off", 7], "A2": ["Cx", 60]})
check("[6] the datum follows the sheet", near(fc(D, "d.DP.Placement.Base.z"), 7))
check(
    "[6] the centre follows the sheet",
    cylinder_centres(D) == [(50, 30), (60, 20), (60, 40), (70, 30)],
    str(cylinder_centres(D)),
)
ok, text, _ = cad(D, "chamfer", "PolarPattern", edges=[0], size="=Vars.Ch")
check(
    "[6] chamfer size expression",
    ok and fc(D, "d.Chamfer.ExpressionEngine") == [("Size", "Vars.Ch")],
)

# --- [7] uneven spacings, originals, multi, vector axis -------------------------------------
D = "LF_Louvre"
plate(D, 240, 40, 4)
cad(D, "variables", "Vars", cells={"A1": ["Pitch", 9]})
geo, cons = rect(5, 5, 8, 35)
cad(D, "sketch", "Slot", geometry=geo, constraints=cons, plane="XY")
cad(D, "pocket", "Slot", through_all=True)
gaps = [None, None, None, "=41 - 3*Vars.Pitch"] * 4 + [None, None, None]
ok, text, _ = cad(D, "pattern", "Pocket", count=20, spacing="=Vars.Pitch", spacings=gaps)
xs = fc(
    D,
    "sorted(round(f.CenterOfMass.x, 3) for f in d.Body.Shape.Faces "
    "if abs(f.normalAt(0, 0).x + 1) < 1e-6 and f.CenterOfMass.x > 1)",
)
starts = [5 + 41 * g + 9 * i for g in range(5) for i in range(4)]
check("[7] 5 groups of 4 slots, 41 apart", ok and xs == [s + 3 for s in starts], str(xs))
check("[7] exact volume", near(volume(D), 240 * 40 * 4 - 20 * 3 * 30 * 4), str(volume(D)))
cad(D, "variables", "Vars", cells={"A1": ["Pitch", 8]})
xs = fc(
    D,
    "sorted(round(f.CenterOfMass.x, 3) for f in d.Body.Shape.Faces "
    "if abs(f.normalAt(0, 0).x + 1) < 1e-6 and f.CenterOfMass.x > 1)",
)
starts = [5 + 41 * g + 8 * i for g in range(5) for i in range(4)]
check("[7] the gaps follow the sheet", xs == [s + 3 for s in starts], str(xs))

hole_cb = HOLE_R3 * 10 + math.pi * (25 - 9) * 2
for case in ("originals", "multi"):
    D = f"LF_{case}"
    plate(D, 50, 60, x0=-50)
    holes(D, "H1", [(10, 10)])
    holes(D, "H2", [(10, 10)], radius=5, offset=10, length=2)
    if case == "originals":
        ok, text, _ = cad(
            D, "pattern", "Pocket", count=3, spacing=10, axis=[1, 1, 0], originals=["Pocket001"]
        )
        check(
            "[7] hole + counterbore along a vector axis",
            ok and near(volume(D), 60000 - 3 * hole_cb),
            text,
        )
        ok, text, _ = cad(D, "pattern", "LinearPattern", count=2, spacing=20, axis="Y")
        check(
            "[7] a pattern of a pattern points to multi",
            not ok and "pattern_type='multi'" in text,
            text,
        )
    else:
        steps = [
            {"pattern_type": "linear", "count": 2, "spacing": 25, "axis": "Y"},
            {"pattern_type": "mirror", "plane": "YZ"},
        ]
        ok, text, _ = cad(
            D, "pattern", "Pocket", pattern_type="multi", originals=["Pocket001"], transforms=steps
        )
        check(
            "[7] MultiTransform grid x mirror",
            ok and near(volume(D), 60000 - 4 * hole_cb) and tip(D) == "MultiTransform",
            text,
        )

# --- [9] pad / pocket modes ------------------------------------------------------------
AREA = math.pi * 25


def shelf(doc):
    """A 100x60x10 plate plus a 100x30x5 shelf floating at z 20..25."""
    plate(doc)
    geo, cons = rect(0, 0, 100, 30)
    cad(doc, "sketch", "Shelf", geometry=geo, constraints=cons, plane="XY", offset=20)
    cad(doc, "pad", "Shelf", length=5)
    cad(
        doc,
        "sketch",
        "C",
        geometry=[{"type": "circle", "center": [50, 15], "radius": 5}],
        plane="XY",
        offset=40,
    )


for label, props, added in (
    ("up_to_first", {"pad_type": "up_to_first"}, AREA * 15),
    ("up_to_last", {"pad_type": "up_to_last"}, AREA * 25),
    ("through_all on a pad = up_to_last", {"through_all": True}, AREA * 25),
    ("up_to_face with offset", {"up_to_face": "+Z", "offset": -2}, AREA * 13),
    ("two lengths", {"length": 5, "length2": 3}, AREA * 8),
):
    D = "LF_PadMode"
    shelf(D)
    before = volume(D)
    ok, text, _ = cad(D, "pad", "C", **props)
    check(
        f"[9] pad {label}", ok and near(volume(D) - before, added), f"{volume(D) - before} | {text}"
    )

for label, props, removed in (
    ("up_to_face -Z", {"up_to_face": "-Z"}, HOLE_R3 * 15),
    ("up_to_first", {"pad_type": "up_to_first"}, HOLE_R3 * 5),
):
    D = "LF_PocketMode"
    shelf(D)
    cad(
        D,
        "sketch",
        "P",
        geometry=[{"type": "circle", "center": [50, 15], "radius": 3}],
        plane="XY",
        offset=25,
    )
    before = volume(D)
    ok, text, _ = cad(D, "pocket", "P", **props)
    check(
        f"[9] pocket {label}",
        ok and near(before - volume(D), removed),
        f"{before - volume(D)} | {text}",
    )

# --- [10] draft neutral plane ---------------------------------------------------------------
D = "LF_Draft"
plate(D, height=40)
ok, text, res = cad(D, "draft", "Pad", faces=["+X", "-X"], angle=5)
taper = 2 * 0.5 * 40 * 40 * math.tan(math.radians(5)) * 60
check(
    "[10] draft defaults to the bottom as neutral plane",
    ok and near(volume(D), 240000 - taper),
    text,
)
ok, text, _ = cad(D, "draft", "Draft", faces=["+Y"], angle=5, neutral_plane="+Y")
check(
    "[10] a drafted face cannot be its own neutral plane", not ok and "being drafted" in text, text
)

# --- [11] a pad that adds nothing ---------------------------------------------------------------
D = "LF_PadNothing"
plate(D)
cad(
    D,
    "sketch",
    "B",
    geometry=[{"type": "circle", "center": [50, 30], "radius": 5}],
    plane="XY",
    offset=10,
)
ok, text, res = cad(D, "pad", "B", length=5, reversed=True)
check("[11] a pad into material warns", ok and "added no material" in text, text)
check(
    "[11] the reply carries the volume before/after",
    res["model"][0]["volume_before"] == res["model"][0]["volume_after"],
)

# --- [12] the body comes from the profile ----------------------------------------------------
D = "LF_Bodies"
plate(D, 40, 30)
c.execute_code(
    f"import FreeCAD as App\nApp.getDocument({D!r}).addObject('PartDesign::Body', 'Body001')"
)
geo, cons = rect(0, 0, 40, 30)
cad(D, "sketch", "B", geometry=geo, constraints=cons, plane="XY", body="Body001")
ok, text, _ = cad(D, "pad", "B", length=5)
check(
    "[12] a pad goes to its sketch's body",
    ok and fc(D, "d.Pad001.getParentGeoFeatureGroup().Name") == "Body001",
    text,
)
ok, text, _ = cad(D, "pad", "B", length=5, body="Body")
check("[12] a contradicting body is refused", not ok and "belongs to Body 'Body001'" in text, text)

# --- [13] pattern of a whole body --------------------------------------------------------------
ok, text, _ = cad(D, "pattern", "Body", count=2, spacing=50)
check("[13] patterning a whole Body is refused", not ok and "whole Body" in text, text)
check(
    "[13] ...and builds no Draft array",
    fc(D, "[o.Name for o in d.Objects if o.Name.startswith('Array')]") == [],
)

# --- [14] aliases that are units ---------------------------------------------------------------
ok, text, _ = cad(D, "variables", "Vars", cells={"A1": ["W", 40]})
check(
    "[14] a unit-symbol alias names the rule",
    not ok and "unit symbol" in text and "Width" in text,
    text,
)

# --- [15] hole ---------------------------------------------------------------------------------
D = "LF_Hole"
plate(D)
geo = [{"type": "circle", "center": [20 + 30 * i, 30], "radius": 3} for i in range(3)]
cad(D, "sketch", "H", geometry=geo, plane="XY", offset=10)
ok, text, _ = cad(D, "hole", "H", diameter=5, depth=6)
check("[15] blind holes", ok and near(volume(D), 60000 - 3 * math.pi * 6.25 * 6), text)
D = "LF_Hole2"
plate(D)
cad(D, "sketch", "H", geometry=geo, plane="XY")
ok, text, _ = cad(D, "hole", "H", diameter=5, through=True)
check(
    "[15] through holes, direction decided",
    ok and near(volume(D), 60000 - 3 * math.pi * 6.25 * 10) and "Reversed" in text,
    text,
)

# --- [16] geometric edge selectors --------------------------------------------------------------
D = "LF_Edges"
plate(D)
holes(D, "H", [(20 + 30 * i, 30) for i in range(3)])
ok, text, res = cad(
    D, "chamfer", "Pocket", edges={"type": "circle", "radius": 3, "at": "+Z"}, size=1
)
rim = math.pi * (3 * 1 + 1 / 3)
check(
    "[16] chamfer the top rims by geometry",
    ok
    and res.get("resolved_edges", {}).get("count") == 3
    and near(volume(D), 60000 - 3 * HOLE_R3 * 10 - 3 * rim),
    text,
)
ok, text, res = cad(D, "fillet", "Chamfer", edges={"parallel": "X", "at": "+Z"}, radius=2)
check("[16] edges along X at the top", ok and res.get("resolved_edges", {}).get("count") == 2, text)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL LIVE FEATURE CHECKS PASSED")
