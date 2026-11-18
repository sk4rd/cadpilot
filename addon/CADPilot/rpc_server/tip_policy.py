"""Which created feature must become its PartDesign Body's Tip, and which
``obj_properties`` keys each feature op takes.

Pure decision data — no FreeCAD import, so it is unit-tested from ``tests/``.

Why this exists: CADPilot used to trust FreeCAD to advance ``Body.Tip`` by
itself. Measured live on FreeCAD 1.1.4, that trust is misplaced for PartDesign
*transform* features. ``cad(operation="pattern", pattern_type="polar",
count=6)`` on a flange built a correct ``PartDesign::PolarPattern`` — the
pattern's own Shape was the 6-hole result (15246 mm^3) — but ``Body.Tip`` was
still the single-hole pocket, so the Body's volume stayed 15874 mm^3: one hole,
reported as success. ``body.Tip = pattern`` fixed it on the spot.

So the builder asks this module instead, and the tip is pushed explicitly.
The set is a whitelist on purpose: an unknown (newer-FreeCAD) type is left
alone rather than assigned as a Tip, because a wrong Tip is far worse than a
stale one — assigning an object that is not a body member leaves the body
``Invalid`` (see ``dress_type`` below).
"""

from __future__ import annotations

import difflib

# PartDesign features that are a modelling step, so the body should show them.
_ADVANCING_TYPES = frozenset(
    {
        # additive / subtractive
        "PartDesign::Pad",
        "PartDesign::Pocket",
        "PartDesign::Revolution",
        "PartDesign::Groove",
        "PartDesign::AdditivePipe",
        "PartDesign::SubtractivePipe",
        "PartDesign::AdditiveLoft",
        "PartDesign::SubtractiveLoft",
        "PartDesign::Hole",
        # dress-up
        "PartDesign::Thickness",
        "PartDesign::Draft",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        # boolean
        "PartDesign::Boolean",
        # transforms — the ones FreeCAD does NOT advance by itself
        "PartDesign::PolarPattern",
        "PartDesign::LinearPattern",
        "PartDesign::Mirrored",
        "PartDesign::MultiTransform",
    }
)

_DRESS = {
    "fillet": {
        "part": "Part::Fillet",
        "pd": "PartDesign::Fillet",
        "prop": "Radius",
        "spec": "radius",
    },
    "chamfer": {
        "part": "Part::Chamfer",
        "pd": "PartDesign::Chamfer",
        "prop": "Size",
        "spec": "size",
    },
}


def advances_tip(type_id: str) -> bool:
    """True when ``type_id`` must be assigned as its Body's Tip after creation."""
    return type_id in _ADVANCING_TYPES


def should_advance(
    type_id: str,
    *,
    body_has_tip: bool,
    tip_is_feat: bool,
    feat_depends_on_tip: bool,
) -> bool:
    """True when the new feature should become its Body's Tip.

    ``Body.Tip`` is what the Body *shows*, so claiming it for a feature that is
    not the end of the chain silently hides everything after it. A dress-up on a
    pad must therefore NOT take the tip away from the pattern that follows it —
    only a successor of the current tip (``feat_depends_on_tip``, i.e. the tip is
    among the feature's OutList) or the first feature of a fresh body may.
    """
    if not advances_tip(type_id) or tip_is_feat:
        return False
    return feat_depends_on_tip if body_has_tip else True


def _dress(kind: str) -> dict[str, str]:
    try:
        return _DRESS[str(kind).lower()]
    except KeyError:
        raise ValueError(
            f"unknown dress feature {kind!r}; expected one of {sorted(_DRESS)}"
        ) from None


def dress_type(kind: str, base_in_body: bool) -> str:
    """The object type to build for a fillet/chamfer.

    A ``Part::Fillet`` is a document-root object: it is not in the Body's Group,
    does not follow the Body's Placement, and assigning it to ``Body.Tip`` is
    accepted silently and leaves the Body ``['Touched', 'Invalid']``. When the
    base feature belongs to a Body, build the PartDesign dress-up instead.
    """
    spec = _dress(kind)
    return spec["pd"] if base_in_body else spec["part"]


def dress_size_property(kind: str) -> str:
    """The scalar size property of the PartDesign dress-up (1.1 has no tuples)."""
    return _dress(kind)["prop"]


def dress_spec_key(kind: str) -> str:
    """The cad() spec key carrying the size — 'radius'/'size', NOT the op name.

    The op is called "fillet" but its spec key is "radius"; conflating the two
    made every fillet op fail with "fillet requires: fillet".
    """
    return _dress(kind)["spec"]


