"""Live checks for sketches a human can keep editing, against a RUNNING FreeCAD.

Covers solver diagnostics, origin-plane attachment, face-attachment warnings,
the constraint and geometry types, point validation, unit values, constraint
names and the sketch_edit op. Every call goes through ``cad_operation``, the
same path the MCP tool takes.

Run: CADPILOT_PORT=<port> uv run --frozen python tests/live_sketch_edit_verify.py
The port is required: this script never falls back to the default 9875.
"""

import json
import math
import os
import sys
import tempfile

if not os.environ.get("CADPILOT_PORT"):
    sys.exit("set CADPILOT_PORT to the FreeCAD instance to test against")
os.environ.setdefault("CADPILOT_HOME", tempfile.mkdtemp(prefix="cadpilot-live-"))

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import cad_operation

c = FreeCADConnection()
FAILURES = []


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def cad(op, doc, name=None, **props):
    """One cad() call; the reply as a dict ({"text": ...} for a plain line)."""
    resp = cad_operation(c, op, doc, obj_name=name, obj_properties=props, auto_audit=False)
    text = " ".join(x.text for x in resp)
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def ok(res):
    return ("summary" in res and "Failed" not in res["summary"]) or (
        "text" in res and "Failed" not in res["text"]
    )


def err(res):
    """The reply's text; warnings are folded into it as "WARNING - ..." lines."""
    return res.get("summary") or res.get("text") or ""


def made(res):
    """The object name a successful reply quotes: "pad 'Pad001' created ..."."""
    return err(res).split("'")[1]


def py(code, doc):
    r = c.execute_code(code, doc_name=doc)
    out = r.get("message") or r.get("output") or ""
    if not r.get("success"):
        raise RuntimeError(f"execute_code failed: {r}")
    return out.split("Output: ", 1)[1].strip() if "Output: " in out else out.strip()


def pyjson(code, doc):
    return json.loads(py(code, doc).splitlines()[-1])


def newdoc(name):
    py(
        f"import FreeCAD\nif '{name}' in FreeCAD.listDocuments(): FreeCAD.closeDocument('{name}')",
        None,
    )
    c.create_document(name)


_INSPECT = r"""
import FreeCAD, json
doc = FreeCAD.getDocument("{doc}")
doc.recompute()
s = doc.getObject("{sk}")
cons = []
for i, k in enumerate(s.Constraints):
    d = dict(type=k.Type, name=k.Name, first=k.First, second=k.Second)
    if k.Type in ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"):
        d["value"] = round(float(k.Value), 6)
    cons.append(d)
geo = []
for i, g in enumerate(s.Geometry):
    e = dict(t=type(g).__name__, constr=s.getConstruction(i))
    for attr in ("StartPoint", "EndPoint", "Center"):
        if hasattr(g, attr):
            e[attr] = [round(v, 4) for v in getattr(g, attr)][:2]
    if hasattr(g, "Radius"):
        e["r"] = round(g.Radius, 4)
    geo.append(e)
pl = s.getGlobalPlacement()
print(json.dumps(dict(
    dof=s.solve() == 0 and s.DoF, state=list(map(str, s.State)), mapmode=s.MapMode,
    support=[(o.Name, list(sub)) for o, sub in s.AttachmentSupport],
    offset=[round(v, 4) for v in s.AttachmentOffset.Base],
    origin=[round(v, 4) for v in pl.Base],
    normal=[round(v, 4) for v in pl.Rotation.multVec(FreeCAD.Vector(0, 0, 1))],
    expr={{str(a): str(b) for a, b in s.ExpressionEngine}},
    cons=cons, geo=geo)))
"""


def inspect(doc, sketch):
    return pyjson(_INSPECT.format(doc=doc, sk=sketch), doc)


LINE = lambda a, b: {"type": "line", "from": a, "to": b}  # noqa: E731


def m1_diagnostics_name_the_spec_entry():
    doc = "SkM1"
    newdoc(doc)
    res = cad(
        "sketch",
        doc,
        "Conflict",
        geometry=[LINE([0, 0], [10, 0])],
        constraints=[
            {"type": "horizontal", "items": [0]},
            {"type": "distance", "items": [0], "value": 10, "name": "Len"},
            {"type": "distance", "items": [0], "value": 20},
        ],
    )
    text = err(res)
    check(
        "M1 conflict names constraints[1] and constraints[2], not the horizontal",
        "constraints[1] (distance 'Len')" in text
        and "constraints[2] (distance)" in text
        and "constraints[0]" not in text,
        text,
    )


