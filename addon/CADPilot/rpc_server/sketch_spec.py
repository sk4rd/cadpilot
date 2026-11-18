"""Translation of the ``sketch`` spec into Sketcher constraint arguments.

Pure — no FreeCAD import, so it is unit-tested from ``tests/``. sketcher_ops
turns each planned entry into ``Sketcher.Constraint(*args)`` (plus the datum)
and owns everything that needs a live document.
"""

from __future__ import annotations

import math
import re
from itertools import pairwise
from typing import Any

# spec point keyword -> Sketcher PointPos
POINT_POS = {"start": 1, "end": 2, "center": 3, "mid": 3}
_POS_NAMES = {1: "start", 2: "end", 3: "center"}

# Part geometry class -> the kind that decides which points exist on it
_KIND_OF_CLASS = {
    "LineSegment": "line",
    "Circle": "circle",
    "Ellipse": "circle",
    "ArcOfCircle": "arc",
    "ArcOfEllipse": "arc",
    "ArcOfHyperbola": "arc",
    "ArcOfParabola": "arc",
    "Point": "point",
    "BSplineCurve": "bspline",
}

# The points each kind exposes. A circle has no start/end and a line no
# centre: both pass addConstraint and fail at solve with MalformedConstraints.
_KIND_POINTS = {
    "line": {"start": 1, "end": 2},
    "arc": {"start": 1, "end": 2, "center": 3, "mid": 3},
    "circle": {"center": 3, "mid": 3},
    "point": {"start": 1, "end": 1, "center": 1, "mid": 1},
    "bspline": {"start": 1, "end": 2},
    # The X axis's start is the sketch origin. Its "end" is a unit point at
    # (1, 0) and the Y axis's start duplicates the origin: neither is meant.
    "x_axis": {"start": 1, "center": 1},
    "y_axis": {},
}

_POINT_HINTS = {
    "line": "a line has no midpoint vertex: a coincident with [g, 'mid'] places the "
    "point there for you (as a symmetric constraint), anywhere else use "
    "symmetric [[g, 'start'], [g, 'end'], [point]]",
    "circle": "a full circle has only its 'center'",
    "x_axis": "the X axis (-1) has one point, the origin: [-1, 'start']",
    "y_axis": "the Y axis (-2) has no point of its own; the origin is [-1, 'start']",
}


def geometry_kinds(internal: list[str], external: list[str] = ()) -> dict[int, str]:
    """GeoId -> kind, from the Part class names of the sketch's geometry.

    Internal geometry is numbered from 0, the axes are -1 (X) and -2 (Y), and
    the i-th external geometry is -(3 + i).
    """
    kinds = {i: _KIND_OF_CLASS.get(name, "other") for i, name in enumerate(internal)}
    kinds[-1], kinds[-2] = "x_axis", "y_axis"
    for i, name in enumerate(external):
        kinds[-(3 + i)] = _KIND_OF_CLASS.get(name, "other")
    return kinds


def _known_ids(kinds: dict) -> str:
    internal = [g for g in kinds if g >= 0]
    external = [g for g in kinds if g <= -3]
    parts = [f"geometry 0..{max(internal)}" if internal else "no geometry"]
    parts.append("the axes -1 (X) and -2 (Y)")
    if external:
        parts.append(f"external {-3}..{min(external)}")
    return ", ".join(parts)


def _check_geo(geo_id: int, kinds: dict | None) -> None:
    if kinds is not None and geo_id not in kinds:
        raise ValueError(f"GeoId {geo_id} does not exist; this sketch has {_known_ids(kinds)}")


DIMENSIONAL = ("distance", "distance_x", "distance_y", "radius", "diameter", "angle")

CONSTRAINT_TYPES = (
    "coincident",
    "horizontal",
    "vertical",
    "tangent",
    "perpendicular",
    "parallel",
    "equal",
    "symmetric",
    "point_on_object",
    "block",
    "distance",
    "distance_x",
    "distance_y",
    "radius",
    "diameter",
    "angle",
)


