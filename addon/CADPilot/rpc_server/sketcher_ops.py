"""Constrained-sketch construction for the RPC ``sketch`` feature op.

A sketch is built atomically: geometry + constraints are applied in one go,
the solver runs, and the result is validated. Callers wrap this in a document
transaction, so any raised error leaves the document untouched.

Result metadata (DoF, solver diagnostics) is stashed module-level and consumed
by ``feature_ops.describe_feature`` — builders only return the object itself.
"""

import contextlib
import importlib
import math

import FreeCAD
import Part
import Sketcher

# rpc_server.sketch_spec is not in rpc_server._RELOAD_ORDER, so it is reloaded
# here, with this module; a plain import would keep its old code alive across
# a hot restart.
sketch_spec = importlib.reload(importlib.import_module("rpc_server.sketch_spec"))

# Result of the most recent build_sketch_gui call; read once by
# describe_feature() via pop_last_sketch_info().
_LAST_SKETCH_INFO: dict | None = None


# A base plane is the body's own origin plane, found by Role. Attaching to it
# (instead of writing a Placement) keeps the sketch with the body and lets the
# offset be an expression; it also makes plane="XZ" and a datum plane on
# XZ_Plane the same frame by construction (a hand-copied rotation table once
# held R_x(-90) for XZ and mirrored every profile drawn on it).
_ORIGIN_ROLES = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}


def pop_last_sketch_info() -> dict | None:
    global _LAST_SKETCH_INFO
    info = _LAST_SKETCH_INFO
    _LAST_SKETCH_INFO = None
    return info


def _get_or_create_body(doc, name: str | None):
    """Resolve the PartDesign Body to build in.

    With ``name``: must exist and be a Body. Without: use the document's only
    Body, create one when absent, and refuse to guess when several exist.
    """
    if name:
        body = doc.getObject(name)
        if body is None:
            raise ValueError(f"Body '{name}' not found in document '{doc.Name}'.")
        if body.TypeId != "PartDesign::Body":
            raise ValueError(f"'{name}' is not a PartDesign::Body (TypeId={body.TypeId}).")
        return body
    bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
    if len(bodies) == 1:
        return bodies[0]
    if not bodies:
        return doc.addObject("PartDesign::Body", "Body")
    raise ValueError(
        f"Document '{doc.Name}' has multiple Bodies "
        f"({[b.Name for b in bodies]}); pass 'body' in obj_properties."
    )


# --- geometry -----------------------------------------------------------------


def _vec2(p):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise ValueError(f"sketch coordinates must be [x, y], got {p!r}")
    return FreeCAD.Vector(float(p[0]), float(p[1]), 0.0)


def _build_geometry_item(item: dict):
    """spec geometry item -> Part geometry (sketch-local 2D, z=0)."""
    gtype = item.get("type")
    if gtype == "line":
        return Part.LineSegment(_vec2(item.get("from")), _vec2(item.get("to")))
    if gtype == "arc":
        center = _vec2(item.get("center"))
        radius = float(item.get("radius"))
        a0 = math.radians(float(item.get("start_angle")))
        a1 = math.radians(float(item.get("end_angle")))
        return Part.ArcOfCircle(Part.Circle(center, FreeCAD.Vector(0, 0, 1), radius), a0, a1)
    if gtype == "circle":
        return Part.Circle(
            _vec2(item.get("center")), FreeCAD.Vector(0, 0, 1), float(item.get("radius"))
        )
    if gtype == "bspline":
        points = item.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError("bspline requires a 'points' list with >= 2 points.")
        poles = [_vec2(p) for p in points]
        bc = Part.BSplineCurve()
        try:
            bc.buildFromPoles(poles, bool(item.get("periodic", False)))
        except TypeError:
            bc.buildFromPoles(poles)
            if item.get("periodic"):
                if hasattr(bc, "makePeriodic"):
                    bc.makePeriodic()
                else:
                    raise ValueError(
                        "periodic bspline not supported by this FreeCAD version."
                    ) from None
        return bc
    if gtype == "point":
        return Part.Point(_vec2(item.get("at")))
    raise ValueError(
        f"unknown geometry type {gtype!r}; supported: {', '.join(sketch_spec.PRIMITIVES)}"
    )


