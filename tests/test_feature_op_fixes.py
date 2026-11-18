"""Part Design feature-op contracts found by the 2026-11 audit.

The addon cannot be imported without FreeCAD, so most of these parse the
source; the pure helpers (tip_policy, property_mapper) are imported directly.
tests/live_features_verify.py proves the same behaviour against a live FreeCAD.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"
_FEATURE = ast.parse((_ADDON / "feature_ops.py").read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _src(name, tree=_FEATURE) -> str:
    return ast.unparse(_func(tree, name))


def test_mirror_of_a_body_feature_builds_a_partdesign_mirrored():
    """A Part::Mirroring of a feature copies the whole body to the document
    root and leaves the Body unchanged; inside a body it must be Mirrored."""
    assert "_build_pd_mirror" in _src("_build_mirror")
    pd = _src("_build_pd_mirror")
    assert "PartDesign::Mirrored" in pd
    assert "MirrorPlane" in pd and "Originals" in pd
    assert "had no effect" in pd, "a mirror that changes nothing must be refused"


# --- what a recompute broke -------------------------------------------------------


@pytest.fixture(scope="module")
def gq():
    """geometry_query with a stub FreeCAD: the health scan reads plain attributes."""
    if "FreeCAD" not in sys.modules:
        stub = types.ModuleType("FreeCAD")
        for name in ("Document", "DocumentObject", "Vector", "Placement", "Rotation"):
            setattr(stub, name, type(name, (), {}))
        sys.modules["FreeCAD"] = stub
    if str(_ADDON.parent) not in sys.path:
        sys.path.insert(0, str(_ADDON.parent))
    from rpc_server import geometry_query

    return geometry_query


class _Shape:
    def __init__(self, volume, valid=True):
        self.Volume = volume
        self._valid = valid

    def isNull(self):
        return False

    def isValid(self):
        return self._valid


class _Obj:
    def __init__(self, name, type_id, state=("Up-to-date",), status="Valid", links=None, **kw):
        self.Name = name
        self.TypeId = type_id
        self.State = list(state)
        self._status = status
        self._links = links or {}
        self.PropertiesList = list(self._links)
        for key, value in kw.items():
            setattr(self, key, value)

    def getStatusString(self):
        return self._status

    def getTypeIdOfProperty(self, prop):
        return "App::PropertyLinkSub"

    def __getattr__(self, name):
        links = self.__dict__.get("_links", {})
        if name in links:
            return links[name]
        raise AttributeError(name)


class _Doc:
    def __init__(self, *objects):
        self.Objects = list(objects)

    def getObject(self, name):
        return next((o for o in self.Objects if o.Name == name), None)


def _chamfer_model(broken):
    pad = _Obj("Pad", "PartDesign::Pad", Shape=_Shape(12000.0))
    chamfer = _Obj(
        "Chamfer",
        "PartDesign::Chamfer",
        state=("Touched", "Invalid") if broken else ("Up-to-date",),
        status="Invalid edge link" if broken else "Valid",
        links={"Base": (pad, ["?Edge8" if broken else "Edge8"])},
        Shape=_Shape(11920.0),
    )
    body = _Obj("Body", "PartDesign::Body", state=("Touched",) if broken else (), Tip=chamfer)
    body.Shape = _Shape(11920.0)
    return _Doc(body, pad, chamfer)


def test_a_dependent_broken_by_the_recompute_is_reported(gq):
    """The audit's case: a spreadsheet change turned a chamfer's link into
    '?Edge8' (Invalid) and the reply still said success with the stale volume."""
    before = _chamfer_model(broken=False)
    issues_before = gq.model_issues(before)
    volumes_before = gq.body_volumes(before)
    assert issues_before == {}
    report = gq.recompute_report(_chamfer_model(broken=True), issues_before, volumes_before)
    assert report["broken"] == ["Chamfer"], "the Body is reported as stale, not as broken"
    text = " ".join(report["warnings"])
    assert "BROKE Chamfer" in text and "'?Edge8'" in text
    assert "STALE" in text and "Body 'Body'" in text
    assert report["model"] == [
        {
            "body": "Body",
            "tip": "Chamfer",
            "volume_before": 11920.0,
            "volume_after": 11920.0,
            "stale": True,
        }
    ]


def test_breakage_that_was_already_there_is_not_reported_again(gq):
    broken = _chamfer_model(broken=True)
    report = gq.recompute_report(broken, gq.model_issues(broken), gq.body_volumes(broken))
    assert "broken" not in report and "warnings" not in report
    assert report["model"][0]["stale"] is True, "the row still says the shape is stale"


def test_unchanged_bodies_are_left_out_unless_asked_for(gq):
    doc = _chamfer_model(broken=False)
    issues, volumes = gq.model_issues(doc), gq.body_volumes(doc)
    assert gq.recompute_report(doc, issues, volumes) == {}
    rows = gq.recompute_report(doc, issues, volumes, bodies={"Body"})["model"]
    assert rows[0]["volume_before"] == rows[0]["volume_after"] == 11920.0


def test_ops_snapshot_the_model_and_report_what_their_recompute_broke():
    gui = _src("create_feature_gui")
    assert gui.index("model_issues(doc)") < gui.index("builder(doc, spec)")
    assert "recompute_report" in gui
    assert "_MODEL_REPORT" in _src("describe_feature_reply")
    factory = ast.parse((_ADDON / "object_factory.py").read_text(encoding="utf-8"))
    edit = _src("edit_object_gui", factory)
    assert "recompute_report" in edit and "_restore_values" in edit
    assert "_overridden_literals" in edit, "a literal under an expression binding must warn"


def test_measure_geometry_says_when_the_shape_is_stale():
    gq_tree = ast.parse((_ADDON / "geometry_query.py").read_text(encoding="utf-8"))
    measure = _src("measure_geometry", gq_tree)
    assert "_stale_tip" in measure and "'stale'" in measure and "'tip'" in measure


def test_self_intersecting_profiles_are_refused():
    for builder in ("_build_padlike", "_build_revlike"):
        assert "_refuse_self_intersecting_profile" in _src(builder)


# --- patterns ---------------------------------------------------------------------


def test_linear_pattern_keeps_its_pitch_when_the_count_changes():
    """Extent mode with Length = spacing*(count-1) froze the overall length, so
    a count bound to a spreadsheet silently changed the pitch."""
    spacing = _src("_set_linear_spacing")
    assert "'Spacing'" in spacing and "'Offset'" in spacing
    assert "_set_or_bind(feat, 'Offset'" in spacing, "the pitch must be bindable"
    assert "count - 1" not in _src("_build_pd_pattern")


def test_linear_pattern_defaults_to_the_profiles_in_plane_axis():
    """The old default, the body's Z axis, is the extrusion axis of every hole
    drilled from an XY sketch: the copies coincided and the op was refused."""
    pd = _src("_build_pd_pattern")
    assert "'H' if _profile_of(base) is not None else 'X'" in pd
    assert "'H_Axis'" in ast.unparse(_FEATURE)


def test_no_effect_guard_compares_with_the_predecessor_and_names_the_cause():
    """base.Shape is the volume where the original sits in the chain, not the
    pattern's predecessor; and the error used to blame FreeCAD."""
    pd = _src("_build_pd_pattern")
    assert "_transform_volumes(feat)" in pd and "base.Shape" not in pd
    assert "_pattern_no_effect_reason" in pd
    reason = _src("_pattern_no_effect_reason")
    for cause in ("normal of", "runs through", "spacing is 0", "outside the part"):
        assert cause in reason
    assert "BaseFeature" in _src("_transform_volumes")
    docs = (Path(__file__).resolve().parents[1] / "src" / "cadpilot" / "tool_docs.py").read_text()
    assert "cannot drive a PartDesign pattern" not in docs
