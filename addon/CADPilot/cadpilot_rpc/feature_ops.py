"""Parametric feature creation for the RPC ``create_feature`` handler.

Every feature is a live FreeCAD object (Part::Fillet, Part::Loft, ...)
linked to its source objects, so edits to sources recompute downstream.
Builders raise on any error; the caller (_run_op_with_screenshot) aborts
the transaction, so a failed feature leaves no residue.
"""

import contextlib
from typing import Any

import FreeCAD
import Part

from cadpilot_rpc import sketcher_ops, tip_policy
from cadpilot_rpc.geometry_query import body_owner, body_volumes, model_issues, recompute_report
from cadpilot_rpc.property_mapper import float_list_expression, format_color, parse_color

# Selector resolution of the most recent face-based build; read once by
# describe_feature() (mirrors sketcher_ops' sketch-info mechanism).
_LAST_FEATURE_INFO: dict | None = None

FEATURE_TYPES = (
    "boolean",
    "fillet",
    "chamfer",
    "loft",
    "sweep",
    "mirror",
    "pattern",
    "move",
    "color",
    "variables",
    "sketch",
    "sketch_edit",
    "pad",
    "pocket",
    "revolution",
    "groove",
    "thickness",
    "draft",
    "datum_plane",
    "hull",
    "hole",
)

_AXIS = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}
_MIRROR_PLANE = {"XY": (0, 0, 1), "XZ": (0, 1, 0), "YZ": (1, 0, 0)}


def _require(spec, *keys):
    missing = [k for k in keys if spec.get(k) is None]
    if missing:
        raise ValueError(f"{spec.get('type')} requires: {', '.join(missing)}")


def _get_obj(doc, name, role="object"):
    obj = doc.getObject(name)
    if obj is None:
        raise ValueError(f"{role} '{name}' not found in document '{doc.Name}'.")
    return obj


def _is_direction_token(item: str) -> bool:
    return item.startswith(("+", "-")) or item.lower() in sketcher_ops._FACE_WORDS


#: Edges a geometric selector resolved to, for the reply (read by describe_feature).
_EDGE_SELECTION: list | None = None

_EDGE_FILTERS = ("face", "parallel", "type", "radius", "at")
_EDGE_TYPES = {"line": "Part::GeomLine", "circle": "Part::GeomCircle"}


def _edge_matches(edge, sel, shape_box) -> bool:
    curve = edge.Curve.TypeId
    if "type" in sel and curve != _EDGE_TYPES[sel["type"]]:
        return False
    if "radius" in sel and (
        curve != "Part::GeomCircle" or abs(edge.Curve.Radius - float(sel["radius"])) > 1e-4
    ):
        return False
    if "parallel" in sel:
        if curve != "Part::GeomLine":
            return False
        run = edge.Vertexes[-1].Point - edge.Vertexes[0].Point
        if run.Length < 1e-9 or run.cross(_axis_vec(sel["parallel"])).Length > 1e-6 * run.Length:
            return False
    if "at" in sel:
        token = sketcher_ops._FACE_WORDS.get(str(sel["at"]).lower(), str(sel["at"]).upper())
        if token not in ("+X", "-X", "+Y", "-Y", "+Z", "-Z"):
            raise ValueError(f"edges.at must be a direction like '+Z' or 'top', got {sel['at']!r}")
        axis = "XYZ"[("X", "Y", "Z").index(token[1])]
        extreme = getattr(shape_box, f"{axis}Max" if token[0] == "+" else f"{axis}Min")
        low, high = getattr(edge.BoundBox, f"{axis}Min"), getattr(edge.BoundBox, f"{axis}Max")
        if abs(low - extreme) > 1e-5 or abs(high - extreme) > 1e-5:
            return False
    return True


def _select_edges(obj, selector) -> list[str]:
    """Geometric edge selectors, resolved now against the current shape.

    A fillet/chamfer otherwise needs the edges' generated names, which meant
    paging through get_topology (live: 282 edges for one chamfer). Each dict is
    an AND of filters, a list of dicts their union:
    face (a face's boundary: '+Y' or 'FaceN'), parallel ('X'/'Y'/'Z' or a
    vector; straight edges), type ('line'/'circle'), radius, at ('+Z': lying
    at the part's extreme in that direction).
    """
    global _EDGE_SELECTION
    shape = obj.Shape
    box = shape.BoundBox
    names: list[str] = []
    for sel in selector if isinstance(selector, list) else [selector]:
        unknown = sorted(set(sel) - set(_EDGE_FILTERS))
        if unknown or not sel:
            raise ValueError(
                f"edge selector {sel!r}: unknown key(s) {unknown}; use {', '.join(_EDGE_FILTERS)}."
            )
        if "type" in sel and sel["type"] not in _EDGE_TYPES:
            raise ValueError(f"edges.type must be 'line' or 'circle', got {sel['type']!r}")
        pool = list(enumerate(shape.Edges, start=1))
        if "face" in sel:
            face = shape.getElement(_resolve_elements(obj, [sel["face"]], "Face")[0])
            pool = [(i, e) for i, e in pool if any(e.isSame(b) for b in face.Edges)]
        hits = [f"Edge{i}" for i, e in pool if _edge_matches(e, sel, box)]
        if not hits:
            raise ValueError(f"edge selector {sel!r} matched no edge of '{obj.Name}'.")
        names.extend(h for h in hits if h not in names)
    _EDGE_SELECTION = []
    for name in names:
        with contextlib.suppress(Exception):
            center = shape.getElement(name).CenterOfMass
            _EDGE_SELECTION.append({"edge": name, "center": [round(float(v), 4) for v in center]})
    return names


def _resolve_elements(obj, selector, kind):
    """Selector -> sub-element names. kind: 'Edge' or 'Face'.

    Faces also accept the direction tokens a sketch's plane.face takes
    ('+Z', 'top', 'MaxX', …): face names are re-derived after every feature, so
    an LLM that knows the direction but not the generated name had to run a
    get_topology round-trip first (live workflow feedback). 'all' and explicit
    index/name lists work exactly as before.
    """
    if kind == "Edge" and (
        isinstance(selector, dict)
        or (isinstance(selector, list) and selector and all(isinstance(i, dict) for i in selector))
    ):
        return _select_edges(obj, selector)
    elements = getattr(obj.Shape, "Edges" if kind == "Edge" else "Faces")
    all_names = [f"{kind}{i + 1}" for i in range(len(elements))]
    if selector == "all":
        return all_names
    if isinstance(selector, str):
        if kind == "Face" and _is_direction_token(selector):
            return [sketcher_ops._resolve_semantic_face(obj, selector)]
        raise ValueError(f"selector must be 'all' or a non-empty list, got {selector!r}")
    if not isinstance(selector, list) or not selector:
        raise ValueError(f"selector must be 'all' or a non-empty list, got {selector!r}")
    names = []
    for item in selector:
        if isinstance(item, int):
            if not 0 <= item < len(all_names):
                raise ValueError(f"{kind} index {item} out of range (0-{len(all_names) - 1}).")
            names.append(all_names[item])
        elif isinstance(item, str):
            if item in all_names:
                names.append(item)
                continue
            if kind == "Face" and _is_direction_token(item):
                names.append(sketcher_ops._resolve_semantic_face(obj, item))
                continue
            raise ValueError(f"'{item}' not a valid sub-element (e.g. {all_names[0]}).")
        else:
            raise ValueError(f"selector items must be int or str, got {item!r}")
    return names


def _record_resolved_faces(base, names) -> None:
    """Echo the faces a selector resolved to, so a wrong pick is visible.

    A direction token picks the FARTHEST face facing that way; on a stepped
    part that can be a different face than the caller pictured, and without an
    echo the result looked exactly like success (live: a circle meant for the
    seat face landed on a wall top 92 mm above it). Faces facing the same way
    are listed as ``same_direction`` — on a phone stand the "+Z" pick was
    right, it was just one of three upward faces, and the alternative was
    only discoverable by trial and error.
    """
    global _LAST_FEATURE_INFO
    info = []
    for name in names:
        entry = {"face": name}
        with contextlib.suppress(Exception):
            face = base.Shape.getElement(name)
            if face is not None:
                entry["center"] = [round(float(v), 4) for v in face.CenterOfMass]
                if face.Surface.TypeId == "Part::GeomPlane":
                    normal = face.normalAt(0, 0)
                    entry["normal"] = [round(float(v), 4) for v in normal]
                    same = []
                    for i, f in enumerate(base.Shape.Faces):
                        if f.Surface.TypeId != "Part::GeomPlane" or f.isSame(face):
                            continue
                        if f.normalAt(0, 0).dot(normal) > 0.999:
                            same.append(
                                {
                                    "face": f"Face{i + 1}",
                                    "center": [round(float(v), 4) for v in f.CenterOfMass],
                                    "area": round(float(f.Area), 4),
                                }
                            )
                    if same:
                        entry["same_direction"] = sorted(same, key=lambda e: -e["area"])[:6]
        info.append(entry)
    _LAST_FEATURE_INFO = {"base": base.Name, "faces": info}


def pop_last_feature_info() -> dict | None:
    """The last selector resolution, read once by describe_feature()."""
    global _LAST_FEATURE_INFO
    info = _LAST_FEATURE_INFO
    _LAST_FEATURE_INFO = None
    return info


def _axis_vec(value, default="Z"):
    if isinstance(value, (list, tuple, dict)):
        vec = _vec3(value, "axis")
        if vec.Length < 1e-12:
            raise ValueError(f"axis vector must not be zero, got {value!r}")
        vec.normalize()
        return vec
    v = _AXIS.get(str(value or default).upper())
    if v is None:
        raise ValueError(f"axis must be 'X'/'Y'/'Z' or a direction [x, y, z], got {value!r}")
    return FreeCAD.Vector(*v)


def _inherit_appearance(feat, base):
    """Copy color/transparency from the base solid to a feature result.

    Part booleans create a fresh ViewObject in the default gray, silently
    dropping whatever color the user set on the base. Only the two
    appearance properties that matter for solids are copied; every access
    is guarded because ViewObject is absent in console mode and the
    assignment can fail on exotic view providers.
    """
    src = getattr(base, "ViewObject", None)
    dst = getattr(feat, "ViewObject", None)
    if src is None or dst is None:
        return
    for prop in ("ShapeColor", "Transparency"):
        with contextlib.suppress(Exception):
            setattr(dst, prop, getattr(src, prop))


def _build_boolean(doc, spec):
    _require(spec, "op", "base", "tool")
    op = spec["op"]
    if op not in ("fuse", "cut", "common"):
        raise ValueError(f"boolean op must be fuse/cut/common, got {op!r}")
    base_obj = _get_obj(doc, spec["base"], "base")
    tool_val = spec["tool"]
    if isinstance(tool_val, list):
        if not tool_val:
            raise ValueError("boolean tool list must not be empty.")
        tools = [_get_obj(doc, name, "tool") for name in tool_val]
    else:
        tools = [_get_obj(doc, tool_val, "tool")]
    name = spec.get("name") or op.capitalize()
    if len(tools) == 1:
        feat = doc.addObject(
            {"fuse": "Part::Fuse", "cut": "Part::Cut", "common": "Part::Common"}[op], name
        )
        feat.Base, feat.Tool = base_obj, tools[0]
    elif op == "fuse":
        # Part::MultiFuse, NOT a compound Tool: a compound of OVERLAPPING tools
        # does not merge them, and the result double-counts the overlap —
        # measured live on a box + arm + knuckle, a compound Tool gave 17461.9
        # mm^3 against the true 15765.5, with the knuckle only partly present.
        feat = doc.addObject("Part::MultiFuse", name)
        feat.Shapes = [base_obj, *tools]
    elif op == "common":
        # "common" with several tools means intersecting ALL of them;
        # Part::MultiCommon is FreeCAD's own form of that. (A compound Tool
        # computes base ∩ (T1 ∪ T2) instead — measured 800 vs 0 mm^3 for the
        # same inputs, a silent semantic flip.)
        feat = doc.addObject("Part::MultiCommon", name)
        feat.Shapes = [base_obj, *tools]
    else:  # cut: base - (T1 ∪ T2), the chained-cut result (verified equal)
        union = doc.addObject("Part::MultiFuse", f"{name}_tools")
        union.Shapes = tools
        doc.recompute()
        view = getattr(union, "ViewObject", None)
        if view is not None:
            view.Visibility = False
        feat = doc.addObject("Part::Cut", name)
        feat.Base, feat.Tool = base_obj, union
    _inherit_appearance(feat, base_obj)
    return feat


def _require_end_of_chain(body, base, op):
    """Refuse a dress-up whose base is not the end of its Body's chain.

    FreeCAD moves ``Body.Tip`` onto a newly created PartDesign feature, so
    dressing a mid-chain feature makes the body SHOW the dress-up and silently
    drop everything after it — measured live: a fillet on the pad under a flange
    left the body at 15999 mm^3 with the six bolt holes gone. PartDesign offers no
    safe mid-chain insert through the API (``Body.insertObject`` duplicated the
    Group entry and still left the tip wrong), so the op refuses. The caller runs
    it in a transaction, so refusing leaves the document untouched.
    """
    tip = getattr(body, "Tip", None)
    if tip_policy.dress_base_is_allowed(base_in_body=True, base_is_tip=tip is base):
        return
    tip_name = tip.Name if tip is not None else "none"
    raise ValueError(
        f"{op} base '{base.Name}' is inside Body '{body.Name}' but is not its Tip "
        f"({tip_name}). A dress-up is built at the END of the chain, and FreeCAD would "
        "move the Body's Tip onto it, silently dropping every later feature (pockets and "
        "patterns included). Dress the Tip instead, or reorder the features in FreeCAD's GUI."
    )


def _build_fillet_chamfer(doc, spec, kind):
    """Build a fillet/chamfer where it belongs: inside the Body when the base
    lives in one, or IS one, at the document root otherwise.

    A ``Part::Fillet`` is a document-root object — it is not in the Body's
    Group, it does not follow the Body's Placement, and ``Body.Tip = <it>`` is
    accepted silently while leaving the Body ``['Touched', 'Invalid']``. A base
    that is a bare Part-level object still wants the Part::Fillet path.
    """
    # Function-top on purpose: the bare-base branch below also writes
    # _LAST_FEATURE_INFO, and when the declaration lived inside the named_body
    # branch that write only reached the global through Python's
    # whole-function scoping — removing that branch would have silenced the
    # hidden_base report (the same trap _build_color was caught by).
    global _LAST_FEATURE_INFO
    size_key = tip_policy.dress_spec_key(kind)
    _require(spec, "base", "edges", size_key)
    base = _get_obj(doc, spec["base"], "base")
    body = _parent_body(base)
    target = base
    named_body = False
    if body is None and getattr(base, "TypeId", "") == "PartDesign::Body":
        # Naming the BODY is how a caller naturally asks for "round the rim of
        # this part", but a Body has no parent Body, so this used to fall
        # through to the root-level ``Part::Fillet``: outside the Body's Group,
        # Body.Tip still on the last feature, and the Body's displayed shape
        # never changed while the reply said "created successfully" (live: a 2mm
        # fillet on a bore rim left the Body at 17591.5 mm^3, the filleted
        # 17545.9 sitting beside it as a second, invisible solid). The GUI does
        # the PartDesign thing here: dress the Body's Tip, inside the Body.
        body = base
        target = getattr(base, "Tip", None)
        named_body = True
        if target is None:
            raise ValueError(
                f"Body '{base.Name}' has no Tip, so there is nothing to {kind}: it holds no "
                "feature yet. Build the feature first (pad/pocket/…), then dress its edges."
            )
    names = _resolve_elements(target, spec["edges"], "Edge")
    raw_size = spec[size_key]
    size = raw_size if _is_expression(raw_size) else _number(raw_size, f"{kind} {size_key}")
    label = spec.get("name") or f"{kind.capitalize()}"
    if body is not None:
        _require_end_of_chain(body, target, kind)
        # FreeCAD 1.1's PartDesign dress-up holds ONE scalar size for all edges
        # (the per-edge tuple form is Part-level only).
        feat = body.newObject(tip_policy.dress_type(kind, True), label)
        _apply_refine(feat, spec)

        def _probe(subset, feat=feat, target=target, doc=doc):
            feat.Base = (target, list(subset))
            doc.recompute()
            return _dress_shape_usable(feat)

        feat.Base = (target, names)
        _set_or_bind(feat, tip_policy.dress_size_property(kind), size)
        _remember_dress_context(kind, size, names, _probe)
        if named_body:
            # The caller named the BODY, so the reply must say which of its
            # features actually got dressed: "which feature is the Tip" is the
            # one thing a Body-named dress-up resolves for them.
            _LAST_FEATURE_INFO = {"dressed": target.Name, "body": body.Name}
        return feat
    if _is_expression(size):
        raise ValueError(
            f"a Part-level {kind} stores one {size_key} per edge, which cannot be bound to an "
            f"expression; pass a number, or build '{base.Name}' as a PartDesign Body feature "
            "to bind it."
        )
    feat = doc.addObject(tip_policy.dress_type(kind, False), label)
    if hasattr(feat, "EdgeLinks"):
        # FreeCAD >= 1.1 rework: Base is a plain link and per-edge sizes live
        # in Edges as (1-based edge index, size_start, size_end) tuples.
        feat.Base = base
        feat.Edges = [(int(n[4:]), size, size) for n in names]

        def _probe(subset, feat=feat, base=base, doc=doc, size=size):
            feat.Base = base
            feat.Edges = [(int(n[4:]), size, size) for n in subset]
            doc.recompute()
            return _dress_shape_usable(feat)

    else:
        feat.Base = (base, names)
        setattr(feat, tip_policy.dress_size_property(kind), size)

        def _probe(subset, feat=feat, base=base, doc=doc):
            feat.Base = (base, list(subset))
            doc.recompute()
            return _dress_shape_usable(feat)

    _remember_dress_context(kind, size, names, _probe)
    # The base is redundant once its own rim is dressed (same solid, sharper
    # edges) and FreeCAD draws BOTH, so the caller was left with two
    # overlapping solids and no hint which one to keep (live: a fillet on a
    # bare Part::Box left both visible). The color is inherited first, so
    # hiding the base cannot expose a default-gray feature where a colored
    # part was. The base's data is untouched — only its display.
    _inherit_appearance(feat, base)
    view = getattr(base, "ViewObject", None)
    if view is not None:
        with contextlib.suppress(Exception):
            view.Visibility = False
        _LAST_FEATURE_INFO = {"hidden_base": base.Name}
    return feat


