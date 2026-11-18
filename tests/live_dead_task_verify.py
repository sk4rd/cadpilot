"""Live check that a GUI task killed by a C++ exception is answered and survived.

FreeCAD 1.1.4 lets some C++ exceptions pass straight through Python: no ``except``
or ``finally`` runs, and ``GUIApplication::notify`` reports "Unhandled
Base::Exception". ``Units.Quantity("1 SpigotR")`` does it. Inside a CADPilot task
that left the queue busy for good: the caller waited out its timeout and every
later call timed out behind it until FreeCAD was restarted.

This script puts that call back into the sketch builder and checks that the dead
call is answered at once, its half-built sketch is rolled back, the queue runs
again and ``get_gui_state`` says what happened.

It DAMAGES the FreeCAD it runs against (that is the point), so run it last and
restart that FreeCAD afterwards:

    CADPILOT_PORT=9879 uv run --frozen python tests/live_dead_task_verify.py
"""

import sys
import time

from live_port import require_test_port

from cadpilot.freecad_client import FreeCADConnection
from cadpilot.operations import cad_operation

require_test_port()
c = FreeCADConnection()
FAILURES: list[str] = []
DOC = "QDeadTask"


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def sketch(name, constraint_name):
    spec = {
        "plane": "XY",
        "geometry": [{"type": "rectangle", "from": [0, 0], "to": [20, 10]}],
        "constraints": [
            {
                "type": "distance_x",
                "items": [[-1, "start"], [1, "end"]],
                "value": 20,
                "name": constraint_name,
            },
        ],
    }
    res = cad_operation(c, "sketch", DOC, obj_name=name, obj_properties=spec, auto_audit=False)
    return "\n".join(getattr(r, "text", str(r)) for r in res)


c.execute_code(
    "import FreeCAD as App\n"
    f"if {DOC!r} in App.listDocuments(): App.closeDocument({DOC!r})\n"
    f"App.newDocument({DOC!r})\n"
    # the name probe as it was before the expression parser replaced it
    "import cadpilot_rpc.sketcher_ops as so\n"
    "def old_probe(sketch, name, where):\n"
    "    try:\n"
    "        App.Units.Quantity(f'1 {name}')\n"
    "    except Exception:\n"
    "        return\n"
    "    raise ValueError(f'{where}: {name!r} is a unit symbol')\n"
    "so.refuse_unit_name = old_probe\n"
)

started = time.monotonic()
out = sketch("Dies", "SpigotR")
took = time.monotonic() - started
check("the call that died is answered at once", took < 10, f"{took:.0f}s")
check("and says what happened", "died inside FreeCAD" in out, out[:300])
check("and what to do about it", "restart FreeCAD" in out and "save_document" in out, out[:600])
check("its changes were rolled back", "rolled back" in out, out[:600])

names = [o["Name"] for o in c.get_objects(DOC)]
check("the half-built sketch is gone", "Dies" not in names, str(names))

state = c.get_gui_state()
check("the queue is not left busy", state.get("processing") is False, str(state))
check(
    "get_gui_state names the damage",
    (state.get("damaged") or {}).get("method") == "create_feature",
    str(state),
)

started = time.monotonic()
out = sketch("Lives", "Width")
took = time.monotonic() - started
check("the next call runs", "created successfully" in out, out[:300])
check("without waiting behind the dead one", took < 10, f"{took:.0f}s")

print(f"\n{len(FAILURES)} failure(s)")
for f in FAILURES:
    print(" -", f)
sys.exit(1 if FAILURES else 0)
