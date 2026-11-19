"""Active-view orientation, sizing, and screenshot capture."""

import contextlib
from typing import Any

import FreeCAD
import FreeCADGui

from cadpilot_rpc.gui_dispatch import _flush_gui_events

_VIEW_DISPATCH = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Top": "viewTop",
    "Right": "viewRight",
    "Back": "viewBack",
    "Left": "viewLeft",
    "Bottom": "viewBottom",
    "Dimetric": "viewDimetric",
    "Trimetric": "viewTrimetric",
}


def _get_view_size(view: Any) -> tuple[int, int]:
    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            return max(1, int(size[0])), max(1, int(size[1]))
        return max(1, int(size.width())), max(1, int(size.height()))
    except Exception:
        return 1024, 768


# Default cap applied when the caller does not request an explicit size:
# the long edge is scaled down to this many pixels. Full-viewport PNGs
# base64-encode to hundreds of KB, and every pixel is paid as tokens by
# the LLM client. Callers can always override with explicit width/height.
DEFAULT_MAX_DIM = 384


def _resolve_screenshot_size(
    view: Any,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    view_width, view_height = _get_view_size(view)
    if width is None and height is None:
        long_edge = max(view_width, view_height)
        if long_edge > DEFAULT_MAX_DIM:
            scale = DEFAULT_MAX_DIM / long_edge
            return max(1, round(view_width * scale)), max(1, round(view_height * scale))
        return view_width, view_height
    resolved_width = view_width if width is None else max(1, int(width))
    resolved_height = view_height if height is None else max(1, int(height))
    return resolved_width, resolved_height


_STD_COMMAND_DISPATCH = {
    "Isometric": "Std_ViewIsometric",
    "Front": "Std_ViewFront",
    "Top": "Std_ViewTop",
    "Right": "Std_ViewRight",
    "Back": "Std_ViewRear",
    "Left": "Std_ViewLeft",
    "Bottom": "Std_ViewBottom",
    "Dimetric": "Std_ViewDimetric",
    "Trimetric": "Std_ViewTrimetric",
}


def apply_view_orientation(view: Any, view_name: str) -> None:
    method_name = _VIEW_DISPATCH.get(view_name)
    if method_name is None:
        raise ValueError(f"Invalid view name: {view_name}")
    if hasattr(view, method_name):
        getattr(view, method_name)()
    else:
        # Fallback for views that lack the direct Python method
        # (e.g. some FreeCAD versions / view types)
        cmd = _STD_COMMAND_DISPATCH.get(view_name)
        if cmd:
            FreeCADGui.runCommand(cmd)
        else:
            FreeCAD.Console.PrintWarning(
                f"apply_view_orientation: no method or command for '{view_name}'\n"
            )


def _send_viewselection(gdoc) -> None:
    """Frame the selection in the RIGHT view: without a binding the foreground
    view; with one, the bound document's own views — ``SendMsgToActiveView``
    would hit whatever tab a concurrent agent holds in front."""
    if gdoc is not None:
        gdoc.sendMsgToViews("ViewSelection")
    else:
        FreeCADGui.SendMsgToActiveView("ViewSelection")


_HELPERS = (
    "Sketcher::SketchObject",
    "PartDesign::Plane",
    "PartDesign::Line",
    "PartDesign::Point",
    "PartDesign::CoordinateSystem",
    "PartDesign::ShapeBinder",
    "PartDesign::SubShapeBinder",
    "App::Origin",
    "App::Plane",
    "App::Line",
    "App::Point",
)


def _owner(obj):
    """The Body or Part a member is shown through, else the object itself."""
    for parent in getattr(obj, "InList", []):
        if parent.TypeId in ("PartDesign::Body", "App::Part") and obj in getattr(
            parent, "Group", []
        ):
            return _owner(parent)
    return obj


def _tidy(doc, focus, show_sketches: bool, isolate: bool) -> list:
    """Hide what gets in the way of looking at the part; returns what to restore.

    Sketches and datums draw over the faces they lie on, and the selection
    highlight paints whole faces, so a capture taken as the user left the view
    hides the seams and edges it is taken to check. The focus object is always
    shown, sketch or not. ``isolate`` also hides every other top-level object.
    """
    changed = []

    def show(obj, visible):
        view = getattr(obj, "ViewObject", None)
        if view is None or bool(view.Visibility) == visible:
            return
        changed.append((view, bool(view.Visibility)))
        view.Visibility = visible

    keep = _owner(focus) if focus is not None else None
    for obj in doc.Objects:
        if obj is focus:
            continue
        helper = obj.TypeId in _HELPERS and not show_sketches
        other = isolate and keep is not None and _owner(obj) is obj and obj is not keep
        if helper or other:
            show(obj, False)
    if focus is not None:
        show(focus, True)
        if keep is not focus:
            show(keep, True)
    return changed


def save_active_screenshot(
    save_path: str,
    view_name: str = "Isometric",
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
    doc_name: str | None = None,
    show_sketches: bool = False,
    isolate: bool = False,
):
    """Save a PNG of the DOCUMENT's active view to ``save_path``.

    With ``doc_name`` the capture targets THAT document's view; without it,
    the foreground view. saveImage() renders OFFSCREEN, so a background
    document (another agent's foreground tab) captures without stealing MDI
    focus — no tab flip, the other agent never notices. ``Gui activation``
    is deliberately absent: FreeCADGui.activateDocument does not even exist
    on 1.1.x, and flipping the foreground tab would yank the MDI focus out
    from under a concurrent agent.

    The capture shows the part as built: sketches, datums and the selection
    highlight are hidden for it (see ``_tidy``) and put back afterwards, and
    the document's unsaved-changes flag is left as it was, so looking at a part
    never reads as an edit.

    Returns ``True`` on success, or an error string on failure (preserves the
    legacy GUI-handler return contract).
    """
    gdoc = None
    hidden: list = []
    modified = None
    prior_selection: list = []
    try:
        if doc_name:
            doc = FreeCAD.getDocument(doc_name)
            gdoc = FreeCADGui.getDocument(doc_name)
            view = gdoc.activeView()
        else:
            # The focus lookup must search the document the captured VIEW
            # shows (the GUI foreground), not App.ActiveDocument: bound calls
            # keep flipping the latter under a concurrent agent.
            gdoc = FreeCADGui.ActiveDocument
            view = gdoc.ActiveView
            doc = gdoc.Document
        if not hasattr(view, "saveImage"):
            return "Current view does not support screenshots"

        focus_target = doc.getObject(focus_object) if (doc and focus_object) else None
        modified = bool(gdoc.Modified)
        # The selection is process-global GUI state and belongs to the user (or
        # another agent): save it, clear it for the capture, hand it back.
        prior_selection = [
            (s.DocumentName, s.ObjectName, list(s.SubElementNames))
            for s in FreeCADGui.Selection.getSelectionEx("", 0)
        ]
        hidden = _tidy(doc, focus_target, show_sketches, isolate)
        # FreeCAD turns the camera over half a second, and framing the focus
        # object in the middle of that turn stops it there: a Front view of one
        # object came out at whatever angle the turn had reached.
        animated = None
        with contextlib.suppress(Exception):
            animated = bool(view.isAnimationEnabled())
            if animated:
                view.setAnimationEnabled(False)
        try:
            apply_view_orientation(view, view_name)
        finally:
            if animated:
                with contextlib.suppress(Exception):
                    view.setAnimationEnabled(True)

        def frame():
            if focus_target is None:
                view.fitAll()
                return
            # The selection is how FreeCAD frames one object; it must be gone
            # again before the capture, or the object comes out highlighted.
            FreeCADGui.Selection.clearSelection()
            FreeCADGui.Selection.addSelection(focus_target)
            _send_viewselection(gdoc)
            FreeCADGui.Selection.clearSelection()

        FreeCADGui.Selection.clearSelection()
        frame()
        _flush_gui_events()
        # On macOS, when the FreeCAD window is not exposed (fully occluded or
        # minimized), saveImage() right after pumping the event loop grabs a blank
        # frame. Re-issuing the framing synchronously forces a redraw first. The
        # flush above is kept intentionally — Linux needs it for the stale-frame
        # fix (#51/#53).
        frame()
        resolved_width, resolved_height = _resolve_screenshot_size(view, width, height)
        view.saveImage(save_path, resolved_width, resolved_height, "Current")
        return True
    except Exception as e:
        return str(e)
    finally:
        with contextlib.suppress(Exception):
            for view_object, visible in hidden:
                view_object.Visibility = visible
        with contextlib.suppress(Exception):
            FreeCADGui.Selection.clearSelection()
            for doc_n, obj_n, subs in prior_selection:
                for sub in subs or [""]:
                    FreeCADGui.Selection.addSelection(doc_n, obj_n, sub)
        with contextlib.suppress(Exception):
            if hidden or prior_selection:
                _flush_gui_events(delay_ms=0)
        with contextlib.suppress(Exception):
            if modified is not None and gdoc is not None and bool(gdoc.Modified) != modified:
                gdoc.Modified = modified
