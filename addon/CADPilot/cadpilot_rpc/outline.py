"""The model tree as text: what a person reads off FreeCAD's tree view, the
property panel and an open sketch, before changing a part.

Read-only on purpose: nothing here solves a sketch, recomputes or touches a
ViewObject, because each of those marks the user's document as changed.
``get_objects`` used to serialize every property of every object (111 KB for a
two-body bracket); an edit needs the names, the order, the dimensions and what
they are bound to, which is a few KB.
"""

from __future__ import annotations

import math
import re

import FreeCAD

try:
    import FreeCADGui
except ImportError:  # freecadcmd
    FreeCADGui = None

_DATUMS = (
    "PartDesign::Plane",
    "PartDesign::Line",
    "PartDesign::Point",
    "PartDesign::CoordinateSystem",
)

# The properties that say what a feature does; the rest is defaults and caches.
_FEATURE_PROPS = (
    "Type",
    "Length",
    "Length2",
    "Offset",
    "Midplane",
    "SideType",
    "Reversed",
    "Angle",
    "Occurrences",
    "Mode",
    "Spacings",
    "Size",
    "Radius",
    "Value",
    "Diameter",
    "Depth",
    "DepthType",
    "Refine",
    "Suppressed",
)
_LINK_PROPS = (
    "Profile",
    "Originals",
    "Base",
    "MirrorPlane",
    "Direction",
    "Axis",
    "Sections",
    "Spine",
)
_DEFAULTS = {"Type": "Length", "SideType": "One side", "Refine": True, "Mode": "Extent"}

# Constraints that place geometry rather than dimension it: listed by type, one line each.
_GEOMETRIC = (
    "Coincident",
    "Horizontal",
    "Vertical",
    "Parallel",
    "Perpendicular",
    "Tangent",
    "Equal",
    "PointOnObject",
    "Symmetric",
    "Block",
    "InternalAlignment",
)
_POS = {0: "", 1: ".start", 2: ".end", 3: ".center"}
_NO_GEO = -2000


def _num(v) -> str:
    if hasattr(v, "Value"):
        v = v.Value
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{round(v, 4):g}"
    return str(v)


def _clip(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 4] + " ..."


def _listed(values) -> str:
    """A list property; a long one (a pattern's spacings) by its length and distinct values."""
    shown = [_num(x) for x in values]
    if len(shown) <= 6:
        return "[" + ", ".join(shown) + "]"
    return f"[{len(shown)} values: {', '.join(sorted(set(shown), key=shown.index)[:6])}]"


def _expressions(obj) -> dict[str, str]:
    return {path.lstrip("."): expr for path, expr in getattr(obj, "ExpressionEngine", [])}


def _state(obj) -> str:
    """What is wrong with an object, '' when nothing is."""
    bad = [s for s in obj.State if s in ("Invalid", "Error", "Restore")]
    if not bad:
        return ""
    reason = ""
    try:
        reason = obj.getStatusString()
    except Exception:
        reason = ""
    return f" **{', '.join(bad)}{': ' + reason if reason and reason not in bad else ''}**"


def _support(obj) -> str:
    support = getattr(obj, "AttachmentSupport", None) or []
    names = []
    for o, subs in support:
        subs = subs if isinstance(subs, (list, tuple)) else [subs]
        names.append(o.Name + ("." + ",".join(s for s in subs if s) if any(subs) else ""))
    text = ", ".join(names) or "nothing"
    offset = getattr(obj, "AttachmentOffset", None)
    if offset is not None and not offset.isIdentity():
        bound = _expressions(obj).get("AttachmentOffset.Base.z")
        base = offset.Base
        moved = _num(base.z) if abs(base.x) < 1e-9 and abs(base.y) < 1e-9 else str(offset)
        text += f", offset {moved}{' = ' + bound if bound else ''}"
    return text


def _sketch_summary(sk) -> str:
    edges = sum(1 for i in range(sk.GeometryCount) if not sk.getConstruction(i))
    state = "fully constrained" if getattr(sk, "FullyConstrained", False) else "UNDER-CONSTRAINED"
    return f"Sketch on {_support(sk)}; {edges} edges; {state}"


