"""Live regression for deleting a PartDesign feature from the middle of a Body.

delete_object refused every feature something was built on, and the next feature of its own
Body always is: a pocket in the middle of a chain could only go by hand (Body.removeObject).
It now takes such a feature out of the chain the way FreeCAD's Delete key does, and still
refuses one that a pattern repeats.

    CADPILOT_PORT=9879 uv run --frozen python tests/live_delete_verify.py
"""

import math
import sys

from live_port import require_test_port

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import cad_operation

require_test_port()
c = FreeCADConnection()
FAILURES: list[str] = []
DOC = "DeleteMidBody"


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def text(res):
    return "\n".join(getattr(r, "text", str(r)) for r in res)


def run(code, doc=DOC):
    res = c.execute_code(code, doc_name=doc)
    if not res.get("success"):
        raise RuntimeError(res.get("error") or res)
    out = str(res.get("message") or res.get("output") or "")
    return out.split("Output:")[-1].strip() if "Output:" in out else out


SETUP = f"""
import FreeCAD as App, Part
if {DOC!r} in App.listDocuments():
    App.closeDocument({DOC!r})
doc = App.newDocument({DOC!r})
body = doc.addObject("PartDesign::Body", "Body")
def sketch(name, x0, y0, x1, y1, z):
    s = body.newObject("Sketcher::SketchObject", name)
    s.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]
    s.MapMode = "FlatFace"
    s.AttachmentOffset = App.Placement(App.Vector(0, 0, z), App.Rotation())
    pts = [App.Vector(x0, y0), App.Vector(x1, y0), App.Vector(x1, y1), App.Vector(x0, y1)]
    for i in range(4):
        s.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]))
    return s
s = sketch("PlateSketch", 0, 0, 60, 40, 0)
pad = body.newObject("PartDesign::Pad", "Plate"); pad.Profile = s; pad.Length = 5
s = sketch("ASketch", 5, 5, 15, 15, 5)
a = body.newObject("PartDesign::Pocket", "PocketA"); a.Profile = s; a.Type = "ThroughAll"
s = sketch("BSketch", 30, 5, 40, 15, 5)
b = body.newObject("PartDesign::Pocket", "PocketB"); b.Profile = s; b.Type = "ThroughAll"
doc.recompute()
print(round(body.Shape.Volume, 3))
"""


def volume():
    return float(
        run(
            f"import FreeCAD as App\nprint(round(App.getDocument({DOC!r}).getObject('Body').Shape.Volume, 3))"
        ).splitlines()[-1]
    )


print(run(SETUP, doc=None))
before = volume()
check(
    "[setup] plate with two 10 x 10 pockets",
    math.isclose(before, 60 * 40 * 5 - 2 * 500, abs_tol=1e-3),
    f"{before}",
)

out = text(cad_operation(c, "delete_object", DOC, obj_name="PocketA", auto_audit=False))
after = volume()
check("[mid] the pocket in the middle of the chain comes out", "Cannot delete" not in out, out)
check(
    "[mid] the body keeps the later pocket and loses this one's cut",
    math.isclose(after, before + 500, abs_tol=1e-3),
    f"{after}",
)
base = run(
    f"import FreeCAD as App\nprint(App.getDocument({DOC!r}).getObject('PocketB').BaseFeature.Name)"
).splitlines()[-1]
check("[mid] the next feature now stands on the one before", base == "Plate", base)

run(f"""import FreeCAD as App
doc = App.getDocument({DOC!r})
body = doc.getObject("Body")
p = body.newObject("PartDesign::LinearPattern", "BPattern")
p.Originals = [doc.getObject("PocketB")]
p.Direction = (doc.getObject("PlateSketch"), ["H_Axis"])
p.Length = 15
p.Occurrences = 2
body.Tip = p
doc.recompute()""")
patterned = volume()
out = text(cad_operation(c, "delete_object", DOC, obj_name="PocketB", auto_audit=False))
check("[pattern] a feature a pattern repeats is still refused", "Cannot delete" in out, out)
check(
    "[pattern] and the body is unchanged",
    math.isclose(volume(), patterned, abs_tol=1e-3),
    f"{volume()}",
)

out = text(cad_operation(c, "delete_object", DOC, obj_name="BPattern", auto_audit=False))
tip = run(
    f"import FreeCAD as App\nprint(App.getDocument({DOC!r}).getObject('Body').Tip.Name)"
).splitlines()[-1]
check(
    "[tip] deleting the tip moves the Tip back",
    tip == "PocketB" and "Cannot delete" not in out,
    f"{tip} {out}",
)

run(f"import FreeCAD as App\nApp.closeDocument({DOC!r})")
print(f"\n{len(FAILURES)} failure(s)")
sys.exit(1 if FAILURES else 0)
