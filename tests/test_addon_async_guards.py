"""execute_code_async must keep its worker away from FreeCAD documents.

Live audit: an async ``doc.addObject`` plus ``recompute`` froze the GUI thread,
the RPC server and the worker within about a second, every time. The addon
cannot be imported without FreeCAD, so the guards are compiled out of its
source into a namespace with a stub FreeCAD, and the wiring is pinned by AST.
"""

import ast
import sys
import threading
import types
from pathlib import Path

import pytest

_RPC = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "cadpilot_rpc" / "rpc_server.py"
_TREE = ast.parse(_RPC.read_text(encoding="utf-8"))

_GUARD_NAMES = {
    "_MAIN_THREAD_ID",
    "_ASYNC_DOC_HINT",
    "OffThreadDocumentAccess",
    "_WorkerFreeCAD",
    "_WorkerFreeCADGui",
    "_live_handle",
    "_poison_tracer",
    "_async_tasks",
    "_async_tasks_lock",
    "_async_threads",
    "_poisoned_threads",
}


def _defined_name(node):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        return getattr(target, "id", None)
    return None


@pytest.fixture()
def guards():
    """The guard definitions from rpc_server.py, run against stub modules."""

    class DocumentObject:
        def __init__(self, name):
            self.Name = name

    class Document:
        def __init__(self, name):
            self.Name = name

    freecad = types.SimpleNamespace(
        DocumentObject=DocumentObject,
        Document=Document,
        Vector=lambda *a: ("Vector", a),
        ActiveDocument=Document("Doc"),
        getDocument=lambda name: Document(name),
    )
    freecadgui = types.SimpleNamespace(SendMsgToActiveView=lambda msg: msg)
    module = ast.Module(
        body=[n for n in _TREE.body if _defined_name(n) in _GUARD_NAMES], type_ignores=[]
    )
    ns = {
        "threading": threading,
        "contextlib": __import__("contextlib"),
        "FreeCAD": freecad,
        "FreeCADGui": freecadgui,
    }
    exec(compile(module, str(_RPC), "exec"), ns)
    assert set(ns) >= _GUARD_NAMES, sorted(_GUARD_NAMES - set(ns))
    return types.SimpleNamespace(**ns)


def _in_worker(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except Exception as e:
            out["error"] = e

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    return out


def test_the_worker_freecad_hands_out_no_document(guards):
    app = guards._WorkerFreeCAD("App")
    out = _in_worker(lambda: app.ActiveDocument)
    assert isinstance(out["error"], guards.OffThreadDocumentAccess)
    assert "commit(" in str(out["error"])
    assert isinstance(_in_worker(lambda: app.getDocument("Doc"))["error"], RuntimeError)
    assert _in_worker(lambda: app.Vector(1, 2, 3))["value"] == ("Vector", (1, 2, 3))
    # Inside commit() the same name runs on the GUI thread and works as usual.
    assert app.ActiveDocument.Name == "Doc"


def test_the_worker_freecadgui_refuses_everything_off_the_gui_thread(guards):
    gui = guards._WorkerFreeCADGui()
    out = _in_worker(lambda: gui.SendMsgToActiveView("ViewFit"))
    assert isinstance(out["error"], guards.OffThreadDocumentAccess)
    assert gui.SendMsgToActiveView("ViewFit") == "ViewFit"


def test_commit_results_must_not_carry_live_handles(guards):
    box = guards.FreeCAD.DocumentObject("Box")
    assert guards._live_handle(box) == "document object 'Box'"
    assert guards._live_handle((box.Name, box)) == "document object 'Box'"
    assert guards._live_handle({"doc": guards.FreeCAD.Document("D")}) == "document 'D'"
    assert guards._live_handle(("Box", 3.5, None)) is None


def test_a_poisoned_worker_stops_at_its_next_line(guards):
    """FreeCAD swallows exceptions raised in an observer slot, so the observer
    only poisons the thread; the task's own next line raises."""
    filename = "<execute_code_async t1>"
    code = compile("steps.append('write')\npoison()\nsteps.append('next line')\n", filename, "exec")
    steps = []

    def poison():
        guards._poisoned_threads.add(threading.get_ident())

    def run():
        ident = threading.get_ident()
        sys.settrace(guards._poison_tracer(filename, ident))
        try:
            exec(code, {"steps": steps, "poison": poison})
        finally:
            sys.settrace(None)
            guards._poisoned_threads.discard(ident)

    out = _in_worker(run)
    assert steps == ["write"]
    assert isinstance(out["error"], guards.OffThreadDocumentAccess)


def _func(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(node):
    return {ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}


def test_the_worker_namespace_is_explicit_and_carries_commit():
    worker = _func("worker")
    assert "globals" not in _calls(worker), "the worker must not get the module namespace"
    namespace = next(
        n.value
        for n in ast.walk(worker)
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "namespace"
    )
    entries = {
        k.value: ast.unparse(v) for k, v in zip(namespace.keys, namespace.values, strict=True)
    }
    assert entries["FreeCADGui"] == "_WorkerFreeCADGui()"
    assert entries["App"].startswith("_WorkerFreeCAD(")
    assert entries["FreeCAD"].startswith("_WorkerFreeCAD(")
    assert entries["commit"].startswith("_make_commit(")


def test_async_status_updates_are_posted_not_awaited():
    """Waiting for the status bar blocked execute_code_async for up to 60s
    whenever the GUI thread was busy."""
    for name in ("_set_status", "_clear_status"):
        calls = _calls(_func(name))
        assert "dispatch_to_gui" not in calls, name
        assert "gui_dispatch.post_to_gui" in calls, name


def test_commit_runs_on_the_gui_thread_in_one_transaction():
    calls = _calls(_func("_commit_in_transaction"))
    assert {
        "doc.openTransaction",
        "step_engine.commit_transaction",
        "doc.abortTransaction",
    } <= calls
    commit = _func("commit")
    dispatch = next(
        c
        for c in ast.walk(commit)
        if isinstance(c, ast.Call) and ast.unparse(c.func) == "dispatch_to_gui"
    )
    assert {kw.arg for kw in dispatch.keywords} >= {"report_held_back"}


def test_the_off_thread_observer_watches_every_kind_of_write():
    observer = next(
        n for n in _TREE.body if isinstance(n, ast.ClassDef) and n.name == "_OffThreadWriteObserver"
    )
    slots = {n.name for n in observer.body if isinstance(n, ast.FunctionDef)}
    assert {
        "slotCreatedObject",
        "slotDeletedObject",
        "slotBeforeChangeObject",
        "slotBeforeRecomputeDocument",
        "slotCreatedDocument",
        "slotDeletedDocument",
    } <= slots
    assert "_install_off_thread_observer" in {
        ast.unparse(n.value.func)
        for n in _TREE.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
    }