def _dimension_lines(sk, indent: str) -> list[str]:
    exprs = _expressions(sk)
    out = []
    for i, c in enumerate(sk.Constraints):
        if c.Type in _GEOMETRIC and not c.Name:
            continue
        if not c.Driving and not c.Name:
            continue
        expr = (exprs.get(f"Constraints.{c.Name}") if c.Name else None) or exprs.get(
            f"Constraints[{i}]"
        )
        value = f"{math.degrees(c.Value):g} deg" if c.Type == "Angle" else _num(c.Value)
        ref = "" if c.Driving else " (reference)"
        out.append(
            f"{indent}{c.Name or '#' + str(i)}: {c.Type} {value}{' = ' + expr if expr else ''}{ref}"
        )
    return out


def _feature_text(obj) -> str:
    exprs = _expressions(obj)
    parts = []
    for prop in _FEATURE_PROPS:
        if prop not in obj.PropertiesList:
            continue
        v = getattr(obj, prop)
        if prop not in exprs and (v in (False, None, "", []) or _DEFAULTS.get(prop) == v):
            continue
        if prop == "Length2" and getattr(obj, "Type", "") != "TwoLengths":
            continue
        if prop in ("Offset", "Angle") and prop not in exprs and abs(getattr(v, "Value", v)) < 1e-9:
            continue
        if prop == "Length" and getattr(obj, "Type", "Length") not in ("Length", "TwoLengths"):
            continue
        shown = _listed(v) if isinstance(v, (list, tuple)) else _num(v)
        bound = _clip(exprs[prop]) if prop in exprs else ""
        parts.append(f"{prop} {shown}{' = ' + bound if bound else ''}")
    for prop in _LINK_PROPS:
        if prop not in obj.PropertiesList:
            continue
        v = getattr(obj, prop)
        subs: list[str] = []
        if isinstance(v, tuple) and len(v) == 2 and hasattr(v[0], "Name"):
            subs = [s for s in (v[1] if isinstance(v[1], (list, tuple)) else [v[1]]) if s]
            v = v[0]
        names = (
            [o.Name for o in v if hasattr(o, "Name")]
            if isinstance(v, list)
            else [v.Name]
            if hasattr(v, "Name")
            else []
        )
        if names:
            shown = ", ".join(names)
            if subs:
                shown += (
                    f" [{len(subs)} sub-elements]"
                    if len(subs) > 4
                    else " [" + ", ".join(subs) + "]"
                )
            parts.append(f"{prop} {shown}")
    return "; ".join(parts)


def _kind(obj) -> str:
    return obj.TypeId.split("::")[-1]


def _label(obj) -> str:
    return obj.Name if obj.Label == obj.Name else f"{obj.Name} (labelled '{obj.Label}')"


def _body_header(body) -> str:
    features = [
        o for o in body.Group if o.TypeId.startswith("PartDesign::") and o.TypeId not in _DATUMS
    ]
    tip = body.Tip
    bits = [f"{len(features)} features", f"tip {tip.Name if tip is not None else 'none'}"]
    if tip is not None and features and features[-1] is not tip and tip in features:
        later = len(features) - 1 - features.index(tip)
        bits[-1] += f" (NOT the last feature: {later} after it are rolled back)"
    shape = body.Shape
    if not shape.isNull():
        bb = shape.BoundBox
        bits.append(f"{round(shape.Volume, 1):g} mm3")
        bits.append(f"{_num(bb.XLength)} x {_num(bb.YLength)} x {_num(bb.ZLength)} mm")
        bits.append(f"{len(shape.Solids)} solid" + ("" if len(shape.Solids) == 1 else "s"))
    props = [
        f"{p} {_num(getattr(body, p))}"
        for p in body.PropertiesList
        if body.getGroupOfProperty(p) not in ("Base", "", "Attachment")
        and p not in ("Tip", "BaseFeature")
        and body.getTypeIdOfProperty(p)
        in ("App::PropertyString", "App::PropertyInteger", "App::PropertyFloat")
        and getattr(body, p) not in ("", None)
    ]
    extra = f"; {'; '.join(props)}" if props else ""
    return f"{_label(body)} (Body: {', '.join(bits)}{extra}){_state(body)}"


