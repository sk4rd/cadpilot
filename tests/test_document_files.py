from cadpilot import operations as ops


def _text(resp):
    return "\n".join(getattr(i, "text", "") for i in resp)


def test_open_document_names_the_document(fake_freecad):
    out = _text(ops.open_document_operation(fake_freecad, "/parts/bracket.FCStd"))
    assert "Opened '/parts/bracket.FCStd' as document 'bracket'" in out


def test_save_document_reports_where(fake_freecad):
    out = _text(ops.save_document_operation(fake_freecad, "bracket", "/parts/b2.FCStd"))
    assert "Saved document 'bracket' to '/parts/b2.FCStd'" in out
