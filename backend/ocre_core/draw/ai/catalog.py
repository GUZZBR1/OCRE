"""Versioned starter catalog of common simple-part candidates.

Catalog membership is only a naming aid; it makes no engineering suitability claim.
"""
CATALOG_VERSION = "0.1.0"
MATERIAL_FALLBACK_VERSION = "0.1.0"

# Candidate labels deliberately remain suggestions requiring human review.
CANDIDATES = {
    "plate": ("plate", "flat plate", "chapa", "placa"),
    "bar": ("bar", "rod", "barra", "haste"),
    "block": ("block", "prism", "bloco", "prisma"),
    "bracket": ("bracket", "support", "suporte", "cantoneira"),
    "screw": ("screw", "bolt", "parafuso"),
    "drill": ("drill", "broca"),
    "monitor": ("monitor", "display", "monitor"),
    "crane": ("crane", "guindaste"),
}

# Candidate vocabularies are not engineering recommendations.
MATERIALS = {
    "plate": ("steel", "aluminum", "aço", "alumínio"),
    "bar": ("steel", "aluminum", "aço", "alumínio"),
    "block": ("steel", "aluminum", "aço", "alumínio"),
    "bracket": ("steel", "aluminum", "aço", "alumínio"),
    "screw": ("steel", "stainless steel", "aço", "aço inoxidável"),
    "drill": ("high-speed steel", "carbide", "aço rápido", "metal duro"),
    "monitor": (),
    "crane": (),
}

_MATERIAL_ALIASES = {
    "steel": "steel", "aço": "steel", "aluminum": "aluminum", "alumínio": "aluminum",
    "stainless steel": "stainless steel", "aço inoxidável": "stainless steel",
    "high-speed steel": "high-speed steel", "aço rápido": "high-speed steel",
    "carbide": "carbide", "metal duro": "carbide", "plastic": "plastic", "plástico": "plastic",
}


# Conservative candidate defaults for simple parts. These are proposals only.
MATERIAL_FALLBACK = {
    "plate": "steel",
    "bar": "steel",
    "block": "steel",
    "bracket": "steel",
    "screw": "steel",
    "drill": "high-speed steel",
}


def match(label: str) -> str | None:
    """Normalize a model label to a known candidate; unknown labels abstain."""
    if not isinstance(label, str):
        return None
    key = " ".join(label.casefold().split())
    return next((name for name, aliases in CANDIDATES.items() if key in aliases or key == name), None)


def material_match(part: str, material: str) -> str | None:
    """Return a catalog material candidate only when explicitly catalogued."""
    if not isinstance(material, str):
        return None
    key = " ".join(material.casefold().split())
    key = _MATERIAL_ALIASES.get(key, key)
    for name in MATERIALS.get(part, ()):
        canonical = _MATERIAL_ALIASES.get(name.casefold(), name.casefold())
        if canonical == key:
            return canonical
    return None