def _build_loft(doc, spec):
    _require(spec, "profiles")
    profiles = [_get_obj(doc, n, "profile") for n in spec["profiles"]]
    if len(profiles) < 2:
        raise ValueError("loft requires at least 2 profiles.")
    # Loft has no base object (CAD_NO_BASE_OPERATIONS), so cad()'s obj_name
    # arrives as spec["base"] — that is the documented name of the NEW loft.
    # Reading only spec["name"] silently named every loft "Loft".
    feat = doc.addObject("Part::Loft", spec.get("name") or spec.get("base") or "Loft")
    feat.Sections = profiles
    feat.Solid = bool(spec.get("solid", True))
    feat.Ruled = bool(spec.get("ruled", False))
    return feat


def _build_sweep(doc, spec):
    _require(spec, "base", "path")
    feat = doc.addObject("Part::Sweep", spec.get("name") or "Sweep")
    feat.Sections = [_get_obj(doc, spec["base"], "profile")]
    feat.Spine = _get_obj(doc, spec["path"], "path")
    feat.Solid = bool(spec.get("solid", True))
    return feat


def _origin_feature(body, role: str):
    """The body origin's datum with ``role`` ('X_Axis', 'XY_Plane', ...), or None."""
    origin = getattr(body, "Origin", None)
    return next(
        (f for f in getattr(origin, "OriginFeatures", []) if getattr(f, "Role", "") == role),
        None,
    )


def _require_transformable(obj, op: str) -> None:
    """Refuse an original that a PartDesign transform cannot repeat.

    FreeCAD transforms additive/subtractive features only; anything else
    recomputes Invalid with "Only additive and subtractive features can be
    transformed" (live on 1.1.4 for a LinearPattern of a LinearPattern).
    """
    is_derived = getattr(obj, "isDerivedFrom", None)
    if not callable(is_derived):
        return
    if is_derived("PartDesign::Transformed"):
        raise ValueError(
            f"{op}: '{obj.Name}' is itself a {obj.TypeId}, and FreeCAD transforms only "
            "additive/subtractive features (pad, pocket, revolution, groove, hole, ...). To "
            "repeat a pattern again, use pattern_type='multi' with transforms=[...] (one "
            "MultiTransform), or name the original features in originals=[...]."
        )
    if not is_derived("PartDesign::FeatureAddSub"):
        raise ValueError(
            f"{op}: '{obj.Name}' ({obj.TypeId}) cannot be repeated: FreeCAD transforms only "
            "additive/subtractive features (pad, pocket, revolution, groove, hole, ...). A "
            "fillet/chamfer/thickness on the copies has to be applied after the pattern."
        )


def _transform_originals(doc, body, base, spec, op: str) -> list:
    """The features a PartDesign transform repeats: obj_name plus ``originals``."""
    extra = spec.get("originals") or []
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, (list, tuple)):
        raise ValueError(f"{op}: originals must be a list of feature names, got {extra!r}.")
    originals = [base]
    for name in extra:
        obj = _get_obj(doc, name, "original")
        if obj not in originals:
            originals.append(obj)
    for obj in originals:
        if _parent_body(obj) is not body:
            raise ValueError(
                f"{op}: original '{obj.Name}' is not a feature of Body '{body.Name}'; a "
                "PartDesign transform repeats features of its own body only."
            )
        _require_transformable(obj, op)
    return originals


def _transform_volumes(feat) -> tuple[float, float] | None:
    """(volume before, volume after) of a transform: its BaseFeature vs itself.

    The predecessor is ``BaseFeature``, not the original: an original earlier in
    the chain carries an older, different volume.
    """
    try:
        prev = getattr(feat, "BaseFeature", None)
        before = float(prev.Shape.Volume) if prev is not None else 0.0
        return before, float(feat.Shape.Volume)
    except Exception:
        return None


def _mirror_plane_ref(doc, body, base, spec):
    """(object, [subelement], label) for a PartDesign mirror plane."""
    face_sel = spec.get("face")
    if face_sel is not None:
        if (
            isinstance(face_sel, (list, tuple))
            and len(face_sel) == 2
            and isinstance(face_sel[0], str)
            and doc.getObject(face_sel[0]) is not None
        ):
            ref = sketcher_ops.attachment_ref(_get_obj(doc, face_sel[0], "mirror face object"))
            items = [face_sel[1]]
        else:
            ref = base
            items = face_sel if isinstance(face_sel, list) else [face_sel]
        name = _resolve_elements(ref, items, "Face")[0]
        face = ref.Shape.getElement(name)
        if face.Surface.TypeId != "Part::GeomPlane":
            raise ValueError(f"mirror face {ref.Name}.{name} is not planar.")
        _record_resolved_faces(ref, [name])
        return ref, [name], f"{ref.Name}.{name}"
    plane = spec.get("plane", "XY")
    if isinstance(plane, dict) and plane.get("datum"):
        dp = _get_obj(doc, plane["datum"], "mirror datum plane")
        return dp, [""], dp.Name
    role = _ORIGIN_ROLE.get(str(plane).upper())
    support = _origin_feature(body, role) if role else None
    if support is None:
        raise ValueError(
            f"mirror plane must be XY/XZ/YZ, {{'datum': name}} or a face, got {plane!r}"
        )
    return support, [""], f"{body.Name} origin {str(plane).upper()} plane"


def _build_pd_mirror(doc, body, base, spec):
    """Mirror PartDesign FEATURES inside their Body (PartDesign::Mirrored).

    A ``Part::Mirroring`` of a feature copies the feature's whole Shape — the
    entire body up to that feature — into a second solid at the document root,
    and the Body itself never changes (live: the reply said success, the Body's
    volume was untouched).
    """
    global _LAST_FEATURE_INFO
    originals = _transform_originals(doc, body, base, spec, "mirror")
    ref, subs, label = _mirror_plane_ref(doc, body, base, spec)
    feat = body.newObject("PartDesign::Mirrored", spec.get("name") or "Mirrored")
    _apply_refine(feat, spec)
    feat.MirrorPlane = (ref, subs)
    feat.Originals = originals
    doc.recompute()
    volumes = _transform_volumes(feat)
    if volumes is not None and abs(volumes[1] - volumes[0]) < 1e-6:
        raise RuntimeError(
            f"mirror of {', '.join(o.Name for o in originals)} had no effect (volume unchanged "
            f"at {round(volumes[0], 1)} mm^3): across {label} the mirrored copy lands where "
            "it adds or removes nothing — outside the part, or onto the original itself (a "
            "feature on the plane, or a part symmetric about it). Pass the plane the copy "
            "should be reflected across (plane='YZ'/'XZ'/'XY', {'datum': name} or a face)."
        )
    _LAST_FEATURE_INFO = {"mirror_plane": label, "originals": [o.Name for o in originals]}
    return feat


def _build_mirror(doc, spec):
    global _LAST_FEATURE_INFO
    _require(spec, "base")
    base = _get_obj(doc, spec["base"], "base")
    body = _parent_body(base)
    if body is not None:
        return _build_pd_mirror(doc, body, base, spec)
    # FreeCAD >= 1.0 renamed Part::Mirror to Part::Mirroring.
    # Try the new name first; fall back only if the type does not exist.
    mirror_type = "Part::Mirroring"
    try:
        feat = doc.addObject(mirror_type, spec.get("name") or "Mirror")
    except Exception:
        # Only fall back if the type itself is unknown; re-raise other errors.
        try:
            mirror_type = "Part::Mirror"
            feat = doc.addObject(mirror_type, spec.get("name") or "Mirror")
        except Exception:
            raise ValueError(
                "Neither Part::Mirroring nor Part::Mirror is available in this FreeCAD version."
            ) from None
    feat.Source = base
    face_sel = spec.get("face")
    if face_sel is not None:
        items = face_sel if isinstance(face_sel, list) else [face_sel]
        names = _resolve_elements(base, items, "Face")
        face = base.Shape.Faces[int(names[0][4:]) - 1]
        umin, umax, vmin, vmax = face.ParameterRange
        feat.Normal = face.normalAt((umin + umax) / 2, (vmin + vmax) / 2)
        feat.Base = face.CenterOfMass
    else:
        normal = _MIRROR_PLANE.get(str(spec.get("plane", "XY")).upper())
        if normal is None:
            raise ValueError(f"mirror plane must be XY/XZ/YZ, got {spec.get('plane')!r}")
        feat.Normal = FreeCAD.Vector(*normal)
        feat.Base = FreeCAD.Vector(0, 0, 0)
    if getattr(base, "TypeId", "") == "PartDesign::Body":
        _LAST_FEATURE_INFO = {
            "note": f"'{base.Name}' is a whole Body, so the mirror is a separate mirrored "
            f"copy ({feat.TypeId}) at the document root and '{base.Name}' is unchanged. To "
            "mirror features INSIDE the body, name the feature (e.g. its Tip) instead."
        }
    return feat


def _parent_body(obj):
    """The PartDesign::Body ``obj`` belongs to, or None (a Part-level object)."""
    for o in getattr(obj, "InList", []):
        if o.TypeId == "PartDesign::Body":
            return o
    return None


_SKETCH_AXES = {"H": "H_Axis", "V": "V_Axis", "N": "N_Axis"}


def _profile_of(feature):
    """The sketch a sketch-based feature is built from, or None."""
    profile = getattr(feature, "Profile", None)
    if isinstance(profile, (list, tuple)):
        profile = profile[0] if profile else None
    return profile if getattr(profile, "TypeId", "") == "Sketcher::SketchObject" else None


def _sketch_axis_vector(sketch, sub: str):
    local = {"H_Axis": (1, 0, 0), "V_Axis": (0, 1, 0), "N_Axis": (0, 0, 1)}[sub]
    return sketch.Placement.Rotation.multVec(FreeCAD.Vector(*local))


def _pattern_axis(body, original, axis, center=None):
    """(link, subelement, direction, point on the axis, label) for a pattern axis.

    'X'/'Y'/'Z' are the body's origin axes; 'H'/'V'/'N' are the axes of the
    original's profile sketch (its in-plane horizontal and vertical, and its
    normal), which follow the sketch wherever it is attached. A direction
    [x, y, z] (body frame) becomes a datum line through ``center`` or the origin.
    """
    if isinstance(axis, (list, tuple, dict)):
        direction = _axis_vec(axis)
        line = _centered_axis_line(body, direction, center if center else [0, 0, 0])
        label = f"direction {[round(v, 4) for v in direction]}"
        return line, "", direction, FreeCAD.Vector(line.Placement.Base), label
    key = str(axis).strip().upper()
    if key in _AXIS:
        line = _origin_feature(body, f"{key}_Axis")
        if line is None:
            raise ValueError(f"body '{body.Name}' origin has no {key} axis.")
        return line, "", FreeCAD.Vector(*_AXIS[key]), FreeCAD.Vector(), f"{body.Name} {key} axis"
    if key in _SKETCH_AXES:
        sketch = _profile_of(original)
        if sketch is None:
            raise ValueError(
                f"axis {axis!r} is an axis of the profile sketch, but '{original.Name}' has no "
                "sketch profile; use 'X'/'Y'/'Z'."
            )
        sub = _SKETCH_AXES[key]
        return (
            sketch,
            sub,
            _sketch_axis_vector(sketch, sub),
            FreeCAD.Vector(sketch.Placement.Base),
            f"{sketch.Name} {sub}",
        )
    raise ValueError(
        f"axis must be 'X'/'Y'/'Z' (body axes) or 'H'/'V'/'N' (the profile sketch's axes) for "
        f"a PartDesign pattern, got {axis!r}"
    )


def _centered_axis_line(body, direction, center):
    """A datum line through ``center`` along ``direction``.

    A PartDesign::PolarPattern rotates about the AXIS LINE it references, and
    the body's origin datum line runs through the body origin — so a bolt
    circle about a face centre (the common intent) would silently clip every
    occurrence that falls outside the material. FreeCAD's own datum line is the
    reference the GUI uses for this; a free Placement is what positions it
    (MapMode "Translate" over the origin line does NOT move it — live-verified).
    """
    line = body.newObject("PartDesign::Line", "CadPilotPolarAxis")
    # A helper, not part of the design. Reopening the file prints FreeCAD's own
    # "Failed to load BRep file ...Shape.brp" for it, as for every datum line;
    # the recompute rebuilds it.
    line.Visibility = False
    line.MapMode = "Deactivated"
    line.Placement = FreeCAD.Placement(
        FreeCAD.Vector(), FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), direction)
    )
    _bind_vector(line, "Placement", center, "center")
    return line


def _pattern_no_effect_reason(ptype, original, direction, point, label, spec) -> str:
    """Why a pattern's copies changed nothing, from the geometry."""
    unit = FreeCAD.Vector(direction)
    unit.normalize()
    sketch = _profile_of(original)
    if ptype == "linear":
        if sketch is not None:
            normal = _sketch_axis_vector(sketch, "N_Axis")
            if abs(abs(unit.dot(normal)) - 1.0) < 1e-6:
                return (
                    f"the direction ({label}) is the normal of '{sketch.Name}', the axis "
                    f"'{original.Name}' was extruded along, so every copy lands inside the first "
                    "one. Use an in-plane direction: leave axis out (the profile's H axis), "
                    "axis='V', or a body axis in the sketch plane"
                )
        spacing = spec.get("spacing")
        if isinstance(spacing, (int, float)) and abs(spacing) < 1e-9:
            return "spacing is 0, so every copy coincides with the original"
    else:
        with contextlib.suppress(Exception):
            offset = original.AddSubShape.CenterOfMass - point
            radial = offset - unit * offset.dot(unit)
            if radial.Length < 1e-6:
                return (
                    f"the rotation axis ({label}) runs through '{original.Name}' itself, so it "
                    "turns about its own centre; pass center = the middle of the circle the "
                    "copies should sit on"
                )
    return (
        "every copy lands where it adds or removes no material (outside the part, or on "
        "material an earlier copy already removed); check axis, spacing/angle and center"
    )


def _spacings_value(spacings, count: int):
    """Validate a ``spacings`` list: count-1 gaps, each a pitch, '=expr' or None."""
    if not isinstance(spacings, (list, tuple)) or len(spacings) != count - 1:
        raise ValueError(
            f"spacings must list the {count - 1} gaps between the {count} copies (one per gap: a "
            f"number, an '=expression', or null for `spacing`), got {spacings!r}."
        )
    values = []
    for gap in spacings:
        if gap is None:
            values.append(-1.0)  # FreeCAD's "use Offset" marker
        elif _is_expression(gap):
            values.append(gap)
        else:
            number = _number(gap, "a spacings item")
            if number <= 0:
                raise ValueError(f"spacings items must be > 0, got {gap!r}.")
            values.append(number)
    return values


def _set_linear_spacing(feat, spec, count: int) -> bool:
    """Set how far apart the copies are; True when a negative value flipped the direction.

    Spacing mode keeps the pitch when the count changes. The old Extent-mode
    ``Length = spacing * (count - 1)`` froze the overall length instead, so a
    count bound to a spreadsheet silently changed the pitch (live: 4 holes at
    20 became 6 holes at 12) and a spacing expression could not be bound at all.
    """
    mode = str(spec.get("mode", "spacing")).lower()
    if mode not in ("spacing", "extent"):
        raise ValueError(
            f"mode must be 'spacing' (pitch) or 'extent' (overall length), got {mode!r}"
        )
    if mode == "extent" and spec.get("spacings") is not None:
        raise ValueError(
            "spacings are the gaps between neighbours (spacing mode); mode='extent' spreads the "
            "copies evenly over length. Drop one of them."
        )
    key = "length" if mode == "extent" else "spacing"
    spacings = spec.get("spacings")
    if spec.get(key) is None and spacings is None:
        raise ValueError(
            "a linear pattern needs "
            + (
                "length (the overall extent)"
                if mode == "extent"
                else "spacing (the pitch) or spacings"
            )
            + "."
        )
    if spacings is not None and _is_expression(spec.get("count")):
        raise ValueError(
            "spacings fixes the number of gaps, so count cannot be an expression with it: bind "
            "the gaps instead (spacings items accept '=expressions')."
        )
    raw = spec.get(key)
    numeric = isinstance(raw, (int, float)) and not isinstance(raw, bool)
    flip = numeric and raw < 0
    if _is_expression(raw) and _expression_value(feat, raw) < 0:
        # Offset and Length are never negative, so a negative expression bound as
        # it stands failed the recompute ("Pattern length too small"). The sign
        # goes to Reversed, as it does for a number; it is read once, here.
        raw, flip = f"=-({raw[1:]})", True
    if "Mode" not in feat.PropertiesList:
        # FreeCAD < 1.1 has Extent mode only: Length spans the count-1 gaps.
        if mode == "extent" and (numeric or _is_expression(raw)):
            _set_or_bind(feat, "Length", abs(float(raw)) if numeric else raw)
            return flip
        if not numeric or spacings is not None or _is_expression(spec.get("count")):
            raise ValueError(
                "spacing mode (an expression-bound spacing or count, spacings) needs FreeCAD 1.1; "
                "this FreeCAD has extent mode only. Pass numbers, or mode='extent' with length."
            )
        feat.Length = abs(float(raw)) * (count - 1)
        return flip
    feat.Mode = "Extent" if mode == "extent" else "Spacing"
    # Length/Offset are PropertyLengths (never negative): the sign goes to Reversed.
    if raw is not None:
        _set_or_bind(
            feat, "Length" if mode == "extent" else "Offset", abs(float(raw)) if numeric else raw
        )
    if spacings is not None:
        values = _spacings_value(spacings, count)
        if raw is None:
            feat.Offset = next((v for v in values if isinstance(v, float) and v > 0), 1.0)
        expression = float_list_expression(values)
        if expression is None:
            feat.Spacings = values
        else:
            feat.setExpression("Spacings", expression)
    return flip