def _add_geometry(sketch, geometry: list, construction: list | None) -> tuple[list, list]:
    """Add the spec's geometry; returns (GeoIds per spec item, implied constraints)."""
    prims, ids, implied = sketch_spec.expand_geometry(geometry, len(sketch.Geometry))
    construction_ids = {int(i) for i in (construction or [])}
    for prim in prims:
        try:
            geo = _build_geometry_item(prim)
        except (TypeError, KeyError, ValueError) as e:
            raise ValueError(f"geometry[{prim['entry']}] ({prim['type']}): {e}") from e
        geo_id = sketch.addGeometry(geo, prim["construction"] or prim["id"] in construction_ids)
        if geo_id != prim["id"]:
            raise RuntimeError(f"internal: expected GeoId {prim['id']}, FreeCAD returned {geo_id}")
    return ids, implied


# --- external geometry ----------------------------------------------------------


def _external_binder(body, doc, ref, elem):
    """Bridge an out-of-body external target through a SubShapeBinder.

    PartDesign forbids external geometry outside the sketch's body; the
    canonical bridge is a SubShapeBinder inside the body referencing the
    target sub-element. Idempotent per (object, element) pair.
    """
    name = f"Ext_{ref.Name}_{elem}"
    binder = doc.getObject(name)
    if binder is None:
        binder = body.newObject("PartDesign::SubShapeBinder", name)
        binder.Support = [(ref, (elem,))]
        doc.recompute()
        view = getattr(binder, "ViewObject", None)
        if view is not None:
            view.Visibility = False
    return binder


def _add_external(sketch, doc, body, external):
    """spec external: [[obj_name, "EdgeN"|"VertexN"], ...] -> GeoIds -3, -4, ...

    External geometry lets a sketch reference another object's edges/vertices
    (cross-part parametric references). The i-th entry occupies GeoId -(3+i);
    constraints reference it like any geometry id, e.g. [-3, "start"].
    Targets outside the sketch's body are bridged via a SubShapeBinder
    (PartDesign scope rule); the binder's shape holds exactly that one
    sub-element, so it is referenced as its own Edge1/Vertex1.
    """
    for i, item in enumerate(external or []):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(
                f"external[{i}] must be [object_name, 'EdgeN'|'VertexN'], got {item!r}"
            )
        ref = doc.getObject(item[0])
        if ref is None:
            raise ValueError(f"external[{i}]: object '{item[0]}' not found.")
        elem = str(item[1])
        if elem.startswith("Edge"):
            pool, n = ref.Shape.Edges, int(elem[4:])
        elif elem.startswith("Vertex"):
            pool, n = ref.Shape.Vertexes, int(elem[6:])
        else:
            raise ValueError(f"external[{i}]: element must be Edge/Vertex, got {elem!r}")
        if n < 1 or n > len(pool):
            raise ValueError(
                f"external[{i}]: '{elem}' out of range on '{item[0]}' (1-{len(pool)})."
            )
        if ref in getattr(body, "Group", []):
            sketch.addExternal(ref.Name, elem)  # addExternal takes (str, str)
        else:
            binder = _external_binder(body, doc, ref, elem)
            # Binder shape = just this one sub-element -> its local name.
            sketch.addExternal(binder.Name, "Edge1" if elem.startswith("Edge") else "Vertex1")


# --- constraints ----------------------------------------------------------------


def _geometry_kinds(sketch) -> dict:
    """GeoId -> kind for the sketch as it is now (ExternalGeo[0:2] are the axes)."""
    return sketch_spec.geometry_kinds(
        [type(g).__name__ for g in sketch.Geometry],
        [type(g).__name__ for g in list(sketch.ExternalGeo)[2:]],
    )


