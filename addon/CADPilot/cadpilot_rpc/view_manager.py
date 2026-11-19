"""Active-view orientation, sizing, and screenshot capture."""

import contextlib
import re
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


# A subject much longer than it is wide still gets a frame a person can read.
_MAX_ASPECT = 3.0
_MARGIN = 1.08
_SKETCH_MARGIN = 2.0


def _camera_axes(view):
    rot = view.getCameraOrientation()
    return rot.multVec(FreeCAD.Vector(1, 0, 0)), rot.multVec(FreeCAD.Vector(0, 1, 0))


def _global_shape(obj, body):
    """``obj.Shape`` where the view draws it, containers included.

    A Body's feature keeps its shape in the Body's frame; anything else carries
    its own Placement in the shape and needs only its containers' on top (a
    Body inside a turned App::Part is drawn turned).
    """
    if body is not None:
        placement = body.getGlobalPlacement()
    else:
        placement = obj.getGlobalPlacement().multiply(obj.Placement.inverse())
    if placement.isIdentity():
        return obj.Shape
    shape = obj.Shape.copy()
    shape.transformShape(placement.toMatrix())
    return shape


def _corners(objects) -> list:
    """The global bounding-box corners of ``objects``, skipping what has no finite shape."""
    from cadpilot_rpc.geometry_query import body_owner

    points = []
    for obj in objects:
        try:
            shape = getattr(obj, "Shape", None)
            if shape is None or shape.isNull():
                continue
            box = _global_shape(obj, body_owner(obj)).BoundBox
        except Exception:
            continue
        if not box.isValid() or box.DiagonalLength > 1e6:
            continue  # a datum plane's box is the whole universe
        points += [
            FreeCAD.Vector(x, y, z)
            for x in (box.XMin, box.XMax)
            for y in (box.YMin, box.YMax)
            for z in (box.ZMin, box.ZMax)
        ]
    return points


def _standalone(doc) -> list:
    """The objects drawn in their own right: not a Body's features, not helpers."""
    from cadpilot_rpc.geometry_query import body_owner

    return [o for o in doc.Objects if body_owner(o) is None and o.TypeId not in _HELPERS]


def _subject_extent(view, doc, focus):
    """Where the capture's subject is on screen: (centre, width, height, depth) with
    width and height along the camera's right and up axes and depth the (nearest,
    farthest) reach of the whole document along its line of sight, or None when
    nothing can be measured.

    The subject is the focus object, or everything shown when there is none.
    """
    if focus is not None:
        objects = [focus]
    else:
        objects = [
            o
            for o in _standalone(doc)
            if getattr(getattr(o, "ViewObject", None), "Visibility", False)
        ]
    points = _corners(objects)
    if not points:
        return None
    right, up = _camera_axes(view)
    xs = [p.dot(right) for p in points]
    ys = [p.dot(up) for p in points]
    centre = FreeCAD.Vector()
    for p in points:
        centre = centre + p
    centre = centre * (1.0 / len(points))
    sight = view.getCameraOrientation().multVec(FreeCAD.Vector(0, 0, -1))
    depths = [p.dot(sight) for p in points + _corners(_standalone(doc))]
    return (
        centre,
        max(max(xs) - min(xs), 1e-3),
        max(max(ys) - min(ys), 1e-3),
        (min(depths), max(depths)),
    )


def _image_size(view, width, height, extent) -> tuple[int, int]:
    """The picture's size: what was asked for, shaped like its subject.

    With one dimension or none given the other used to come from the window, so
    a width of 400 in a tall window made a 400 x 1173 picture with a long part
    lying across a sixth of it.
    """
    if extent is None or (width is not None and height is not None):
        return _resolve_screenshot_size(view, width, height)
    aspect = min(max(extent[1] / extent[2], 1 / _MAX_ASPECT), _MAX_ASPECT)
    if width is not None:
        return max(1, int(width)), max(1, round(int(width) / aspect))
    if height is not None:
        return max(1, round(int(height) * aspect)), max(1, int(height))
    if aspect >= 1:
        return DEFAULT_MAX_DIM, max(1, round(DEFAULT_MAX_DIM / aspect))
    return max(1, round(DEFAULT_MAX_DIM * aspect)), DEFAULT_MAX_DIM