def _setup_linear(feat, body, original, spec, count: int):
    """Direction and spacing of a LinearPattern; returns (direction, point, label, link)."""
    default = "H" if _profile_of(original) is not None else "X"
    link, sub, direction, point, label = _pattern_axis(body, original, spec.get("axis", default))
    feat.Direction = (link, [sub])
    flip = _set_linear_spacing(feat, spec, count)
    feat.Reversed = bool(spec.get("reversed", False)) != flip
    return direction, point, label, link


def _setup_polar(feat, body, original, spec):
    """Axis and angle of a PolarPattern; returns (direction, point, label, link)."""
    center = spec.get("center")
    link, sub, direction, point, label = _pattern_axis(
        body, original, spec.get("axis", "Z"), center
    )
    # ``center`` was silently ignored here (only the Draft path read it), so a
    # caller's bolt circle about a face centre rotated about the body origin
    # instead and lost the occurrences that fell outside the plate — reported
    # as plain success.
    if center and getattr(link, "TypeId", "") != "PartDesign::Line":
        link, sub = _centered_axis_line(body, direction, center), ""
        label = f"{label} through {center}"
    feat.Axis = (link, [sub])
    _set_or_bind(feat, "Angle", spec.get("angle", 360.0))
    feat.Reversed = bool(spec.get("reversed", False))
    return direction, point, label, link


def _refuse_inert_pattern(feat, original, ptype, direction, point, label, spec) -> None:
    """Refuse a pattern whose copies changed nothing, naming the cause."""
    volumes = _transform_volumes(feat)
    if volumes is None or abs(volumes[1] - volumes[0]) >= 1e-6:
        return
    if direction is None:
        reason = "the combined copies land where they add or remove no material"
    else:
        link = feat.Direction[0] if ptype == "linear" else feat.Axis[0]
        if getattr(link, "TypeId", "") == "PartDesign::Line":
            point = FreeCAD.Vector(link.Placement.Base)  # a bound centre is known only now
        reason = _pattern_no_effect_reason(ptype, original, direction, point, label, spec)
    raise RuntimeError(
        f"pattern of '{original.Name}' had no effect (volume unchanged at "
        f"{round(volumes[0], 1)} mm^3): {reason}. Nothing was created."
    )


def _build_pd_pattern(doc, body, base, spec, ptype: str, count: int):
    """Pattern PartDesign FEATURES inside their Body.

    Draft's array replicates the base object's whole Shape — for a PartDesign
    feature that Shape is the entire body, so arraying a hole produced N
    overlapping copies of the whole part instead of N holes in one part. A
    PartDesign::(Polar|Linear)Pattern with ``Originals=[feature]`` repeats the
    feature's own contribution, which is what "pattern this hole" means.
    """
    global _LAST_FEATURE_INFO
    originals = _transform_originals(doc, body, base, spec, "pattern")
    if ptype == "polar":
        feat = body.newObject("PartDesign::PolarPattern", spec.get("name") or "PolarPattern")
        direction, point, label, _link = _setup_polar(feat, body, base, spec)
    elif ptype == "linear":
        # The profile's in-plane H axis by default: the old default, the body's
        # Z axis, is the extrusion axis of every hole drilled from an XY sketch,
        # so the copies landed inside each other and the pattern was refused.
        feat = body.newObject("PartDesign::LinearPattern", spec.get("name") or "LinearPattern")
        direction, point, label, _link = _setup_linear(feat, body, base, spec, count)
    else:
        raise ValueError(f"pattern_type must be linear/polar/multi, got {ptype!r}")
    _apply_refine(feat, spec)
    # AFTER the transform props: assigning Originals re-derives the shape.
    feat.Originals = originals
    feat.Occurrences = count
    doc.recompute()
    _refuse_inert_pattern(feat, base, ptype, direction, point, label, spec)
    _LAST_FEATURE_INFO = {"axis": label}
    if len(originals) > 1:
        _LAST_FEATURE_INFO["originals"] = [o.Name for o in originals]
    return feat


def _build_multi_transform(doc, body, base, spec):
    """Chain several transforms in ONE PartDesign::MultiTransform.

    FreeCAD transforms additive/subtractive features only, so a pattern of a
    pattern recomputes Invalid; a grid (linear x linear), a mirrored bolt
    circle and the like are a MultiTransform whose steps apply in order.
    """
    global _LAST_FEATURE_INFO
    steps = spec.get("transforms")
    if not isinstance(steps, list) or not steps:
        raise ValueError(
            "pattern_type='multi' needs transforms: a list of steps applied in order, each "
            "{'pattern_type': 'linear'|'polar'|'mirror', ...that type's keys}."
        )
    originals = _transform_originals(doc, body, base, spec, "pattern")
    feat = body.newObject("PartDesign::MultiTransform", spec.get("name") or "MultiTransform")
    _apply_refine(feat, spec)
    subs, labels = [], []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"transforms[{i}] must be a dict, got {step!r}")
        kind = str(step.get("pattern_type", "")).lower()
        problem = tip_policy.unknown_transform_step_keys(kind, step)
        if problem:
            raise ValueError(f"transforms[{i}]: {problem}")
        if kind == "mirror":
            sub = body.newObject("PartDesign::Mirrored", f"{feat.Name}_Mirror")
            ref, subnames, label = _mirror_plane_ref(doc, body, base, step)
            sub.MirrorPlane = (ref, subnames)
        else:
            count = _pattern_count(doc, step.get("count"))
            if count < 2:
                raise ValueError(f"transforms[{i}]: count must be >= 2.")
            if kind == "linear":
                sub = body.newObject("PartDesign::LinearPattern", f"{feat.Name}_Linear")
                _direction, _point, label, _link = _setup_linear(sub, body, base, step, count)
            else:
                sub = body.newObject("PartDesign::PolarPattern", f"{feat.Name}_Polar")
                _direction, _point, label, _link = _setup_polar(sub, body, base, step)
            sub.Occurrences = count
            _bind_count_expression(sub, step.get("count"), kind)
            label = f"{label} x{count}"
        with contextlib.suppress(Exception):
            sub.ViewObject.Visibility = False
        subs.append(sub)
        labels.append(f"{kind}: {label}")
    feat.Transformations = subs
    feat.Originals = originals
    doc.recompute()
    _refuse_inert_pattern(feat, base, "multi", None, None, "", spec)
    _LAST_FEATURE_INFO = {"transforms": labels, "originals": [o.Name for o in originals]}
    return feat


@contextlib.contextmanager
def _active_document(doc):
    """Make ``doc`` FreeCAD's ACTIVE document for the duration of the block.

    Draft's array helpers create their Array in ``FreeCAD.ActiveDocument``, so
    patterning a document that is merely OPEN raised "PropertyLink does not
    support external object" — multi-document sessions (the norm, and what a
    parallel test run produces) failed on a call that works when the document
    happens to be active. The previous active document is restored afterwards
    so driving another document does not yank the user's view away.
    """
    previous = None
    with contextlib.suppress(Exception):
        active = FreeCAD.ActiveDocument
        previous = active.Name if active is not None else None
    if previous != doc.Name:
        FreeCAD.setActiveDocument(doc.Name)
    try:
        yield
    finally:
        if previous and previous != doc.Name:
            with contextlib.suppress(Exception):
                FreeCAD.setActiveDocument(previous)


def _pattern_count(doc, raw) -> int:
    """Resolve the pattern count, which may be an expression binding.

    ``count="=Vars.n_holes"`` used to die inside ``int()`` ("invalid literal
    for int() with base 10"), so a Spreadsheet could not drive an array even
    though every other op accepts "=expr". A throwaway object borrows FreeCAD's
    expression engine to evaluate it up front; it is created and removed inside
    the op's own transaction, so it leaves no trace in the document.
    """
    if isinstance(raw, str) and raw.startswith("="):
        probe = doc.addObject("App::FeaturePython", "_CADPilotExprProbe")
        invalid = False
        value = 0.0
        try:
            probe.addProperty("App::PropertyFloat", "Value")
            probe.setExpression("Value", raw[1:])
            doc.recompute()
            # An alias that does not exist does NOT raise: the property simply
            # stays 0 and goes Invalid, which then surfaced as a baffling
            # "pattern count must be >= 2" instead of naming the bad expression.
            invalid = "Invalid" in [str(s) for s in getattr(probe, "State", [])]
            value = float(probe.Value)
        except Exception as e:
            raise ValueError(
                f"pattern count expression {raw!r} could not be evaluated ({e})."
            ) from None
        finally:
            with contextlib.suppress(Exception):
                doc.removeObject(probe.Name)
        if invalid:
            raise ValueError(
                f"pattern count expression {raw!r} did not resolve to a number — check the "
                "spreadsheet alias/object name (a misspelled alias evaluates to 0, not an error)."
            )
        return round(value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"pattern count must be an integer >= 2 or an expression like '=Vars.n_holes', "
            f"got {raw!r}."
        ) from None


def _bind_count_expression(feat, raw, ptype: str) -> None:
    """Keep the pattern count tied to its spreadsheet expression.

    ``_pattern_count`` resolves "=Vars.n_holes" once; without the binding the
    pattern froze at the value the spreadsheet had on creation day (live: a
    flange stayed at 6 holes after n_holes became 8, while pad and pocket
    followed their aliases) — valid geometry, silently the wrong model.
    PartDesign patterns take Occurrences, Draft arrays NumberPolar/NumberX.
    """
    if not (isinstance(raw, str) and raw.startswith("=")):
        return
    props = ("NumberPolar", "NumberX") if ptype == "polar" else ("NumberX",)
    for prop in (*props, "Occurrences"):
        if prop not in list(getattr(feat, "PropertiesList", []) or []):
            continue
        with contextlib.suppress(Exception):
            feat.setExpression(prop, raw[1:])
        return


def _build_pattern(doc, spec):
    _require(spec, "base")
    base = _get_obj(doc, spec["base"], "base")
    ptype = str(spec.get("pattern_type", "linear"))
    if ptype not in ("linear", "polar", "multi"):
        raise ValueError(f"pattern_type must be linear/polar/multi, got {ptype!r}")
    if getattr(base, "TypeId", "") == "PartDesign::Body":
        tip = getattr(base, "Tip", None)
        raise ValueError(
            f"'{base.Name}' is a whole Body. Patterning it would build a Draft array of the "
            "entire body at the document root, outside any body, with the Body itself "
            "unchanged. Name the feature to repeat instead"
            + (f" (the Body's Tip is '{tip.Name}')" if tip is not None else "")
            + "; for copies of the whole part, pattern a Part-level solid."
        )
    body = _parent_body(base)
    if ptype == "multi":
        misplaced = sorted(k for k in tip_policy.TRANSFORM_STEP_KEYS["any"] if k in spec)
        if misplaced:
            raise ValueError(
                f"with pattern_type='multi', {', '.join(misplaced)} belong inside each "
                "transforms step, not at the top level."
            )
        if body is None:
            raise ValueError(
                "pattern_type='multi' builds a PartDesign::MultiTransform, which repeats "
                f"features inside a Body; '{base.Name}' is not in one."
            )
        return _build_multi_transform(doc, body, base, spec)
    if spec.get("transforms") is not None:
        raise ValueError("transforms is only read with pattern_type='multi'.")
    _require(spec, "count")
    count = _pattern_count(doc, spec["count"])
    if count < 2:
        raise ValueError("pattern count must be >= 2.")
    if body is not None:
        feat = _build_pd_pattern(doc, body, base, spec, ptype, count)
        _bind_count_expression(feat, spec.get("count"), ptype)
        return feat
    pd_only = sorted(k for k in ("originals", "spacings", "mode", "length") if k in spec)
    if pd_only:
        raise ValueError(
            f"{', '.join(pd_only)} need a PartDesign feature as the base; '{base.Name}' is a "
            "Part-level object, which is repeated with a Draft array (spacing, axis, angle, "
            "center only)."
        )

    import Draft

    make_array = getattr(Draft, "make_array", None) or Draft.makeArray
    with _active_document(doc):
        if ptype == "linear":
            _require(spec, "spacing")
            direction = _axis_vec(spec.get("axis"), default="X")
            spacing = spec["spacing"]
            pitch = 1.0 if _is_expression(spacing) else _number(spacing, "pattern spacing")
            feat = make_array(base, direction * pitch, FreeCAD.Vector(0, 0, 0), count, 1)
            if _is_expression(spacing):
                for axis, component in zip("xyz", direction, strict=True):
                    if abs(component) > 1e-12:
                        feat.setExpression(
                            f".IntervalX.{axis}", f"({spacing[1:]}) * {float(component)!r}"
                        )
        else:
            feat = make_array(base, FreeCAD.Vector(0, 0, 0), 360.0, count)
            _bind_vector(feat, "Center", spec.get("center", [0, 0, 0]), "center")
            _set_or_bind(feat, "Angle", spec.get("angle", 360.0))
            axis = str(spec.get("axis", "Z")).upper()
            if axis != "Z" and hasattr(feat, "Axis"):
                feat.Axis = _axis_vec(axis)
    if spec.get("name"):
        feat.Label = spec["name"]
    _bind_count_expression(feat, spec.get("count"), ptype)
    return feat


def _is_expression(value) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _number(value, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{what} must be a number or an expression like '=Vars.depth', got {value!r}."
        ) from None


def _bind_vector(obj, prop: str, values, what: str):
    """Assign an [x, y, z] whose items may be '=expr' strings; returns the numeric part."""
    if isinstance(values, dict):
        values = [values.get("x", 0), values.get("y", 0), values.get("z", 0)]
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"{what} must be [x, y, z] (items may be '=expressions'), got {values!r}")
    numeric = FreeCAD.Vector(*(0.0 if _is_expression(v) else _number(v, what) for v in values))
    current = getattr(obj, prop)
    if isinstance(current, FreeCAD.Placement):
        obj.Placement = FreeCAD.Placement(numeric, current.Rotation)
        path = f".{prop}.Base"
    else:
        setattr(obj, prop, numeric)
        path = f".{prop}"
    for axis, value in zip("xyz", values, strict=True):
        if _is_expression(value):
            obj.setExpression(f"{path}.{axis}", value[1:])
    return numeric


def _expression_value(obj, expression: str) -> float:
    """The current value of an '=expression' evaluated in ``obj``'s context; 0 if it can't be."""
    try:
        value = obj.evalExpression(expression[1:])
    except Exception:
        return 0.0
    value = getattr(value, "Value", value)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _set_or_bind(obj, prop, value):
    """Assign a property, or bind it to the ExpressionEngine when the value
    is a string starting with '=' (same semantics as property_mapper)."""
    if isinstance(value, str) and value.startswith("="):
        obj.setExpression(prop, value[1:])
    else:
        setattr(obj, prop, value)


def _build_variables(doc, spec):
    """Create or update a Spreadsheet parameter table (idempotent).

    spec: {"type": "variables", "base": name|None,
           "cells": {"A1": ["alias", value], ...}}
    Values: number -> literal; string starting with '=' -> formula;
    other strings -> quoted text cell.
    """
    cells = spec.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValueError("variables requires a non-empty 'cells' dict.")
    name = spec.get("base") or spec.get("name") or "Spreadsheet"
    ss = doc.getObject(name)
    if ss is None:
        ss = doc.addObject("Spreadsheet::Sheet", name)
    # The same setter ``edit_object`` uses for cells: shared so both paths
    # validate identically ("Invalid alias" alone names nothing).
    from .property_mapper import set_spreadsheet_cells

    set_spreadsheet_cells(ss, cells)
    doc.recompute()
    return ss