def point_ref(item: Any, kinds: dict | None = None) -> tuple[int, int]:
    """[geo_id, "start"|"end"|"center"|"mid"] -> (geo_id, PointPos).

    With ``kinds`` (see :func:`geometry_kinds`) the GeoId must exist and the
    keyword must name a point that kind of geometry has. The origin is
    [-1, "start"] (or [-1, "center"]).
    """
    if not (isinstance(item, (list, tuple)) and len(item) == 2):
        raise ValueError(f"a point must be [geo_id, point], got {item!r}")
    geo_id, pos = int(item[0]), item[1]
    # FreeCAD's own PointPos numbers are accepted for the keywords.
    pos = _POS_NAMES.get(pos, pos) if type(pos) is int else str(pos).lower()
    if pos not in POINT_POS:
        raise ValueError(f"point must be one of {sorted(POINT_POS)}, got {item!r}")
    _check_geo(geo_id, kinds)
    kind = (kinds or {}).get(geo_id) or {-1: "x_axis", -2: "y_axis"}.get(geo_id)
    points = _KIND_POINTS.get(kind, POINT_POS)
    if pos not in points:
        hint = _POINT_HINTS.get(kind) or f"a {kind} has {sorted(points)}"
        raise ValueError(f"{list(item)!r}: {hint}")
    return geo_id, points[pos]


def geo_ref(item: Any, kinds: dict | None = None) -> int:
    """A whole-geometry reference: ``[geo_id]`` or a bare ``geo_id``.

    A caller writing a constraint by hand naturally writes ``"items": [0]``
    (the geometry INDEX), and a 2-element item is a POINT reference elsewhere,
    so a bare int can never be ambiguous here.
    """
    if isinstance(item, int):
        geo_id = item
    elif isinstance(item, (list, tuple)) and len(item) == 1:
        geo_id = int(item[0])
    else:
        raise ValueError(f"an edge must be a geo_id or [geo_id] (the geometry index), got {item!r}")
    _check_geo(geo_id, kinds)
    return geo_id


def is_point_item(item: Any) -> bool:
    return isinstance(item, (list, tuple)) and len(item) == 2


def _count(items: list, low: int, high: int | None, shape: str) -> None:
    """Refuse an item count the constraint cannot use, instead of dropping extras."""
    n = len(items)
    if n < low or (high is not None and n > high):
        raise ValueError(f"takes {shape}, got {n} item(s)")


def _line_mid(item: Any, kinds: dict | None) -> bool:
    return (
        kinds is not None
        and is_point_item(item)
        and kinds.get(int(item[0])) == "line"
        and str(item[1]).lower() in ("mid", "center")
    )


def _coincident(a: Any, b: Any, kinds: dict | None) -> tuple:
    """Two points made one; a line's midpoint becomes Symmetric(start, end, point)."""
    mids = [i for i in (a, b) if _line_mid(i, kinds)]
    if len(mids) == 2:
        raise ValueError(
            "two line midpoints cannot meet in one constraint; put a construction point "
            "at one midpoint and make the other symmetric about it"
        )
    if mids:
        line = int(mids[0][0])
        _check_geo(line, kinds)
        other = b if mids[0] is a else a
        return ("Symmetric", line, 1, line, 2, *point_ref(other, kinds))
    return ("Coincident", *point_ref(a, kinds), *point_ref(b, kinds))


