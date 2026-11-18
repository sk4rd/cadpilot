"""End-to-end live verification of the add-on runtime against a running FreeCAD.

Covers what the unit tests can only model: the threaded RPC server answering
status calls while a call holds the GUI thread, two mutations never
interleaving, the heartbeat surviving a long task, dismissing a modal that a
running task opened, execute_code_async document work through commit() and
its guards, late completions after a timeout, and the active document being
handed back after a bound execute_code.

The port comes from CADPILOT_PORT and nothing else: this script mutates
documents and must never reach a FreeCAD it was not pointed at.

Run: CADPILOT_PORT=<port> uv run --frozen python tests/live_runtime_verify.py
"""

import os
import sys
import threading
import time

from live_port import require_test_port

from cadpilot.diagnostics import gui_state_note, probe_gui_state, probe_rpc
from cadpilot.freecad_client import FreeCADConnection

require_test_port()

PORT = int(os.environ["CADPILOT_PORT"])

c = FreeCADConnection(timeout=120)
FAILURES = []
DOC = "RuntimeLive"
OTHER = "RuntimeOther"

GD = "import sys\ngd = sys.modules['cadpilot_rpc.gui_dispatch']\n"
RS = "import sys\nrs = sys.modules['cadpilot_rpc.rpc_server']\n"


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def output(res) -> str:
    return str(res.get("message", "")).split("Output:", 1)[-1].strip()