def dress_base_is_allowed(*, base_in_body: bool, base_is_tip: bool) -> bool:
    """Whether a dress-up on this base can be built without wrecking the body.

    A Part-level base goes down the Part::Fillet path and touches no body.
    Inside a Body, the dress-up must sit at the END of the chain: FreeCAD 1.1
    moves ``Body.Tip`` onto a newly created PartDesign feature, so dressing a
    mid-chain feature makes the body show the dress-up and silently DROP every
    later feature — measured live: filleting the pad under a flange put the body
    at 15999 mm^3 with the six bolt holes gone. There is no safe API for the
    mid-chain insert either (``Body.insertObject`` duplicated the Group entry and
    left the tip wrong), so the caller must refuse.
    """
    return (not base_in_body) or base_is_tip


_DRESS_KEYS = ("edges", "name")
_PADLIKE_KEYS = ("length", "through_all", "pad_type", "reversed", "midplane", "body", "name")
_REVLIKE_KEYS = ("angle", "axis", "reversed", "body", "name")

#: The obj_properties keys each feature op reads, the one list the builders
#: are checked against. An unknown key used to be dropped silently: live,
#: ``pattern(mode="Spacing", spacings=[...])`` built evenly spaced copies and
#: reported success.
SPEC_KEYS: dict[str, frozenset[str]] = {
    op: frozenset(keys)
    for op, keys in {
        "boolean": ("op", "tool", "name"),
        "fillet": (*_DRESS_KEYS, "radius"),
        "chamfer": (*_DRESS_KEYS, "size"),
        "loft": ("profiles", "solid", "ruled", "name"),
        "sweep": ("path", "solid", "name"),
        "mirror": ("plane", "face", "originals", "name"),
        "pattern": (
            "count",
            "pattern_type",
            "spacing",
            "spacings",
            "mode",
            "length",
            "axis",
            "angle",
            "center",
            "reversed",
            "originals",
            "transforms",
            "name",
        ),
        "move": ("translate", "rotate", "placement"),
        "color": (
            "color",
            "transparency",
            "line_color",
            "line_width",
            "point_size",
            "display_mode",
            "draw_style",
            "visible",
            "objects",
        ),
        "variables": ("cells", "name"),
        "sketch": (
            "geometry",
            "plane",
            "offset",
            "body",
            "construction",
            "external",
            "constraints",
        ),
        "pad": _PADLIKE_KEYS,
        "pocket": _PADLIKE_KEYS,
        "revolution": _REVLIKE_KEYS,
        "groove": _REVLIKE_KEYS,
        "thickness": ("faces", "value", "reversed", "body", "name"),
        "draft": ("faces", "angle", "neutral_plane", "pull_direction", "body", "name"),
        "datum_plane": ("plane", "offset", "body"),
        "hull": ("sketches", "margin"),
    }.items()
}

#: The keys of one ``transforms`` step of pattern_type="multi", per step type.
TRANSFORM_STEP_KEYS: dict[str, frozenset[str]] = {
    "linear": frozenset(
        {"pattern_type", "count", "spacing", "spacings", "mode", "length", "axis", "reversed"}
    ),
    "polar": frozenset({"pattern_type", "count", "angle", "axis", "center", "reversed"}),
    "mirror": frozenset({"pattern_type", "plane", "face"}),
}
TRANSFORM_STEP_KEYS["any"] = frozenset().union(*TRANSFORM_STEP_KEYS.values()) - {"pattern_type"}

# The feature spec's own fields, filled in by the dispatcher, not the caller.
_SPEC_FIELDS = frozenset({"type", "base", "description"})


def unknown_spec_keys(op: str, spec: dict) -> str:
    """'' when every key of ``spec`` is one ``op`` reads, else the refusal text."""
    allowed = SPEC_KEYS.get(op)
    if allowed is None:
        return ""
    # A key set to None asks for nothing (callers pass {"name": None} for "no
    # name"), so only keys that carry a value can be ignored by mistake.
    unknown = sorted(
        k for k, v in spec.items() if v is not None and k not in allowed and k not in _SPEC_FIELDS
    )
    if not unknown:
        return ""
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, sorted(allowed), n=1)
        hints.append(f"{key!r} (did you mean {close[0]!r}?)" if close else repr(key))
    return (
        f"{op} does not take {', '.join(hints)}; it would be ignored, so nothing was "
        f"built. Valid keys for {op}: {', '.join(sorted(allowed))} "
        f"(see operation_help('{op}'))."
    )


def unknown_transform_step_keys(kind: str, step: dict) -> str:
    """'' for a valid MultiTransform step, else the refusal text."""
    allowed = TRANSFORM_STEP_KEYS.get(kind) if kind != "any" else None
    if allowed is None:
        return f"pattern_type must be 'linear', 'polar' or 'mirror', got {kind!r}."
    unknown = sorted(k for k in step if k not in allowed)
    if not unknown:
        return ""
    return (
        f"a {kind} step does not take {', '.join(map(repr, unknown))}; valid keys: "
        f"{', '.join(sorted(allowed))}."
    )
