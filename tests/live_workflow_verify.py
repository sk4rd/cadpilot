"""Live checks for what modelling a real part in the GUI turned up (FreeCAD 1.1.4).

Needs a running FreeCAD with the addon, on the port in CADPILOT_PORT; start one with
scripts/live-freecad. Never run it against the FreeCAD you work in.
"""

import json
import os
import sys

from live_port import require_test_port

from cadpilot import operations as ops
from cadpilot.freecad_client import FreeCADConnection

require_test_port()


c = FreeCADConnection()
DOC = "WorkflowLive"
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def text(resp):
    return "\n".join(getattr(i, "text", "") for i in resp)


def reply_json(resp):
    t = text(resp)
    return json.loads(t[t.find("{") :])


def run(code):
    out = c.execute_code(code, doc_name=DOC)
    return out.get("message", "").split("Output:")[-1].strip()


if DOC in (c.list_documents() or []):
    c.execute_code(f"import FreeCAD; FreeCAD.closeDocument('{DOC}')")
ops.create_document_operation(c, DOC)

# --- spreadsheet text and quantity cells --------------------------------------------
ops.cad_operation(
    c,
    "variables",
    DOC,
    obj_name="Params",
    obj_properties={
        "cells": {
            "A1": ["", "Width"],
            "B1": ["Width", 30],
            "B2": ["Gap", "10 mm"],
            "B3": ["Sum", "=Gap * 2"],
        }
    },
)
contents = run(
    "s = App.getDocument('WorkflowLive').getObject('Params')\n"
    "print(repr(s.getContents('A1')), repr(s.getContents('B2')), s.get('Sum'))"
)
check(
    "a text cell is FreeCAD text, without quote marks", contents.startswith('"\'Width"'), contents
)
check("a quantity cell is a quantity", "'=10 mm'" in contents and "20.0 mm" in contents, contents)

# --- a plan whose steps report their own results -----------------------------------
steps = [
    {
        "operation": "sketch",
        "obj_name": "Base",
        "obj_properties": {
            "plane": "XY",
            "geometry": [{"type": "rectangle", "from": [0, 0], "to": [30, 20]}],
            "constraints": [
                {"type": "coincident", "items": [[0, "start"], [-1, "start"]]},
                {
                    "type": "distance_x",
                    "items": [[0, "start"], [0, "end"]],
                    "value": "=Params.Width",
                    "name": "Width",
                },
                {
                    "type": "distance_y",
                    "items": [[1, "start"], [1, "end"]],
                    "value": 20,
                    "name": "Depth",
                },
            ],
        },
    },
    {"operation": "pad", "obj_name": "Base", "obj_properties": {"length": 5, "name": "Plate"}},
    {"operation": "pad", "obj_name": "Base", "obj_properties": {"length": 5, "name": "Extra"}},
]
ops.step_plan_operation(c, DOC, steps)
res = reply_json(ops.step_control_operation(c, DOC, "run_next"))
entry = (res.get("executed") or [{}])[0]
result = entry.get("result") or {}
check("run_next reports the step's own result", result.get("dof") == 0, json.dumps(entry)[:400])

# --- a change mid-plan is recorded ahead of the plan, which survives -----------------
reply = text(
    ops.execute_code_operation(
        c, "App.getDocument('WorkflowLive').getObject('Base').solve()", doc_name=DOC
    )
)
check(
    "a snippet with an undo entry is not called read-only", "read-only" not in reply, reply[-200:]
)
notes = c.take_plan_notes()
check("the reply says the plan moved", any("now steps 4-5" in n for n in notes), str(notes))
status = reply_json(ops.step_control_operation(c, DOC, "status"))
states = [(r["operation"], r["state"]) for r in status.get("records", [])]
check(
    "the plan survives, after the change",
    states
    == [
        ("variables", "done"),
        ("sketch", "done"),
        ("execute_code", "done"),
        ("pad", "planned"),
        ("pad", "planned"),
    ],
    str(states),
)

# --- a read-only snippet is not recorded --------------------------------------------
reply = text(
    ops.execute_code_operation(
        c, "print(len(App.getDocument('WorkflowLive').Objects))", doc_name=DOC
    )
)
status = reply_json(ops.step_control_operation(c, DOC, "status"))
check("a read-only snippet says so", "read-only" in reply, reply[-200:])
check("and adds no step", len(status.get("records", [])) == 5, str(len(status.get("records", []))))

# --- the plan still runs ----------------------------------------------------------
res = reply_json(ops.step_control_operation(c, DOC, "run_next"))
entry = (res.get("executed") or [{}])[0]
volume = run("print(round(App.getDocument('WorkflowLive').getObject('Body').Shape.Volume, 3))")
check(
    "the next planned step runs",
    entry.get("success") and volume == "3000.0",
    f"{entry} volume={volume}",
)

# --- save to a new folder, close, open again --------------------------------------
target = os.path.join(os.environ.get("TMPDIR", "/tmp"), "cadpilot-live", "nested", "Workflow.FCStd")
reply = text(ops.save_document_operation(c, DOC, target))
check(
    "save_document saves to a new folder",
    "Saved document" in reply and os.path.isfile(target),
    reply,
)
c.execute_code(f"import FreeCAD; FreeCAD.closeDocument('{DOC}')")
reply = text(ops.open_document_operation(c, target))
# a reopened file is named after the file, not after the document it was saved from
out = c.execute_code("print(round(App.getDocument('Workflow').getObject('Body').Shape.Volume, 3))")
volume = out.get("message", "").split("Output:")[-1].strip()
check(
    "open_document brings it back",
    "Opened" in reply and volume == "3000.0",
    f"{reply} volume={volume}",
)
reply = text(ops.open_document_operation(c, target))
check("opening it again switches to it", "already open" in reply, reply)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL LIVE CHECKS PASSED")