def in_thread(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def run_async(code, wait=60.0):
    tid = c.execute_code_async(code)["task_id"]
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        state = c.get_task_result(tid)
        if state.get("status") != "running":
            return state
        time.sleep(0.3)
    return c.get_task_result(tid)


def timed(fn):
    started = time.monotonic()
    value = fn()
    return value, time.monotonic() - started


# --- setup ----------------------------------------------------------------------
for name in (DOC, OTHER):
    if name in c.list_documents():
        c.execute_code(f"App.closeDocument('{name}')")
c.create_document(DOC)
c.create_document(OTHER)  # created last, so it is the active one

# --- the active document is handed back after a bound run -------------------------
res = c.execute_code("App.ActiveDocument.addObject('Part::Box', 'Bound')", None, DOC)
check("bound execute_code changed its own document", res.get("document") == DOC, str(res))
active = output(
    c.execute_code("print(App.ActiveDocument.Name, FreeCADGui.ActiveDocument.Document.Name)")
)
check("the previously active document is active again", active == f"{OTHER} {OTHER}", active)

# --- status calls answer while a call holds the GUI thread ------------------------
busy, _ = in_thread(lambda: FreeCADConnection().execute_code("import time; time.sleep(5)"))
time.sleep(0.7)
pong, ping_s = timed(c.ping)
check("ping answers during a long call", pong is True and ping_s < 1.0, f"{ping_s:.2f}s")
state, state_s = timed(c.get_gui_state)
check(
    "get_gui_state answers and says the GUI is busy",
    state_s < 1.0 and state.get("processing") is True,
    f"{state_s:.2f}s {state}",
)
log, log_s = timed(lambda: c.get_addon_log(limit=1))
check(
    "get_addon_log answers during a long call", log_s < 1.0 and log.get("success"), f"{log_s:.2f}s"
)
_, async_s = timed(lambda: c.execute_code_async("task_print('queued behind nothing')"))
check("execute_code_async returns at once on a busy GUI", async_s < 1.0, f"{async_s:.2f}s")
note = gui_state_note(probe_gui_state("localhost", PORT))
check(
    "diagnose calls it back-pressure, not a wedge",
    probe_rpc("localhost", PORT).get("reachable") and "busy" in note,
    note,
)
busy.join(15)

# --- two mutations never interleave on the GUI thread ----------------------------
SER = (
    "import time\n"
    "t0 = time.time()\n"
    "App.ActiveDocument.addObject('Part::Box', 'Ser')\n"
    "time.sleep(1.0)\n"
    "print(t0, time.time())\n"
)
pair = [in_thread(lambda: FreeCADConnection().execute_code(SER, None, DOC)) for _ in range(2)]
spans = []
for thread, box in pair:
    thread.join(20)
    spans.append(tuple(float(x) for x in output(box.get("value", {})).split()))
spans.sort()
check(
    "concurrent mutations run one after the other",
    len(spans) == 2 and spans[0][1] <= spans[1][0],
    str(spans),
)
undo = output(c.execute_code(f"print(App.getDocument('{DOC}').UndoNames[:2])"))
check("each got its own undo entry", undo.count("execute_code") == 2, undo)

# --- the heartbeat survives a task that pumps events past its tick ---------------
c.execute_code(
    "import time\n"
    "from PySide import QtWidgets\n"
    "t = time.time()\n"
    "while time.time() - t < 2.0:\n"
    "    QtWidgets.QApplication.processEvents()\n"
    "    time.sleep(0.01)\n"
)
beat = run_async(
    GD + "import threading, time\n"
    "done = threading.Event()\n"
    "gd._rpc_request_queue.put(done.set)\n"  # queued without a wake
    "t = time.time()\n"
    "task_print(done.wait(3), round(time.time() - t, 2))\n",
    wait=10,
)
ran, after_s = (beat.get("output") or "False 9").split()
check("an unwoken task still runs after a long task", ran == "True", str(beat))
check("the heartbeat picked it up within a tick or two", float(after_s) < 1.5, after_s)

# --- a modal opened inside a running task can be dismissed -----------------------
blocker, blocked = in_thread(
    lambda: FreeCADConnection().execute_code(
        "from PySide import QtWidgets\n"
        "mb = QtWidgets.QMessageBox(FreeCADGui.getMainWindow())\n"
        "mb.setWindowTitle('Runtime live modal')\n"
        "print('exec ->', mb.exec())\n"
    )
)
time.sleep(1.5)
reason = c.get_gui_state().get("defer_reason")
check("get_gui_state names the modal the task opened", reason == "modal", str(reason))
gone, dismiss_s = timed(c.dismiss_blocking_dialog)
check(
    "dismiss closes it at once",
    gone.get("success") and (gone.get("dismissed") or {}).get("title") == "Runtime live modal",
    f"{dismiss_s:.1f}s {gone}",
)
blocker.join(20)
check(
    "the task that opened it completes", (blocked.get("value") or {}).get("success"), str(blocked)
)

# --- execute_code_async: document work through commit() --------------------------
work = run_async(
    "import time\n"
    "t = time.time()\n"
    "def boxes(d):\n"
    "    for i in range(50):\n"
    "        d.addObject('Part::Box', f'Async{i}')\n"
    "    d.recompute()\n"
    f"commit(boxes, '{DOC}')\n"
    "a, b = commit(lambda d: (d.Async0.Shape.copy(),"
    " d.Async1.Shape.translated(App.Vector(5, 5, 5))), " + repr(DOC) + ")\n"
    "fused = a.fuse(b).removeSplitter()\n"  # off the GUI thread, on copies
    "def write_back(d):\n"
    "    d.addObject('Part::Feature', 'AsyncFused').Shape = fused\n"
    "    d.recompute()\n"
    f"commit(write_back, '{DOC}')\n"
    "task_print(round(fused.Volume, 3), round(time.time() - t, 2))\n"
)
volume = float((work.get("output") or "0 0").split()[0])
check("commit() workload finishes", work.get("status") == "done", str(work))
check("the off-thread fuse of copies is exact", abs(volume - 1875.0) < 1e-6, str(volume))
names = output(
    c.execute_code(
        f"d = App.getDocument('{DOC}')\n"
        "print(len([o for o in d.Objects if o.Name.startswith('Async')]),"
        " sum('execute_code_async' in n for n in d.UndoNames))"
    )
).split()
check("51 objects landed, as two undoable steps", names == ["51", "2"], str(names))

failed = run_async(f"commit(lambda d: 1 / 0, '{DOC}')", wait=10)
check(
    "an exception in fn is raised in the worker",
    failed.get("status") == "error" and "ZeroDivisionError" in (failed.get("error") or ""),
    str(failed),
)
guard = run_async(f"App.getDocument('{DOC}')", wait=10)
check(
    "App.getDocument in the worker fails at once",
    guard.get("status") == "error" and "commit(" in (guard.get("error") or ""),
    str(guard),
)
gui = run_async("FreeCADGui.SendMsgToActiveView('ViewFit')", wait=10)
check("FreeCADGui in the worker fails at once", gui.get("status") == "error", str(gui))
leak = run_async(f"commit(lambda d: d.addObject('Part::Box', 'Leak'), '{DOC}')", wait=10)
leaked = output(c.execute_code(f"print(App.getDocument('{DOC}').getObject('Leak') is None)"))
check(
    "commit() refuses a live object and rolls back",
    leak.get("status") == "error" and leaked == "True",
    f"{leak} leaked={leaked}",
)

# --- a call that times out mid-task reports its late completion ------------------
c.execute_code(RS + "rs.FreeCADRPC.EXECUTE_CODE_TIMEOUT = 2")
try:
    late = c.execute_code(
        "import time; time.sleep(4); App.ActiveDocument.addObject('Part::Box', 'LateBox')",
        None,
        DOC,
    )
finally:
    time.sleep(3)
    nxt = c.execute_code(RS + "rs.FreeCADRPC.EXECUTE_CODE_TIMEOUT = 90")
check(
    "the timeout says the call may still complete",
    late.get("may_still_complete") is True and "before retrying" in late.get("error", ""),
    str(late),
)
records = nxt.get("late_completions") or []
check(
    "the next reply carries the late completion",
    len(records) == 1
    and records[0].get("method") == "execute_code"
    and records[0].get("outcome") == "completed",
    str(records),
)
landed = output(c.execute_code(f"print(App.getDocument('{DOC}').getObject('LateBox') is not None)"))
check("and the change did land", landed == "True", landed)
again = c.execute_code("print(1)")
check("a late completion is reported once", "late_completions" not in again, str(again))

# --- cancelled tasks are not counted as queued -----------------------------------
busy, _ = in_thread(lambda: FreeCADConnection().execute_code("import time; time.sleep(3)"))
time.sleep(0.5)
# The worker's own status-bar posts are real queued tasks; only the cancelled
# one may be left out.
probe = run_async(
    GD + "gd.dispatch_to_gui(lambda: None, timeout=0.3)\n"
    "task_print(gd._rpc_request_queue.qsize() - gd.backpressure()['queue_depth'])",
    wait=10,
)
check(
    "queue_depth leaves out the timed-out task", probe.get("output", "").strip() == "1", str(probe)
)
busy.join(10)

# --- a write that slips past the guards stops the worker -------------------------
rogue = run_async(
    "import FreeCAD\n"
    f"d = FreeCAD.getDocument('{DOC}')\n"
    "b = d.addObject('Part::Box', 'Rogue')\n"
    "task_print('next line ran')\n"
    "b.Length = 3\n",
    wait=10,
)
check(
    "an off-thread write fails the task",
    rogue.get("status") == "error" and "worker thread" in (rogue.get("error") or ""),
    str(rogue),
)
check("the worker stopped before its next line", "next line ran" not in rogue.get("output", ""))
pong, ping_s = timed(c.ping)
check("FreeCAD still answers after it", pong is True and ping_s < 2.0, f"{ping_s:.2f}s")

# --- cleanup ----------------------------------------------------------------------
for name in (DOC, OTHER):
    c.execute_code(f"App.closeDocument('{name}')")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL LIVE CHECKS PASSED")
