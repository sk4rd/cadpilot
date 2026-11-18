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
# A FreeCAD that was killed with documents open greets the next start with a
# modal Document Recovery dialog, which holds every queued call back.
for _ in range(3):
    if c.get_gui_state().get("defer_reason") != "modal":
        break
    c.dismiss_blocking_dialog()
    time.sleep(1)
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
    if not res.get("success"):
        raise RuntimeError(f"snippet failed: {res.get('error')}")
    return str(res.get("message") or "").split("Output: ", 1)[-1].strip()


_EDITS = 0


def user(doc, body):
    """Run ``body`` the way a GUI edit runs: later, on the GUI thread, unmuted.

    ``d`` is the document inside ``body``. Waits until the edit has run and the
    observer's deferred reads have settled.
    """
    global _EDITS
    _EDITS += 1
    token = f"edit{_EDITS}"
    code = (
        "import FreeCAD, Sketcher\n"
        "from PySide import QtCore\n"
        "def _edit():\n"
        f"    d = FreeCAD.getDocument({doc!r})\n"
        "    try:\n" + textwrap.indent(textwrap.dedent(body), "        ") + "\n"
        "    finally:\n"
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


def cad(op, doc, name=None, desc="", **props):
    text = "\n".join(
        x.text
        for x in ops.cad_operation(
            c, op, doc, obj_name=name, obj_properties=props, description=desc, auto_audit=False
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

# --- A3: a spreadsheet cell edit ---------------------------------------------
user(D, "v = d.getObject('Params'); v.set('A1', '70'); d.recompute()")
cells = step_params(D, "variables")["cells"]
check("A3 the cell edit syncs", cells["A1"] == ["Wd", 70], str(cells))
text = changes(D)
check("A3 Claude is told about the cell", "Params.A1 (Wd) 60 -> 70" in text, text)

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


print()
if CHANGE_TEXTS:
    print("Example of what Claude reads:\n" + CHANGE_TEXTS[0])
print(f"\n{len(FAILURES)} failure(s)")
for failure in FAILURES:
    print(" -", failure)
sys.exit(1 if FAILURES else 0)