def _fit_camera(view, extent, image_width: int, image_height: int, margin=_MARGIN) -> bool:
    """Fill the picture with the subject. True when the camera was set.

    FreeCAD's own framing fits the subject's bounding sphere into the WINDOW, so
    a long part seen end-on, or a picture shaped unlike the window, came out
    small. An orthographic camera's height is the extent of the picture's
    shorter side; a perspective camera is left to FreeCAD's framing.

    The camera is read and written as Inventor text: the node itself needs pivy,
    which a FreeCAD build may not carry ("No SWIG wrapped library loaded").
    """
    try:
        text = view.getCamera()
        if "OrthographicCamera" not in text:
            return False
        position = re.search(r"^(\s*position)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.M)
        if position is None or re.search(r"^\s*height\s+\S+\s*$", text, re.M) is None:
            return False
        centre, wide, high, (nearest, farthest) = extent
        wide, high = wide * margin, high * margin
        aspect = image_width / image_height
        height = max(high, wide / aspect) if aspect >= 1 else max(wide, high * aspect)
        right, up = _camera_axes(view)
        sight = view.getCameraOrientation().multVec(FreeCAD.Vector(0, 0, -1))
        # The camera stands in front of the whole document and sees through all
        # of it. FreeCAD's framing of one small object left it a few millimetres
        # away with the clipping planes hugging that object: a sketch came out
        # empty, and the body around it was cut off.
        reach = max(farthest - nearest, 1.0)
        at = right * centre.dot(right) + up * centre.dot(up) + sight * (nearest - 0.5 * reach - 1.0)
        text = (
            text[: position.start()]
            + f"{position.group(1)} {at.x!r} {at.y!r} {at.z!r}"
            + text[position.end() :]
        )
        for key, value in (
            ("height", height),
            ("nearDistance", 0.25 * reach),
            ("farDistance", 2.0 * reach + 2.0),
        ):
            text = re.sub(rf"^(\s*{key})\s+\S+\s*$", rf"\g<1> {value!r}", text, count=1, flags=re.M)
        view.setCamera(text)
        return True
    except Exception:
        return False


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


def _containers(obj) -> list:
    """The Bodies and Parts ``obj`` is shown through, innermost first."""
    out = []
    current = obj
    while True:
        parent = next(
            (
                p
                for p in getattr(current, "InList", [])
                if p.TypeId in ("PartDesign::Body", "App::Part")
                and current in getattr(p, "Group", [])
            ),
            None,
        )
        if parent is None or parent in out:
            return out
        out.append(parent)
        current = parent


def _tidy(doc, focus, show_sketches: bool, isolate: bool) -> list:
    """Hide what gets in the way of looking at the part; returns what to restore.

    Sketches and datums draw over the faces they lie on, and the selection
    highlight paints whole faces, so a capture taken as the user left the view
    hides the seams and edges it is taken to check. The focus object is always
    shown, and a focused sketch is shown on its Body (see ``_overlay``).
    ``isolate`` also hides everything that is not the
    focus object, inside it, or a container it is shown through: its siblings
    inside a shared App::Part go too, which hiding other top-level objects
    alone left in the picture.
    """
    changed = []

    def show(obj, visible):
        view = getattr(obj, "ViewObject", None)
        if view is None or bool(view.Visibility) == visible:
            return
        changed.append((view, "Visibility", bool(view.Visibility)))
        view.Visibility = visible

    keep = set()
    sketch = focus is not None and focus.TypeId == "Sketcher::SketchObject"
    if focus is not None:
        keep = {focus.Name, *(c.Name for c in _containers(focus))}
        keep.update(o.Name for o in getattr(focus, "OutListRecursive", []))
        # A sketch is looked at on its Body, which shows through its features:
        # they stay. (A focused FEATURE hides the later ones, and so shows the
        # part as it was at that feature.)
        body = _sketch_body(focus) if sketch else None
        if body is not None:
            keep.update(o.Name for o in body.Group)
    for obj in doc.Objects:
        if obj is focus:
            continue
        helper = obj.TypeId in _HELPERS and not show_sketches
        other = isolate and focus is not None and obj.Name not in keep
        if helper or other:
            show(obj, False)
    if focus is not None:
        show(focus, True)
        for container in _containers(focus):
            show(container, True)
        if sketch:
            changed += _overlay(focus)
    return changed


def _sketch_body(sketch):
    return next((c for c in _containers(sketch) if c.TypeId == "PartDesign::Body"), None)


# A sketch shown on its part: drawn to be seen, through a part made of glass.
_OVERLAY_LINE = (1.0, 0.0, 0.75)
_OVERLAY_WIDTH = 6.0
_OVERLAY_GLASS = 75


def _overlay(sketch) -> list:
    """Make a sketch readable on the part it belongs to; returns what to restore.

    A sketch is drawn in the same thin dark line as the solid's own edges, and
    one on a plane through the solid is behind its faces: over the part it could
    not be told from the part, or not be seen at all. The sketch gets a thick
    line in a colour no part has, and its Body turns translucent.
    """
    changed = []

    def put(view, prop, value):
        if view is None or prop not in view.PropertiesList:
            return
        with contextlib.suppress(Exception):
            before = getattr(view, prop)
            setattr(view, prop, value)
            changed.append((view, prop, before))

    view = getattr(sketch, "ViewObject", None)
    put(view, "LineColor", _OVERLAY_LINE)
    put(view, "PointColor", _OVERLAY_LINE)
    put(view, "LineWidth", _OVERLAY_WIDTH)
    put(view, "PointSize", _OVERLAY_WIDTH + 2)
    body = _sketch_body(sketch)
    if body is not None:
        put(getattr(body, "ViewObject", None), "Transparency", _OVERLAY_GLASS)
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
    highlight are hidden for it (see ``_tidy``) and put back afterwards, the
    picture is shaped like its subject and filled with it, and the user's own
    camera and the document's unsaved-changes flag are left as they were, so
    looking at a part never reads as an edit.

    Returns ``True`` on success, or an error string on failure (preserves the
    legacy GUI-handler return contract).
    """
    gdoc = None
    hidden: list = []
    modified = None
    prior_selection: list = []
    camera_before = None
    view = None
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
        with contextlib.suppress(Exception):
            camera_before = view.getCamera()
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
        extent = _subject_extent(view, doc, focus_target)
        resolved_width, resolved_height = _image_size(view, width, height, extent)
        if extent is not None:
            # a sketch is looked at for where it sits, so it gets room around it
            on_part = focus_target is not None and _sketch_body(focus_target) is not None
            sketch = on_part and focus_target.TypeId == "Sketcher::SketchObject"
            margin = _SKETCH_MARGIN if sketch else _MARGIN
            _fit_camera(view, extent, resolved_width, resolved_height, margin)
        view.saveImage(save_path, resolved_width, resolved_height, "Current")
        return True
    except Exception as e:
        return str(e)
    finally:
        for view_object, prop, value in reversed(hidden):
            with contextlib.suppress(Exception):
                setattr(view_object, prop, value)
        with contextlib.suppress(Exception):
            FreeCADGui.Selection.clearSelection()
            for doc_n, obj_n, subs in prior_selection:
                for sub in subs or [""]:
                    FreeCADGui.Selection.addSelection(doc_n, obj_n, sub)
        # The user's view is theirs: a capture from behind must not leave them
        # looking at the back of the part.
        with contextlib.suppress(Exception):
            if camera_before and view is not None:
                view.setCamera(camera_before)
        with contextlib.suppress(Exception):
            if hidden or prior_selection or camera_before:
                _flush_gui_events(delay_ms=0)
        with contextlib.suppress(Exception):
            if modified is not None and gdoc is not None and bool(gdoc.Modified) != modified:
                gdoc.Modified = modified
