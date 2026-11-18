"""Translation of the ``sketch`` spec into Sketcher constraint arguments.

Pure — no FreeCAD import, so it is unit-tested from ``tests/``. sketcher_ops
turns each planned entry into ``Sketcher.Constraint(*args)`` (plus the datum)
and owns everything that needs a live document.
"""

from __future__ import annotations

import math
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


def constraint_args(ctype: str, items: list) -> tuple:
    """spec constraint -> Sketcher.Constraint args (without any value)."""
    if ctype == "coincident":
        g1, p1 = point_ref(items[0])
        g2, p2 = point_ref(items[1])
        return ("Coincident", g1, p1, g2, p2)
    if ctype in ("horizontal", "vertical"):
        return (ctype.capitalize(), geo_ref(items[0]))
    if ctype in ("tangent", "perpendicular"):
        if all(is_point_item(i) for i in items[:2]):
            g1, p1 = point_ref(items[0])
            g2, p2 = point_ref(items[1])
            return (ctype.capitalize(), g1, p1, g2, p2)
        return (ctype.capitalize(), geo_ref(items[0]), geo_ref(items[1]))
    if ctype in ("parallel", "equal"):
        return (ctype.capitalize(), geo_ref(items[0]), geo_ref(items[1]))
    if ctype == "symmetric":
        g1, p1 = point_ref(items[0])
        g2, p2 = point_ref(items[1])
        third = items[2]
        if is_point_item(third):  # symmetry about a point
            g3, p3 = point_ref(third)
            return ("Symmetric", g1, p1, g2, p2, g3, p3)
        return ("Symmetric", g1, p1, g2, p2, geo_ref(third))  # about a line
    if ctype == "distance":
        if len(items) == 1:
            return ("Distance", geo_ref(items[0]))
        a, b = items[0], items[1]
        if is_point_item(a) and is_point_item(b):
            g1, p1 = point_ref(a)
            g2, p2 = point_ref(b)
            return ("Distance", g1, p1, g2, p2)
        if is_point_item(a):
            g1, p1 = point_ref(a)
            return ("Distance", g1, p1, geo_ref(b))
        g2, p2 = point_ref(b)
        return ("Distance", g2, p2, geo_ref(a))
    if ctype in ("distance_x", "distance_y"):
        fc_type = "DistanceX" if ctype == "distance_x" else "DistanceY"
        if len(items) == 1:
            # ONE item is either a point (distance from the origin) or a whole
            # geometry (FreeCAD's edge form: the x/y extent of a line).
            if is_point_item(items[0]):
                g, p = point_ref(items[0])
                return (fc_type, g, p)
            return (fc_type, geo_ref(items[0]))
        g1, p1 = point_ref(items[0])
        g2, p2 = point_ref(items[1])
        return (fc_type, g1, p1, g2, p2)
    if ctype == "radius":
        return ("Radius", geo_ref(items[0]))
    if ctype == "angle":
        if len(items) == 1:
            return ("Angle", geo_ref(items[0]))
        return ("Angle", geo_ref(items[0]), geo_ref(items[1]))
    raise ValueError(f"unknown constraint type {ctype!r}; supported: {', '.join(CONSTRAINT_TYPES)}")


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
        items = c.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"constraints[{i}] ({ctype}): 'items' must be a non-empty list.")
        try:
            args = constraint_args(ctype, items)
        except (IndexError, TypeError) as e:
            raise ValueError(f"constraints[{i}] ({ctype}): malformed items: {e}") from e

        value = c.get("value")
        if ctype in DIMENSIONAL:
            if value is None:
                raise ValueError(
                    f"constraints[{i}] ({ctype}): dimensional constraint requires 'value'."
                )
            value_datum, expr = datum(ctype, value)
        else:
            if value is not None:
                raise ValueError(
                    f"constraints[{i}] ({ctype}): geometric constraint takes no 'value'."
                )
            value_datum, expr = None, None
        planned.append(
            {
                "args": args,
                "datum": value_datum,
                "expr": expr,
                "name": str(c["name"]) if c.get("name") else None,
                "entry": i,
                "type": ctype,
            }
        )
    return planned
