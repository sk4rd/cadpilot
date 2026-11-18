"""Live regression for what modelling a printer part (a WS7040 blower mount) ran into.

One section per problem:
  [body]    a sketch naming a Body that does not exist yet failed every plan at step 1
  [unit]    a constraint named "SpigotR" hung the GUI thread for good (C++ exception
            through Python from the unit-name probe)
  [spacing] a negative =expression spacing in a multi pattern failed to recompute
  [refine]  refining a fuse flush with rounded corners gave an invalid shape: the pad
            after it reported "added no material" while it threw the body away, and a
            pocket on it could not tell which way cut material, so it cut air
  [upto]    an up-to pad whose profile no face covers ran far past the part as success
  [axis]    the polar pattern's helper axis showed in every view
  [batch]   a batch step run by step_control came back as a bare success
  [journal] a fix recorded after a failed step landed behind it; failed steps could
            not be dropped
  [midplane] every pad and pocket wrote the deprecated Midplane property

    CADPILOT_PORT=9879 uv run --frozen python tests/live_session_quirks_verify.py
"""

import json
import os
import sys
import time
from pathlib import Path

from live_port import require_test_port

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import cad_operation, step_control_operation, step_plan_operation

require_test_port()
c = FreeCADConnection()
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def text(res):
    return "\n".join(getattr(r, "text", str(r)) for r in res)


def cad(doc, op, target=None, obj_type=None, ops=None, **props):
    return text(
        cad_operation(
            c,
            op,
            doc,
            obj_name=target,
            obj_type=obj_type,
            ops=ops,
            obj_properties=props or None,
            auto_audit=False,
        )
    )


def fc(doc, expr):
    res = c.execute_code(
        f"import FreeCAD as App\nd = App.getDocument({doc!r})\nprint(repr({expr}))", doc_name=doc
    )
    out = str(res.get("message") or res.get("output") or "")
    return out.split("Output:")[-1].strip().splitlines()[-1] if "Output:" in out else out


def newdoc(name):
    c.execute_code(
        "import FreeCAD as App\n"
        f"if {name!r} in App.listDocuments(): App.closeDocument({name!r})\n"
        f"App.newDocument({name!r})"
    )
    return name


def rect(x0, y0, x1, y1):
    return {
        "geometry": [{"type": "rectangle", "from": [x0, y0], "to": [x1, y1]}],
        "constraints": [
            {
                "type": "distance_x",
                "items": [[-1, "start"], [0, "start"]],
                "value": x0,
                "name": "X0",
            },
            {
                "type": "distance_y",
                "items": [[-1, "start"], [0, "start"]],
                "value": y0,
                "name": "Y0",
            },
            {"type": "distance_x", "items": [[-1, "start"], [1, "end"]], "value": x1, "name": "X1"},
            {"type": "distance_y", "items": [[-1, "start"], [1, "end"]], "value": y1, "name": "Y1"},
        ],
    }