def m7_lists_expand_into_one_constraint_per_item():
    doc = "SkM7"
    newdoc(doc)
    circles = [{"type": "circle", "center": [10 * i, 0], "radius": 2 + i} for i in range(3)]
    res = cad(
        "sketch",
        doc,
        "Holes",
        geometry=[LINE([0, 10], [20, 12]), LINE([0, 20], [20, 23]), *circles],
        constraints=[
            {"type": "horizontal", "items": [0, 1]},
            {"type": "radius", "items": [2, 3, 4], "value": 4, "name": "HoleR"},
        ],
    )
    s = inspect(doc, "Holes")
    lines = s["geo"][:2]
    check(
        "M7 horizontal [0, 1] levels both lines",
        ok(res) and all(abs(g["StartPoint"][1] - g["EndPoint"][1]) < 1e-6 for g in lines),
        f"{res} {lines}",
    )
    check(
        "M7 radius [2, 3, 4] gives one named radius and makes the others equal",
        [g["r"] for g in s["geo"][2:]] == [4.0, 4.0, 4.0]
        and [k["type"] for k in s["cons"]]
        == ["Horizontal", "Horizontal", "Radius", "Equal", "Equal"]
        and s["cons"][2]["name"] == "HoleR",
        str(s["cons"]),
    )
    res = cad(
        "sketch",
        doc,
        "TooMany",
        geometry=circles,
        constraints=[{"type": "distance", "items": [0, 1, 2], "value": 5}],
    )
    check("M7 a distance over three edges is refused", "takes one edge" in err(res), err(res))


def m1_new_constraint_types_solve():
    doc = "SkTypes"
    newdoc(doc)
    geo = [
        LINE([0, 0], [10, 0]),
        {"type": "circle", "center": [5, 5], "radius": 4.5},
        LINE([12, 0], [12, 10]),
        {"type": "point", "at": [3, 3]},
    ]
    cases = {
        "point_on_object": {"type": "point_on_object", "items": [[3, "start"], 0]},
        "diameter": {"type": "diameter", "items": [1], "value": 9},
        "block": {"type": "block", "items": [2]},
        "horizontal two points": {"type": "horizontal", "items": [[0, "start"], [1, "center"]]},
        "vertical two points": {"type": "vertical", "items": [[2, "start"], [0, "end"]]},
        "distance point to line": {"type": "distance", "items": [[3, "start"], 0], "value": 2},
        "tangent edge to edge": {"type": "tangent", "items": [0, 1]},
        "tangent via point": {"type": "tangent", "items": [0, 1, [3, "start"]]},
        "perpendicular endpoint on edge": {"type": "perpendicular", "items": [[2, "start"], 0]},
    }
    for label, con in cases.items():
        res = cad("sketch", doc, None, geometry=geo, constraints=[con])
        check(f"m1 {label} solves", ok(res) and not res.get("diagnostics"), err(res) or str(res))


def m2_m3_m8_points_follow_the_geometry():
    doc = "SkPts"
    newdoc(doc)
    c.create_object(
        doc, {"Name": "Cyl", "Type": "Part::Cylinder", "Properties": {"Radius": 4, "Height": 5}}
    )
    res = cad(
        "sketch",
        doc,
        "OnCyl",
        geometry=[{"type": "circle", "center": [1, 1], "radius": 2}],
        external=[["Cyl", "Edge2"]],
        constraints=[{"type": "coincident", "items": [[0, "center"], [-3, "center"]]}],
    )
    s = inspect(doc, "OnCyl") if ok(res) else {}
    check(
        "m2 a circle centred on an external circle's center solves",
        ok(res) and s["geo"][0]["Center"] == [0.0, 0.0],
        err(res) or str(s.get("geo")),
    )
    res = cad(
        "sketch",
        doc,
        "Mid",
        geometry=[LINE([1, 1], [5, 1])],
        constraints=[{"type": "coincident", "items": [[0, "mid"], [-1, "start"]]}],
    )
    g = inspect(doc, "Mid")["geo"][0] if ok(res) else {}
    check(
        "m3 a line's mid coincident with the origin centres the line",
        ok(res)
        and abs(g["StartPoint"][0] + g["EndPoint"][0]) < 1e-6
        and abs(g["StartPoint"][1] + g["EndPoint"][1]) < 1e-6,
        err(res) or str(g),
    )
    res = cad(
        "sketch",
        doc,
        None,
        geometry=[LINE([1, 1], [5, 1])],
        constraints=[{"type": "distance_x", "items": [[0, "mid"]], "value": 3}],
    )
    check(
        "m3 a line 'mid' outside a coincident is refused",
        "no midpoint vertex" in err(res),
        err(res),
    )
    for item, hint in (([-1, "end"], "the X axis (-1)"), ([-2, "start"], "the Y axis (-2)")):
        res = cad(
            "sketch",
            doc,
            None,
            geometry=[LINE([1, 1], [5, 1])],
            constraints=[{"type": "coincident", "items": [[0, "start"], item]}],
        )
        check(f"m8 {item} is refused", hint in err(res), err(res))