def _body_lines(body, dimensions: bool) -> list[str]:
    out = [_body_header(body)]
    width = max((len(o.Name) for o in body.Group), default=0)
    for obj in body.Group:
        if obj.TypeId == "App::Origin":
            continue
        head = f"  {obj.Name.ljust(width)}  "
        if obj.TypeId == "Sketcher::SketchObject":
            out.append(f"{head}{_sketch_summary(obj)}{_state(obj)}")
            if dimensions:
                out += _dimension_lines(obj, "      ")
        elif obj.TypeId in _DATUMS:
            out.append(f"{head}{_kind(obj)} on {_support(obj)}{_state(obj)}")
        else:
            text = _feature_text(obj)
            out.append(f"{head}{_kind(obj)}{' ' + text if text else ''}{_state(obj)}")
    return out


def _cell_order(cell: str):
    return (int(re.sub(r"\D", "", cell) or 0), re.sub(r"\d", "", cell))


def _next_column(cell: str) -> str:
    col, row = re.sub(r"\d", "", cell), re.sub(r"\D", "", cell)
    return f"{col[:-1]}{chr(ord(col[-1]) + 1)}{row}" if col and col[-1] != "Z" else ""


def _sheet_lines(sheet, notes: bool) -> list[str]:
    cells = sorted(sheet.getUsedCells(), key=_cell_order)
    aliased = [(c, sheet.getAlias(c)) for c in cells if sheet.getAlias(c)]
    out = [f"{_label(sheet)} (Spreadsheet, {len(aliased)} aliases){_state(sheet)}"]
    for cell, alias in aliased:
        content = sheet.getContents(cell)
        try:
            value = _num(sheet.get(alias))
        except Exception as e:
            value = f"ERROR {e}"
        line = f"  {alias} = {value}{' (' + content + ')' if content.startswith('=') else ''}"
        if notes:
            note = ""
            right = _next_column(cell)
            if right:
                note = sheet.getContents(right).lstrip("'")
            line = f"  {cell} {line.strip()}{'  -- ' + note if note else ''}"
        out.append(line)
    return out


def _point(p) -> str:
    return f"({_num(p.x)}, {_num(p.y)})"


def _geometry_line(sk, i: int) -> str:
    g = sk.Geometry[i]
    kind = type(g).__name__
    if kind == "LineSegment":
        text = f"line {_point(g.StartPoint)} -> {_point(g.EndPoint)}"
    elif kind == "ArcOfCircle":
        text = f"arc center {_point(g.Center)} r {_num(g.Radius)}, {_point(g.StartPoint)} -> {_point(g.EndPoint)}"
    elif kind == "Circle":
        text = f"circle center {_point(g.Center)} r {_num(g.Radius)}"
    elif kind == "Point":
        text = f"point ({_num(g.X)}, {_num(g.Y)})"
    else:
        text = kind
    return f"  {i} {text}{' [construction]' if sk.getConstruction(i) else ''}"


def _ref(geo: int, pos: int) -> str:
    if geo == _NO_GEO:
        return ""
    name = {-1: "x-axis", -2: "y-axis"}.get(geo, str(geo) if geo >= 0 else f"external{-geo - 3}")
    if geo == -1 and pos == 1:
        return "origin"
    return name + _POS.get(pos, "")


def _constraint_refs(c) -> str:
    refs = [_ref(c.First, c.FirstPos), _ref(c.Second, c.SecondPos), _ref(c.Third, c.ThirdPos)]
    return ", ".join(r for r in refs if r)