def _add_constraints(sketch, constraints: list, implied: list = ()) -> tuple[list, list]:
    """Add the spec's constraints, then those its composite geometry implies.

    The implied ones go last so the spec's own entries keep their order.
    Returns (origins indexed like Constraints, unnamed dimensional entries).
    """
    origins = [None] * len(sketch.Constraints)
    taken = {k.Name: f"Constraints[{i}]" for i, k in enumerate(sketch.Constraints) if k.Name}
    planned = sketch_spec.plan_constraints(constraints, _geometry_kinds(sketch), taken)
    for plan in [*planned, *implied]:
        args = plan["args"]
        if plan["datum"] is None:
            con = Sketcher.Constraint(*args)
        else:
            con = Sketcher.Constraint(*args, plan["datum"])
        idx = sketch.addConstraint(con)
        if plan["name"]:
            sketch.renameConstraint(idx, plan["name"])
        if plan["expr"] is not None:
            try:
                sketch.setExpression(f"Constraints[{idx}]", plan["expr"])
            except Exception as e:
                raise ValueError(
                    f"constraints[{plan['entry']}] ({plan['type']}): expression binding failed: {e}"
                ) from e
        origins.append(sketch_spec.origin_of(plan))
    return origins, sketch_spec.unnamed_dimensions(planned)


# --- attachment -----------------------------------------------------------------

# Direction words for the semantic face selector. Face names are re-derived
# after every feature, so a name read off one feature can silently mean a
# different face on the next one: the sketch then attaches to the wrong plane
# and its pocket cuts air while still reporting success (seen live — a sketch
# meant for the top attached to the +Y side after the bore pocket shifted the
# names). A direction is stable across features.
_FACE_WORDS = {
    "top": "+Z",
    "bottom": "-Z",
    "right": "+X",
    "left": "-X",
    "back": "+Y",
    "front": "-Y",
    # Alternates an LLM reaches for (and that FreeCAD's own datum/frame names
    # resemble): "MaxX" is clearly +X but used to die as an out-of-range FaceN.
    "maxx": "+X",
    "minx": "-X",
    "maxy": "+Y",
    "miny": "-Y",
    "maxz": "+Z",
    "minz": "-Z",
    "xmax": "+X",
    "xmin": "-X",
    "ymax": "+Y",
    "ymin": "-Y",
    "zmax": "+Z",
    "zmin": "-Z",
    "x+": "+X",
    "x-": "-X",
    "y+": "+Y",
    "y-": "-Y",
    "z+": "+Z",
    "z-": "-Z",
}


def attachment_ref(obj):
    """The object an attachment should NAME, so it cannot form a dependency cycle.

    A sketch (or datum plane) attached to a PartDesign **Body**'s face is a cycle
    waiting for the next feature: the Body's Shape IS its Tip's result, so the
    moment a feature built on that profile joins the body the graph closes
    (Body.Shape -> Pocket -> profile sketch -> Body.Shape). FreeCAD neither fails
    nor warns: the new feature stays ``Touched`` forever and reading its Shape
    raises the bare "shape is invalid", which reads like a broken profile.

    Live-caught in a plan run (document Stress2): a bolt-hole sketch created with
    ``plane={"face": ["Body", "+Z"]}`` made every pocket on it fail with
    "produced an invalid Shape", and the failure then cascaded (the polar pattern
    could not find its base). Re-pointing the SAME sketch at the Pad feature made
    the identical pocket compute exactly 57207.213 mm^3.

    Resolution: name the Body's Tip, which owns the same faces under the same
    numbering (the Body's Shape is its result), or the adopted BaseFeature when
    the body has no tip. Anything that is not a Body comes back unchanged.
    """
    if getattr(obj, "TypeId", "") != "PartDesign::Body":
        return obj
    tip = getattr(obj, "Tip", None)
    if tip is not None:
        return tip
    base = getattr(obj, "BaseFeature", None)
    return base if base is not None else obj


def _resolve_semantic_face(ref, token: str) -> str:
    """Resolve '+Z' / 'top' / '-X' … to the name of the matching planar face."""
    want = _FACE_WORDS.get(str(token).lower(), str(token).upper())
    if want not in ("+X", "-X", "+Y", "-Y", "+Z", "-Z"):
        raise ValueError(
            f"plane.face must be [obj, 'FaceN'] or a direction (+X/-Z/top/bottom/…), got {token!r}"
        )
    axis = "XYZ".index(want[1])
    sign = 1.0 if want[0] == "+" else -1.0
    best: tuple[float, str] | None = None
    for i, face in enumerate(ref.Shape.Faces, start=1):
        try:
            if face.Surface.TypeId != "Part::GeomPlane":
                continue
            normal = face.normalAt(0, 0)
        except Exception:
            continue
        if normal[axis] * sign < 0.999:  # must face along ±axis
            continue
        # Several faces can share a direction (e.g. a stepped top): take the
        # farthest along it, which is the outer face a user means.
        rank = face.CenterOfMass[axis] * sign
        if best is None or rank > best[0]:
            best = (rank, f"Face{i}")
    if best is None:
        raise ValueError(f"'{ref.Name}' has no planar face facing {want}")
    return best[1]