def m4_unit_strings_are_values():
    doc = "SkUnits"
    newdoc(doc)
    res = cad(
        "sketch",
        doc,
        "U",
        geometry=[LINE([0, 0], [10, 0]), LINE([0, 0], [5, 5])],
        constraints=[
            {"type": "distance", "items": [0], "value": "1.5 cm"},
            {"type": "angle", "items": [0, 1], "value": "30 deg"},
        ],
    )
    cons = inspect(doc, "U")["cons"] if ok(res) else []
    check(
        "m4 '1.5 cm' and '30 deg' are accepted as values",
        ok(res)
        and math.isclose(cons[0]["value"], 15.0)
        and math.isclose(cons[1]["value"], math.radians(30), abs_tol=1e-6),
        err(res) or str(cons),
    )


def m6_names_are_checked_and_reported():
    doc = "SkNames"
    newdoc(doc)
    geo = [LINE([0, 0], [10, 0]), {"type": "circle", "center": [5, 5], "radius": 2}]
    res = cad(
        "sketch",
        doc,
        None,
        geometry=geo,
        constraints=[
            {"type": "distance", "items": [0], "value": 10, "name": "Width"},
            {"type": "radius", "items": [1], "value": 2, "name": "Width"},
        ],
    )
    check(
        "m6 a duplicate name names both constraint indices",
        "constraints[1] (radius): name 'Width' is already used by constraints[0]" in err(res),
        err(res),
    )
    res = cad(
        "sketch",
        doc,
        None,
        geometry=geo,
        constraints=[{"type": "distance", "items": [5], "value": 10, "name": "Width"}],
    )
    check(
        "m6 a bad GeoId names the constraint index",
        "constraints[0] (distance): GeoId 5 does not exist" in err(res),
        err(res),
    )
    res = cad(
        "sketch",
        doc,
        None,
        geometry=geo,
        constraints=[
            {"type": "distance", "items": [0], "value": 10, "name": "Width"},
            {"type": "radius", "items": [1], "value": 2},
        ],
    )
    check(
        "m6 the reply lists unnamed dimensions",
        res.get("unnamed_dimensions") == ["constraints[1] (radius)"],
        str(res),
    )


def m1_composite_geometry_is_fully_constrainable():
    doc = "SkShapes"
    newdoc(doc)
    cad("variables", doc, "Params", cells={"A1": ["width", 40], "A2": ["depth", 20]})
    res = cad(
        "sketch",
        doc,
        "Plate",
        geometry=[{"type": "rectangle", "from": [0, 0], "to": [30, 10]}],
        constraints=[
            {"type": "coincident", "items": [[0, "start"], [-1, "start"]]},
            {"type": "distance_x", "items": [0], "value": "=Params.width", "name": "Width"},
            {"type": "distance_y", "items": [1], "value": "=Params.depth", "name": "Depth"},
        ],
    )
    check(
        "m1 rectangle + origin + two dims is fully constrained",
        ok(res) and res.get("dof") == 0 and res.get("geometry_ids") == [[0, 1, 2, 3]],
        str(res),
    )
    pad = cad("pad", doc, "Plate", length=5)
    vol = c.measure_geometry(doc, made(pad)) if ok(pad) else {}
    check(
        "m1 the rectangle pads to w*h*5",
        ok(pad) and math.isclose(vol.get("volume_mm3", 0), 40 * 20 * 5, rel_tol=1e-6),
        f"{pad} {vol}",
    )
    res = cad(
        "sketch",
        doc,
        "Slot",
        geometry=[
            {"type": "slot", "from": [0, 0], "to": [20, 0], "radius": 3},
            {"type": "polyline", "points": [[0, 20], [10, 20], [5, 28]], "closed": True},
        ],
        constraints=[
            {"type": "coincident", "items": [[0, "center"], [-1, "start"]]},
            {"type": "distance", "items": [[0, "center"], [1, "center"]], "value": 20, "name": "L"},
            {"type": "radius", "items": [0], "value": 3, "name": "R"},
        ],
    )
    check(
        "m1 slot + origin + length + radius is fully constrained; polyline joins close",
        ok(res) and res.get("geometry_ids") == [[0, 1, 2, 3], [4, 5, 6]] and res.get("dof") == 6,
        str(res),
    )