def constraint_args(ctype: str, items: list, kinds: dict | None = None) -> list[tuple]:
    """spec constraint -> one Sketcher.Constraint args tuple per constraint.

    A list of edges or points expands instead of being cut to the first one or
    two: ``horizontal [0, 2]`` is two constraints, ``equal [0, 1, 2]`` the chain
    0=1, 1=2, and ``radius [0, 1, 2]`` one radius on 0 with 1 and 2 equal to it,
    so a single named dimension drives them all. ``point_on_object`` puts every
    point it lists on its last item.
    """

    def pt(item):
        return point_ref(item, kinds)

    def geo(item):
        return geo_ref(item, kinds)

    if ctype == "coincident":
        _count(items, 2, None, "two or more points [g, p]")
        return [_coincident(a, b, kinds) for a, b in pairwise(items)]
    if ctype in ("horizontal", "vertical"):
        if len(items) == 2 and all(is_point_item(i) for i in items):
            g1, p1 = pt(items[0])
            g2, p2 = pt(items[1])
            return [(ctype.capitalize(), g1, p1, g2, p2)]
        return [(ctype.capitalize(), geo(item)) for item in items]
    if ctype in ("tangent", "perpendicular"):
        fc_type = ctype.capitalize()
        _count(items, 2, 3, "two edges, two points, a point and an edge, or two edges and a point")
        if len(items) == 3:
            # Two curves meeting at a point that lies on both (TangentViaPoint).
            g, p = pt(items[2])
            return [(f"{fc_type}ViaPoint", geo(items[0]), geo(items[1]), g, p)]
        a, b = items
        if is_point_item(a) and is_point_item(b):
            g1, p1 = pt(a)
            g2, p2 = pt(b)
            return [(fc_type, g1, p1, g2, p2)]
        if is_point_item(a) or is_point_item(b):
            point, edge = (a, b) if is_point_item(a) else (b, a)
            g, p = pt(point)
            return [(fc_type, g, p, geo(edge))]
        return [(fc_type, geo(a), geo(b))]
    if ctype in ("parallel", "equal"):
        _count(items, 2, None, "two or more edges")
        return [(ctype.capitalize(), geo(a), geo(b)) for a, b in pairwise(items)]
    if ctype == "symmetric":
        _count(items, 3, 3, "two points and a line or a point")
        g1, p1 = pt(items[0])
        g2, p2 = pt(items[1])
        third = items[2]
        if is_point_item(third):  # symmetry about a point
            g3, p3 = pt(third)
            return [("Symmetric", g1, p1, g2, p2, g3, p3)]
        return [("Symmetric", g1, p1, g2, p2, geo(third))]  # about a line
    if ctype == "point_on_object":
        _count(items, 2, None, "one or more points [g, p] followed by the edge they lie on")
        edge = geo(items[-1])
        return [("PointOnObject", *pt(point), edge) for point in items[:-1]]
    if ctype == "block":
        return [("Block", geo(item)) for item in items]
    if ctype == "distance":
        _count(items, 1, 2, "one edge, two points, or a point and an edge")
        if len(items) == 1:
            return [("Distance", geo(items[0]))]
        a, b = items[0], items[1]
        if is_point_item(a) and is_point_item(b):
            g1, p1 = pt(a)
            g2, p2 = pt(b)
            return [("Distance", g1, p1, g2, p2)]
        if is_point_item(a):
            g1, p1 = pt(a)
            return [("Distance", g1, p1, geo(b))]
        g2, p2 = pt(b)
        return [("Distance", g2, p2, geo(a))]
    if ctype in ("distance_x", "distance_y"):
        _count(items, 1, 2, "one point, one edge, or two points")
        fc_type = "DistanceX" if ctype == "distance_x" else "DistanceY"
        if len(items) == 1:
            # ONE item is either a point (distance from the origin) or a whole
            # geometry (FreeCAD's edge form: the x/y extent of a line).
            if is_point_item(items[0]):
                g, p = pt(items[0])
                return [(fc_type, g, p)]
            return [(fc_type, geo(items[0]))]
        g1, p1 = pt(items[0])
        g2, p2 = pt(items[1])
        return [(fc_type, g1, p1, g2, p2)]
    if ctype in ("radius", "diameter"):
        first = geo(items[0])
        return [(ctype.capitalize(), first)] + [("Equal", first, geo(i)) for i in items[1:]]
    if ctype == "angle":
        _count(items, 1, 2, "one or two edges")
        if len(items) == 1:
            return [("Angle", geo(items[0]))]
        return [("Angle", geo(items[0]), geo(items[1]))]
    raise ValueError(f"unknown constraint type {ctype!r}; supported: {', '.join(CONSTRAINT_TYPES)}")


# Sketcher constraint types that carry a datum
_DATUM_TYPES = frozenset({"Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"})


