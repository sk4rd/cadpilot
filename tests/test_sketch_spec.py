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
        {"type": "tangent", "items": [0, 1, 2, 3]},
        {"type": "symmetric", "items": [[0, "start"], [0, "end"], 1, 2]},
        {"type": "angle", "items": [0, 1, 2], "value": 5},
        {"type": "equal", "items": [0]},
    ):
        with pytest.raises(ValueError, match=rf"constraints\[0\] \({c['type']}\): takes "):
            _plan(c)


def test_a_name_cannot_label_an_expanded_geometric_entry():
    with pytest.raises(ValueError, match="one name cannot label the 2 constraints"):
        _plan({"type": "horizontal", "items": [0, 2], "name": "H"})


def test_point_on_object_diameter_and_block():
    assert _args({"type": "point_on_object", "items": [[0, "start"], -1]}) == [
        ("PointOnObject", 0, 1, -1)
    ]
    assert _args({"type": "point_on_object", "items": [[0, "start"], [1, "end"], 3]}) == [
        ("PointOnObject", 0, 1, 3),
        ("PointOnObject", 1, 2, 3),
    ]
    plans = _plan({"type": "diameter", "items": [2, 3], "value": 8, "name": "Bore"})
    assert [(p["args"], p["datum"], p["name"]) for p in plans] == [
        (("Diameter", 2), 8.0, "Bore"),
        (("Equal", 2, 3), None, None),
    ]
    assert _args({"type": "block", "items": [0, 1]}) == [("Block", 0), ("Block", 1)]


def test_horizontal_and_vertical_between_two_points():
    assert _args({"type": "horizontal", "items": [[0, "start"], [2, "center"]]}) == [
        ("Horizontal", 0, 1, 2, 3)
    ]
    assert _args({"type": "vertical", "items": [[0, "end"], [1, "start"]]}) == [
        ("Vertical", 0, 2, 1, 1)
    ]


def test_tangent_and_perpendicular_forms():
    assert _args({"type": "tangent", "items": [0, 1]}) == [("Tangent", 0, 1)]
    assert _args({"type": "tangent", "items": [[0, "end"], [1, "start"]]}) == [
        ("Tangent", 0, 2, 1, 1)
    ]
    assert _args({"type": "tangent", "items": [[0, "end"], 1]}) == [("Tangent", 0, 2, 1)]
    assert _args({"type": "perpendicular", "items": [1, [0, "end"]]}) == [
        ("Perpendicular", 0, 2, 1)
    ]
    assert _args({"type": "tangent", "items": [0, 1, [3, "start"]]}) == [
        ("TangentViaPoint", 0, 1, 3, 1)
    ]


# Two lines, a circle and an arc, one external circle and one external line.
KINDS = ss.geometry_kinds(
    ["LineSegment", "LineSegment", "Circle", "ArcOfCircle"], ["Circle", "LineSegment"]
)


def _kplan(*constraints):
    return [p["args"] for p in ss.plan_constraints(list(constraints), KINDS)]


def test_geometry_kinds_number_internal_axes_and_external():
    assert KINDS == {
        0: "line",
        1: "line",
        2: "circle",
        3: "arc",
        -1: "x_axis",
        -2: "y_axis",
        -3: "circle",
        -4: "line",
    }


def test_an_external_circle_exposes_its_center():
    """Coincident(p, -3, center) on an external circle solves (live 1.1.4); the
    old blanket rule refused 'center' on every external id."""
    assert _kplan({"type": "coincident", "items": [[2, "center"], [-3, "center"]]}) == [
        ("Coincident", 2, 3, -3, 3)
    ]
    with pytest.raises(ValueError, match="a full circle has only its 'center'"):
        _kplan({"type": "coincident", "items": [[0, "start"], [-3, "start"]]})
    assert _kplan({"type": "coincident", "items": [[0, "start"], [-4, "end"]]}) == [
        ("Coincident", 0, 1, -4, 2)
    ]


def test_a_line_midpoint_coincidence_becomes_symmetric():
    """[line, 'mid'] passed addConstraint and failed at solve (Malformed)."""
    assert _kplan({"type": "coincident", "items": [[0, "mid"], [-1, "start"]]}) == [
        ("Symmetric", 0, 1, 0, 2, -1, 1)
    ]
    assert _kplan({"type": "coincident", "items": [[2, "center"], [1, "mid"]]}) == [
        ("Symmetric", 1, 1, 1, 2, 2, 3)
    ]
    with pytest.raises(ValueError, match=r"constraints\[0\] \(distance_x\).*no midpoint vertex"):
        _kplan({"type": "distance_x", "items": [[0, "mid"]], "value": 5})
    with pytest.raises(ValueError, match="two line midpoints"):
        _kplan({"type": "coincident", "items": [[0, "mid"], [1, "mid"]]})