def _vec3(vec) -> list:
    """A vector in the same rounded form as the other echoes (4 decimals)."""
    return [round(float(v), 4) + 0.0 for v in vec]


def _frame_echo(sketch) -> dict:
    """The sketch's origin and axes in world coordinates (after its attachment).

    Every attachment needs it. A face's own center reads as "the sketch origin
    is here" while the origin sits on the face's parametric origin, a corner
    (live: a centered rib profile hung half outside the part, the volume still
    adding up); and which world direction the sketch's x runs along on a side
    face had to be looked up (it ran along -Y).
    """
    placement = sketch.getGlobalPlacement()
    rot = placement.Rotation
    return {
        "sketch_origin": _vec3(placement.Base),
        "x_axis": _vec3(rot.multVec(FreeCAD.Vector(1, 0, 0))),
        "y_axis": _vec3(rot.multVec(FreeCAD.Vector(0, 1, 0))),
        "normal": _vec3(rot.multVec(FreeCAD.Vector(0, 0, 1))),
    }


def _face_echo(obj, face_name: str) -> dict:
    """The face a selector resolved to: name, center, normal, and the other
    planar faces facing the same way (largest first) — on a stepped part the
    pick can be right and still not the face the caller pictured."""
    entry = {"object": obj.Name, "face": face_name}
    with contextlib.suppress(Exception):
        face = obj.Shape.getElement(face_name)
        if face is not None:
            entry["center"] = _vec3(face.CenterOfMass)
            if face.Surface.TypeId == "Part::GeomPlane":
                normal = face.normalAt(0, 0)
                entry["normal"] = _vec3(normal)
                same = [
                    {"face": f"Face{i}", "center": _vec3(f.CenterOfMass), "area": round(f.Area, 4)}
                    for i, f in enumerate(obj.Shape.Faces, start=1)
                    if f.Surface.TypeId == "Part::GeomPlane"
                    and not f.isSame(face)
                    and f.normalAt(0, 0).dot(normal) > 0.999
                ]
                if same:
                    entry["same_direction"] = sorted(same, key=lambda e: -e["area"])[:6]
    return entry


def _face_center_point(face) -> tuple[FreeCAD.Vector, bool]:
    """The face's middle: the AREA centroid, and whether it lies on material.

    ``Face.CenterOfMass`` is the area centroid. For a face with an INNER WIRE
    (a hole) or a concave shape it can sit off the material — the old fallback
    for that case (midpoint of the longest edge's vertices) is the SEAM for
    any closed edge, so centering a sketch on a holed face teleported its
    origin onto the rim (live: a bolt circle meant for r25 landed at x=35 and
    the pocket cut air, reported as plain success). The centroid IS the
    honest "middle of the face" — the caller offsets from it predictably —
    so it is always returned and the off-material case is flagged for the
    echo instead of being silently replaced by an arbitrary rim point.
    """
    point = FreeCAD.Vector(face.CenterOfMass)
    try:
        on_material = bool(face.isInside(point, 1e-5, True))
    except Exception:
        on_material = True
    return point, on_material


def center_attachment(obj, ref, face_name, doc, extra_offset: float = 0.0):
    """Move an attached object's origin to the middle of its face.

    A ``FlatFace`` attachment lands the object's origin on the face's
    PARAMETRIC origin — for a rectangular side face that is a corner. A circle
    meant for the middle of a 100x60 face therefore ended up at the corner and
    the pocket cut a clipped half-hole through the wall, reported as plain
    success (live). ``plane={"face": [...], "center": true}`` asks for the
    middle; the delta is stored as an AttachmentOffset in the attachment's own
    frame, so the association with the face is kept. Returns a warning when
    that middle is off the material, else None.
    """
    doc.recompute()
    try:
        n = int(str(face_name)[4:]) if str(face_name).startswith("Face") else 0
        face = ref.Shape.Faces[n - 1]
    except Exception:
        return
    target, on_material = _face_center_point(face)
    placement = obj.Placement
    delta = placement.Rotation.inverted().multVec(target - placement.Base)
    if extra_offset:
        # The caller's `offset` is along the face normal (= the attachment's Z).
        delta = delta + FreeCAD.Vector(0, 0, extra_offset)
    obj.AttachmentOffset = FreeCAD.Placement(delta, FreeCAD.Rotation())
    doc.recompute()
    if not on_material:
        # A hole or a concave section swallows the centroid: the origin sits
        # at the visual middle, but the material is an offset ring/limb away.
        return (
            "the face's middle lies off the material (a hole or a concave "
            "section) — the sketch origin is placed there anyway, so offset "
            "the profile from it rather than drawing at [0, 0]"
        )
    return None