def m3_base_planes_attach_to_the_origin_with_an_expression_offset():
    doc = "SkOrigin"
    newdoc(doc)
    cad("variables", doc, "Params", cells={"A1": ["lift", 7]})
    res = cad(
        "sketch",
        doc,
        "Front",
        plane="XZ",
        offset="=Params.lift",
        geometry=[{"type": "circle", "center": [0, 0], "radius": 2}],
    )
    s = inspect(doc, "Front") if ok(res) else {}
    check(
        "M3 an XZ sketch is attached FlatFace to the body's XZ_Plane",
        ok(res) and s["mapmode"] == "FlatFace" and s["support"][0][0].startswith("XZ_Plane"),
        err(res) or str(s),
    )
    check(
        "M3 the offset is bound to the spreadsheet and lands along -Y",
        s.get("expr", {}).get(".AttachmentOffset.Base.z") == "Params.lift"
        and s.get("origin") == [0.0, -7.0, 0.0],
        str(s),
    )
    plane = res.get("plane", {})
    check(
        "M3 the reply echoes the XZ axes in world terms",
        plane.get("x_axis") == [1.0, 0.0, 0.0]
        and plane.get("y_axis") == [0.0, 0.0, 1.0]
        and plane.get("normal") == [0.0, -1.0, 0.0]
        and plane.get("offset") == "=Params.lift",
        str(plane),
    )
    cad("variables", doc, "Params", cells={"A1": ["lift", 12]})
    check(
        "M3 changing the cell moves the sketch",
        inspect(doc, "Front")["origin"] == [0.0, -12.0, 0.0],
        str(inspect(doc, "Front")),
    )
    cad("edit_object", doc, "Body", Placement={"Base": {"x": 100, "y": 0, "z": 0}})
    check(
        "M3 the sketch moves with its body",
        inspect(doc, "Front")["origin"] == [100.0, -12.0, 0.0],
        str(inspect(doc, "Front")),
    )


def _stepped_block(doc):
    """A 30x20x10 block from a named, spreadsheet-driven rectangle."""
    cad("variables", doc, "Params", cells={"A1": ["width", 30], "A2": ["depth", 20]})
    cad(
        "sketch",
        doc,
        "Base",
        geometry=[{"type": "rectangle", "from": [0, 0], "to": [30, 20]}],
        constraints=[
            {"type": "coincident", "items": [[0, "start"], [-1, "start"]]},
            {"type": "distance_x", "items": [0], "value": "=Params.width", "name": "Width"},
            {"type": "distance_y", "items": [1], "value": "=Params.depth", "name": "Depth"},
        ],
    )
    return made(cad("pad", doc, "Base", length=10))


def m4_face_attachments_warn_that_they_are_fragile():
    doc = "SkFace"
    newdoc(doc)
    pad = _stepped_block(doc)
    res = cad(
        "sketch",
        doc,
        "Top",
        plane={"face": [pad, "+Z"]},
        geometry=[{"type": "circle", "center": [5, 5], "radius": 2}],
    )
    warnings = err(res)
    check(
        "M4 a face attachment warns that it can break on upstream edits",
        ok(res)
        and "can break when upstream geometry changes" in warnings
        and "picked by '+Z'" in warnings,
        str(res),
    )
    res = cad(
        "sketch",
        doc,
        "Lifted",
        plane="XY",
        offset="=Params.depth / 2",
        geometry=[{"type": "circle", "center": [5, 5], "radius": 2}],
    )
    check(
        "M4 a base-plane sketch carries no fragility warning",
        ok(res) and "upstream geometry" not in err(res),
        str(res),
    )


def m5_a_centred_face_sketch_says_it_is_fixed():
    doc = "SkCenter"
    newdoc(doc)
    pad = _stepped_block(doc)
    res = cad(
        "sketch",
        doc,
        "Mid",
        plane={"face": [pad, "+Z"], "center": True},
        geometry=[{"type": "circle", "center": [0, 0], "radius": 2}],
    )
    warnings = err(res)
    check(
        "M5 center: true says the offset is fixed at build time",
        ok(res)
        and "fixed at build time" in warnings
        and res.get("plane", {}).get("center_offset") == [15.0, 10.0],
        str(res),
    )
    cad("variables", doc, "Params", cells={"A1": ["width", 50]})
    check(
        "M5 (as documented) the centre stays where it was measured",
        inspect(doc, "Mid")["origin"][:2] == [15.0, 10.0],
        str(inspect(doc, "Mid")),
    )