_UNITS = {
    "length": {"mm": 1.0, "cm": 10.0, "m": 1000.0, "um": 0.001, "µm": 0.001, "in": 25.4},
    "angle": {"deg": 1.0, "°": 1.0, "rad": 180.0 / math.pi},
}
_QUANTITY = re.compile(r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*(\S*)\s*")


def parse_quantity(value: Any, dimension: str = "length") -> float:
    """A number or a unit string ("10 mm", "1.5cm", "45 deg", "45°") -> mm or degrees."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{value!r} is not a number")
    if not isinstance(value, str):
        return float(value)
    match = _QUANTITY.fullmatch(value)
    if match is None:
        raise ValueError(f"{value!r} is not a number or a quantity like '10 mm'")
    number, unit = float(match[1]), match[2]
    if not unit:
        return number
    units = _UNITS[dimension]
    factor = units.get(unit) or units.get(unit.lower())
    if factor is None:
        raise ValueError(f"{value!r}: {unit!r} is not a {dimension} unit ({', '.join(units)})")
    return number * factor


def offset_value(value: Any) -> tuple[float, str | None]:
    """An attachment offset: (mm, None), or (0.0, expression) for "=expr"."""
    if value is None or value == "":
        return 0.0, None
    if isinstance(value, str) and value.strip().startswith("="):
        expr = value.strip()[1:].strip()
        if not expr:
            raise ValueError(f"{value!r}: '=' needs an expression after it")
        return 0.0, expr
    return parse_quantity(value, "length"), None


def datum(ctype: str, value: Any) -> tuple[float, str | None]:
    """A dimensional constraint's (datum, expression).

    The datum is in Sketcher's internal units (mm, radians); the spec gives
    lengths in mm and angles in degrees unless the value names its unit. An
    ``"=expr"`` value yields a placeholder datum that the expression replaces
    on the next recompute.
    """
    if isinstance(value, str) and value.strip().startswith("="):
        expr = value.strip()[1:].strip()
        if not expr:
            raise ValueError(f"{value!r}: '=' needs an expression after it")
        return 1.0, expr
    if ctype == "angle":
        return math.radians(parse_quantity(value, "angle")), None
    number = parse_quantity(value, "length")
    if ctype in ("radius", "diameter") and number <= 0:
        raise ValueError(f"{value!r}: a {ctype} must be positive")
    return number, None


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_name(name: Any) -> str:
    """A constraint name usable in an expression (``Sketch.Constraints.<name>``)."""
    text = str(name)
    if not _NAME.fullmatch(text):
        raise ValueError(
            f"name {text!r} must be an identifier (letters, digits, '_'; not starting with a "
            "digit) so expressions can reference it as <Sketch>.Constraints.<name>"
        )
    return text


def plan_constraints(
    constraints: list | None, kinds: dict | None = None, taken: dict | None = None
) -> list[dict]:
    """The spec's constraint list -> one planned entry per Sketcher constraint.

    An entry can expand into several (see :func:`constraint_args`); each keeps
    the spec index it came from, so the solver's complaints map back to it.

    Each entry: ``args`` (Sketcher.Constraint positional args without the
    datum), ``datum`` (None for a geometric constraint), ``expr`` (to bind
    after adding), ``name``, and ``entry``/``type`` (the spec index and type it
    came from).
    ``kinds`` (see :func:`geometry_kinds`) validates every GeoId and point;
    ``taken`` maps names already in the sketch to where they are, so a
    duplicate is refused before FreeCAD's bare "Duplicate constraint".
    Raises ValueError naming the offending entry.
    """
    planned = []
    names = dict(taken or {})
    for i, c in enumerate(constraints or []):
        if not isinstance(c, dict):
            raise ValueError(f"constraints[{i}] must be a dict, got {c!r}")
        ctype = c.get("type")
        where = f"constraints[{i}] ({ctype})"
        items = c.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"{where}: 'items' must be a non-empty list.")
        try:
            all_args = constraint_args(ctype, items, kinds)
        except (IndexError, TypeError) as e:
            raise ValueError(f"{where}: malformed items: {e}") from e
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from e

        value = c.get("value")
        if ctype in DIMENSIONAL:
            if value is None:
                raise ValueError(f"{where}: dimensional constraint requires 'value'.")
            try:
                value_datum, expr = datum(ctype, value)
            except ValueError as e:
                raise ValueError(f"{where}: value {e}") from e
        else:
            if value is not None:
                raise ValueError(f"{where}: geometric constraint takes no 'value'.")
            value_datum, expr = None, None
        name = None
        if c.get("name") not in (None, ""):
            try:
                name = check_name(c["name"])
            except ValueError as e:
                raise ValueError(f"{where}: {e}") from e
            if name in names:
                raise ValueError(f"{where}: name {name!r} is already used by {names[name]}")
            names[name] = f"constraints[{i}]"
        if name and ctype not in DIMENSIONAL and len(all_args) > 1:
            raise ValueError(
                f"{where}: one name cannot label the {len(all_args)} constraints these items "
                "expand to; split the entry or drop the name."
            )
        for args in all_args:
            dimensional = args[0] in _DATUM_TYPES
            planned.append(
                {
                    "args": args,
                    "datum": value_datum if dimensional else None,
                    "expr": expr if dimensional else None,
                    "name": name if dimensional or ctype not in DIMENSIONAL else None,
                    "entry": i,
                    "type": ctype,
                }
            )
    return planned


def unnamed_dimensions(planned: list[dict]) -> list[str]:
    """Spec entries whose dimension has no name, so a user cannot find it by name."""
    out = []
    for plan in planned:
        if plan["datum"] is not None and not plan["name"]:
            label = f"constraints[{plan['entry']}] ({plan['type']})"
            if label not in out:
                out.append(label)
    return out


def origin_of(plan: dict) -> dict:
    """What a planned constraint reports when the solver names it."""
    out = {"entry": f"{plan.get('source', 'constraints')}[{plan['entry']}]", "type": plan["type"]}
    if plan.get("name"):
        out["name"] = plan["name"]
    return out


# FreeCAD's solver lists -> reply keys
DIAGNOSTIC_KEYS = {
    "ConflictingConstraints": "conflicting",
    "RedundantConstraints": "redundant",
    "PartiallyRedundantConstraints": "partially_redundant",
    "MalformedConstraints": "malformed",
}

SOLVE_RESULTS = {
    -1: "the solver did not converge",
    -2: "redundant constraints",
    -3: "conflicting constraints",
    -5: "malformed constraints",
}


def map_diagnostics(raw: dict, origins: list) -> dict:
    """FreeCAD's solver lists -> one entry per constraint, naming where it came from.

    The lists number constraints from 1, the way FreeCAD's own messages do,
    while ``Constraints`` is indexed from 0: ``ConflictingConstraints == [2, 3]``
    means ``Constraints[1]`` and ``Constraints[2]``. ``origins[i]`` describes
    live constraint ``i`` (see :func:`origin_of`); a constraint the caller did
    not add in this call gets an entry with only its index.
    """
    out = {}
    for key, numbers in raw.items():
        entries = []
        for n in numbers:
            index = int(n) - 1
            origin = origins[index] if 0 <= index < len(origins) else None
            entries.append({"constraint": index, **(origin or {})})
        if entries:
            out[DIAGNOSTIC_KEYS.get(key, key)] = entries
    return out


def describe_entry(entry: dict) -> str:
    """One diagnostics entry as text: ``constraints[3] (distance 'Width')``."""
    where = entry.get("entry") or f"Constraints[{entry['constraint']}]"
    name = f" '{entry['name']}'" if entry.get("name") else ""
    kind = entry.get("type")
    return f"{where} ({kind}{name})" if kind else f"{where}{name}"


def diagnostics_text(mapped: dict) -> str:
    return "; ".join(
        f"{key}: {', '.join(describe_entry(e) for e in entries)}" for key, entries in mapped.items()
    )


PRIMITIVES = ("line", "arc", "circle", "bspline", "point")
COMPOSITES = ("polyline", "rectangle", "slot")


def _xy(value: Any, what: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{what} must be [x, y], got {value!r}")
    return float(value[0]), float(value[1])


def _line(a: tuple, b: tuple) -> dict:
    if math.dist(a, b) < 1e-9:
        raise ValueError(f"two consecutive points coincide at {list(a)}")
    return {"type": "line", "from": list(a), "to": list(b)}


def _implied(kind: str, entry: int, *args: tuple) -> list[dict]:
    return [
        {
            "args": a,
            "datum": None,
            "expr": None,
            "name": None,
            "entry": entry,
            "type": kind,
            "source": "geometry",
        }
        for a in args
    ]


def _polyline(item: dict) -> tuple[list[dict], list[tuple]]:
    """Lines through ``points``; ``closed`` adds the closing segment."""
    points = item.get("points")
    closed = bool(item.get("closed"))
    if not isinstance(points, list) or len(points) < (3 if closed else 2):
        raise ValueError(f"needs a 'points' list of at least {3 if closed else 2} [x, y]")
    pts = [_xy(p, f"points[{k}]") for k, p in enumerate(points)]
    if closed and math.dist(pts[0], pts[-1]) < 1e-9:
        pts = pts[:-1]  # the caller repeated the first point to close it
    segments = list(pairwise(pts)) + ([(pts[-1], pts[0])] if closed else [])
    lines = [_line(a, b) for a, b in segments]
    joints = [(k, 2, k + 1, 1) for k in range(len(lines) - 1)]
    if closed:
        joints.append((len(lines) - 1, 2, 0, 1))
    return lines, [("Coincident", *j) for j in joints]


def _rectangle(item: dict) -> tuple[list[dict], list[tuple]]:
    """Corners ``from`` and ``to``: bottom, right, top, left as seen from ``from``."""
    x0, y0 = _xy(item.get("from"), "from")
    x1, y1 = _xy(item.get("to"), "to")
    if abs(x1 - x0) < 1e-9 or abs(y1 - y0) < 1e-9:
        raise ValueError("'from' and 'to' are opposite corners and must differ in x and in y")
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    lines = [_line(corners[k], corners[(k + 1) % 4]) for k in range(4)]
    implied = [("Coincident", k, 2, (k + 1) % 4, 1) for k in range(4)]
    implied += [("Horizontal", 0), ("Horizontal", 2), ("Vertical", 1), ("Vertical", 3)]
    return lines, implied


def _slot(item: dict) -> tuple[list[dict], list[tuple]]:
    """A stadium: arcs around ``from`` and ``to`` joined by two tangent lines.

    The order matches FreeCAD's slot tool: arc at ``from``, arc at ``to``,
    then the two lines; an axis-aligned slot is also kept horizontal/vertical.
    """
    c1 = _xy(item.get("from"), "from")
    c2 = _xy(item.get("to"), "to")
    try:
        r = float(item.get("radius"))
    except (TypeError, ValueError):
        raise ValueError(f"needs a numeric 'radius', got {item.get('radius')!r}") from None
    if r <= 0:
        raise ValueError(f"radius must be positive, got {r}")
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    if math.hypot(dx, dy) < 1e-9:
        raise ValueError("'from' and 'to' are the two arc centres and must differ")
    theta = math.degrees(math.atan2(dy, dx))
    nx, ny = -math.sin(math.radians(theta)), math.cos(math.radians(theta))
    out1, in1 = (c1[0] + r * nx, c1[1] + r * ny), (c1[0] - r * nx, c1[1] - r * ny)
    out2, in2 = (c2[0] + r * nx, c2[1] + r * ny), (c2[0] - r * nx, c2[1] - r * ny)
    prims = [
        {
            "type": "arc",
            "center": list(c1),
            "radius": r,
            "start_angle": theta + 90,
            "end_angle": theta + 270,
        },
        {
            "type": "arc",
            "center": list(c2),
            "radius": r,
            "start_angle": theta - 90,
            "end_angle": theta + 90,
        },
        _line(in1, in2),
        _line(out2, out1),
    ]
    implied = [
        ("Tangent", 0, 2, 2, 1),
        ("Tangent", 2, 2, 1, 1),
        ("Tangent", 1, 2, 3, 1),
        ("Tangent", 3, 2, 0, 1),
        ("Equal", 0, 1),
    ]
    if abs(dy) < 1e-9:
        implied.append(("Horizontal", 2))
    elif abs(dx) < 1e-9:
        implied.append(("Vertical", 2))
    return prims, implied


_COMPOSITE_BUILDERS = {"polyline": _polyline, "rectangle": _rectangle, "slot": _slot}


def _offset_args(args: tuple, base: int) -> tuple:
    """Shift the GeoIds of a composite's local constraint (local id k -> base + k)."""
    kind = args[0]
    if kind in ("Coincident", "Tangent"):
        return (kind, args[1] + base, args[2], args[3] + base, args[4])
    return (kind, *(a + base for a in args[1:]))


def expand_geometry(geometry: list, base: int = 0) -> tuple[list[dict], list[list[int]], list]:
    """Spec geometry -> (primitives, GeoIds per spec item, implied constraints).

    A polyline, rectangle or slot becomes several lines and arcs, numbered in
    order from ``base``, plus the constraints that make them one shape (the
    joints; a rectangle's horizontals and verticals; a slot's tangencies and
    equal radii). Every primitive carries its GeoId as ``id``, the spec index
    as ``entry`` and its ``construction`` flag; the implied constraints are
    planned entries (see :func:`plan_constraints`) with ``source`` "geometry".
    """
    prims: list[dict] = []
    ids: list[list[int]] = []
    implied: list[dict] = []
    for i, item in enumerate(geometry):
        if not isinstance(item, dict):
            raise ValueError(f"geometry[{i}] must be a dict, got {item!r}")
        gtype = item.get("type")
        construction = bool(item.get("construction"))
        start = base + len(prims)
        if gtype in PRIMITIVES:
            parts, local = [dict(item)], []
        elif gtype in _COMPOSITE_BUILDERS:
            try:
                parts, local = _COMPOSITE_BUILDERS[gtype](item)
            except ValueError as e:
                raise ValueError(f"geometry[{i}] ({gtype}): {e}") from e
        else:
            raise ValueError(
                f"geometry[{i}]: unknown geometry type {gtype!r}; supported: "
                f"{', '.join(PRIMITIVES + COMPOSITES)}"
            )
        for k, part in enumerate(parts):
            part.update(id=start + k, entry=i, construction=construction)
        prims.extend(parts)
        ids.append([start + k for k in range(len(parts))])
        implied.extend(_implied(gtype, i, *(_offset_args(a, start) for a in local)))
    return prims, ids, implied