def origin_plane(body, key: str):
    """The body's origin plane for "XY"/"XZ"/"YZ"."""
    role = _ORIGIN_ROLES[key]
    for feat in getattr(getattr(body, "Origin", None), "OriginFeatures", []):
        if getattr(feat, "Role", "") == role:
            return feat
    raise ValueError(f"body '{body.Name}' has no {role} in its Origin.")


def _offset_spec(spec, plane) -> tuple[float, str | None]:
    """(offset in mm, expression) from plane.offset or the top-level offset."""
    raw = spec.get("offset")
    if isinstance(plane, dict) and plane.get("offset") is not None:
        raw = plane["offset"]
    try:
        return sketch_spec.offset_value(raw)
    except ValueError as e:
        raise ValueError(f"offset {e}") from e


def _apply_offset(obj, offset: float, expr: str | None) -> None:
    """Shift an attached object along its normal, by a number or an expression."""
    base = FreeCAD.Vector(obj.AttachmentOffset.Base)
    base.z += offset
    obj.AttachmentOffset = FreeCAD.Placement(base, FreeCAD.Rotation())
    if expr:
        try:
            obj.setExpression(".AttachmentOffset.Base.z", expr)
        except Exception as e:
            raise ValueError(f"offset expression {expr!r} failed: {e}") from e


def _face_fragility(ref, face_name: str, token: str, via: str = "") -> str:
    picked = f" (picked by {token!r})" if token != face_name else ""
    return (
        f"{via}hangs off {ref.Name}.{face_name}{picked}. A face reference can break when "
        "upstream geometry changes: FreeCAD re-derives face names, so after an edit the "
        f"attachment can move to another face, or read '?{face_name}' and go Invalid. For a "
        "sketch that survives edits, attach it to a base plane with an expression offset "
        '(plane="XY", offset="=Params.h") or to a datum plane on an origin plane.'
    )


