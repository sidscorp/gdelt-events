"""Preset view registry for the GDELT dashboard.

Each view is one of:

  kind="filters": a bundle of the same filter args the API already accepts.
                  Merged with user-supplied filters at query time.
  kind="fda_match": special-cased semi-join against fda_match_cache.
  kind="tag_match": keyword/theme tag join against article_tags.

Add a new view by appending to VIEWS and restarting the dashboard.
"""

VIEWS = [
    # --- Technology & AI ---
    {
        "id": "ai-general",
        "name": "AI Sector",
        "description": (
            "Artificial intelligence breakthroughs, model releases, company news, "
            "investments, and the broader AI/ML landscape."
        ),
        "kind": "tag_match",
        "tag_category": "ai_general",
        "default_hours": 24,
        "group": "Technology & AI",
    },
    {
        "id": "ai-regulation",
        "name": "AI Governance & Regulation",
        "description": (
            "AI governance, legislation, safety frameworks, executive orders, "
            "bias audits, and the evolving regulatory landscape."
        ),
        "kind": "tag_match",
        "tag_category": "ai_regulation",
        "default_hours": 24,
        "group": "Technology & AI",
    },
    {
        "id": "ai-defense",
        "name": "AI & Defense",
        "description": (
            "Military and defense applications of AI: autonomous weapons, "
            "defense AI contracts, military AI policy, AI in warfare."
        ),
        "kind": "tag_match",
        "tag_category": "ai_defense",
        "default_hours": 24,
        "group": "Technology & AI",
    },
    {
        "id": "ai-sector-impact",
        "name": "AI in Industry",
        "description": (
            "AI applied in specific industries — healthcare, finance, "
            "education, manufacturing, agriculture, law, creative work."
        ),
        "kind": "tag_match",
        "tag_category": "ai_sector_impact",
        "default_hours": 24,
        "group": "Technology & AI",
    },
    {
        "id": "semiconductors",
        "name": "Semiconductors",
        "description": (
            "Chipmakers and foundries, fabs, chip supply, export controls, "
            "process advances, and semiconductor policy."
        ),
        "kind": "tag_match",
        "tag_category": "semiconductors",
        "default_hours": 24,
        "group": "Technology & AI",
    },
    # --- Security ---
    {
        "id": "oss-vulnerabilities",
        "name": "Open Source Vulnerabilities",
        "description": (
            "CVEs, exploits, and security flaws in open source software, "
            "package ecosystems, and developer tools."
        ),
        "kind": "tag_match",
        "tag_category": "oss_vulnerabilities",
        "default_hours": 24,
        "group": "Security",
    },
    {
        "id": "cyber-attacks",
        "name": "Cybersecurity",
        "description": (
            "Data breaches, ransomware campaigns, nation-state operations, "
            "and cyberattack incidents worldwide."
        ),
        "kind": "tag_match",
        "tag_category": "cyber_attacks",
        "default_hours": 24,
        "group": "Security",
    },
    # --- Health & Science ---
    {
        "id": "public-health",
        "name": "Public Health",
        "description": (
            "Disease outbreaks, vaccines and drug approvals, health agencies "
            "and policy, hospital systems."
        ),
        "kind": "tag_match",
        "tag_category": "public_health",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "medical-devices",
        "name": "Medical Devices",
        "description": (
            "Articles mentioning specific medical device types -- "
            "implants, scanners, surgical tools, diagnostics."
        ),
        "kind": "tag_match",
        "tag_category": "medical_devices",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "fda-medical-devices",
        "name": "Medical Device Companies",
        "description": (
            "FDA-registered device manufacturers acting as device companies: "
            "product launches, clearances, recalls, trials, medtech deals. "
            "Name matches are semantically gated to filter out stock-market "
            "noise and incidental mentions."
        ),
        "kind": "tag_match",
        "tag_category": "meddev_companies",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "fda-agency",
        "name": "FDA",
        "description": (
            "U.S. Food and Drug Administration as a regulator: drug and device "
            "approvals, recalls, warning letters, inspections, advisory "
            "committees, policy changes, and agency leadership."
        ),
        "kind": "tag_match",
        "tag_category": "fda_agency",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "nih-news",
        "name": "NIH",
        "description": (
            "National Institutes of Health: research funding and grants, "
            "major study findings, institute Directors, and NIH policy."
        ),
        "kind": "tag_match",
        "tag_category": "nih_news",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "cms-news",
        "name": "CMS",
        "description": (
            "Centers for Medicare & Medicaid Services: rulemaking, "
            "reimbursement changes, Medicare and Medicaid policy, enrollment, "
            "and agency leadership."
        ),
        "kind": "tag_match",
        "tag_category": "cms_news",
        "default_hours": 24,
        "group": "Health & Science",
    },
    {
        "id": "va-news",
        "name": "VA",
        "description": (
            "Department of Veterans Affairs: VA healthcare and hospitals, "
            "veterans' benefits and claims, VA leadership, and veterans policy."
        ),
        "kind": "tag_match",
        "tag_category": "va_news",
        "default_hours": 24,
        "group": "Health & Science",
    },
    # --- World & Economy ---
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
        "group": "World & Economy",
    },
    {
        "id": "geopolitics-conflict",
        "name": "Geopolitics & Conflict",
        "description": (
            "Wars and military operations, ceasefires, sanctions, coups, "
            "interstate tensions, and major diplomacy."
        ),
        "kind": "tag_match",
        "tag_category": "geopolitics_conflict",
        "default_hours": 24,
        "group": "World & Economy",
    },
    {
        "id": "energy-climate",
        "name": "Energy & Climate",
        "description": (
            "Oil and gas markets, power grids, renewables, nuclear, energy "
            "security, climate policy, and extreme weather's energy impact."
        ),
        "kind": "tag_match",
        "tag_category": "energy_climate",
        "default_hours": 24,
        "group": "World & Economy",
    },
]

# Display order for pill groups in the UI. Any group not listed here
# (e.g. "My Pills" for custom pills) renders at the end in encounter order.
GROUP_ORDER = [
    "Technology & AI",
    "Security",
    "Health & Science",
    "World & Economy",
]


def find_view(view_id: str) -> dict | None:
    for v in VIEWS:
        if v["id"] == view_id:
            return v
    return None