def _sketch_lines(sk) -> list[str]:
    body = next((o for o in sk.InList if o.TypeId == "PartDesign::Body"), None)
    users = [o.Name for o in sk.InList if o.TypeId != "PartDesign::Body"]
    out = [
        f"{_label(sk)} ({_sketch_summary(sk)}"
        f"{'; in ' + body.Name if body else ''}{'; used by ' + ', '.join(users) if users else ''}){_state(sk)}"
    ]
    for problem in ("ConflictingConstraints", "RedundantConstraints", "MalformedConstraints"):
        bad = list(getattr(sk, problem, []) or [])
        if bad:
            out.append(f"{problem}: {bad}")
    out.append("geometry (index, sketch coordinates):")
    out += [_geometry_line(sk, i) for i in range(sk.GeometryCount)]
    external = list(getattr(sk, "ExternalGeometry", []) or [])
    if external:
        out.append("external: " + "; ".join(f"{o.Name}.{','.join(subs)}" for o, subs in external))
    exprs = _expressions(sk)
    dims, placed = [], {}
    for i, c in enumerate(sk.Constraints):
        if c.Type in _GEOMETRIC and not c.Name:
            placed.setdefault(c.Type, []).append(_constraint_refs(c).replace(", ", "="))
            continue
        expr = (exprs.get(f"Constraints.{c.Name}") if c.Name else None) or exprs.get(
            f"Constraints[{i}]"
        )
        value = f"{math.degrees(c.Value):g} deg" if c.Type == "Angle" else _num(c.Value)
        dims.append(
            f"  {i} {c.Name or '(unnamed)'}: {c.Type} {value}{' = ' + expr if expr else ''}"
            f"{'' if c.Driving else ' (reference)'}  [{_constraint_refs(c)}]"
        )
    out.append("dimensions (index, name, value, binding, what it measures):")
    out += dims or ["  none"]
    for kind, refs in placed.items():
        out.append(f"{kind}: {'; '.join(refs)}")
    return out


def _selection_lines() -> list[str]:
    if FreeCADGui is None:
        return []
    out = []
    for s in FreeCADGui.Selection.getSelectionEx("", 0):
        for sub in [x for x in s.SubElementNames if x] or [""]:
            obj = s.Object
            parts = sub.split(".")
            # a path through a body ends in the feature that made the face or edge
            for name in parts[:-1]:
                obj = obj.Document.getObject(name) or obj
            text = f"{s.DocumentName}: {obj.Name} ({_kind(obj)})"
            element = parts[-1]
            if element:
                text += f" {element}{_element_text(s.Object, sub)}"
            out.append(text)
    return out


def _vec(v) -> str:
    return f"({_num(v.x)}, {_num(v.y)}, {_num(v.z)})"


def _element_text(obj, sub: str) -> str:
    """Where a selected face, edge or vertex is, so "this face" can be found again."""
    try:
        shape = obj.getSubObject(sub)
        kind = shape.ShapeType
        if kind == "Face":
            surface = type(shape.Surface).__name__
            u0, u1, v0, v1 = shape.ParameterRange
            normal = shape.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            return f": {surface} at {_vec(shape.CenterOfMass)}, normal {_vec(normal)}, {_num(shape.Area)} mm2"
        if kind == "Edge":
            curve = type(shape.Curve).__name__
            if len(shape.Vertexes) == 2:
                return (
                    f": {curve} {_vec(shape.Vertexes[0].Point)} -> {_vec(shape.Vertexes[1].Point)}"
                )
            return f": {curve} at {_vec(shape.CenterOfMass)}, length {_num(shape.Length)}"
        if kind == "Vertex":
            return f": {_vec(shape.Point)}"
    except Exception:
        pass
    return ""


def _document_flags(doc) -> list[str]:
    flags = []
    if FreeCAD.ActiveDocument is doc:
        flags.append("active")
    gdoc = None
    if FreeCADGui is not None:
        try:
            gdoc = FreeCADGui.getDocument(doc.Name)
        except Exception:
            gdoc = None
    if gdoc is not None:
        if gdoc.Modified:
            flags.append("unsaved changes")
        editing = gdoc.getInEdit()
        if editing is not None:
            flags.append(f"the user is editing {editing.Object.Name}")
        view = gdoc.ActiveView
        body = view.getActiveObject("pdbody") if hasattr(view, "getActiveObject") else None
        if body is not None:
            flags.append(f"active body {body.Name}")
    touched = [o.Name for o in doc.Objects if "Touched" in o.State]
    if touched:
        flags.append(
            f"needs recompute: {', '.join(touched[:6])}{' ...' if len(touched) > 6 else ''}"
        )
    return flags