def _attach_sketch(sketch, doc, spec, body) -> tuple[dict, list[str]]:
    """Attach ``sketch`` as ``spec`` says; returns (echo, warnings)."""
    plane = spec.get("plane", "XY")
    offset, offset_expr = _offset_spec(spec, plane)
    support_prop = "AttachmentSupport" if hasattr(sketch, "AttachmentSupport") else "Support"

    if isinstance(plane, str):
        key = plane.upper()
        if key not in _ORIGIN_ROLES:
            raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
        support = origin_plane(body, key)
        setattr(sketch, support_prop, [(support, "")])
        sketch.MapMode = "FlatFace"
        _apply_offset(sketch, offset, offset_expr)
        echo = {
            "plane": key,
            "support": support.Name,
            "offset": f"={offset_expr}" if offset_expr else offset,
        }
        return echo, []

    if isinstance(plane, dict) and plane.get("face"):
        face = plane["face"]
        if not (isinstance(face, (list, tuple)) and len(face) == 2):
            raise ValueError("plane.face must be [object_name, 'FaceN'].")
        ref = doc.getObject(face[0])
        if ref is None:
            raise ValueError(f"plane face object '{face[0]}' not found.")
        # A Body's face must be named through its Tip: attaching to the Body
        # itself makes the next feature built on this profile a dependency cycle
        # (see attachment_ref).
        ref = attachment_ref(ref)
        token = face_name = str(face[1])
        if face_name.startswith(("+", "-")) or face_name.lower() in _FACE_WORDS:
            face_name = _resolve_semantic_face(ref, face_name)
        n = int(face_name[4:]) if face_name.startswith("Face") else 0
        if n < 1 or n > len(ref.Shape.Faces):
            if not face_name.startswith("Face"):
                raise ValueError(
                    f"plane.face[1] must be a face name ('FaceN', 1-"
                    f"{len(ref.Shape.Faces)}) or a direction token ('+Z', '-X', 'top', "
                    "'bottom', 'front', 'back', 'left', 'right', 'MaxX', ...), got "
                    f"{face_name!r}."
                )
            raise ValueError(
                f"'{face_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Faces)})."
            )
        setattr(sketch, support_prop, [(ref, face_name)])
        sketch.MapMode = "FlatFace"
        warnings = [_face_fragility(ref, face_name, token, "the sketch ")]
        echo = _face_echo(ref, face_name)
        echo["centered"] = bool(plane.get("center"))
        if plane.get("center"):
            # Origin at the middle of the face instead of its parametric
            # origin (a corner on rectangular faces); the caller's offset is
            # along the face normal = the attachment frame's Z.
            off_material = center_attachment(sketch, ref, face_name, doc)
            if off_material:
                warnings.append(off_material)
            shift = _vec3(sketch.AttachmentOffset.Base)[:2]
            echo["center_offset"] = shift
            warnings.append(
                f"center: true is fixed at build time: the origin was moved to the face's "
                f"middle by a constant AttachmentOffset {shift} in the face's frame, which "
                "does not follow later dimension changes. To keep a profile centred as the "
                "parameters change, sketch on a base or datum plane and dimension it from the "
                "spreadsheet (e.g. distance_x '=Params.width / 2')."
            )
        _apply_offset(sketch, offset, offset_expr)
        if not plane.get("center"):
            echo["note"] = (
                "the sketch origin sits on the face's parametric origin (a corner on a "
                'rectangular face); pass "center": true to put it at the face center'
            )
        return echo, warnings

    if isinstance(plane, dict) and plane.get("datum"):
        ref = doc.getObject(str(plane["datum"]))
        if ref is None:
            raise ValueError(f"datum plane '{plane['datum']}' not found.")
        setattr(sketch, support_prop, [(ref, "")])
        sketch.MapMode = "FlatFace"
        echo = {"object": ref.Name, "type": "datum"}
        warnings = []
        with contextlib.suppress(Exception):
            # Follow the datum's own support so the echo names the REAL base
            # face the sketch hangs off. LinkSub sub-elements arrive as a list
            # or tuple — str() on it leaked a Python repr ("('Face3',)").
            support = list(getattr(ref, "AttachmentSupport", None) or [])
            if support and support[0]:
                subs = support[0][1] if len(support[0]) > 1 else ""
                if isinstance(subs, (list, tuple)):
                    subs = subs[0] if subs else ""
                if subs:
                    echo["support"] = _face_echo(support[0][0], str(subs))
                    if str(subs).startswith("Face"):
                        warnings.append(
                            _face_fragility(
                                support[0][0], str(subs), str(subs), f"the datum plane {ref.Name} "
                            )
                        )
                else:
                    echo["support"] = {"object": support[0][0].Name}
        _apply_offset(sketch, offset, offset_expr)
        return echo, warnings

    raise ValueError(
        f"plane must be XY/XZ/YZ or {{'face': ...}} or {{'datum': ...}}, got {plane!r}"
    )


# --- solver diagnostics -----------------------------------------------------------


def _solver_diagnostics(sketch) -> dict:
    """FreeCAD's raw solver lists (1-based), whatever this version exposes."""
    diag = {}
    for attr in sketch_spec.DIAGNOSTIC_KEYS:
        value = getattr(sketch, attr, None)
        if value:
            diag[attr] = [int(i) for i in value]
    return diag


def _solve(sketch, origins) -> tuple:
    """Solve; raise with the offending spec entries named. Returns (dof, diagnostics)."""
    rc = sketch.solve()
    dof = getattr(sketch, "DoF", None)
    diag = sketch_spec.map_diagnostics(_solver_diagnostics(sketch), origins)
    if rc != 0 or diag.get("conflicting"):
        reason = sketch_spec.SOLVE_RESULTS.get(rc, "solver error")
        detail = sketch_spec.diagnostics_text(diag) or f"state={list(map(str, sketch.State))}"
        raise RuntimeError(
            f"sketch solve failed (rc={rc}, {reason}); DoF={dof}; {detail}. "
            "Remove or change those constraints and retry."
        )
    return dof, diag


