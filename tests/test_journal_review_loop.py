"""The review loop's pure half: what the journal does with the user's own edits."""

import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import step_journal as sj  # noqa: E402


def test_a_named_constraint_binding_is_found_by_name():
    exprs = {".Constraints.Width": "Params.Wd", "Constraints[3]": "Params.Ht"}
    assert sj.constraint_expression(exprs, 2, "Width") == "Params.Wd"
    assert sj.constraint_expression(exprs, 3, "") == "Params.Ht"
    assert sj.constraint_expression(exprs, 1, "") is None
    assert sj.constraint_expression(exprs, 2, "") is None


def test_a_named_binding_survives_an_edit_of_another_constraint():
    exprs = {".Constraints.Width": "Params.Wd"}
    expr = sj.constraint_expression(exprs, 2, "Width")
    assert sj.map_constraint_value("distance", "=Params.Wd", 80.0, expr) == "=Params.Wd"
