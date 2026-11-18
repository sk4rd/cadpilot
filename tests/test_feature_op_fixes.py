"""Part Design feature-op contracts found by the 2026-11 audit.

The addon cannot be imported without FreeCAD, so most of these parse the
source; the pure helpers (tip_policy, property_mapper) are imported directly.
tests/live_features_verify.py proves the same behaviour against a live FreeCAD.
"""

import ast
from pathlib import Path

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