def _vec3(v, name):
    """Vector as [x, y, z] or {"x":.., "y":.., "z":..} -> FreeCAD.Vector.

    The rest of the API takes plain coordinate lists; dict-only here used to
    crash with AttributeError on the natural list form.
    """
    if isinstance(v, dict):
        return FreeCAD.Vector(float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return FreeCAD.Vector(float(v[0]), float(v[1]), float(v[2]))
    raise ValueError(f"{name} must be [x, y, z] or {{'x':.., 'y':.., 'z':..}}, got {v!r}")


def _build_move(doc, spec):
    """Apply a relative translation and/or rotation to an existing object.

    This is NOT a parametric feature — it directly modifies the object's Placement.
    Supported spec keys (vectors accept [x,y,z] or {"x":..,"y":..,"z":..}):
      translate: [dx, dy, dz] or {"x": dx, "y": dy, "z": dz}  — relative translation
      rotate: {"axis": [ax,ay,az], "angle": degrees}  — relative rotation
      placement: {"Base": [x,y,z], "Rotation": {"Axis": [...], "Angle": deg}}  — absolute override
    If both translate/rotate and placement are given, placement wins (absolute).
    """
    _require(spec, "base")
    obj = _get_obj(doc, spec["base"], "base")
    # PartDesign features do not own their frame: FreeCAD rewrites their
    # Placement on the next recompute (live-verified — a moved Pad returned to
    # (0,0,0) on the next cad() call, and the boolean that followed fused the
    # UN-MOVED shape). Moving the owning Body is the only persistent version of
    # the same intent; describe_feature reports the redirect.
    owner = body_owner(obj)
    if owner is not None:
        obj = owner
    current = obj.Placement

    # Absolute placement override
    if "placement" in spec:
        p = spec["placement"]
        base = p.get("Base", p.get("Position", {"x": 0, "y": 0, "z": 0}))
        rot_data = p.get("Rotation", {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0})
        new_base = _vec3(base, "placement.Base")
        axis = rot_data.get("Axis", {"x": 0, "y": 0, "z": 1})
        new_rot = FreeCAD.Rotation(
            _vec3(axis, "placement.Rotation.Axis"),
            float(rot_data.get("Angle", 0)),
        )
        _watch = _watched_dependent_bodies(doc, obj, new_base - current.Base)
        moved = _assign_placement(doc, obj, new_base, new_rot)
        _shift_unfollowed_bodies(doc, _watch, new_base - current.Base)
        return moved

    # Relative translation / rotation — at least one is required.
    translate = spec.get("translate", {})
    rotate = spec.get("rotate", {})
    if not translate and not rotate:
        raise ValueError("move requires at least one of: translate, rotate, placement.")
    delta = _vec3(translate, "translate") if translate else FreeCAD.Vector(0, 0, 0)

    # Relative rotation
    delta_rot = FreeCAD.Rotation()
    if rotate:
        if not isinstance(rotate, dict):
            raise ValueError(f"rotate must be a dict {{'axis':…, 'angle':…}}, got {rotate!r}")
        r_axis = _vec3(rotate.get("axis", {"x": 0, "y": 0, "z": 1}), "rotate.axis")
        r_angle = float(rotate.get("angle", 0))  # degrees
        delta_rot = FreeCAD.Rotation(r_axis, r_angle)

    # Compose the new placement. Translation is always in the GLOBAL frame;
    # rotation is applied around the object's current placement base.
    if translate and rotate:
        # Translate first (global), then rotate around the new position
        new_base = current.Base + delta
        new_rot = delta_rot.multiply(current.Rotation)
    elif translate:
        new_base = current.Base + delta
        new_rot = current.Rotation
    elif rotate:
        new_base = current.Base
        new_rot = delta_rot.multiply(current.Rotation)
    _watch = _watched_dependent_bodies(doc, obj, new_base - current.Base)
    moved = _assign_placement(doc, obj, new_base, new_rot)
    _shift_unfollowed_bodies(doc, _watch, new_base - current.Base)
    return moved


def _watched_dependent_bodies(doc, obj, delta):
    """(Body, tip bbox) for every Body whose feature chain consumes obj.

    Only bodies that DEPEND on the moved object can fail to follow it; the
    snapshot is taken before the move so the follow check compares honestly.
    """
    watch = []
    if delta.Length < 1e-9:
        return watch
    try:
        dependents = list(obj.InListRecursive)
    except Exception:
        return watch
    for o in dependents:
        if getattr(o, "TypeId", "") != "PartDesign::Body":
            continue
        tip = o.Tip if getattr(o, "Tip", None) is not None else o
        try:
            bb = tip.Shape.BoundBox
            watch.append((o, (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)))
        except Exception:
            continue
    return watch


def _shift_unfollowed_bodies(doc, watch, delta) -> list[str]:
    """Shift a dependent Body whose visible model did NOT follow the move.

    A root solid adopted as a dress-up chain's base (draft/thickness/fillet
    over a Part::Box) builds its members in the BODY frame with the base's
    Placement stripped: moving the base updated the base object alone while
    the whole visible chain read the old position, every member Up-to-date and
    no error anywhere (live: move reported success and the model never
    moved). Bodies whose tip bbox DID travel already carry the delta —
    shifting them too would double it — so only the frozen ones are moved.
    """
    shifted = []
    for body, before in watch:
        tip = body.Tip if getattr(body, "Tip", None) is not None else body
        try:
            bb = tip.Shape.BoundBox
        except Exception:
            continue
        followed = (
            abs(bb.XMin - before[0]) > 1e-6
            or abs(bb.YMin - before[1]) > 1e-6
            or abs(bb.ZMin - before[2]) > 1e-6
        )
        if followed:
            continue
        _assign_placement(doc, body, body.Placement.Base + delta, body.Placement.Rotation)
        shifted.append(body.Name)
    if shifted:
        global _AUTO_DIRECTION_NOTE
        _AUTO_DIRECTION_NOTE = (
            "the moved object is the adopted base of "
            + ", ".join(shifted)
            + "; those members build in the body frame and did not follow the "
            "placement, so the Body itself was shifted by the same delta to "
            "move the visible model"
        )
    return shifted


def _assign_placement(doc, obj, new_base, new_rot):
    """Write the Placement, then verify it survives a recompute.

    A move that FreeCAD silently undoes (an attached sketch or datum plane
    recomputes its Placement away) must not be reported as success — the
    readback turns it into an honest failure that rolls the whole op back.
    """
    obj.Placement = FreeCAD.Placement(new_base, new_rot)
    doc.recompute()
    back = obj.Placement
    drift = (back.Base - new_base).Length
    with contextlib.suppress(Exception):
        drift += abs(back.Rotation.multiply(new_rot.inverted()).Angle) * 100.0  # deg -> ~mm
    if drift > 1e-4:
        raise RuntimeError(
            f"move did not persist on '{obj.Name}': it is back at "
            f"[{round(back.Base.x, 4)}, {round(back.Base.y, 4)}, {round(back.Base.z, 4)}] "
            "after a recompute. Attached sketches/datum planes get their Placement from "
            "their attachment (move the supporting face or change the offset instead), and "
            "a PartDesign feature is positioned by its Body."
        )
    return obj


# --- appearance ----------------------------------------------------------------
#
# A color op writes the ViewObject's appearance properties and nothing else: it
# adds no feature, needs no recompute and has no geometry to validate. It is a
# feature op purely so it inherits the whole machinery a step needs — its own
# transaction (live-verified on 1.1.4: a ViewObject write inside a transaction
# DOES produce an undo entry, so the color is a rollback-able, replayable step),
# the journal record, the session step and the label grammar.

#: obj_properties keys the color op understands. A key outside this set is
#: ignored by the other builders, but here "nothing recognized" would be a
#: silent no-op, so an unrecognized-only call is refused.
_APPEARANCE_KEYS = (
    "color",
    "transparency",
    "line_color",
    "line_width",
    "point_size",
    "display_mode",
    "draw_style",
    "visible",
)

#: obj_name values that mean "every object in the document".
_COLOR_ALL = ("*", "all", "every")

#: How many per-object entries the reply carries. A wildcard sweep on a real
#: model paints every object, and the answer is the COUNT plus which ones were
#: redirected or skipped: dumping one entry per object into the model's context
#: is the same context cost the screenshot pipeline was rebuilt to avoid.
_COLOR_REPORT_MAX = 20


def _parse_transparency(value, key="transparency"):
    """FreeCAD's ``Transparency`` is a PERCENTAGE 0-100 (an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number 0-100 (percent), got {value!r}.")
    num = float(value)
    if 0.0 < num < 1.0:
        raise ValueError(
            f"{key}={value!r} looks like a fraction; FreeCAD reads this as a percentage. "
            f"Pass {round(num * 100)} to mean {round(num * 100)}%."
        )
    if not 0.0 <= num <= 100.0:
        raise ValueError(f"{key} must be 0-100 (percent), got {value!r}.")
    return round(num)


def _parse_enum(view, prop, value, key):
    """Validate an enumerated ViewObject property against FreeCAD's own list.

    The list is read off the live view provider instead of being hardcoded: a
    Body, a Part feature and a TechDraw view do not share one. A provider that
    offers no list at all (an exotic custom ViewProvider) cannot be validated,
    so the value passes through and the write itself decides.
    """
    choices = []
    with contextlib.suppress(Exception):
        choices = [str(c) for c in view.getEnumerationsOfProperty(prop) or []]
    text = str(value).strip()
    if not choices:
        return text
    for choice in choices:
        if choice.lower() == text.lower():
            return choice
    raise ValueError(f"{key} must be one of {', '.join(choices)}, got {value!r}.")


def _color_targets(doc, spec):
    """The (object, redirect note, named-explicitly) triples a color op applies to.

    ``obj_name`` names one object, or uses ``*``/``all`` for every object in
    the document; an optional ``objects`` list names several at once. A
    PartDesign feature is redirected to its owning Body: the feature's own
    ViewObject is hidden, so coloring it changes nothing a user or a render ever
    sees (the same redirect the move op needs, for the same reason — a feature
    owns neither its display nor its frame).

    The third element says whether the caller NAMED this object. A wildcard sweep
    meets objects that cannot be painted at all (a Spreadsheet's view provider
    has no ShapeColor, and `variables` puts one in every parametric document), and
    failing the whole sweep over one of them would make `obj_name="*"` useless on
    exactly the models worth coloring. An explicitly named target has no such
    excuse: it must fail.
    """
    raw: list[tuple[str, bool]] = []
    if spec.get("base"):
        raw.append((spec["base"], True))
    extra = spec.get("objects")
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, (list, tuple)):
        raw.extend((item, True) for item in extra)

    names: list[tuple[str, bool]] = []
    for item, explicit in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"color targets must be object names, got {item!r}.")
        name = item.strip()
        if name.lower() in _COLOR_ALL:
            names.extend((o.Name, False) for o in doc.Objects)
        else:
            names.append((name, explicit))
    if not names:
        raise ValueError(
            "color requires obj_name (an object name, or '*' for every object in the document)."
        )

    targets: list[tuple[object, str, bool]] = []
    seen: set[str] = set()
    for name, explicit in names:
        obj = _get_obj(doc, name, "target")
        note = ""
        owner = body_owner(obj)
        if owner is not None and owner is not obj:
            note = (
                f"'{obj.Name}' is a PartDesign feature; its own ViewObject is hidden, so the "
                f"appearance was applied to its Body '{owner.Name}'."
            )
            obj = owner
        if obj.Name in seen:
            continue
        seen.add(obj.Name)
        targets.append((obj, note, explicit))
    if not targets:
        raise ValueError("color matched no object in the document.")
    return targets


#: appearance spec key -> the ViewObject property it writes. Used to REFUSE a
#: target that cannot carry the property at all, instead of letting FreeCAD's
#: raw AttributeError out (which names the property but not the object).
_APPEARANCE_PROPS = {
    "color": "ShapeColor",
    "transparency": "Transparency",
    "line_color": "LineColor",
    "line_width": "LineWidth",
    "point_size": "PointSize",
    "display_mode": "DisplayMode",
    "draw_style": "DrawStyle",
    "visible": "Visibility",
}


def _fresh_material(src, diffuse=None, transparency=None):
    """A NEW ``Material`` copying ``src``'s channels, optionally overridden.

    A fresh object is the whole point: FreeCAD treats an identical assignment as
    a no-op (no ``onChanged``, so no display-node rebuild), which is why a
    same-value write does not repaint.
    """
    factory = getattr(FreeCAD, "Material", None)
    mat = factory() if factory is not None else src
    for prop in ("AmbientColor", "EmissiveColor", "SpecularColor", "Shininess"):
        with contextlib.suppress(Exception):
            setattr(mat, prop, getattr(src, prop))
    with contextlib.suppress(Exception):
        rgb = tuple(diffuse) if diffuse is not None else tuple(src.DiffuseColor)
        mat.DiffuseColor = rgb
    with contextlib.suppress(Exception):
        alpha = (
            transparency if transparency is not None else float(getattr(src, "Transparency", 0.0))
        )
        mat.Transparency = float(alpha)
    return mat


def _uniform_material(entries):
    """``entries[0]`` when every entry carries the same visible channels, else None.

    None means a genuine per-face design, which must not be flattened.
    """

    def key(mat):
        return (
            *(round(float(c), 4) for c in mat.DiffuseColor),
            round(float(getattr(mat, "Transparency", 0.0)), 4),
        )

    if not entries:
        return None
    first = key(entries[0])
    return entries[0] if all(key(m) == first for m in entries) else None


def _refresh_appearance(obj) -> bool:
    """Re-apply an object's appearance in place, forcing a display-node rebuild.

    Measured on 1.1.4: the 3D view draws the PartDesign BODY's node (hiding the
    Body hides the part; hiding the tip feature does not), and creating a new
    PartDesign feature makes it rebuild that node with FreeCAD's DEFAULT
    material. So the colour of a coloured Body visually VANISHES the moment a
    feature is added, while every stored value still says the new colour — the
    worst kind of silent state. Re-assigning the appearance with a FRESH material
    rebuilds the node and the colour comes back (live: (185,45,45) ->
    (110,116,120) after a fillet -> (184,45,44) after this call).

    A genuine per-face material list is left alone, and its render is NOT
    recoverable after a tip advance: four attempts all stayed default-gray
    (re-assigning the Body's list with fresh materials, copying the list onto the
    tip feature's ViewObject, ``obj.touch()`` + recompute, ``ViewObject.touch()``
    + ``updateGui()``). Flattening it would destroy a design the caller never
    asked to change, so the stored design is preserved and the view keeps
    FreeCAD's default until the caller re-colours (the color op does flatten, on
    purpose, because a whole-object colour is exactly what it promises).
    """
    view = getattr(obj, "ViewObject", None)
    if view is None:
        return False
    entries = list(getattr(view, "ShapeAppearance", ()) or ())
    src = _uniform_material(entries)
    if src is None:
        return False
    try:
        view.ShapeAppearance = (_fresh_material(src),)
        return True
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"CADPilot: could not refresh {obj.Name}'s appearance: {exc}\n"
        )
        return False


def _apply_appearance(obj, spec) -> dict:
    """Apply one object's appearance and read it back.

    Raises on anything that would make the write a silent no-op: a view provider
    that has no such property (a Spreadsheet, a TechDraw view — objects with no
    3D shape carry no appearance), or one that does not keep the color it was
    given. The caller decides what a failure means: an explicitly NAMED target
    fails the step, a wildcard sweep records it as skipped.
    """
    view = getattr(obj, "ViewObject", None)
    if view is None:
        raise RuntimeError(
            f"'{obj.Name}' has no ViewObject, so it cannot be colored (console mode / a "
            "document object without a view provider)."
        )
    missing = [
        f"{key} ({prop})"
        for key, prop in _APPEARANCE_PROPS.items()
        if key in spec and not hasattr(view, prop)
    ]
    if missing:
        raise RuntimeError(
            f"'{obj.Name}' ({type(view).__name__}) cannot take {', '.join(missing)}: it has no "
            "3D shape to paint (a Spreadsheet or a TechDraw view carries no appearance)."
        )
    applied = {}
    if "color" in spec:
        wanted = parse_color(spec["color"])
        view.ShapeColor = wanted  # writes through to the persisted ShapeAppearance
        applied["color"] = format_color(wanted)
    if "transparency" in spec:
        applied["transparency"] = _parse_transparency(spec["transparency"])
        view.Transparency = applied["transparency"]
    if "line_color" in spec:
        view.LineColor = parse_color(spec["line_color"])
        applied["line_color"] = format_color(spec["line_color"])
    if "line_width" in spec:
        width = float(spec["line_width"])
        if not width > 0:
            raise ValueError(f"line_width must be greater than 0, got {spec['line_width']!r}.")
        view.LineWidth = width
        applied["line_width"] = width
    if "point_size" in spec:
        applied["point_size"] = int(spec["point_size"])
        view.PointSize = applied["point_size"]
    if "display_mode" in spec:
        applied["display_mode"] = _parse_enum(
            view, "DisplayMode", spec["display_mode"], "display_mode"
        )
        view.DisplayMode = applied["display_mode"]
    if "draw_style" in spec:
        applied["draw_style"] = _parse_enum(view, "DrawStyle", spec["draw_style"], "draw_style")
        view.DrawStyle = applied["draw_style"]
    if "visible" in spec:
        if not isinstance(spec["visible"], bool):
            raise ValueError(f"visible must be true or false, got {spec['visible']!r}.")
        view.Visibility = spec["visible"]
        applied["visible"] = spec["visible"]

    if "color" in spec:
        # Read it back off the view provider: a color that silently did not stick
        # must not read as a successful repaint.
        back = tuple(float(c) for c in view.ShapeColor)
        wanted = parse_color(spec["color"])
        if max(abs(back[i] - wanted[i]) for i in range(3)) > 0.02:
            raise RuntimeError(
                f"color did not persist on '{obj.Name}': asked for "
                f"{format_color(wanted)}, the view provider reports {format_color(back)}."
            )

    entries = list(getattr(view, "ShapeAppearance", ()) or ())
    if len(entries) > 1:
        # A per-face material list is NOT repainted by a property write: live,
        # two colour writes on a 3-entry object left the view drawing the FIRST
        # one (the stored materials were green, the render stayed blue), because
        # the node's material array is only rebuilt by an actual ShapeAppearance
        # assignment. Collapsing to ONE uniform entry is this op's semantics
        # anyway (a whole-object appearance), so do it and say so.
        view.ShapeAppearance = (
            _fresh_material(
                entries[0],
                diffuse=parse_color(spec["color"]) if "color" in spec else None,
                transparency=(
                    applied["transparency"] / 100.0 if "transparency" in applied else None
                ),
            ),
        )
        applied["normalized_appearance"] = f"{len(entries)} materials -> 1 uniform"
    return {"object": obj.Name, **applied}


def _build_color(doc, spec):
    """Set appearance (color/transparency/line/display) on one or more objects.

    Spec keys (obj_properties): color, transparency, line_color, line_width,
    point_size, display_mode, draw_style, visible — see operation_help("color").
    Colors accept 0..1 floats, 0-255 ints, "#rrggbb" or a name.
    """
    if not [k for k in _APPEARANCE_KEYS if k in spec]:
        raise ValueError(
            "color needs at least one appearance key in obj_properties: "
            + ", ".join(_APPEARANCE_KEYS)
            + "."
        )
    colored = []
    skipped = []
    for obj, note, explicit in _color_targets(doc, spec):
        try:
            entry = _apply_appearance(obj, spec)
        except Exception as e:
            if explicit:
                raise
            # A wildcard sweep meets objects that cannot be painted at all (a
            # Spreadsheet's view provider has no ShapeColor, and `variables` puts
            # one in every parametric document). Failing the whole sweep over one
            # of them would make obj_name="*" useless on exactly the models worth
            # coloring — but the caller must still be told which ones it missed.
            skipped.append({"object": obj.Name, "reason": f"{type(e).__name__}: {e}"})
            continue
        if note:
            entry["note"] = note
        colored.append(entry)
    if not colored:
        raise RuntimeError(
            "color matched no object it could paint: "
            + "; ".join(f"{s['object']} ({s['reason']})" for s in skipped[:4])
        )

    # `global` is load-bearing: without it this assignment binds a LOCAL and
    # describe_feature's pop finds nothing, so the reply's `colored` readback
    # came back empty on every call (live-caught).
    global _LAST_FEATURE_INFO
    _LAST_FEATURE_INFO = {"color": colored, "skipped": skipped}
    first = doc.getObject(colored[0]["object"])
    FreeCAD.Console.PrintMessage(
        "CADPilot: appearance applied to "
        + ", ".join(f"{c['object']} ({c.get('color', 'unchanged')})" for c in colored[:6])
        + "\n"
    )
    return first


# --- Sketcher / PartDesign -------------------------------------------------------


def _build_sketch(doc, spec):
    return sketcher_ops.build_sketch_gui(doc, spec)


def _build_sketch_edit(doc, spec):
    return sketcher_ops.edit_sketch_gui(doc, spec)


def _require_closed_profile(sketch, op):
    wires = getattr(sketch.Shape, "Wires", [])
    if not any(w.isClosed() for w in wires):
        raise ValueError(
            f"{op} requires a closed profile; '{sketch.Name}' has no closed wire "
            "(check coincident constraints between endpoint pairs)."
        )


def _wire_crossing(wire):
    """A point where two edges of ``wire`` meet away from a shared vertex, or None."""
    edges = wire.Edges
    for i, a in enumerate(edges):
        for b in edges[i + 1 :]:
            if not a.BoundBox.intersect(b.BoundBox):
                continue
            dist, pairs, _info = a.distToShape(b)
            if dist > 1e-7:
                continue
            shared = [
                v.Point
                for v in a.Vertexes
                if any((v.Point - w.Point).Length < 1e-7 for w in b.Vertexes)
            ]
            for point, _other in pairs:
                if all((point - s).Length > 1e-6 for s in shared):
                    return point
    return None


def _refuse_self_intersecting_profile(sketch, op):
    """A closed profile that crosses itself is not one region.

    FreeCAD pads it anyway: live on 1.1.4 a bow-tie outline became two
    triangles touching at a point (2 solids, 6000 mm^3), Valid, reported as
    success.
    """
    for wire in getattr(sketch.Shape, "Wires", []):
        if not wire.isClosed():
            continue
        try:
            point = _wire_crossing(wire)
        except Exception:
            continue
        if point is not None:
            local = sketch.Placement.inverse().multVec(point)
            raise ValueError(
                f"{op} profile '{sketch.Name}' crosses itself at sketch "
                f"({round(local.x, 3)}, {round(local.y, 3)}): FreeCAD would split it into "
                "separate regions and report success. Redraw the outline so that no two "
                "edges cross."
            )


def _feature_body(doc, spec, anchor, op: str):
    """The Body a feature built on ``anchor`` (its profile or base) belongs in.

    It used to come from ``body`` alone, so with two bodies every pad needed it
    and a wrong one was accepted: a pad landed in Body built from Body001's
    sketch. The anchor's own body decides; ``body`` may only confirm it.
    """
    owner = _parent_body(anchor) if anchor is not None else None
    named = spec.get("body")
    if owner is None:
        return sketcher_ops._get_or_create_body(doc, named)
    if named and named not in (owner.Name, owner.Label):
        raise ValueError(
            f"{op}: '{anchor.Name}' belongs to Body '{owner.Name}', not '{named}'. A feature is "
            f"built in the body of what it is built on; drop body or pass body='{owner.Name}'."
        )
    return owner


def _profile_sketch(doc, spec, op):
    _require(spec, "base")
    sketch = _get_obj(doc, spec["base"], "profile sketch")
    if sketch.TypeId != "Sketcher::SketchObject":
        raise ValueError(f"'{spec['base']}' is not a sketch (TypeId={sketch.TypeId}).")
    return sketch


# A cut that removed nothing changes no volume at all, so any hair of
# difference is a real removal. Shared by the direction probe and by the
# "removed no material" warning so the two cannot disagree.
_CUT_EPS = 1e-6


def _set_length(feat, op, value):
    """Assign Length with the op's parameter named on parse failures.

    FreeCAD's own messages for a bad quantity name nothing ("syntax error",
    "wrong type as quantity: NoneType"), so the caller could not tell which
    parameter was wrong. Negative values are legal in FreeCAD (the feature
    goes the other way) and only warn via describe_feature.
    """
    try:
        _set_or_bind(feat, "Length", value)
    except Exception as e:
        raise ValueError(
            f"{op} length {value!r} is not a valid length: {e}. Use a number in mm "
            "(7.5 or '10mm') or an expression like '=Vars.thickness'."
        ) from None


def _padlike_attachment_base(body, sketch):
    """The solid an attached profile cuts/fuses into, seen through the chain.

    PartDesign fuses a pocket/pad with the solid its sketch is DIRECTLY
    attached to, but not through an intermediary: a sketch attached to a datum
    plane that is itself attached to a solid gave the pocket no material at
    all, so it extruded its profile as a floating disk and reported success
    (live-verified: body volume 942.5 mm^3 of nothing instead of a cut). Walk
    the attachment chain and hand the solid to the body as its BaseFeature,
    which is what the GUI's "Base feature" tool does.

    A datum plane/point has a Shape of its own (a face / a vertex) but no
    SOLIDS, and adopting one as BaseFeature is silently refused — so only an
    object that actually carries a solid ends the walk.
    """
    ref = sketch
    for _ in range(4):
        support = list(getattr(ref, "AttachmentSupport", None) or [])
        if not support:
            return None
        obj = support[0][0] if support[0] else None
        if obj is None:
            return None
        shape = getattr(obj, "Shape", None)
        if shape is not None and not shape.isNull() and getattr(shape, "Solids", None):
            owner = body_owner(obj)
            if owner is not None and owner is not body:
                # The material lives in ANOTHER body; linking it as this
                # body's BaseFeature would span two bodies. Report no material
                # (the cut then warns instead of pretending).
                return None
            return obj
        ref = obj
    return None


def _ensure_material_base(doc, body, sketch, op):
    """Give the Body a base solid when its profile's material lives outside.

    No-op for a body that already has material, for a plain un-attached
    profile, and when the resolved solid is already a member.
    """
    try:
        if body is None or (body.Shape is not None and not body.Shape.isNull()):
            return None
        if getattr(body, "BaseFeature", None) is not None:
            return None
        solid = _padlike_attachment_base(body, sketch)
        if solid is None or solid in list(getattr(body, "Group", [])):
            return None
        body.BaseFeature = solid
        doc.recompute()
        FreeCAD.Console.PrintMessage(
            f"CADPilot: {op} profile is attached to '{solid.Name}' through a datum; "
            f"adopted it as {body.Name}'s BaseFeature so the feature has material.\n"
        )
        return solid
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: could not adopt a base feature: {e}\n")
        return None


def _body_material_volume(body) -> float:
    """The Body's volume BEFORE the new feature is added, or 0.0 when unreadable.

    Read it at the right moment: once the feature is built, ``body.Shape`` is
    that feature's own result and a "did this remove anything" comparison would
    be against itself. A body whose material comes from an adopted BaseFeature
    (a datum-attached profile) has a Tip with no volume, so that comes second.
    """
    for candidate in (getattr(body, "Tip", None), getattr(body, "BaseFeature", None)):
        if candidate is None:
            continue
        with contextlib.suppress(Exception):
            shape = getattr(candidate, "Shape", None)
            if shape is not None and not shape.isNull() and float(shape.Volume) > 0:
                return float(shape.Volume)
    return 0.0


_AUTO_DIRECTION_NOTE = ""


def _take_auto_direction_note() -> str:
    global _AUTO_DIRECTION_NOTE
    note, _AUTO_DIRECTION_NOTE = _AUTO_DIRECTION_NOTE, ""
    return note


_UNREFINED: set[str] = set()


def _apply_refine(feat, spec) -> None:
    """Set an explicit ``refine`` on a feature the moment it exists.

    The builders compute the feature while they decide things about it (which
    way a pocket cuts, whether an up-to pad ran away). Set after the builder,
    as it first was, ``refine=false`` let those probes read the REFINED result:
    live, a pocket on a body whose refined shape is invalid measured more
    volume both ways, concluded it cut air either way and removed nothing.
    """
    if spec.get("refine") is not None and "Refine" in feat.PropertiesList:
        feat.Refine = bool(spec["refine"])


def _unrefine_if_broken(doc, feat, spec) -> bool:
    """Turn Refine off when only refining made ``feat``'s result invalid; True if it did.

    Refine merges coplanar faces, and on a face shared by several features (a
    spine flush with a box whose rounded corners are tangent to it) OCC's merge
    can return an unorientable shell from a valid fuse — live: the unrefined
    fuse checked valid, the refined one gained 98 mm^3 and failed Shape.check,
    the pad after it kept only its own solid, and a pocket on it could not tell
    which way cut material, so it cut air. An explicit ``refine`` is the
    caller's and is left alone.
    """
    if spec.get("refine") is not None or not getattr(feat, "Refine", False):
        return False
    feat.Refine = False
    doc.recompute()
    _shape, valid, _vol, _solids, error = _read_shape_state(feat, doc)
    if valid and not error:
        _UNREFINED.add(feat.Name)
        return True
    feat.Refine = True
    doc.recompute()
    return False


def _read_unrefining(doc, feat, spec):
    """_read_shape_state, retried unrefined when the refined result is broken."""
    state = _read_shape_state(feat, doc)
    if (state[4] or not state[1]) and _unrefine_if_broken(doc, feat, spec):
        state = _read_shape_state(feat, doc)
    return state


def _auto_detect_cut_direction(doc, feat, spec, fc_type, base_vol) -> None:
    """Turn around a pocket that cuts AIR instead of material.

    FreeCAD's own default pocket direction points away from the solid when the
    profile lies on the body's start plane, and it says nothing about it: a
    fresh ``PartDesign::Pocket`` with FreeCAD's untouched defaults (profile on
    the XY plane under a pad spanning z 0..8, Length 5) removed exactly 0 mm^3,
    while ``Reversed = True`` removed the analytic pi*r^2*length (measured
    natively on 1.1.4). A GUI user ticks "Reversed" because the dialog shows the
    result; a caller over MCP gets a silent no-op cut, which is the one outcome a
    ``pocket`` can never mean.

    So the direction is decided from the geometry, not from FreeCAD's default:
    when the forward direction provably removes nothing (the volume did not
    budge) and reversing it removes material, the reversed direction wins and the
    reply says so. An explicit ``reversed`` is the caller's decision and is never
    overridden, ``midplane`` is symmetric so there is nothing to decide, and when
    NEITHER direction removes anything the feature is left exactly as asked for:
    that is a profile outside the solid or a bad attachment, and the existing
    no-material warning explains it better than a silent flip would.
    """
    if fc_type not in ("PartDesign::Pocket", "PartDesign::Hole") or base_vol <= 0:
        return
    if spec.get("reversed") is not None or spec.get("midplane"):
        return
    doc.recompute()
    _shape, _valid, forward_vol, _solids, forward_error = _read_unrefining(doc, feat, spec)
    if forward_error:
        # An unreadable Shape reports volume 0.0, which would read as "removed
        # EVERYTHING" and flip the cut on a feature that never computed at all.
        # Leave the direction alone; create_feature_gui reports the real fault.
        return
    if forward_vol < base_vol - _CUT_EPS:
        return  # it cut something: nothing to decide
    feat.Reversed = True
    doc.recompute()
    _shape, _valid, reversed_vol, _solids, reversed_error = _read_unrefining(doc, feat, spec)
    if reversed_error:
        feat.Reversed = False
        doc.recompute()
        return
    if reversed_vol >= base_vol - _CUT_EPS:
        feat.Reversed = False  # cuts air either way: not a direction problem
        doc.recompute()
        return
    global _AUTO_DIRECTION_NOTE
    note = (
        f"'{feat.Name}' cut material in the reverse direction, so its Reversed "
        f"property was set for you (removed {round(base_vol - reversed_vol, 3)} mm^3; "
        "the caller's direction would have cut air). Pass reversed=true/false "
        "explicitly to decide it yourself."
    )
    _AUTO_DIRECTION_NOTE = f"{_AUTO_DIRECTION_NOTE} {note}".strip()
    FreeCAD.Console.PrintMessage(f"CADPilot: {note}\n")


def _add_note(text: str) -> None:
    global _AUTO_DIRECTION_NOTE
    _AUTO_DIRECTION_NOTE = (
        f"{_AUTO_DIRECTION_NOTE} {text}".strip() if text else _AUTO_DIRECTION_NOTE
    )


def _up_to_face_ref(doc, body, spec):
    """(object, face name) a pad/pocket ends at: [obj, face] or a token on the body."""
    target = spec["up_to_face"]
    if isinstance(target, (list, tuple)) and len(target) == 2:
        ref = sketcher_ops.attachment_ref(_get_obj(doc, target[0], "up_to_face object"))
        token = target[1]
    elif isinstance(target, str):
        ref = sketcher_ops.attachment_ref(body)
        if ref is body:
            raise ValueError(
                "up_to_face as a bare direction needs a body that already has a shape."
            )
        token = target
    else:
        raise ValueError(
            f"up_to_face must be [object, '+Z'|'FaceN'] or a direction, got {target!r}"
        )
    name = _resolve_elements(ref, [token], "Face")[0]
    _record_resolved_faces(ref, [name])
    return ref, name


def _apply_padlike_mode(feat, op, fc_mode, spec, face_ref) -> None:
    if fc_mode == "Length":
        _set_length(feat, op, spec.get("length", 10.0))
        return
    if fc_mode == "TwoLengths":
        _set_length(feat, op, spec.get("length", 10.0))
        try:
            _set_or_bind(feat, "Length2", spec["length2"])
        except Exception as e:
            raise ValueError(
                f"{op} length2 {spec['length2']!r} is not a valid length: {e}."
            ) from None
        if "SideType" in feat.PropertiesList:
            # FreeCAD 1.1 hides the TwoLengths type ("?TwoLengths") behind SideType.
            feat.SideType = "Two sides"
        else:
            feat.Type = "TwoLengths"
        return
    try:
        feat.Type = fc_mode
    except Exception as e:
        raise ValueError(f"{op} cannot use {fc_mode} in this FreeCAD: {e}.") from None
    if face_ref is not None:
        feat.UpToFace = (face_ref[0], [face_ref[1]])
    if spec.get("offset") is not None:
        _set_or_bind(feat, "Offset", spec["offset"])


def _flip_up_to_direction(doc, feat, spec, fc_mode) -> None:
    """An up-to feature whose direction finds no face is built the other way.

    FreeCAD extrudes along the sketch normal (a pocket against it), so a
    profile above the part padded "up to first" fails with "No faces found in
    this direction" (live) while the reversed one is exactly what was meant.
    An explicit ``reversed`` is never overridden.
    """
    if fc_mode not in ("UpToFirst", "UpToLast", "UpToFace") or spec.get("reversed") is not None:
        return
    doc.recompute()
    if "Invalid" not in [str(s) for s in feat.State]:
        return
    feat.Reversed = True
    doc.recompute()
    if "Invalid" in [str(s) for s in feat.State]:
        feat.Reversed = False
        doc.recompute()
        return
    _add_note(
        f"'{feat.Name}' found no face in its default direction, so it was built reversed "
        "(Reversed=true). Pass reversed explicitly to decide it yourself."
    )


def _square_face(centre, normal, half: float):
    """A square planar face of side 2*half, centred on ``centre``, normal to ``normal``."""
    u = normal.cross(FreeCAD.Vector(1, 0, 0))
    if u.Length < 1e-6:
        u = normal.cross(FreeCAD.Vector(0, 1, 0))
    u.normalize()
    v = normal.cross(u)
    v.normalize()
    corners = [centre + (u * a + v * b) * half for a, b in ((1, 1), (-1, 1), (-1, -1), (1, -1))]
    return Part.Face(Part.makePolygon([*corners, corners[0]]))


def _in_body_frame(feat, shape):
    """``shape`` (a copy), moved from ``feat``'s own frame into its Body's.

    A PartDesign feature's Shape already carries the feature's Placement, but
    its AddSubShape does not, and that Placement is the first sketch's: every
    feature of a body started on XZ or YZ is rotated against its own Shape.
    """
    moved = shape.copy()
    moved.Placement = feat.Placement.multiply(moved.Placement)
    return moved


def _ends_on_a_plane_of(added, sketch_plane, direction, limiters) -> bool:
    """True when every far cap of ``added`` lies in the plane of a planar face of ``limiters``.

    FreeCAD extends a planar up-to face that the profile overhangs, so a pad up
    to a tilted face smaller than the profile legitimately ends past the part
    (live: a 40x40 profile up to a 20x20 face at 30 degrees, valid, was refused
    as "5.8 mm past").
    """
    normal = sketch_plane.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    caps = []
    for face in added.Faces:
        surface = face.Surface
        if not isinstance(surface, Part.Plane):
            u, v = surface.parameter(face.CenterOfMass)
            if abs(face.normalAt(u, v).dot(direction)) > 1e-6:
                return False  # a curved end: it stopped on a curved face, or ran away
            continue
        if abs(surface.Axis.dot(direction)) < 1e-6:
            continue  # a side of the prism
        on_sketch = abs((surface.Position - sketch_plane.Base).dot(normal)) < 1e-6
        if on_sketch and surface.Axis.cross(normal).Length < 1e-6:
            continue  # the start cap
        caps.append(surface)
    planes = [
        face.Surface
        for shape in limiters
        for face in shape.Faces
        if isinstance(face.Surface, Part.Plane)
    ]
    return bool(caps) and all(
        any(
            cap.Axis.cross(plane.Axis).Length < 1e-6
            and abs((cap.Position - plane.Position).dot(plane.Axis)) < 1e-4
            for plane in planes
        )
        for cap in caps
    )


def _refuse_runaway_up_to(doc, feat, fc_mode) -> None:
    """Refuse an up-to feature that ran past the part instead of stopping at a face.

    When no face covers the whole profile (a stub whose top rises above the
    cylinder it should end on), FreeCAD's up-to modes do not fail: they extrude
    a long way and report success (live on 1.1.4: up_to_first, up_to_face and
    UpToShape all ran a 36 mm stub out to x = 227..254). The end of an up-to
    feature must lie within the part it is limited by.

    Everything is compared in the Body's frame. Mixing in the sketch's GLOBAL
    placement and the feature-local AddSubShape refused valid pads (live: a
    stub padded up to a wall in a body started on XZ, "20.0 mm past the part";
    a moved Body the same) and let a real runaway through in a rotated Body.
    """
    if fc_mode not in ("UpToFirst", "UpToLast", "UpToFace"):
        return
    base = getattr(feat, "BaseFeature", None)
    if base is None:
        return
    doc.recompute()
    try:
        added, part = _in_body_frame(feat, feat.AddSubShape), base.Shape
        plane = feat.Profile[0].Placement
        direction = plane.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        # FreeCAD turns an up-to feature toward its face whatever Reversed says,
        # so the side of the sketch plane the solid lies on is the direction
        if (added.BoundBox.Center - plane.Base).dot(direction) < 0:
            direction = -direction
    except Exception:
        return
    if added.isNull() or part.isNull():
        return

    far = 1e5
    wall = _square_face(direction * far, direction, 2 * far)

    def reach(shape) -> float:
        # exact for curved faces too, where the farthest point is no vertex
        return far - shape.distToShape(wall)[0]

    limiters = [part]
    with contextlib.suppress(Exception):
        link, subs = feat.UpToFace
        body = _parent_body(feat)
        for sub in subs or [""]:
            target = link.Shape.getElement(sub) if sub else link.Shape
            if body is not None and _parent_body(link) is not body and link is not body:
                # a face of an object outside the body is in the global frame
                target = target.copy()
                target.Placement = body.getGlobalPlacement().inverse().multiply(target.Placement)
            limiters.append(target)
    limit = max(reach(shape) for shape in limiters)
    # a positive offset ends past the face on purpose
    with contextlib.suppress(Exception):
        limit += max(0.0, float(getattr(feat.Offset, "Value", feat.Offset)))
    past = reach(added) - limit
    if past <= 1e-3:
        return
    with contextlib.suppress(Exception):
        if _ends_on_a_plane_of(added, plane, direction, limiters):
            return
    raise RuntimeError(
        f"{fc_mode} found no face of the part that covers the whole profile, so FreeCAD "
        f"extruded {past:.1f} mm past the part instead of stopping. Pad a length, or limit "
        "it to a face that covers the whole profile."
    )


def _build_padlike(doc, spec, fc_type, default_name):
    op = fc_type.split("::")[-1].lower()
    sketch = _profile_sketch(doc, spec, fc_type)
    body = _feature_body(doc, spec, sketch, op)
    fc_mode, mode_note = tip_policy.padlike_mode(op, spec)
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    _refuse_self_intersecting_profile(sketch, op)
    _ensure_material_base(doc, body, sketch, op)
    # Snapshot the material BEFORE the feature joins the body: afterwards its
    # Shape is the feature's own result (see _body_material_volume).
    base_vol = _body_material_volume(body)
    face_ref = _up_to_face_ref(doc, body, spec) if fc_mode == "UpToFace" else None
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    _apply_refine(feat, spec)
    feat.Profile = sketch
    # through_all is PartDesign's OWN parametric through-all, not a big numeric
    # length: a numeric stand-in only goes through while the part is thinner
    # than it (live: a Ø6 "through" hole in a flange left a 10 mm floor at
    # 20 mm, +1696 mm³ of uncut material). `length` is ignored in the
    # through/up-to modes, by FreeCAD's own semantics.
    _apply_padlike_mode(feat, op, fc_mode, spec, face_ref)
    feat.Reversed = bool(spec.get("reversed", False))
    if fc_mode != "TwoLengths":
        # Never write False over a "Two sides" feature: it resets it to one side
        # (live: 5 + 3 built 5 mm only).
        midplane = bool(spec.get("midplane", False))
        if "SideType" in feat.PropertiesList:
            # 1.1 deprecates Midplane and warns on every write to it.
            feat.SideType = "Symmetric" if midplane else "One side"
        else:
            feat.Midplane = midplane
    _add_note(mode_note)
    _flip_up_to_direction(doc, feat, spec, fc_mode)
    _refuse_runaway_up_to(doc, feat, fc_mode)
    _auto_detect_cut_direction(doc, feat, spec, fc_type, base_vol)
    return feat


def _build_pad(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pad", "Pad")


def _build_pocket(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pocket", "Pocket")


# A recompute failure with an empty StatusString leaves the caller nothing to
# act on; these name the operation's own known trap instead.
_FAILURE_HINTS = {
    # OCC's MakeThickSolid fails on a removed face that carries an INNER WIRE
    # (measured live: a 60x40x10 plate with a 20x20 blind pocket refused
    # thickness("+Z") at value 2 and 3, StatusString empty, while the opposite
    # face worked).
    "thickness": "OCC fails when a removed face carries an inner wire (a hole/pocket); "
    "try reversed=true, a smaller value, or the opposite face",
    "revolution": "check that the profile is a closed wire on ONE side of the axis "
    "(a profile crossing its revolve axis is rejected by FreeCAD)",
    "groove": "check that the profile is a closed wire on one side of the axis",
    "draft": "the neutral plane must be perpendicular to the drafted faces and meet them, and "
    "a large angle can make the tapered faces cross; check neutral_plane, pull_direction and "
    "the angle",
}

_REV_AXIS = {"X": "H_Axis", "Y": "V_Axis", "Z": "N_Axis"}


def _build_revlike(doc, spec, fc_type, default_name):
    sketch = _profile_sketch(doc, spec, fc_type)
    body = _feature_body(doc, spec, sketch, fc_type.split("::")[-1].lower())
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    _refuse_self_intersecting_profile(sketch, fc_type.split("::")[-1].lower())
    _ensure_material_base(doc, body, sketch, fc_type.split("::")[-1].lower())
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    _apply_refine(feat, spec)
    feat.Profile = sketch
    _set_or_bind(feat, "Angle", spec.get("angle", 360.0))
    # Default "Y" (the sketch's V axis) is the lathe setup a profile is
    # normally drawn for: v = axial position, u = radius. The old default "Z"
    # was the profile normal, i.e. always degenerate.
    axis = spec.get("axis", "Y")
    edge = axis.get("edge") if isinstance(axis, dict) else None
    if edge:
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
            raise ValueError("axis.edge must be [object_name, 'EdgeN'].")
        ref = _get_obj(doc, edge[0], "axis edge object")
        edge_name = str(edge[1])
        n = int(edge_name[4:]) if edge_name.startswith("Edge") else 0
        if n < 1 or n > len(ref.Shape.Edges):
            raise ValueError(
                f"'{edge_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Edges)})."
            )
        feat.ReferenceAxis = (ref, [edge_name])
    else:
        sub = _REV_AXIS.get(str(axis).upper())
        if sub is None:
            raise ValueError("axis must be 'X'/'Y'/'Z' or {\"edge\": [obj, \"EdgeN\"]}")
        if sub == "N_Axis":
            # Revolving a planar profile about its own normal keeps every
            # point in the drawing plane: the result is a flat zero-volume
            # shell, never a solid (FreeCAD still calls it valid).
            raise ValueError(
                "axis='Z' is the profile's own normal — revolving about it sweeps a "
                "flat zero-volume shell, not a solid. Use 'X'/'Y' (the sketch's "
                'in-plane axes) or {"edge": [obj, "EdgeN"]}.'
            )
        feat.ReferenceAxis = (sketch, [sub])
    feat.Reversed = bool(spec.get("reversed", False))
    return feat


def _build_hole(doc, spec):
    """PartDesign::Hole at every circle centre of a sketch (plain drilled holes).

    The hole's diameter is its own; the circles only place it. Threads, counter
    bores and countersinks are FreeCAD's Hole task panel's to set afterwards.
    """
    _require(spec, "diameter")
    sketch = _profile_sketch(doc, spec, "hole")
    body = _feature_body(doc, spec, sketch, "hole")
    circles = [
        g
        for i, g in enumerate(sketch.Geometry)
        if g.TypeId == "Part::GeomCircle" and not sketch.getConstruction(i)
    ]
    if not circles:
        raise ValueError(
            f"hole needs circles in '{sketch.Name}': a hole is drilled at each circle's centre."
        )
    through = bool(spec.get("through"))
    if through == (spec.get("depth") is not None):
        raise ValueError("hole needs exactly one of depth (mm or '=expression') or through=true.")
    point = str(spec.get("drill_point", "flat")).lower()
    if point not in ("flat", "angled"):
        raise ValueError(f"drill_point must be 'flat' or 'angled', got {spec.get('drill_point')!r}")
    doc.recompute()
    _ensure_material_base(doc, body, sketch, "hole")
    base_vol = _body_material_volume(body)
    feat = body.newObject("PartDesign::Hole", spec.get("name") or "Hole")
    _apply_refine(feat, spec)
    feat.Profile = sketch
    feat.Threaded = False
    _set_or_bind(feat, "Diameter", spec["diameter"])
    if through:
        feat.DepthType = "ThroughAll"
    else:
        feat.DepthType = "Dimension"
        _set_or_bind(feat, "Depth", spec["depth"])
    feat.DrillPoint = point.capitalize()
    feat.Reversed = bool(spec.get("reversed", False))
    feat.Midplane = bool(spec.get("midplane", False))
    _auto_detect_cut_direction(doc, feat, spec, "PartDesign::Hole", base_vol)
    return feat


def _build_revolution(doc, spec):
    return _build_revlike(doc, spec, "PartDesign::Revolution", "Revolution")


def _build_groove(doc, spec):
    return _build_revlike(doc, spec, "PartDesign::Groove", "Groove")


_ORIGIN_ROLE = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}


def _build_datum_plane(doc, spec):
    """PartDesign datum plane on a base plane or existing face (+offset)."""
    _require(spec, "plane")
    plane = spec["plane"]
    anchor = None
    if isinstance(plane, dict) and isinstance(plane.get("face"), (list, tuple)) and plane["face"]:
        anchor = doc.getObject(str(plane["face"][0]))
        if anchor is not None:
            anchor = sketcher_ops.attachment_ref(anchor)
    body = _feature_body(doc, spec, anchor, "datum_plane")
    dp = body.newObject("PartDesign::Plane", spec.get("base") or "DatumPlane")
    support_prop = "AttachmentSupport" if hasattr(dp, "AttachmentSupport") else "Support"
    center_target = None
    if isinstance(plane, str):
        role = _ORIGIN_ROLE.get(plane.upper())
        if role is None:
            raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
        origin = getattr(body, "Origin", None)
        support = None
        for feat in getattr(origin, "OriginFeatures", []):
            if getattr(feat, "Role", "") == role:
                support = feat
                break
        if support is None:
            raise ValueError(f"body '{body.Name}' origin has no {role}.")
        setattr(dp, support_prop, [(support, "")])
    elif isinstance(plane, dict) and plane.get("face"):
        face = plane["face"]
        if not (isinstance(face, (list, tuple)) and len(face) == 2):
            raise ValueError("plane.face must be [object_name, 'FaceN'].")
        ref = _get_obj(doc, face[0], "datum plane face object")
        # Never name the Body itself: its Shape is its Tip's result, so a feature
        # built on anything attached there closes a dependency cycle (see
        # sketcher_ops.attachment_ref).
        ref = sketcher_ops.attachment_ref(ref)
        face_name = str(face[1])
        # Direction tokens (+Z/top/…) resolve like a sketch's plane.face —
        # face names are re-derived after every feature, so the direction is
        # the stable reference.
        if face_name.startswith(("+", "-")) or face_name.lower() in sketcher_ops._FACE_WORDS:
            face_name = sketcher_ops._resolve_semantic_face(ref, face_name)
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
        setattr(dp, support_prop, [(ref, face_name)])
        _record_resolved_faces(ref, [face_name])
        if plane.get("center"):
            center_target = (ref, face_name)
    else:
        raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
    dp.MapMode = "FlatFace"
    raw = spec.get("offset", 0) or 0
    bound = _is_expression(raw)
    offset = 0.0 if bound else _number(raw, "datum_plane offset")
    if center_target is not None:
        # plane={"face": [...], "center": true}: origin at the middle of the
        # face instead of its parametric origin (a corner on rectangular
        # faces), so a sketch attached to this datum lands where it looks.
        sketcher_ops.center_attachment(dp, center_target[0], center_target[1], doc, offset)
    elif offset:
        dp.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, offset), FreeCAD.Rotation())
    if bound:
        # The offset runs along the plane normal, the attachment's own Z.
        dp.setExpression(".AttachmentOffset.Base.z", raw[1:])
    return dp


def _hull_view_faces(sketch):
    """Closed wires of a view sketch -> planar faces (global coords)."""
    faces = [Part.Face(w) for w in getattr(sketch.Shape, "Wires", []) if w.isClosed()]
    if not faces:
        raise ValueError(f"hull view '{sketch.Name}' has no closed wire.")
    return faces


def _build_hull(doc, spec):
    """Visual hull: intersect the extrusions of 2-3 view-profile sketches.

    Result is a static Part::Feature (no proxy — survives document reload
    without the addon on sys.path). Re-running with the same name replaces
    the Shape in place (iterate: edit a view sketch, re-run hull).

    v1 limits: view sketches must sit at the global origin (their Placement
    IS the global frame); each view contributes its closed wires fused, so
    use ONE outer profile per view (hole wires are not subtracted).
    """
    _require(spec, "sketches")
    names = spec["sketches"]
    if isinstance(names, dict):
        names = [names[k] for k in ("top", "front", "side") if names.get(k)]
    if not isinstance(names, list) or not 2 <= len(names) <= 3:
        raise ValueError("hull requires 2-3 view sketches (list or {'top','front','side'}).")
    views = []
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for n in names:
        s = _get_obj(doc, n, "hull view sketch")
        if s.TypeId != "Sketcher::SketchObject":
            raise ValueError(f"'{n}' is not a sketch (TypeId={s.TypeId}).")
        faces = _hull_view_faces(s)
        for f in faces:
            bb = f.BoundBox
            mins = [min(mins[0], bb.XMin), min(mins[1], bb.YMin), min(mins[2], bb.ZMin)]
            maxs = [max(maxs[0], bb.XMax), max(maxs[1], bb.YMax), max(maxs[2], bb.ZMax)]
        views.append((s, faces))

    diag = (FreeCAD.Vector(*maxs) - FreeCAD.Vector(*mins)).Length
    margin = float(spec.get("margin") or max(1.0, 0.05 * diag))
    corners = [
        FreeCAD.Vector(x, y, z)
        for x in (mins[0], maxs[0])
        for y in (mins[1], maxs[1])
        for z in (mins[2], maxs[2])
    ]

    prisms = []
    for s, faces in views:
        normal = s.Placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        dots = [c.dot(normal) for c in corners]
        tmin, tmax = min(dots) - margin, max(dots) + margin
        prism = None
        for f in faces:
            fc = f.copy()  # translate() is in-place; never mutate sketch geometry
            fc.translate(normal * (tmin - f.CenterOfMass.dot(normal)))
            e = fc.extrude(normal * (tmax - tmin))
            prism = e if prism is None else prism.fuse(e)
        prisms.append(prism)

    result = prisms[0]
    for p in prisms[1:]:
        result = result.common(p)
    if not result.Solids or result.Volume < 1e-6:
        raise RuntimeError(
            "visual hull is empty — the view profiles do not overlap along every axis."
        )
    if len(result.Solids) > 1:
        result = max(result.Solids, key=lambda sol: sol.Volume)

    name = spec.get("base") or "Hull"
    existing = doc.getObject(name)
    if existing is not None:
        if existing.TypeId != "Part::Feature":
            raise ValueError(
                f"'{name}' exists and is not a hull result (TypeId={existing.TypeId})."
            )
        existing.Shape = result
        return existing
    feat = doc.addObject("Part::Feature", name)
    feat.Shape = result
    return feat


def _build_thickness(doc, spec):
    _require(spec, "faces", "value")
    if spec["faces"] == "all":
        # faces = the faces to OPEN. Selecting all of them leaves nothing to
        # remove: FreeCAD accepts the feature and returns the solid unchanged,
        # so the old path reported success with identical geometry (live).
        raise ValueError(
            "thickness needs the faces to OPEN; faces='all' removes every wall and "
            "FreeCAD returns the solid unchanged. List the faces to remove "
            '(e.g. faces=[5] or ["Face5"]) or all but one.'
        )
    base = _get_obj(doc, spec["base"], "base feature")
    body = _feature_body(doc, spec, base, "thickness")
    owner = _parent_body(base)
    if owner is not None:
        _require_end_of_chain(owner, base, "thickness")
    names = _resolve_elements(base, spec["faces"], "Face")
    _record_resolved_faces(base, names)
    feat = body.newObject("PartDesign::Thickness", spec.get("name") or "Thickness")
    _apply_refine(feat, spec)
    if "Faces" in feat.PropertiesList:
        # FreeCAD <= 1.0: plain Base link + separate Faces LinkSub.
        feat.Base = base
        feat.Faces = (base, names)
    else:
        # FreeCAD >= 1.1 rework: faces ride along on the Base LinkSub.
        feat.Base = (base, names)
    _set_or_bind(feat, "Value", spec["value"])
    feat.Reversed = bool(spec.get("reversed", False))
    return feat


_NEUTRAL_PREFERENCE = ("-Z", "+Z", "-Y", "+Y", "-X", "+X")


def _default_neutral_plane(base, names) -> str:
    """The face a draft pivots on when the caller names none.

    The old default was the first DRAFTED face itself, which FreeCAD can never
    build, so every draft without neutral_plane failed. The usual mould setup
    is the bottom: a planar face perpendicular to every drafted face, -Z first.
    """
    shape = base.Shape
    drafted = []
    for name in names:
        face = shape.getElement(name)
        if face.Surface.TypeId == "Part::GeomPlane":
            drafted.append(face.normalAt(0, 0))
    best = None
    for i, face in enumerate(shape.Faces, start=1):
        if f"Face{i}" in names or face.Surface.TypeId != "Part::GeomPlane":
            continue
        normal = face.normalAt(0, 0)
        if any(abs(normal.dot(n)) > 1e-6 for n in drafted):
            continue
        for rank, token in enumerate(_NEUTRAL_PREFERENCE):
            axis = "XYZ".index(token[1])
            sign = 1.0 if token[0] == "+" else -1.0
            if normal[axis] * sign > 1.0 - 1e-6:
                key = (rank, -face.CenterOfMass[axis] * sign)
                if best is None or key < best[0]:
                    best = (key, f"Face{i}")
    if best is None:
        raise ValueError(
            "draft needs neutral_plane: no planar face of the base is perpendicular to every "
            "drafted face, so there is no face for the taper to pivot on. Pass "
            "neutral_plane='-Z' (a direction token or FaceN)."
        )
    return best[1]


def _build_draft(doc, spec):
    _require(spec, "faces", "angle")
    base = _get_obj(doc, spec["base"], "base feature")
    body = _feature_body(doc, spec, base, "draft")
    owner = _parent_body(base)
    if owner is not None:
        _require_end_of_chain(owner, base, "draft")
    names = _resolve_elements(base, spec["faces"], "Face")
    _record_resolved_faces(base, names)
    neutral = spec.get("neutral_plane")
    if neutral is None:
        neutral_names = [_default_neutral_plane(base, names)]
    else:
        neutral_names = _resolve_elements(
            base, neutral if isinstance(neutral, list) else [neutral], "Face"
        )
        if neutral_names[0] in names:
            raise ValueError(
                f"draft neutral_plane {neutral_names[0]} is one of the faces being drafted; the "
                "neutral plane is the face the taper pivots on (usually the bottom, '-Z'), and "
                "it must be perpendicular to the drafted faces."
            )
    global _LAST_FEATURE_INFO
    if isinstance(_LAST_FEATURE_INFO, dict):
        _LAST_FEATURE_INFO["neutral_plane"] = neutral_names[0] + (
            " (default: the outermost face perpendicular to every drafted face, bottom first)"
            if neutral is None
            else ""
        )
    feat = body.newObject("PartDesign::Draft", spec.get("name") or "Draft")
    _apply_refine(feat, spec)
    if "Faces" in feat.PropertiesList:
        feat.Base = base
        feat.Faces = (base, names)
    else:
        feat.Base = (base, names)
    _set_or_bind(feat, "Angle", spec["angle"])
    feat.NeutralPlane = (base, neutral_names[:1])
    direction = spec.get("pull_direction")
    if direction is not None:
        # PullDirection is a LinkSub: {"edge": ["ObjName", "EdgeN"]} —
        # the draft pulls along that edge's direction.
        edge = direction.get("edge") if isinstance(direction, dict) else None
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
            raise ValueError(
                'pull_direction must be {"edge": [obj, "EdgeN"]} '
                "(or omitted to keep the FreeCAD default)."
            )
        ref = _get_obj(doc, edge[0], "pull direction edge object")
        edge_name = str(edge[1])
        n = int(edge_name[4:]) if edge_name.startswith("Edge") else 0
        if n < 1 or n > len(ref.Shape.Edges):
            raise ValueError(
                f"'{edge_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Edges)})."
            )
        feat.PullDirection = (ref, [edge_name])
    return feat


_CUT_TYPES = ("pocket", "groove", "hole")


def _cut_without_material(feat) -> str:
    """Warn when a cut feature has no solid to cut into.

    A pocket/groove whose profile resolves to no material (an un-attached
    sketch in an empty body, an attachment chain ending in another body) still
    "succeeds": FreeCAD returns the profile's own extrusion — a floating disk
    where the user expected a hole (live: 942.5 mm^3 of nothing). The material
    must come from ANOTHER member: reading `body.Shape` alone is not enough,
    because by the time this runs the body's tip IS the cut feature, so its
    own extrusion looked like material and the warning went silent
    (live-verified: an unsupported sketch in an empty body reported plain
    success).
    """
    try:
        if getattr(feat, "BaseFeature", None) is not None:
            return ""
        body = _parent_body(feat)
        if body is not None:
            adopted = getattr(body, "BaseFeature", None)
            if adopted is not None:
                sh = getattr(adopted, "Shape", None)
                if sh is not None and not sh.isNull() and float(sh.Volume) > 0:
                    return ""  # an adopted base solid is the material
            for member in getattr(body, "Group", []) or []:
                if member is feat:
                    continue
                if not tip_policy.advances_tip(getattr(member, "TypeId", "")):
                    continue
                sh = getattr(member, "Shape", None)
                if sh is not None and not sh.isNull() and float(sh.Volume) > 0:
                    return ""  # attachment fusion with an existing member
        return (
            f"{feat.Name} has no material to cut: neither the body nor a BaseFeature "
            "provides a solid, so the result is the profile's own extrusion (a floating "
            "disk), not a hole. Attach the profile to a face of the solid this body "
            "contains, or give the body its base first."
        )
    except Exception:
        return ""


def _cut_removed_nothing(feat) -> str:
    """Warn when a cut feature removed no material.

    A pocket attached to the wrong face (or whose profile sits outside the
    solid) cuts air and still reports success — the sketch lands on a bad plane
    silently. The ground truth is the body's volume just before this feature:
    a PartDesign feature's ``BaseFeature`` is its predecessor in the body.
    """
    try:
        baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            profile = feat.Profile[0]
            support = list(getattr(profile, "AttachmentSupport", None) or [])
            baseline = support[0][0] if support else None
        if baseline is None:
            return ""
        prev_vol = float(baseline.Shape.Volume)
        new_vol = float(feat.Shape.Volume)
    except Exception:
        return ""
    if prev_vol <= 0 or new_vol < prev_vol - _CUT_EPS:
        return ""
    return (
        f"{feat.Name} removed no material (volume unchanged at {round(new_vol, 1)} mm^3): "
        "the profile is probably outside the solid or the sketch attached to the wrong "
        "face. Face names shift after every feature — prefer a direction selector, "
        "plane={'face': [obj, '+Z']}."
    )


def _add_added_nothing(feat) -> str:
    """Warn when an additive feature added no material.

    A pad that extrudes into material the part already has (the wrong
    direction, or a profile inside the solid) still "succeeds" with the volume
    unchanged — the pocket's no-op warning had no additive twin (live: a pad
    pointed into the plate reported plain success).
    """
    try:
        baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            return ""
        prev_vol = float(baseline.Shape.Volume)
        new_vol = float(feat.Shape.Volume)
    except Exception:
        return ""
    if prev_vol <= 0 or abs(new_vol - prev_vol) > _CUT_EPS:
        return ""  # it grew; a body that SHRANK never gets here (_refuse_shrunk_body)
    return (
        f"{feat.Name} added no material (volume unchanged at {round(new_vol, 1)} mm^3): the "
        "profile extrudes into material the part already has. Check the direction "
        "(reversed=true extrudes the other way) and the sketch's plane."
    )


def _refuse_shrunk_body(feat) -> None:
    """Fail an additive feature that left the body SMALLER than it found it.

    That only happens when the fuse with its base failed and FreeCAD kept the
    new solid alone (live: a 1128 mm^3 foot padded onto a 71413 mm^3 part that
    an earlier refine had left invalid). It was first reported as "added no
    material" and then as a warning on a successful step, while the part was
    gone; raising aborts the step and leaves the body as it was.
    """
    try:
        baseline = feat.BaseFeature
        prev_vol = float(baseline.Shape.Volume)
        new_vol = float(feat.Shape.Volume)
    except Exception:
        return
    if prev_vol > 0 and new_vol < prev_vol - _CUT_EPS:
        raise RuntimeError(
            f"{feat.Name} SHRANK the body from {round(prev_vol, 1)} to {round(new_vol, 1)} mm^3: "
            f"the fuse with '{baseline.Name}' failed and kept only the new solid, so the step "
            f"was not applied. '{baseline.Name}' is probably invalid (Shape.isValid()); "
            "rebuilding it with refine=false is the usual fix."
        )


def _pad_created_disconnected_solid(feat) -> str:
    """Warn when an additive feature left the Body with a floating solid.

    A PartDesign Body has AllowCompound=true by default, so a pad whose
    profile does not touch the base still "succeeds" — the Body quietly
    becomes a two-solid compound (live: a 750 mm^3 slab floating 15 mm off
    the base, counted into the Body's volume, no error anywhere). A solid
    count that grew past the predecessor's is the signal.
    """
    try:
        baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            profile = feat.Profile[0] if getattr(feat, "Profile", None) else None
            support = list(getattr(profile, "AttachmentSupport", None) or [])
            baseline = support[0][0] if support else None
        if baseline is None:
            return ""
        before = len(baseline.Shape.Solids)
        after = len(feat.Shape.Solids)
    except Exception:
        return ""
    if after <= max(before, 1):
        return ""
    return (
        f"{feat.Name} produced {after} disconnected solid(s) (the base had {before}): the "
        "profile does not touch the base, so the pad floats next to it (FreeCAD bodies "
        "allow compounds, so this is valid but almost never intended). Check the sketch's "
        "attachment and its position on the face."
    )


def _thickness_changed_nothing(feat) -> str:
    """Warn when a shell left the volume unchanged.

    The remaining case is a selection that spans the whole surface (or a body
    whose walls cannot be offset): FreeCAD computes, returns the base solid,
    and the op would report plain success with identical geometry.
    """
    try:
        baseline = getattr(feat, "Base", None)
        if isinstance(baseline, tuple):
            baseline = baseline[0]
        if baseline is None:
            baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            return ""
        prev = float(baseline.Shape.Volume)
        new = float(feat.Shape.Volume)
    except Exception:
        return ""
    if prev <= 0 or abs(new - prev) > max(1e-6, prev * 1e-9):
        return ""
    return (
        f"{feat.Name} changed nothing (volume still {round(new, 1)} mm^3): the selected "
        "faces leave no wall to create. For a shell, select the faces to OPEN — a "
        "selection covering the whole surface removes nothing."
    )


def _thickness_grew_outward(feat) -> str:
    """Warn when the shell ADDED material outside the original part.

    PartDesign's Thickness default offsets outward whenever the selected face
    normal points out of the material (live: a 100x60x40 box came back
    -3..103 in every axis, 240000 -> 59850 mm^3 with the walls sitting on the
    OUTSIDE). "Shell" usually means hollowing the part, so the growth is worth
    naming; reversed=true flips the direction.
    """
    try:
        baseline = getattr(feat, "Base", None)
        if isinstance(baseline, tuple):
            baseline = baseline[0]
        if baseline is None:
            baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            return ""
        b0 = baseline.Shape.BoundBox
        b1 = feat.Shape.BoundBox
    except Exception:
        return ""
    grew = (
        b1.XMin < b0.XMin - 1e-6
        or b1.YMin < b0.YMin - 1e-6
        or b1.ZMin < b0.ZMin - 1e-6
        or b1.XMax > b0.XMax + 1e-6
        or b1.YMax > b0.YMax + 1e-6
        or b1.ZMax > b0.ZMax + 1e-6
    )
    if not grew:
        return ""
    return (
        f"{feat.Name} added material OUTSIDE the base part (bbox grew from "
        f"[{round(b0.XMin, 1)}, {round(b0.YMin, 1)}, {round(b0.ZMin, 1)}] x "
        f"[{round(b0.XMax, 1)}, {round(b0.YMax, 1)}, {round(b0.ZMax, 1)}] to "
        f"[{round(b1.XMin, 1)}, {round(b1.YMin, 1)}, {round(b1.ZMin, 1)}] x "
        f"[{round(b1.XMax, 1)}, {round(b1.YMax, 1)}, {round(b1.ZMax, 1)}]): the "
        "selected face's normal points out of the material, so the wall was offset "
        "outward. Pass reversed=true for an inward (hollowing) shell."
    )


def describe_feature_reply(feat, spec) -> dict:
    """``describe_feature`` plus the shape-check advisory — the RPC layer's call.

    The advisory belongs to the op that JUST ran, so it is merged here rather
    than in every branch of describe_feature (whose body keeps its own, widely
    asserted, shape)."""
    out = describe_feature(feat, spec) or {}
    note = _take_shape_check_note()
    direction = _take_auto_direction_note()
    if direction:
        # An auto-decided cut direction is a silent-geometry fix the caller must
        # see: the geometry is right, but the STEP as written would not be.
        note = f"{note} {direction}".strip() if note else direction
    if note:
        warnings = out.get("warnings")
        if isinstance(warnings, list):
            warnings.append(note)
        else:
            out["warnings"] = [note]
    global _MODEL_REPORT
    report, _MODEL_REPORT = _MODEL_REPORT, {}
    if report.get("warnings"):
        # Breakage first: it is the headline, the op's own advisories follow.
        out["warnings"] = report["warnings"] + list(out.get("warnings") or [])
    for key in ("broken", "model"):
        if report.get(key):
            out[key] = report[key]
    return out


def describe_feature(feat, spec) -> dict:
    """Extra result fields for the RPC response (sketch, hull, cut no-ops)."""
    global _EDGE_SELECTION
    op = spec.get("type")
    if op == "hull":
        sh = feat.Shape
        return {"volume_mm3": round(sh.Volume, 2), "solids": len(sh.Solids)}
    if op == "color":
        # One entry per object actually painted, with the values READ BACK off
        # the ViewObject: the reply is the only place a multi-object or
        # redirected (feature -> Body) call becomes visible, and `skipped` is the
        # only place a wildcard sweep says what it could not paint. Both lists
        # are capped (the counts always ride along) so a whole-document sweep
        # cannot dump hundreds of rows into the caller's context.
        info = pop_last_feature_info() or {}
        colored = info.get("color") or []
        skipped = info.get("skipped") or []
        out = {"colored_count": len(colored), "colored": colored[:_COLOR_REPORT_MAX]}
        if len(colored) > _COLOR_REPORT_MAX:
            out["colored_truncated"] = len(colored) - _COLOR_REPORT_MAX
        if skipped:
            out["skipped_count"] = len(skipped)
            out["skipped"] = skipped[:_COLOR_REPORT_MAX]
            names = ", ".join(s["object"] for s in skipped[:5])
            out["warnings"] = [
                f"color skipped {len(skipped)} object(s) it cannot paint: {names} "
                f"— first reason: {skipped[0]['reason']}"
            ]
        return out
    if op in ("fillet", "chamfer"):
        info = pop_last_feature_info() or {}
        out = {}
        if info.get("dressed"):
            # A Body was named: name the feature that was dressed, or "which
            # feature is the Body's Tip" stays a guess.
            out = {
                "dressed_object": info["dressed"],
                "note": f"the {op} was built on '{info['dressed']}', the Tip of Body "
                f"'{info['body']}' (a PartDesign dress-up lives inside its Body and "
                "becomes its new Tip).",
            }
        elif info.get("hidden_base"):
            out = {
                "hidden_base": info["hidden_base"],
                "note": f"the {op} replaced '{info['hidden_base']}' in the display: the "
                "base object is the same solid with sharper edges, so its Visibility was "
                "turned off to avoid two overlapping solids. Its data is untouched; set "
                "Visibility back on if you need to see it.",
            }
        if _EDGE_SELECTION:
            # A geometric selector picks edges by rule: the reply must show
            # which ones, or a wrong pick reads as success.
            out["resolved_edges"] = {
                "count": len(_EDGE_SELECTION),
                "edges": _EDGE_SELECTION[:_COLOR_REPORT_MAX],
            }
        _EDGE_SELECTION = None
        return out
    warnings: list[str] = []
    resolved = (
        pop_last_feature_info()
        if op in ("thickness", "draft", "datum_plane", "mirror", "pattern")
        else None
    )
    if op in _CUT_TYPES:
        message = _cut_without_material(feat) or _cut_removed_nothing(feat)
        if message:
            warnings.append(message)
    if op in ("pad", "pocket"):
        length = spec.get("length")
        if isinstance(length, (int, float)) and length < 0:
            warnings.append(
                f"{feat.Name} length {length} is negative: FreeCAD reads that as the "
                "opposite direction (it is not an error), and the geometry went that way."
            )
    if op in ("pad", "revolution"):
        message = _add_added_nothing(feat)
        if message:
            warnings.append(message)
    if op == "pad":
        message = _pad_created_disconnected_solid(feat)
        if message:
            warnings.append(message)
    if op == "thickness":
        for helper in (_thickness_changed_nothing, _thickness_grew_outward):
            message = helper(feat)
            if message:
                warnings.append(message)
    if op == "move" and spec.get("base") and feat is not None and spec["base"] != feat.Name:
        # The move went to the owning Body (a feature's Placement is reset by
        # every recompute) — say so, or the redirect looks like the wrong
        # object was moved.
        return {
            "moved_object": feat.Name,
            "note": (
                f"'{spec['base']}' is a PartDesign feature: FreeCAD resets a feature's "
                f"Placement on the next recompute, so the move was applied to its Body "
                f"'{feat.Name}'."
            ),
        }
    if op not in ("sketch", "sketch_edit"):
        out = {"warnings": warnings} if warnings else {}
        if resolved:
            # Which faces the selector resolved to (center + normal): a
            # direction token picks the farthest face facing that way, so a
            # surprise pick must be visible in the reply.
            out["resolved"] = resolved
        return out
    info = sketcher_ops.pop_last_sketch_info() or {}
    out = {k: v for k, v in info.items() if k not in ("object_name", "warnings")}
    warnings.extend(info.get("warnings") or [])
    if info.get("fully_constrained") is False:
        warnings.append(
            f"sketch '{feat.Name}' is under-constrained (DoF={info.get('dof')}); "
            "add dimensional/geometric constraints to fully define it."
        )
    if warnings:
        out["warnings"] = warnings
    return out


_BUILDERS = {
    "boolean": _build_boolean,
    "fillet": lambda doc, spec: _build_fillet_chamfer(doc, spec, "fillet"),
    "chamfer": lambda doc, spec: _build_fillet_chamfer(doc, spec, "chamfer"),
    "loft": _build_loft,
    "sweep": _build_sweep,
    "mirror": _build_mirror,
    "pattern": _build_pattern,
    "move": _build_move,
    "color": _build_color,
    "variables": _build_variables,
    "sketch": _build_sketch,
    "sketch_edit": _build_sketch_edit,
    "pad": _build_pad,
    "pocket": _build_pocket,
    "revolution": _build_revolution,
    "groove": _build_groove,
    "thickness": _build_thickness,
    "draft": _build_draft,
    "datum_plane": _build_datum_plane,
    "hull": _build_hull,
    "hole": _build_hole,
}


def _advance_body_tip(feat) -> bool:
    """Make ``feat`` its Body's Tip. Returns True when the tip actually moved.

    FreeCAD advances Body.Tip by itself for additive/subtractive features, but
    NOT for a PartDesign transform. Measured on 1.1.4: a polar pattern built from
    a flange pocket was correct on its own (its Shape was the 6-hole result,
    15246 mm^3) while Body.Tip stayed on the single-hole pocket, so the Body kept
    ONE hole (15874 mm^3) and the op reported success.

    Only a successor of the current tip may claim it: Body.Tip is what the body
    SHOWS, so a dress-up on a feature in the middle of the chain taking the tip
    would silently hide every later feature. ``OutList`` — FreeCAD's own "what do
    I link to" — is the honest test for "the tip is my predecessor". A body-less
    feature (a Part-level build) is left alone.
    """
    type_id = getattr(feat, "TypeId", "")
    if not tip_policy.advances_tip(type_id):
        return False
    body = _parent_body(feat)
    if body is None:
        return False
    tip = getattr(body, "Tip", None)
    depends_on_tip = tip is not None and any(o is tip for o in getattr(feat, "OutList", []))
    if not tip_policy.should_advance(
        type_id,
        body_has_tip=tip is not None,
        tip_is_feat=tip is feat,
        feat_depends_on_tip=depends_on_tip,
    ):
        return False
    try:
        body.Tip = feat
    except Exception as exc:
        # A stale tip merely hides the feature; an exception here would discard
        # an otherwise valid build, so warn instead of failing the op.
        FreeCAD.Console.PrintWarning(
            f"CADPilot: could not set {body.Name}.Tip = {feat.Name}: {exc}\n"
        )
        return False
    return True


def _status_string(feat) -> str:
    """FreeCAD's own failure reason for a feature, '' when it has none.

    1.1.4 exposes this as the METHOD ``getStatusString()``; the old
    ``getattr(feat, "StatusString", "")`` silently returned nothing (the
    property does not exist), so every recompute failure lost its reason —
    "Revolve axis intersects the sketch" reached the caller as a bare
    "check parameters/geometry".
    """
    for getter in (
        lambda: feat.getStatusString(),
        lambda: getattr(feat, "StatusString", ""),
    ):
        try:
            status = str(getter() or "").strip()
        except Exception:
            continue
        if status and status != "Invalid":
            return status
    return ""


#: Context of the LAST dress-up (fillet/chamfer) built, for the failure
#: diagnosis below: the kind, the size, the resolved edge names and a probe that
#: re-points the feature at a SUBSET of them and reports whether the result has
#: a usable shape (:func:`_dress_failure_detail`).
_DRESS_CONTEXT: dict[str, Any] = {}


def _remember_dress_context(kind, size, names, probe) -> None:
    _DRESS_CONTEXT.clear()
    _DRESS_CONTEXT.update({"kind": kind, "size": size, "names": list(names), "probe": probe})


def _settle_shape_caches(doc, spec) -> None:
    """Materialise the Shape of every object a feature is about to be built on.

    FreeCAD builds a PartDesign shape LAZILY, and a feature whose predecessor's
    Shape has not been read computes to a Touched/Null shape with no error.
    Live-verified on 1.1.4: after reopening a saved model, re-running an
    identical pocket (the same params that had built fine) produced NO shape at
    all, and reading the SKETCH's Shape first made the same build return exactly
    the analytic result (58800 -> 57207.2 mm^3, one through hole). A recompute is
    not enough; the READ is what performs the build, so the op performs it here.
    """
    if not isinstance(spec, dict):
        return
    targets = []
    for value in (spec.get("base"), spec.get("profile"), spec.get("path"), spec.get("tool")):
        if isinstance(value, str) and value:
            with contextlib.suppress(Exception):
                targets.append(_get_obj(doc, value, "link"))
    profiles = spec.get("profiles")
    if isinstance(profiles, list):
        for value in profiles:
            if isinstance(value, str) and value:
                with contextlib.suppress(Exception):
                    targets.append(_get_obj(doc, value, "profile"))
    body = _parent_body(targets[0]) if targets else None
    tip = getattr(body, "Tip", None) if body is not None else None
    if tip is not None:
        targets.append(tip)  # the feature's real baseline inside a Body
    for obj in targets:
        with contextlib.suppress(Exception):
            _ = obj.Shape
    with contextlib.suppress(Exception):
        doc.recompute()


def _read_shape_state(feat, doc):
    """(shape, valid, volume, solids, error) for a freshly built feature.

    Retried ONCE on failure, because a PartDesign shape builds LAZILY: the first
    access after a recompute can raise FreeCAD's bare "shape is invalid" for a
    shape that reads perfectly well on the second attempt. Measured on 1.1.4 —
    an ordinary 6-rim fillet reported exactly those two words as its whole
    failure, the same call succeeded on the next try with identical inputs, and
    every single rim was valid; the retry is the difference. The volume/solids
    probes belong INSIDE this guard for the same reason (they raise first).
    """
    shape, valid, volume, solids, error = None, False, 0.0, 0, ""
    for attempt in (1, 2):
        try:
            shape = getattr(feat, "Shape", None)
            valid = True if shape is None or shape.isNull() else shape.isValid()
            volume = float(getattr(shape, "Volume", 0.0) or 0.0) if shape is not None else 0.0
            solids = len(getattr(shape, "Solids", []) or []) if shape is not None else 0
            return shape, valid, volume, solids, ""
        except Exception as e:
            error = f"the validity check could not run ({type(e).__name__}: {e})"
            shape, valid, volume, solids = None, False, 0.0, 0
            if attempt == 1:
                with contextlib.suppress(Exception):
                    doc.recompute()
    return shape, valid, volume, solids, error


def _dress_shape_usable(feat) -> bool:
    """Does a dress-up probe have a readable, non-null Shape?

    Null/raising IS the failure being diagnosed; OCC's ``isValid`` is
    deliberately not consulted (it is a false negative on ordinary PartDesign
    fillets — see _SHAPE_CHECK_NOTE). The read is retried once, like every other
    shape read (see _read_shape_state).
    """
    _shape, _valid, _volume, _solids, error = _read_shape_state(feat, feat.Document)
    return not error and _shape is not None and not _shape.isNull()


def _dress_retry_repair() -> bool:
    """Re-apply the SAME edge set once and report whether it now works.

    A dress-up that comes out with a Null Shape is not always a geometry
    verdict: live on 1.1.4, the six bore rims of a patterned plate failed on one
    attempt and produced exactly the 6x roundover (36778.9 mm^3 = 36787.3 -
    6 x 1.393) on the next, with identical inputs — a stale recompute in the
    document's graph is enough. Failing a caller's step over that is wrong, so
    the op retries in place (the Base assignment is idempotent) and only reports
    a failure when the retry fails too.
    """
    probe = _DRESS_CONTEXT.get("probe")
    names = list(_DRESS_CONTEXT.get("names") or [])
    if not callable(probe) or not names:
        return False
    try:
        return bool(probe(names))
    except Exception:
        return False


def _dress_failure_detail(op: str = "") -> str:
    """Test a failed fillet/chamfer's edges ONE at a time and say what happens.

    A multi-edge dress-up that OCC refuses reports nothing usable — live on
    1.1.4, a 0.5 mm fillet on the six bore rims of a patterned plate failed with
    a bare "shape is invalid", while every single rim was fine on its own (and
    so was the top rims two at a time: only the bottom rims fail as a SET).
    "Invalid" cannot be acted on; "split these edges across several fillet
    calls" can. The retries run inside the op's transaction, which aborts when
    the op raises, so the half-built feature is never left behind.
    """
    ctx = dict(_DRESS_CONTEXT)
    _DRESS_CONTEXT.clear()
    if op not in ("fillet", "chamfer"):
        # A stale context from an EARLIER dress-up must never decorate an
        # unrelated failure: live-caught, a pocket that could not compute
        # reported "every one of the 4 edges fails at 4.0 mm on its own".
        return ""
    probe = ctx.get("probe")
    names = list(ctx.get("names") or [])
    kind, size = ctx.get("kind"), ctx.get("size")
    if not callable(probe) or len(names) < 2 or len(names) > 12:
        return ""  # one recompute per edge: not worth it on a large edge set
    bad: list[str] = []
    for name in names:
        try:
            ok = probe([name])
        except Exception:
            ok = False
        if not ok:
            bad.append(name)
    with contextlib.suppress(Exception):
        probe(names)  # hand the full set back, so the caller sees its own failure
    if not bad:
        return (
            f" Each of the {len(names)} edges {kind}s fine on its own (retried one at a "
            f"time), so OCC refuses the SET: split the edges across several {kind} calls, or "
            f"lower the {kind} size."
        )
    if len(bad) == len(names):
        return (
            f" Every one of the {len(names)} edges fails at {size} mm on its own too (retried "
            f"one at a time), so this is not a combination problem: try a smaller {kind} size, "
            "or check the geometry around those edges with Part > Check geometry."
        )
    return (
        f" {len(bad)} of the {len(names)} edge(s) fail on their own too ({', '.join(bad)}); the "
        f"other {len(names) - len(bad)} succeed alone, so the set fails in combination. "
        f"{kind.capitalize()} the failing edges in a separate call."
    )


def _invalid_shape_error(feat_type, feat, detail: str | None = None) -> str:
    """ "<op> produced an invalid Shape" was raised for a self-intersecting
    profile, a dangling reference and a corrupted dependency graph alike — three
    different causes behind one opaque message. Name the object and carry
    FreeCAD's own reason.

    ``detail`` overrides the reason read off the feature: when the validity
    CHECK itself failed there is no status to quote, and quoting FreeCAD's
    (perfectly healthy) "Valid" produced the self-contradictory "produced an
    invalid Shape — FreeCAD says: Valid" (live: a fillet whose BRepCheck threw).
    """
    status = detail or _status_string(feat)
    reason = f" — FreeCAD says: {status}" if status else ""
    return (
        f"{feat_type} '{feat.Name}' produced an invalid Shape{reason}. "
        "Typical causes: the profile/section is not a closed face or wire, the "
        "profile self-intersects, or it references a deleted object. Select "
        f"'{feat.Name}' in FreeCAD and use Part > Check geometry for the OCC fault."
    )


# A feature whose recompute SUCCEEDED, whose FreeCAD status is "Valid" and whose
# shape measures correctly, but whose OCC BRepCheck reports it invalid. That
# combination is a FALSE NEGATIVE of isValid() — refused live for a legitimate
# 2 mm PartDesign fillet on a patterned flange (volume exactly the analytic
# roundover, status Valid, isValid() False) — so it is downgraded from a hard
# failure to a warning the caller can see. Read once, by describe_feature.
_SHAPE_CHECK_NOTE = ""


# What the op's recompute did to the model (geometry_query.recompute_report):
# new breakage, stale bodies and the Body volume before/after. Read once, by
# describe_feature_reply.
_MODEL_REPORT: dict = {}


def _take_shape_check_note() -> str:
    global _SHAPE_CHECK_NOTE
    note, _SHAPE_CHECK_NOTE = _SHAPE_CHECK_NOTE, ""
    return note


def _dress_repaired_note(ftype: str, feat) -> str:
    """A dress-up whose first recompute failed and whose retry succeeded.

    Kept rather than raised: the inputs did not change, so the failure was a
    stale recompute, and failing the caller's step over it loses real work.
    """
    return (
        f"{ftype} '{feat.Name}' came out invalid on the first recompute; re-applying the "
        "same edges produced a valid shape, so the step was kept (a stale recompute can do "
        "this — no parameter changed)."
    )


def create_feature_gui(doc, spec):
    """Create one parametric feature from ``spec``; returns the object.

    Raises ValueError/RuntimeError on any failure. Caller wraps this in a
    transaction, so raising means the document stays untouched.
    """
    ftype = spec.get("type")
    builder = _BUILDERS.get(ftype)
    if builder is None:
        raise ValueError(f"unknown feature type {ftype!r}; supported: {', '.join(FEATURE_TYPES)}")
    problem = tip_policy.unknown_spec_keys(ftype, spec)
    if problem:
        raise ValueError(problem)
    # Notes are per-build: a replay (step_engine) also calls this, and a note it
    # leaves behind must never be reported as the NEXT caller's outcome.
    global _AUTO_DIRECTION_NOTE, _LAST_FEATURE_INFO, _EDGE_SELECTION
    _AUTO_DIRECTION_NOTE = ""
    _LAST_FEATURE_INFO = None
    _EDGE_SELECTION = None
    _UNREFINED.clear()
    # Read the Shape of what this feature is built on FIRST: a lazy predecessor
    # makes the new feature compute to nothing, silently (see the helper).
    _settle_shape_caches(doc, spec)
    global _MODEL_REPORT
    _MODEL_REPORT = {}
    if ftype != "color":
        issues_before = model_issues(doc)
        volumes_before = body_volumes(doc)
    feat = builder(doc, spec)
    # "move" directly modifies Placement and "color" only the ViewObject —
    # neither builds a feature, so the recompute/validity/volume checks below do
    # not apply. They must not either: a color op has no business failing
    # because the object it decorates is a half-built or invalid leftover.
    if ftype in ("move", "color"):
        if ftype == "move":
            _MODEL_REPORT = recompute_report(doc, issues_before, volumes_before)
        return feat
    doc.recompute()
    if _advance_body_tip(feat):
        doc.recompute()
    # A tip advance rebuilds the Body's display node with FreeCAD's DEFAULT
    # material, so a colour set earlier visually vanishes (see
    # _refresh_appearance). Refresh BEFORE the caller reads anything back.
    body = _parent_body(feat)
    if body is not None and getattr(body, "Tip", None) is feat:
        _refresh_appearance(body)
    state = [str(s) for s in getattr(feat, "State", [])]
    # A dress-up can come out Invalid/Null from a STALE recompute alone (see
    # _dress_retry_repair): re-apply the same edges once before believing it.
    dress = ftype in ("fillet", "chamfer")
    repaired = False
    if "Invalid" in state and dress and _dress_retry_repair():
        repaired = True
        state = [str(s) for s in getattr(feat, "State", [])]
    if "Invalid" in state:
        # FreeCAD's actual failure reason (bad support, missing subelement,
        # "Revolve axis intersects the sketch", …) — "check parameters/geometry"
        # alone sends the caller hunting blind. 1.1.4 exposes it as the METHOD
        # getStatusString(); the old property read returned nothing, so every
        # failure lost its reason.
        status = _status_string(feat)
        detail = f" — {status}" if status and status != "Invalid" else ""
        hint = _FAILURE_HINTS.get(ftype, "check parameters/geometry.")
        raise RuntimeError(
            f"{ftype} failed to recompute{detail} ({hint}){_dress_failure_detail(ftype)}"
        )
    global _SHAPE_CHECK_NOTE
    _SHAPE_CHECK_NOTE = _dress_repaired_note(ftype, feat) if repaired else ""
    shape = None
    check_error = ""
    # The read is retried internally (see _read_shape_state): a lazily built
    # PartDesign shape raises FreeCAD's bare "shape is invalid" on its FIRST
    # access and reads fine on the second.
    shape, valid, volume, solids, check_error = _read_shape_state(feat, doc)
    if not valid and not repaired:
        # A feature that reads as unbuildable may simply have been built on a
        # lazy predecessor: settle those caches and read again before giving up.
        _settle_shape_caches(doc, spec)
        shape, valid, volume, solids, check_error = _read_shape_state(feat, doc)
    if not valid and _unrefine_if_broken(doc, feat, spec):
        shape, valid, volume, solids, check_error = _read_shape_state(feat, doc)
    if not valid and dress and not repaired and _dress_retry_repair():
        valid = True
        check_error = ""
        shape, valid, volume, solids, _err = _read_shape_state(feat, doc)
        _SHAPE_CHECK_NOTE = _dress_repaired_note(ftype, feat)
    if not valid:
        status = _status_string(feat)
        if check_error or (status and status != "Valid"):
            raise RuntimeError(
                _invalid_shape_error(ftype, feat, check_error or status)
                + _dress_failure_detail(ftype)
            )
        # FreeCAD says Valid, the shape is non-null, the recompute committed and
        # the volume is sane: OCC's BRepCheck disagrees, which it does for good
        # PartDesign fillets (see _SHAPE_CHECK_NOTE). Advisory, not fatal — a
        # false negative that hard-fails a correct model is worse than a warning.
        volume_note = ""
        with contextlib.suppress(Exception):
            volume_note = (
                f" — it holds {len(shape.Solids)} solid(s), volume {float(shape.Volume):.1f} mm^3"
                if shape is not None
                else ""
            )
        _SHAPE_CHECK_NOTE = (
            f"{ftype} '{feat.Name}' recomputed and FreeCAD reports it Valid, but OCC's "
            f"own validity check (Shape.isValid) says otherwise{volume_note}. The "
            "geometry was kept, since that combination is usually a false negative "
            "(it happens on ordinary PartDesign fillets). Verify with Part > Check "
            "geometry if this part matters."
        )
    if feat.Name in _UNREFINED:
        _SHAPE_CHECK_NOTE = (
            f"Refine was turned off on '{feat.Name}': refining its result gave a shape that "
            "fails OCC's validity check, and the unrefined one is valid. Pass refine=false "
            "on the features after it too, or each one is retried the same way."
        )
    # A negative volume is never a legitimate solid: OCC returns one for a
    # self-intersecting result (a profile straddling its revolve axis is the
    # classic case) and FreeCAD still calls the feature valid — the caller
    # would get silent garbage. Only shapes that actually HOLD solids are
    # judged: a PartDesign::Plane's infinite face reports a garbage "volume"
    # whose SIGN follows its offset, so an unscoped check refused every
    # datum_plane with a negative offset (live: offset -5 → "volume -1.3e+98").
    if volume < -1e-6 and solids > 0:
        hint = (
            "the profile probably crosses its revolve axis"
            if ftype in ("revolution", "groove")
            else "check the profile/parameters"
        )
        raise RuntimeError(
            f"{ftype} produced a self-intersecting solid (volume {volume:.1f} mm^3 < 0); {hint}."
        )
    if ftype in ("pad", "revolution"):
        _refuse_shrunk_body(feat)
    # Settle the lazy Shape caches INSIDE this op: a read immediately after the
    # commit (the caller's measure_geometry, the connectivity audit) otherwise
    # saw an interim result — live: a datum-plane pocket reported Body volume
    # 239100.0 for a cut that is exactly 239057.5 once the caches catch up.
    with contextlib.suppress(Exception):
        _ = feat.Shape
        body = _parent_body(feat)
        if body is not None:
            _ = body.Shape
    body = _parent_body(feat)
    own = body is not None and tip_policy.advances_tip(getattr(feat, "TypeId", ""))
    _MODEL_REPORT = recompute_report(
        doc, issues_before, volumes_before, skip={feat.Name}, bodies={body.Name} if own else None
    )
    return feat