def m7_every_reply_echoes_the_world_frame():
    doc = "SkEcho"
    newdoc(doc)
    _stepped_block(doc)
    cad(
        "sketch",
        doc,
        "Step",
        plane="XY",
        offset=10,
        geometry=[{"type": "rectangle", "from": [0, 0], "to": [10, 20]}],
    )
    upper = made(cad("pad", doc, "Step", length=5))
    res = cad(
        "sketch",
        doc,
        "OnStep",
        plane={"face": [upper, "+Z"]},
        geometry=[{"type": "circle", "center": [2, 2], "radius": 1}],
    )
    plane = res.get("plane", {})
    same = plane.get("same_direction") or []
    check(
        "m7 a direction pick on a stepped part lists same_direction faces",
        ok(res)
        and plane.get("center", [0, 0, 0])[2] == 15.0
        and any(f["center"][2] == 10.0 for f in same),
        str(plane),
    )
    res = cad(
        "sketch",
        doc,
        "Side",
        plane={"face": [upper, "-Y"]},
        geometry=[{"type": "circle", "center": [2, 2], "radius": 1}],
    )
    plane, live = res.get("plane", {}), inspect(doc, "Side") if ok(res) else {}
    check(
        "m7 a face-attached sketch reports its x/y axes and normal in world terms",
        ok(res)
        and plane.get("normal") == [0.0, -1.0, 0.0]
        and plane.get("normal") == live.get("normal")
        and plane.get("sketch_origin") == live.get("origin")
        and {"x_axis", "y_axis"} <= set(plane),
        f"{plane} {live}",
    )
    cad("datum_plane", doc, "Mid", plane="YZ", offset=5)
    res = cad(
        "sketch",
        doc,
        "OnDatum",
        plane={"datum": "Mid"},
        geometry=[{"type": "circle", "center": [2, 2], "radius": 1}],
    )
    plane = res.get("plane", {})
    check(
        "m7 a datum-attached sketch echoes its axes",
        ok(res)
        and plane.get("normal") == [1.0, 0.0, 0.0]
        and plane.get("x_axis") == [0.0, 1.0, 0.0]
        and plane.get("sketch_origin") == [5.0, 0.0, 0.0],
        str(plane),
    )


def _objects(doc):
    return {o["Name"] for o in c.get_objects(doc)}


def m2_an_existing_sketch_name_is_refused_or_replaced():
    doc = "SkReuse"
    newdoc(doc)
    pad = _stepped_block(doc)
    before = _objects(doc)
    res = cad("sketch", doc, "Base", geometry=[LINE([0, 0], [1, 0])])
    check(
        "M2 sketch with an existing name is refused and points to sketch_edit",
        not ok(res) and "sketch_edit" in err(res) and _objects(doc) == before,
        f"{err(res)} extra={_objects(doc) - before}",
    )
    res = cad(
        "sketch",
        doc,
        "Base",
        replace=True,
        geometry=[{"type": "rectangle", "from": [0, 0], "to": [10, 10]}],
        constraints=[
            {"type": "coincident", "items": [[0, "start"], [-1, "start"]]},
            {"type": "distance_x", "items": [0], "value": 12, "name": "Width"},
            {"type": "distance_y", "items": [1], "value": 12, "name": "Depth"},
        ],
    )
    vol = c.measure_geometry(doc, pad).get("volume_mm3")
    check(
        "M2 replace: true rebuilds the same sketch and its pad follows",
        ok(res)
        and res.get("replaced") is True
        and res.get("dof") == 0
        and _objects(doc) == before
        and math.isclose(vol or 0, 12 * 12 * 10, rel_tol=1e-6),
        f"{res} vol={vol}",
    )


CHECKS = [
    m1_diagnostics_name_the_spec_entry,
    m7_lists_expand_into_one_constraint_per_item,
    m1_new_constraint_types_solve,
    m2_m3_m8_points_follow_the_geometry,
    m4_unit_strings_are_values,
    m6_names_are_checked_and_reported,
    m1_composite_geometry_is_fully_constrainable,
    m3_base_planes_attach_to_the_origin_with_an_expression_offset,
    m4_face_attachments_warn_that_they_are_fragile,
    m5_a_centred_face_sketch_says_it_is_fixed,
    m7_every_reply_echoes_the_world_frame,
    m2_an_existing_sketch_name_is_refused_or_replaced,
]

if __name__ == "__main__":
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:  # a crashed check is a failure, not an abort
            check(fn.__name__, False, f"{type(e).__name__}: {e}")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL LIVE CHECKS PASSED")