def document_outline(doc, limit: int = 50, offset: int = 0) -> str:
    """The whole document: its sheets, each body's features in order, loose objects, the selection."""
    flags = _document_flags(doc)
    out = [
        f"{doc.Name}: {doc.FileName or '(never saved)'}{' [' + '; '.join(flags) + ']' if flags else ''}"
    ]
    in_body = set()
    for body in (o for o in doc.Objects if o.TypeId == "PartDesign::Body"):
        in_body.update(o.Name for o in body.OutListRecursive)
    for sheet in (o for o in doc.Objects if o.TypeId == "Spreadsheet::Sheet"):
        out += ["", *_sheet_lines(sheet, notes=False)]
    for body in (o for o in doc.Objects if o.TypeId == "PartDesign::Body"):
        out += ["", *_body_lines(body, dimensions=False)]
    loose = [
        o
        for o in doc.Objects
        if o.TypeId not in ("PartDesign::Body", "Spreadsheet::Sheet") and o.Name not in in_body
    ]
    if loose:
        page = loose[offset : offset + limit]
        out += ["", f"other objects ({len(loose)}, showing {offset + 1}-{offset + len(page)}):"]
        for o in page:
            shape = getattr(o, "Shape", None)
            volume = ""
            if shape is not None and not shape.isNull() and shape.Solids:
                volume = f", {round(shape.Volume, 1):g} mm3"
            out.append(f"  {_label(o)} ({o.TypeId}{volume}){_state(o)}")
    if doc is FreeCAD.ActiveDocument or len(FreeCAD.listDocuments()) == 1:
        selected = [s for s in _selection_lines() if s.startswith(doc.Name + ":")]
        out += [
            "",
            "selected: "
            + ("; ".join(s.split(": ", 1)[1] for s in selected) if selected else "nothing"),
        ]
    return "\n".join(out)


def _feature_lines(obj, body) -> list[str]:
    """One feature of a Body: what it is set to, what it is built on and where that is."""
    text = _feature_text(obj)
    base = getattr(obj, "BaseFeature", None)
    users = [
        o.Name for o in obj.InList if o is not body and getattr(o, "BaseFeature", None) is not obj
    ]
    out = [
        f"{_label(obj)} ({_kind(obj)} in {body.Name}"
        f"{'; follows ' + base.Name if base is not None else '; first feature'}"
        f"{'; used by ' + ', '.join(users) if users else ''}"
        f"{'; the tip' if body.Tip is obj else ''}){_state(obj)}"
    ]
    if text:
        out.append("  " + text.replace("; ", "\n  "))
    for prop in ("Base", "Profile"):
        v = getattr(obj, prop, None)
        if isinstance(v, tuple) and len(v) == 2 and hasattr(v[0], "Name"):
            subs = [s for s in (v[1] if isinstance(v[1], (list, tuple)) else [v[1]]) if s]
            for sub in subs[:24]:
                out.append(f"  {v[0].Name}.{sub}{_element_text(v[0], sub)}")
            if len(subs) > 24:
                out.append(f"  ... {len(subs) - 24} more sub-elements")
    shape = getattr(obj, "Shape", None)
    if shape is not None and not shape.isNull():
        out.append(f"  the body up to here: {round(shape.Volume, 1):g} mm3")
    return out


def object_outline(obj) -> str | None:
    """One sheet, body, sketch or Body feature in full; None for anything else."""
    if obj.TypeId == "Spreadsheet::Sheet":
        return "\n".join(_sheet_lines(obj, notes=True))
    if obj.TypeId == "PartDesign::Body":
        return "\n".join(_body_lines(obj, dimensions=True))
    if obj.TypeId == "Sketcher::SketchObject":
        return "\n".join(_sketch_lines(obj))
    body = next((o for o in obj.InList if o.TypeId == "PartDesign::Body" and obj in o.Group), None)
    if body is not None and obj.TypeId.startswith("PartDesign::") and obj.TypeId not in _DATUMS:
        return "\n".join(_feature_lines(obj, body))
    return None