def rounded(x0, y0, x1, y1, r):
    """A rectangle with all four corners rounded, fully constrained."""
    g = [
        {"type": "line", "from": [x0 + r, y0], "to": [x1 - r, y0]},
        {
            "type": "arc",
            "center": [x1 - r, y0 + r],
            "radius": r,
            "start_angle": 270,
            "end_angle": 360,
        },
        {"type": "line", "from": [x1, y0 + r], "to": [x1, y1 - r]},
        {"type": "arc", "center": [x1 - r, y1 - r], "radius": r, "start_angle": 0, "end_angle": 90},
        {"type": "line", "from": [x1 - r, y1], "to": [x0 + r, y1]},
        {
            "type": "arc",
            "center": [x0 + r, y1 - r],
            "radius": r,
            "start_angle": 90,
            "end_angle": 180,
        },
        {"type": "line", "from": [x0, y1 - r], "to": [x0, y0 + r]},
        {
            "type": "arc",
            "center": [x0 + r, y0 + r],
            "radius": r,
            "start_angle": 180,
            "end_angle": 270,
        },
    ]
    con = [{"type": "tangent", "items": [[i, "end"], [(i + 1) % 8, "start"]]} for i in range(8)]
    con += [
        {"type": "horizontal", "items": [0, 4]},
        {"type": "vertical", "items": [2, 6]},
        {"type": "radius", "items": [1, 3, 5, 7], "value": r, "name": "R"},
        {"type": "distance_x", "items": [[-1, "start"], [6, "start"]], "value": x0, "name": "X0"},
        {"type": "distance_x", "items": [[-1, "start"], [2, "start"]], "value": x1, "name": "X1"},
        {"type": "distance_y", "items": [[-1, "start"], [0, "start"]], "value": y0, "name": "Y0"},
        {"type": "distance_y", "items": [[-1, "start"], [4, "start"]], "value": y1, "name": "Y1"},
    ]
    return {"geometry": g, "constraints": con}


# [body] -------------------------------------------------------------------------
d = newdoc("QBody")
out = cad(d, "sketch", "S", plane="XY", body="Base", **rect(0, 0, 10, 10))
check(
    "[body] a sketch creates the Body it names",
    fc(d, "d.getObject('Base').TypeId") == "'PartDesign::Body'",
    out[:300],
)
check("[body] and says so", "did not exist, so it was created" in out, out[:300])
step_plan_operation(
    c,
    d,
    [
        {
            "operation": "sketch",
            "obj_name": "S2",
            "obj_properties": dict(rect(0, 0, 5, 5), plane="XY", body="Other"),
        }
    ],
)
out = text(step_control_operation(c, d, "run_next"))
check(
    "[body] a planned sketch into a new Body runs",
    '"success": true' in out or '"success":true' in out,
    out[:300],
)

# [unit] -------------------------------------------------------------------------
d = newdoc("QUnit")
spec = rect(0, 0, 20, 10)
spec["constraints"][2]["name"] = "SpigotR"
started = time.monotonic()
out = cad(d, "sketch", "S", plane="XY", **spec)
took = time.monotonic() - started
check(
    "[unit] a constraint named SpigotR builds",
    "created successfully" in out and took < 20,
    f"{took:.0f}s: {out[:300]}",
)
spec["constraints"][2]["name"] = "mm"
out = cad(d, "sketch", "S3", plane="XY", **spec)
check("[unit] a unit symbol name is still refused", "unit symbol" in out, out[:300])

# [spacing] ----------------------------------------------------------------------
d = newdoc("QSpacing")
cad(d, "variables", "Params", cells={"A1": ["Pitch", 40]})
cad(d, "sketch", "S", plane="XY", **rect(0, 0, 50, 50))
cad(d, "pad", "S", length=5, name="Plate")
cad(
    d,
    "sketch",
    "H",
    plane="XY",
    offset=5,
    **{
        "geometry": [{"type": "circle", "center": [45, 5], "radius": 1}],
        "constraints": [
            {"type": "radius", "items": [0], "value": 1, "name": "R"},
            {
                "type": "distance_x",
                "items": [[-1, "start"], [0, "center"]],
                "value": 45,
                "name": "CX",
            },
            {
                "type": "distance_y",
                "items": [[-1, "start"], [0, "center"]],
                "value": 5,
                "name": "CY",
            },
        ],
    },
)
cad(d, "pocket", "H", through_all=True, name="Hole")
out = cad(
    d,
    "pattern",
    "Hole",
    pattern_type="multi",
    transforms=[
        {"pattern_type": "linear", "count": 2, "spacing": "=-Params.Pitch", "axis": "X"},
        {"pattern_type": "linear", "count": 2, "spacing": "=Params.Pitch", "axis": "Y"},
    ],
)
vol = float(fc(d, "d.getObject('Body').Shape.Volume") or 0)
check(
    "[spacing] a negative expression spacing patterns the other way",
    abs(vol - (12500 - 4 * 3.14159265 * 5)) < 0.1,
    f"{vol}: {out[:300]}",
)