def test_axis_points_other_than_the_origin_are_refused():
    """[-1, 'end'] used to map silently to the origin; FreeCAD's own (-1, 2) is
    a unit point at (1, 0), and [-2, 'start'] duplicates the origin."""
    assert _kplan({"type": "coincident", "items": [[0, "start"], [-1, "center"]]}) == [
        ("Coincident", 0, 1, -1, 1)
    ]
    with pytest.raises(ValueError, match=r"the X axis \(-1\) has one point, the origin"):
        _kplan({"type": "coincident", "items": [[0, "start"], [-1, "end"]]})
    with pytest.raises(ValueError, match=r"the Y axis \(-2\) has no point"):
        ss.plan_constraints([{"type": "coincident", "items": [[0, "start"], [-2, "start"]]}])


def test_freecad_point_numbers_are_accepted_as_keywords():
    """The live corpus writes [-1, 1] for the origin (FreeCAD's PointPos)."""
    assert _kplan({"type": "coincident", "items": [[0, 1], [-1, 1]]}) == [
        ("Coincident", 0, 1, -1, 1)
    ]
    assert _kplan({"type": "coincident", "items": [[2, 3], [3, 2]]}) == [("Coincident", 2, 3, 3, 2)]
    with pytest.raises(ValueError, match=r"the X axis \(-1\) has one point"):
        _kplan({"type": "coincident", "items": [[0, 1], [-1, 2]]})


def test_a_missing_geo_id_names_the_constraint_and_the_valid_range():
    with pytest.raises(ValueError) as e:
        _kplan(
            {"type": "horizontal", "items": [0]},
            {"type": "distance", "items": [7], "value": 3},
        )
    assert str(e.value) == (
        "constraints[1] (distance): GeoId 7 does not exist; this sketch has geometry 0..3, "
        "the axes -1 (X) and -2 (Y), external -3..-4"
    )
    with pytest.raises(ValueError, match=r"GeoId -5 does not exist"):
        _kplan({"type": "point_on_object", "items": [[0, "start"], -5]})


@pytest.mark.parametrize(
    ("value", "dimension", "expected"),
    [
        (10, "length", 10.0),
        ("10", "length", 10.0),
        ("10 mm", "length", 10.0),
        ("1.5cm", "length", 15.0),
        ("0.5 in", "length", 12.7),
        ("-2.5e1 mm", "length", -25.0),
        ("45 deg", "angle", 45.0),
        ("45°", "angle", 45.0),
        (" 90 ", "angle", 90.0),
    ],
)
def test_parse_quantity(value, dimension, expected):
    assert math.isclose(ss.parse_quantity(value, dimension), expected)


def test_parse_quantity_refuses_the_wrong_dimension_and_junk():
    with pytest.raises(ValueError, match="'deg' is not a length unit"):
        ss.parse_quantity("10 deg", "length")
    with pytest.raises(ValueError, match="is not a number or a quantity"):
        ss.parse_quantity("ten mm", "length")
    with pytest.raises(ValueError, match="is not a number"):
        ss.parse_quantity(True, "length")


def test_unit_values_reach_the_datum_in_internal_units():
    """ "10 mm" died as float('10 mm'); an angle in rad must not be converted again."""
    (dist,) = _plan({"type": "distance", "items": [0], "value": "10 mm"})
    assert dist["datum"] == 10.0
    (ang,) = _plan({"type": "angle", "items": [0], "value": "0.5 rad"})
    assert math.isclose(ang["datum"], 0.5)
    (ang,) = _plan({"type": "angle", "items": [0], "value": "30 deg"})
    assert math.isclose(ang["datum"], math.radians(30))
    with pytest.raises(ValueError, match=r"constraints\[0\] \(distance\): value '10 deg'"):
        _plan({"type": "distance", "items": [0], "value": "10 deg"})
    with pytest.raises(
        ValueError, match=r"constraints\[0\] \(radius\): value '-3 mm': a radius must be"
    ):
        _plan({"type": "radius", "items": [0], "value": "-3 mm"})


def test_constraint_names_are_identifiers_and_unique():
    with pytest.raises(ValueError, match=r"constraints\[0\] \(distance\): name 'bad name'"):
        _plan({"type": "distance", "items": [0], "value": 5, "name": "bad name"})
    with pytest.raises(
        ValueError,
        match=r"constraints\[1\] \(radius\): name 'W' is already used by constraints\[0\]",
    ):
        _plan(
            {"type": "distance", "items": [0], "value": 5, "name": "W"},
            {"type": "radius", "items": [1], "value": 5, "name": "W"},
        )
    with pytest.raises(ValueError, match=r"'W' is already used by Constraints\[4\]"):
        ss.plan_constraints(
            [{"type": "distance", "items": [0], "value": 5, "name": "W"}],
            taken={"W": "Constraints[4]"},
        )


def test_unnamed_dimensions_are_listed_once_per_entry():
    plans = _plan(
        {"type": "horizontal", "items": [0]},
        {"type": "distance", "items": [0], "value": 5, "name": "Len"},
        {"type": "radius", "items": [1, 2], "value": 5},
    )
    assert ss.unnamed_dimensions(plans) == ["constraints[2] (radius)"]
