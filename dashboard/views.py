"""Preset view registry for the GDELT dashboard.

Each view is one of:

  kind="filters": a bundle of the same filter args the API already accepts.
                  Merged with user-supplied filters at query time.
  kind="fda_match": special-cased semi-join against fda_match_cache.
  kind="tag_match": keyword/theme tag join against article_tags.

Add a new view by appending to VIEWS and restarting the dashboard.
"""

VIEWS = [
    {
        "id": "fda-medical-devices",
        "name": "Medical Device Companies",
        "description": (
            "Articles mentioning FDA-registered medical device manufacturers. "
            "Verified = company name confirmed alongside device context in description. "
            "Strict = legal names only. Broad = includes abbreviated names."
        ),
        "kind": "fda_match",
        "default_hours": 24,
        "default_match_types": ["legal", "contextual"],
        "available_match_types": [
            {
                "id": "verified",
                "label": "Verified",
                "description": (
                    "Company name + medical device keyword confirmed in article description. "
                    "Highest precision — filters out incidental name matches."
                ),
                "match_types": ["legal", "contextual"],
            },
            {
                "id": "legal",
                "label": "Strict",
                "description": "Full legal names only. High precision.",
                "match_types": ["legal"],
            },
            {
                "id": "broad",
                "label": "Broad",
                "description": "Includes base names (Pfizer, Stryker, ...). High recall, more noise.",
                "match_types": ["legal", "stripped"],
            },
        ],
    },
    {
        "id": "medical-devices",
        "name": "Medical Devices",
        "description": "Articles mentioning specific medical device types — implants, scanners, surgical tools, diagnostics.",
        "kind": "tag_match",
        "tag_category": "medical_devices",
        "default_hours": 24,
    },
    {
        "id": "supply-chain-alerts",
        "name": "Supply Chain Alerts",
        "description": (
            "Geopolitical events, natural disasters, trade disruptions, "
            "and other signals that could impact supply chains."
        ),
        "kind": "tag_match",
        "tag_category": "supply_chain",
        "default_hours": 24,
    },
    {
        "id": "semiconductors",
        "name": "Semiconductor & Chip Geopolitics",
        "description": (
            "Chipmakers, fab capacity, export controls, trade restrictions, "
            "and the geopolitics of the global semiconductor supply chain."
        ),
        "kind": "tag_match",
        "tag_category": "semiconductors",
        "default_hours": 24,
    },
    {
        "id": "ai-general",
        "name": "AI & Machine Learning",
        "description": (
            "Artificial intelligence breakthroughs, model releases, company news, "
            "investments, and the broader AI/ML landscape."
        ),
        "kind": "tag_match",
        "tag_category": "ai_general",
        "default_hours": 24,
    },
    {
        "id": "ai-defense",
        "name": "AI in Defense & Intelligence",
        "description": (
            "Military AI applications, autonomous weapons, defense contracts, "
            "AI surveillance, and national security implications."
        ),
        "kind": "tag_match",
        "tag_category": "ai_defense",
        "default_hours": 24,
    },
    {
        "id": "ai-regulation",
        "name": "AI Regulation & Policy",
        "description": (
            "AI governance, legislation, safety frameworks, executive orders, "
            "bias audits, and the evolving regulatory landscape."
        ),
        "kind": "tag_match",
        "tag_category": "ai_regulation",
        "default_hours": 24,
    },
    {
        "id": "ai-sector-impact",
        "name": "AI Sector Impact",
        "description": (
            "AI transforming industries — healthcare, finance, education, "
            "manufacturing, creative tools, and workforce displacement."
        ),
        "kind": "tag_match",
        "tag_category": "ai_sector_impact",
        "default_hours": 24,
    },
]


def find_view(view_id: str) -> dict | None:
    for v in VIEWS:
        if v["id"] == view_id:
            return v
    return None