# [refine] -----------------------------------------------------------------------
# The blower mount's base up to its spine, numbers only: a tray and a box with
# rounded corners, hollowed, four posts, then a spine flush with both.
d = newdoc("QRefine")
steps = json.loads((Path(__file__).parent / "live_refine_spine_steps.json").read_text())
out = ""
for step in steps:
    out = text(
        cad_operation(
            c,
            step["operation"],
            d,
            obj_name=step["obj_name"],
            obj_properties=step["obj_properties"],
            auto_audit=False,
        )
    )
check(
    "[refine] the spine fuse ends up valid",
    fc(d, "d.getObject('Spine').Shape.isValid()") == "True",
    out[:600],
)
check(
    "[refine] by building it unrefined, and saying so",
    "Refine was turned off on 'Spine'" in out,
    out[:600],
)
cad(d, "sketch", "FootSketch", plane="XY", **rect(-20, -14, 0, 5))
out = cad(d, "pad", "FootSketch", length=3, name="Foot")
foot = float(fc(d, "d.getObject('Foot').Shape.Volume") or 0)
spine = float(fc(d, "d.getObject('Spine').Shape.Volume") or 0)
check("[refine] the next pad fuses onto the body", foot > spine, f"{foot} vs {spine}: {out[:400]}")

circle = {
    "geometry": [{"type": "circle", "center": [-46, 104], "radius": 1.45}],
    "constraints": [
        {"type": "radius", "items": [0], "value": 1.45, "name": "R"},
        {"type": "distance_x", "items": [[0, "center"], [-1, "start"]], "value": 46, "name": "CX"},
        {"type": "distance_y", "items": [[-1, "start"], [0, "center"]], "value": 104, "name": "CY"},
    ],
}
cad(d, "sketch", "ScrewSketch", plane="XY", **circle)
before = float(fc(d, "d.getObject('Foot').Shape.Volume") or 0)
out = cad(d, "pocket", "ScrewSketch", through_all=True, name="Screw")
after = float(fc(d, "d.getObject('Screw').Shape.Volume") or 0)
check(
    "[refine] a pocket from the bottom plane still finds the material",
    before - after > 10,
    f"{before} -> {after}: {out[:500]}",
)

# [upto] -------------------------------------------------------------------------
d = newdoc("QUpTo")
disc = {
    "geometry": [{"type": "circle", "center": [0, 0], "radius": 32.9}],
    "constraints": [
        {"type": "coincident", "items": [[0, "center"], [-1, "start"]]},
        {"type": "radius", "items": [0], "value": 32.9, "name": "R"},
    ],
}
cad(d, "sketch", "V", plane="XY", **disc)
cad(d, "pad", "V", length=18, name="Volute")
stub = {
    "geometry": [{"type": "circle", "center": [22.4, 13], "radius": 8.5}],
    "constraints": [
        {"type": "radius", "items": [0], "value": 8.5, "name": "R"},
        {
            "type": "distance_x",
            "items": [[-1, "start"], [0, "center"]],
            "value": 22.4,
            "name": "CX",
        },
        {"type": "distance_y", "items": [[-1, "start"], [0, "center"]], "value": 13, "name": "CY"},
    ],
}
cad(d, "sketch", "St", plane="YZ", offset=-36.3, **stub)
out = cad(d, "pad", "St", pad_type="up_to_first", name="Stub")
check("[upto] an up_to_first that runs past the part is refused", "past the part" in out, out[:300])
stub["geometry"][0].update(center=[22.4, 9], radius=4)
stub["constraints"][0]["value"] = 4
stub["constraints"][2]["value"] = 9
cad(d, "sketch", "St2", plane="YZ", offset=-36.3, **stub)
out = cad(d, "pad", "St2", pad_type="up_to_first", name="Stub2")
check(
    "[upto] one that stops at the cylinder still builds", "created successfully" in out, out[:300]
)

