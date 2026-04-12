"""Preset view registry for the GDELT dashboard.

Each view is one of:

  kind="filters": a bundle of the same filter args the API already accepts.
                  Merged with user-supplied filters at query time.
  kind="fda_match": special-cased semi-join against fda_match_cache.

Add a new view by appending to VIEWS and restarting the dashboard.
"""

VIEWS = [
    {
        "id": "fda-medical-devices",
        "name": "Medical Device Companies",
        "description": (
            "Articles mentioning any FDA-registered medical device manufacturer. "
            "Use the Strict/Broad toggle to trade precision for recall."
        ),
        "kind": "fda_match",
        "default_hours": 24,
        "default_match_types": ["legal"],
        "available_match_types": [
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
]


def find_view(view_id: str) -> dict | None:
    for v in VIEWS:
        if v["id"] == view_id:
            return v
    return None
