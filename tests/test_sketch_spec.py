"""Sketch spec translation (constraint mapping, values) — pure, no FreeCAD."""

import math
import sys
from pathlib import Path

import pytest

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import sketch_spec as ss  # noqa: E402


def _plan(*constraints):
    return ss.plan_constraints(list(constraints))


def _args(*constraints):
    return [p["args"] for p in _plan(*constraints)]


def test_point_and_whole_geometry_references():
    assert _args({"type": "coincident", "items": [[0, "end"], [1, "start"]]}) == [
        ("Coincident", 0, 2, 1, 1)
    ]
    assert _args({"type": "horizontal", "items": [0]}) == [("Horizontal", 0)]
    assert _args({"type": "horizontal", "items": [[0]]}) == [("Horizontal", 0)]


def test_distance_forms():
    assert _args({"type": "distance", "items": [0], "value": 5}) == [("Distance", 0)]
    assert _args({"type": "distance", "items": [[0, "start"], [1, "end"]], "value": 5}) == [
        ("Distance", 0, 1, 1, 2)
    ]
    assert _args({"type": "distance", "items": [[0, "start"], 1], "value": 5}) == [
        ("Distance", 0, 1, 1)
    ]
    assert _args({"type": "distance_x", "items": [[0, "start"]], "value": 5}) == [
        ("DistanceX", 0, 1)
    ]
    assert _args({"type": "distance_y", "items": [2], "value": 5}) == [("DistanceY", 2)]


def test_angle_value_is_degrees_in_and_radians_out():
    (plan,) = _plan({"type": "angle", "items": [0], "value": 30})
    assert math.isclose(plan["datum"], math.radians(30))
    assert plan["expr"] is None


def test_expression_value_binds_with_a_placeholder_datum():
    (plan,) = _plan({"type": "distance", "items": [0], "value": "=Params.width", "name": "Width"})
    assert plan["expr"] == "Params.width"
    assert plan["name"] == "Width"
    assert plan["entry"] == 0 and plan["type"] == "distance"


def test_value_rules():
    with pytest.raises(ValueError, match=r"constraints\[0\] \(radius\).*requires 'value'"):
        _plan({"type": "radius", "items": [0]})
    with pytest.raises(ValueError, match=r"constraints\[0\] \(horizontal\).*takes no 'value'"):
        _plan({"type": "horizontal", "items": [0], "value": 3})


def test_solver_diagnostics_are_one_based_and_name_the_spec_entry():
    """FreeCAD's ConflictingConstraints == [2, 3] for a horizontal line with
    two lengths means Constraints[1] and Constraints[2] (live-verified on
    1.1.4); read as spec indices it blamed the horizontal and missed one."""
    plans = _plan(
        {"type": "horizontal", "items": [0]},
        {"type": "distance", "items": [0], "value": 10, "name": "Len"},
        {"type": "distance", "items": [0], "value": 20},
    )
    origins = [ss.origin_of(p) for p in plans]
    mapped = ss.map_diagnostics({"ConflictingConstraints": [2, 3]}, origins)
    assert mapped == {
        "conflicting": [
            {"constraint": 1, "entry": "constraints[1]", "type": "distance", "name": "Len"},
            {"constraint": 2, "entry": "constraints[2]", "type": "distance"},
        ]
    }
    assert ss.diagnostics_text(mapped) == (
        "conflicting: constraints[1] (distance 'Len'), constraints[2] (distance)"
    )


def test_diagnostics_for_constraints_not_added_by_the_call_keep_their_index():
    mapped = ss.map_diagnostics({"RedundantConstraints": [1]}, [None])
    assert mapped == {"redundant": [{"constraint": 0}]}
    assert ss.diagnostics_text(mapped) == "redundant: Constraints[0]"


def test_a_list_of_edges_expands_instead_of_dropping_the_rest():
    """horizontal used only items[0] and equal/parallel only the first two, so
    the rest of a list was silently unconstrained."""
    assert _args({"type": "horizontal", "items": [0, 2, [4]]}) == [
        ("Horizontal", 0),
        ("Horizontal", 2),
        ("Horizontal", 4),
    ]
    assert _args({"type": "equal", "items": [0, 1, 2]}) == [("Equal", 0, 1), ("Equal", 1, 2)]
    assert _args({"type": "parallel", "items": [1, 3, 5]}) == [
        ("Parallel", 1, 3),
        ("Parallel", 3, 5),
    ]
    assert _args({"type": "coincident", "items": [[0, "end"], [1, "start"], [2, "start"]]}) == [
        ("Coincident", 0, 2, 1, 1),
        ("Coincident", 1, 1, 2, 1),
    ]


def test_a_radius_list_is_one_named_dimension_plus_equalities():
    plans = _plan({"type": "radius", "items": [0, 1, 2], "value": 4, "name": "R"})
    assert [p["args"] for p in plans] == [("Radius", 0), ("Equal", 0, 1), ("Equal", 0, 2)]
    assert [p["name"] for p in plans] == ["R", None, None]
    assert [p["datum"] for p in plans] == [4.0, None, None]
    assert {p["entry"] for p in plans} == {0}


def test_extra_items_that_cannot_expand_are_refused():
    for c in (
        {"type": "distance", "items": [0, 1, 2], "value": 5},
        {"type": "tangent", "items": [0, 1, 2]},
        {"type": "symmetric", "items": [[0, "start"], [0, "end"], 1, 2]},
        {"type": "angle", "items": [0, 1, 2], "value": 5},
        {"type": "equal", "items": [0]},
    ):
        with pytest.raises(ValueError, match=rf"constraints\[0\] \({c['type']}\): takes "):
            _plan(c)


def test_a_name_cannot_label_an_expanded_geometric_entry():
    with pytest.raises(ValueError, match="one name cannot label the 2 constraints"):
        _plan({"type": "horizontal", "items": [0, 2], "name": "H"})