# [axis] -------------------------------------------------------------------------
cad(
    d,
    "sketch",
    "H",
    plane="XY",
    offset=18,
    **{
        "geometry": [{"type": "circle", "center": [20, 0], "radius": 2}],
        "constraints": [
            {"type": "radius", "items": [0], "value": 2, "name": "R"},
            {
                "type": "distance_x",
                "items": [[-1, "start"], [0, "center"]],
                "value": 20,
                "name": "CX",
            },
            {"type": "point_on_object", "items": [[0, "center"], -1]},
        ],
    },
)
cad(d, "pocket", "H", length=5, name="Hole")
cad(d, "pattern", "Hole", pattern_type="polar", count=3, center=[2, 0, 0], name="Holes")
check(
    "[axis] the polar pattern's helper axis is hidden",
    fc(d, "d.getObject('CadPilotPolarAxis').Visibility") == "False",
)

# [batch] ------------------------------------------------------------------------
d = newdoc("QBatch")
step_plan_operation(
    c,
    d,
    [
        {
            "operation": "sketch",
            "obj_name": "S",
            "obj_properties": dict(rect(0, 0, 50, 50), plane="XY"),
        },
        {
            "operation": "batch",
            "ops": [
                {
                    "operation": "pad",
                    "obj_name": "S",
                    "obj_properties": {"length": 5, "name": "Plate"},
                }
            ],
        },
    ],
)
out = json.loads(text(step_control_operation(c, d, "run_all")))
entry = out["executed"][-1]
check(
    "[batch] a batch step reports its ops",
    "volume_after" in json.dumps(entry.get("result", {})),
    json.dumps(entry)[:300],
)

# [journal] ----------------------------------------------------------------------
d = newdoc("QJournal")
step_plan_operation(
    c,
    d,
    [
        {
            "operation": "sketch",
            "obj_name": "S",
            "obj_properties": dict(rect(0, 0, 50, 50), plane="XY", body="Base"),
        },
        {"operation": "pad", "obj_name": "S", "obj_properties": {"length": 5}},
    ],
)
# make step 1 fail: an object called Base that is no Body
c.execute_code(
    f"import FreeCAD as App\nApp.getDocument({d!r}).addObject('App::FeaturePython', 'Base')"
)
text(step_control_operation(c, d, "run_next"))
c.execute_code(f"import FreeCAD as App\nApp.getDocument({d!r}).removeObject('Base')")
cad(d, "create_object", "Base", obj_type="PartDesign::Body")
status = json.loads(text(step_control_operation(c, d, "status")))
order = [(s["operation"], s["state"]) for s in status["steps"] if s["operation"] != "execute_code"]
check(
    "[journal] the fix is recorded before the failed step",
    order[:2] == [("create_object", "done"), ("sketch", "failed")],
    str(order),
)
failed = next(s["index"] for s in status["steps"] if s["state"] == "failed")
out = text(step_control_operation(c, d, "drop", index=failed))
check(
    "[journal] a failed step can be dropped",
    f'"dropped": {failed}' in out or f'"dropped":{failed}' in out,
    out[:300],
)

# [midplane] ---------------------------------------------------------------------
log = os.environ.get("FREECAD_LOG", f"/tmp/cadpilot-live-{os.environ['CADPILOT_PORT']}/freecad.log")
if os.path.exists(log):
    check(
        "[midplane] no Midplane deprecation in the log",
        "Midplane property" not in Path(log).read_text(),
    )
else:
    print("[SKIP] [midplane] no FreeCAD log at", log)

print(f"\n{len(FAILURES)} failure(s)")
for f in FAILURES:
    print(" -", f)
sys.exit(1 if FAILURES else 0)