# --- entry point ------------------------------------------------------------------


def _sketch_to_replace(doc, existing, spec):
    """The existing sketch emptied for a rebuild, with its body; refuses without ``replace``.

    A second ``sketch`` call with the same name used to make "Name001" next to
    the original, so the caller's edit landed on a copy no feature used.
    """
    name = existing.Name
    is_sketch = existing.TypeId == "Sketcher::SketchObject"
    if not spec.get("replace"):
        hint = (
            f"change it with cad(operation='sketch_edit', obj_name='{name}', ...), or pass "
            '"replace": true to rebuild it in place (same object, so features built on it '
            "follow)"
            if is_sketch
            else "choose another name"
        )
        raise ValueError(f"'{name}' already exists in '{doc.Name}' ({existing.TypeId}); {hint}.")
    if not is_sketch:
        raise ValueError(f"replace: '{name}' is a {existing.TypeId}, not a sketch.")
    body = existing.getParentGeoFeatureGroup()
    if body is None or body.TypeId != "PartDesign::Body":
        raise ValueError(f"replace: sketch '{name}' is not inside a PartDesign Body.")
    if spec.get("body") and spec["body"] != body.Name:
        raise ValueError(f"replace: sketch '{name}' lives in '{body.Name}', not '{spec['body']}'.")
    for path, _expr in list(existing.ExpressionEngine):
        if "Constraints" in path or "AttachmentOffset" in path:
            existing.setExpression(path, None)
    existing.deleteAllConstraints()
    existing.deleteAllGeometry()
    for i in reversed(range(len(existing.ExternalGeometry))):
        existing.delExternal(i)
    existing.AttachmentOffset = FreeCAD.Placement()
    return existing, body


def build_sketch_gui(doc, spec):
    """Build a constrained sketch atomically; returns the SketchObject.

    Raises on any failure — the caller's transaction wrapper aborts, so the
    document stays untouched.
    """
    global _LAST_SKETCH_INFO
    _LAST_SKETCH_INFO = None

    geometry = spec.get("geometry")
    if not isinstance(geometry, list) or not geometry:
        raise ValueError("sketch requires a non-empty 'geometry' list.")

    name = spec.get("base")
    existing = doc.getObject(name) if name else None
    if existing is not None:
        sketch, body = _sketch_to_replace(doc, existing, spec)
    else:
        body = _get_or_create_body(doc, spec.get("body"))
        sketch = body.newObject("Sketcher::SketchObject", name or "Sketch")

    attachment, warnings = _attach_sketch(sketch, doc, spec, body)
    geometry_ids, implied = _add_geometry(sketch, geometry, spec.get("construction"))
    _add_external(sketch, doc, body, spec.get("external"))
    origins, unnamed = _add_constraints(sketch, spec.get("constraints"), implied)
    doc.recompute()
    dof, diag = _solve(sketch, origins)

    _LAST_SKETCH_INFO = {
        "object_name": sketch.Name,
        "body": body.Name,
        "dof": int(dof) if dof is not None else None,
        "fully_constrained": (dof == 0) if dof is not None else None,
        "geometry_count": len(sketch.Geometry),
        "constraint_count": len(sketch.Constraints),
        "external_count": len(spec.get("external") or []),
        "diagnostics": diag or None,
    }
    if any(len(i) != 1 for i in geometry_ids):
        _LAST_SKETCH_INFO["geometry_ids"] = geometry_ids
    if existing is not None:
        _LAST_SKETCH_INFO["replaced"] = True
    if unnamed:
        _LAST_SKETCH_INFO["unnamed_dimensions"] = unnamed
    # Which face/plane the sketch actually landed on, with its center and
    # normal — a direction token on a stepped part picks the FARTHEST face
    # facing that way, and without this echo the wrong pick looked exactly
    # like success (live: a seat-face sketch landed on a wall top 92 mm up).
    _LAST_SKETCH_INFO["plane"] = {**attachment, **_frame_echo(sketch)}
    if warnings:
        _LAST_SKETCH_INFO["warnings"] = warnings
    return sketch
