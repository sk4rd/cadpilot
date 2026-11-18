"""``execute_code`` must survive a snippet that closes or replaces the document.

Reproduced live on 1.1.4 (twice, while cleaning up test documents): a snippet
that calls ``FreeCAD.closeDocument(<the active document>)`` commits its changes,
then the wrapper's own bookkeeping reads the document NAME off a reference that
is now deleted and raises::

    ReferenceError: Cannot access attribute 'Name' of deleted object

so a snippet that had already succeeded is reported to the client as a failure.
The same landmine hits a snippet that replaces the active document
(``setActiveDocument``), which is the documented way to direct a step's journal
entry at another document.

The addon cannot be imported without FreeCAD, so this parses the source — the
same approach as the other addon guard tests.
"""

import ast
from pathlib import Path

_RPC_SERVER = (
    Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "cadpilot_rpc" / "rpc_server.py"
)
_TREE = ast.parse(_RPC_SERVER.read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _parents(body):
    parents = {}
    for node in ast.walk(body):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_guarded(node, parents) -> bool:
    """True when ``node`` sits inside a try block or a contextlib.suppress."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try):
            return True
        if isinstance(current, ast.With):
            for item in current.items:
                if any(
                    isinstance(n, ast.Attribute) and n.attr == "suppress"
                    for n in ast.walk(item.context_expr)
                ):
                    return True
        current = parents.get(current)
    return False


def test_document_name_is_not_read_after_the_snippet_could_delete_it():
    body = _func(_TREE, "execute_code")
    parents = _parents(body)
    reads = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Attribute)
        and n.attr == "Name"
        and isinstance(n.value, ast.Name)
        and n.value.id == "doc"
    ]
    assert reads, "the wrapper is expected to report which document it changed"
    unguarded = [n for n in reads if not _is_guarded(n, parents)]
    assert not unguarded, (
        "the snippet may close or replace the active document; reading doc.Name afterwards "
        "raises ReferenceError on the deleted object and reports a successful snippet as a failure"
    )


def _restore_helpers(active: str | None):
    """The restore helpers from rpc_server.py, run against a stub FreeCAD."""
    import contextlib
    import types

    state = {"active": active}

    class StubFreeCAD:
        @property
        def ActiveDocument(self):
            return types.SimpleNamespace(Name=state["active"]) if state["active"] else None

        def setActiveDocument(self, name):
            state["active"] = name

    freecad = StubFreeCAD()
    wanted = {"_active_document_name", "_restore_active_document"}
    module = ast.Module(
        body=[n for n in _TREE.body if getattr(n, "name", None) in wanted], type_ignores=[]
    )
    ns = {"contextlib": contextlib, "FreeCAD": freecad}
    exec(compile(module, str(_RPC_SERVER), "exec"), ns)
    return ns, state


def test_a_bound_run_hands_the_active_document_back():
    """execute_code(doc_name=...) activated the bound document and never gave
    the user's one back, so their view and Steps panel jumped to whatever
    document Claude last touched."""
    ns, state = _restore_helpers("UserDoc")
    previous = ns["_active_document_name"]()
    state["active"] = "Bound"  # the binding
    ns["_restore_active_document"](previous, "Bound")
    assert state["active"] == "UserDoc"

    state["active"] = "Elsewhere"  # the snippet switched documents itself
    ns["_restore_active_document"]("UserDoc", "Bound")
    assert state["active"] == "Elsewhere", "a deliberate switch must stick"

    body = _func(_TREE, "run_and_restore")
    finals = [n for n in ast.walk(body) if isinstance(n, ast.Try) and n.finalbody]
    assert any(
        "_restore_active_document" in ast.unparse(stmt) for t in finals for stmt in t.finalbody
    ), "the restore must also run when the snippet raises"
