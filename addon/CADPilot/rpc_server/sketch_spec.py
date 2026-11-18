"""Translation of the ``sketch`` spec into Sketcher constraint arguments.

Pure — no FreeCAD import, so it is unit-tested from ``tests/``. sketcher_ops
turns each planned entry into ``Sketcher.Constraint(*args)`` (plus the datum)
and owns everything that needs a live document.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

# spec point keyword -> Sketcher PointPos
POINT_POS = {"start": 1, "end": 2, "center": 3, "mid": 3}

DIMENSIONAL = ("distance", "distance_x", "distance_y", "radius", "angle")

CONSTRAINT_TYPES = (
    "coincident",
    "horizontal",
    "vertical",
    "tangent",
    "perpendicular",
    "parallel",
    "equal",
    "symmetric",
    "distance",
    "distance_x",
    "distance_y",
    "radius",
    "angle",
)


def point_ref(item: Any, what: str = "item") -> tuple[int, int]:
    """[geo_id, "start"|"end"|...] -> (geo_id, PointPos).

    GeoId -1 is the sketch X axis; its start point (origin) is the common
    anchor, so any point keyword on -1 maps to the origin (PointPos 1).
    """
    if not (isinstance(item, (list, tuple)) and len(item) == 2):
        raise ValueError(f"{what} must be [geo_id, point], got {item!r}")
    geo_id, pos = int(item[0]), str(item[1]).lower()
    if geo_id == -1:
        return -1, 1
    p = POINT_POS.get(pos)
    if p is None:
        raise ValueError(f"{what}: point must be one of {sorted(POINT_POS)}, got {item[1]!r}")
    return geo_id, p


def geo_ref(item: Any, what: str = "item") -> int:
    """A whole-geometry reference: ``[geo_id]`` or a bare ``geo_id``.

    A caller writing a constraint by hand naturally writes ``"items": [0]``
    (the geometry INDEX), and a 2-element item is a POINT reference elsewhere,
    so a bare int can never be ambiguous here.
    """
    if isinstance(item, int):
        return item
    if not (isinstance(item, (list, tuple)) and len(item) == 1):
        raise ValueError(f"{what} must be a geo_id or [geo_id] (the geometry index), got {item!r}")
    return int(item[0])


def is_point_item(item: Any) -> bool:
    return isinstance(item, (list, tuple)) and len(item) == 2


def _count(items: list, low: int, high: int | None, shape: str) -> None:
    """Refuse an item count the constraint cannot use, instead of dropping extras."""
    n = len(items)
    if n < low or (high is not None and n > high):
        raise ValueError(f"takes {shape}, got {n} item(s)")


def constraint_args(ctype: str, items: list) -> list[tuple]:
    """spec constraint -> one Sketcher.Constraint args tuple per constraint.

    A list of edges or points expands instead of being cut to the first one or
    two: ``horizontal [0, 2]`` is two constraints, ``equal [0, 1, 2]`` the chain
    0=1, 1=2, and ``radius [0, 1, 2]`` one radius on 0 with 1 and 2 equal to it,
    so a single named dimension drives them all.
    """
    if ctype == "coincident":
        _count(items, 2, None, "two or more points [g, p]")
        out = []
        for a, b in pairwise(items):
            g1, p1 = point_ref(a)
            g2, p2 = point_ref(b)
            out.append(("Coincident", g1, p1, g2, p2))
        return out
    if ctype in ("horizontal", "vertical"):
        return [(ctype.capitalize(), geo_ref(item)) for item in items]
    if ctype in ("tangent", "perpendicular"):
        _count(items, 2, 2, "two edges or two points")
        if all(is_point_item(i) for i in items):
            g1, p1 = point_ref(items[0])
            g2, p2 = point_ref(items[1])
            return [(ctype.capitalize(), g1, p1, g2, p2)]
        return [(ctype.capitalize(), geo_ref(items[0]), geo_ref(items[1]))]
    if ctype in ("parallel", "equal"):
        _count(items, 2, None, "two or more edges")
        return [(ctype.capitalize(), geo_ref(a), geo_ref(b)) for a, b in pairwise(items)]
    if ctype == "symmetric":
        _count(items, 3, 3, "two points and a line or a point")
        g1, p1 = point_ref(items[0])
        g2, p2 = point_ref(items[1])
        third = items[2]
        if is_point_item(third):  # symmetry about a point
            g3, p3 = point_ref(third)
            return [("Symmetric", g1, p1, g2, p2, g3, p3)]
        return [("Symmetric", g1, p1, g2, p2, geo_ref(third))]  # about a line
    if ctype == "distance":
        _count(items, 1, 2, "one edge, two points, or a point and an edge")
        if len(items) == 1:
            return [("Distance", geo_ref(items[0]))]
        a, b = items[0], items[1]
        if is_point_item(a) and is_point_item(b):
            g1, p1 = point_ref(a)
            g2, p2 = point_ref(b)
            return [("Distance", g1, p1, g2, p2)]
        if is_point_item(a):
            g1, p1 = point_ref(a)
            return [("Distance", g1, p1, geo_ref(b))]
        g2, p2 = point_ref(b)
        return [("Distance", g2, p2, geo_ref(a))]
    if ctype in ("distance_x", "distance_y"):
        _count(items, 1, 2, "one point, one edge, or two points")
        fc_type = "DistanceX" if ctype == "distance_x" else "DistanceY"
        if len(items) == 1:
            # ONE item is either a point (distance from the origin) or a whole
            # geometry (FreeCAD's edge form: the x/y extent of a line).
            if is_point_item(items[0]):
                g, p = point_ref(items[0])
                return [(fc_type, g, p)]
            return [(fc_type, geo_ref(items[0]))]
        g1, p1 = point_ref(items[0])
        g2, p2 = point_ref(items[1])
        return [(fc_type, g1, p1, g2, p2)]
    if ctype == "radius":
        first = geo_ref(items[0])
        return [("Radius", first)] + [("Equal", first, geo_ref(i)) for i in items[1:]]
    if ctype == "angle":
        _count(items, 1, 2, "one or two edges")
        if len(items) == 1:
            return [("Angle", geo_ref(items[0]))]
        return [("Angle", geo_ref(items[0]), geo_ref(items[1]))]
    raise ValueError(f"unknown constraint type {ctype!r}; supported: {', '.join(CONSTRAINT_TYPES)}")


# Sketcher constraint types that carry a datum
_DATUM_TYPES = frozenset({"Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"})


def datum(ctype: str, value: Any) -> tuple[float, str | None]:
    """A dimensional constraint's (datum, expression).

    The datum is in Sketcher's internal units (mm, radians); the spec gives
    angles in degrees. An ``"=expr"`` value yields a placeholder datum that
    the expression replaces on the next recompute.
    """
    if isinstance(value, str) and value.startswith("="):
        return 1.0, value[1:]
    if ctype == "angle":
        return math.radians(float(value)), None
    return float(value), None


def plan_constraints(constraints: list | None) -> list[dict]:
    """The spec's constraint list -> one planned entry per Sketcher constraint.

    An entry can expand into several (see :func:`constraint_args`); each keeps
    the spec index it came from, so the solver's complaints map back to it.

    Each entry: ``args`` (Sketcher.Constraint positional args without the
    datum), ``datum`` (None for a geometric constraint), ``expr`` (to bind
    after adding), ``name``, and ``entry``/``type`` (the spec index and type it
    came from).
    Raises ValueError naming the offending entry.
    """
    planned = []
    for i, c in enumerate(constraints or []):
        if not isinstance(c, dict):
            raise ValueError(f"constraints[{i}] must be a dict, got {c!r}")
        ctype = c.get("type")
        where = f"constraints[{i}] ({ctype})"
        items = c.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"{where}: 'items' must be a non-empty list.")
        try:
            all_args = constraint_args(ctype, items)
        except (IndexError, TypeError) as e:
            raise ValueError(f"{where}: malformed items: {e}") from e
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from e

        value = c.get("value")
        if ctype in DIMENSIONAL:
            if value is None:
                raise ValueError(f"{where}: dimensional constraint requires 'value'.")
            value_datum, expr = datum(ctype, value)
        else:
            if value is not None:
                raise ValueError(f"{where}: geometric constraint takes no 'value'.")
            value_datum, expr = None, None
        name = str(c["name"]) if c.get("name") else None
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


def origin_of(plan: dict) -> dict:
    """What a planned constraint reports when the solver names it."""
    out = {"entry": f"constraints[{plan['entry']}]", "type": plan["type"]}
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
