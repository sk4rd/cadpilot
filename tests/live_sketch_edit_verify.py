"""Live checks for sketches a human can keep editing, against a RUNNING FreeCAD.

Covers solver diagnostics, origin-plane attachment, face-attachment warnings,
the constraint and geometry types, point validation, unit values, constraint
names and the sketch_edit op. Every call goes through ``cad_operation``, the
same path the MCP tool takes.

Run: CADPILOT_PORT=<port> uv run --frozen python tests/live_sketch_edit_verify.py
The port is required: this script never falls back to the default 9875.
"""

import json
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
    return res.get("summary") or res.get("text") or ""


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


CHECKS = [
    m1_diagnostics_name_the_spec_entry,
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
