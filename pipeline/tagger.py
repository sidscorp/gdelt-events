"""Article tagger — keyword + theme classification for dashboard view pills.

Each category is a config entry with a keyword list and optional GKG theme
prefixes. Adding a new pill is a config change: append to CATEGORIES and
rebuild. The tagger builds one combined Aho-Corasick automaton across all
categories so each article is scanned once regardless of category count.
"""

import logging
import re
import time
from pathlib import Path

import ahocorasick
import duckdb

from .config import DB_PATH
from .loader import _open_connection

log = logging.getLogger(__name__)

CHUNK_SIZE = 50_000
_WORD_RE = re.compile(r"\w")

# ---------------------------------------------------------------------------
# Category definitions — add a new pill by appending here
#
# Fields per category:
#   keywords              recall candidates (word-bounded Aho-Corasick scan)
#   gkg_theme_prefixes    GKG theme candidates
#   scan_description      scan GAL descriptions too
#   description           semantic query for the pill scorer (pill_scorer.py):
#                         embedded once; article vectors are cosine-scored
#                         against it. Membership rule: sem >= sem_hi, OR
#                         keyword hit AND sem >= sem_lo. Articles without a
#                         vector (non-English) keep keyword-only tagging.
#   neg_description       optional anti-query; articles scoring higher against
#                         this than against `description` are rejected.
#   sem_hi / sem_lo       thresholds for the membership rule.
#   semantic_only         no keyword candidates — membership purely by sem_hi.
# ---------------------------------------------------------------------------

SEM_HI_DEFAULT = 0.62
SEM_LO_DEFAULT = 0.48

CATEGORIES: dict[str, dict] = {
    "supply_chain": {
        "description": (
            "Events that disrupt the production, sourcing, manufacture, or "
            "transport of goods: factory fires and closures, port and shipping "
            "disruptions, logistics failures, export controls, tariffs and trade "
            "restrictions, industrial shortages, product recalls — stories where "
            "the impact on supply chains, manufacturing, or trade flows is part "
            "of the story itself."
        ),
        "neg_description": (
            "General news about wars, weather, disasters, or politics with no "
            "stated connection to goods, manufacturing, logistics, or trade."
        ),
        # Fuzziest intent + noisiest candidates: only 'relevant' verdicts get
        # in ('borderline' admits too much disaster/geopolitics adjacency).
        "judge_strict": True,
        "keywords": [
            "recall", "shortage", "disruption", "supply chain", "supply-chain",
            "factory fire", "plant fire", "plant closure", "plant shutdown",
            "factory closure", "factory shutdown", "port strike", "dock strike",
            "tariff", "tariffs", "sanction", "sanctions", "export ban", "import ban",
            "trade war", "bankruptcy", "layoff", "layoffs", "mass layoff",
            "production halt", "contamination", "cyberattack", "cyber attack",
            "ransomware", "earthquake", "hurricane", "typhoon", "flood", "flooding",
            "wildfire", "volcano", "tsunami", "explosion", "chemical spill",
            "embargo", "blockade", "shipping delay", "freight disruption",
            "container shortage", "semiconductor shortage", "chip shortage",
            "raw material", "critical mineral", "rare earth",
            "fda warning", "warning letter", "consent decree", "import alert",
            "device recall", "product recall",
        ],
        "gkg_theme_prefixes": [
            "ECON_TARIFFS", "ECON_BANKRUPTCY", "ECON_STRIKE",
            "ENV_SPILL",
            # MANMADE_DISASTER excluded — fires on ~20% of GKG (any accident/crime).
            # Factory fires/explosions covered by GAL keyword scan instead.
        ],
        "scan_description": True,
    },
    "medical_devices": {
        "description": (
            "News about medical devices and the medical device industry: "
            "implants, scanners, surgical tools, diagnostics, device makers' "
            "products, regulatory clearances and recalls, clinical use of "
            "devices."
        ),
        "keywords": [
            "pacemaker", "defibrillator", "cardiac implant", "heart valve",
            "stent", "catheter", "angioplasty", "bypass graft",
            "ventilator", "respirator", "cpap", "oxygen concentrator",
            "insulin pump", "glucose monitor", "continuous glucose",
            "infusion pump", "iv pump", "syringe pump",
            "mri", "ct scanner", "x-ray", "ultrasound", "mammography",
            "endoscope", "laparoscope", "arthroscope", "colonoscope",
            "surgical robot", "robotic surgery", "da vinci",
            "hip replacement", "knee replacement", "joint replacement",
            "spinal implant", "orthopedic implant", "bone screw",
            "cochlear implant", "hearing aid", "hearing implant",
            "contact lens", "intraocular lens", "lasik",
            "dental implant", "dental crown", "orthodontic",
            "prosthetic", "prosthesis", "artificial limb",
            "blood pressure monitor", "pulse oximeter", "thermometer",
            "dialysis", "hemodialysis", "peritoneal dialysis",
            "pacemaker lead", "guidewire", "balloon catheter",
            "surgical mesh", "hernia mesh",
            "wearable device", "wearable monitor", "health wearable",
            "point-of-care", "diagnostic test", "rapid test",
            "sterilizer", "autoclave",
            "3d printed implant", "bioprinting",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "semiconductors": {
        "description": (
            "Semiconductor industry news: chipmakers and foundries, fab "
            "construction, chip supply and shortages, export controls on chips, "
            "process-node and packaging advances, semiconductor policy and "
            "subsidies."
        ),
        "keywords": [
            "semiconductor", "semiconductors", "chipmaker", "chipmakers",
            "chip shortage", "chip export", "chip ban",
            "tsmc", "samsung foundry", "intel foundry", "globalfoundries",
            "fab", "fabrication plant", "wafer", "wafer fab",
            "silicon wafer", "photolithography", "euv", "extreme ultraviolet",
            "asml", "nvidia", "qualcomm", "broadcom", "amd",
            "chip act", "chips act", "chips and science",
            "export control", "entity list",
            "advanced packaging", "hbm", "high bandwidth memory",
            "ai chip", "ai chips", "gpu shortage",
            "gallium", "germanium", "rare earth",
            "semiconductor supply", "chip supply",
            "foundry capacity", "node", "nanometer",
            "3nm", "5nm", "2nm", "gate-all-around",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "ai_general": {
        "description": (
            "Artificial intelligence news: model releases, AI companies and "
            "startups, research breakthroughs, AI investment and adoption, "
            "the AI industry broadly."
        ),
        "keywords": [
            "artificial intelligence", "machine learning", "deep learning",
            "neural network", "large language model", "llm",
            "generative ai", "chatbot", "chatgpt", "openai", "anthropic",
            "google deepmind", "meta ai", "mistral",
            "foundation model", "transformer model",
            "ai model", "ai system", "ai agent",
            "ai startup", "ai investment", "ai acquisition", "ai partnership",
            "natural language processing", "computer vision",
            "reinforcement learning", "ai benchmark",
            "ai safety", "ai alignment", "ai ethics",
        ],
        "gkg_theme_prefixes": [
            "SOC_EMERGINGTECH", "SOC_TECHNOLOGYSECTOR",
            "TECH_AUTOMATION", "TECH_BIGDATA", "TECH_SUPERCOMPUTING",
            "TECH_VIRTUALREALITY",
        ],
        "scan_description": True,
    },
    "ai_defense": {
        "judge_strict": True,
        "description": (
            "Military and defense applications of artificial intelligence: "
            "autonomous weapons, defense AI contracts and companies, military "
            "AI policy, AI in warfare and national security."
        ),
        "keywords": [
            "autonomous weapon", "military ai", "defense ai", "ai warfare",
            "lethal autonomous", "drone warfare", "killer robot",
            "ai surveillance", "palantir", "anduril", "shield ai",
            "project maven", "ai defense contract",
            "autonomous vehicle military", "ai missile", "predictive targeting",
            "ai cybersecurity", "cyber warfare ai",
            "national security ai", "pentagon ai", "darpa ai", "aukus",
        ],
        "gkg_theme_prefixes": [
            "DRONES", "CYBER_ATTACK",
        ],
        "scan_description": True,
    },
    "ai_regulation": {
        "description": (
            "AI governance and regulation: legislation, executive orders, "
            "safety frameworks and institutes, standards, compliance "
            "requirements, court cases and copyright disputes about AI."
        ),
        "keywords": [
            "ai regulation", "ai policy", "ai governance", "ai legislation",
            "ai act", "eu ai act", "ai executive order",
            "ai safety institute", "ai summit", "ai bill",
            "ai oversight", "ai accountability", "ai transparency",
            "responsible ai", "ai bias", "algorithmic bias",
            "ai discrimination", "ai audit", "ai compliance", "ai risk",
            "deepfake regulation", "ai copyright", "ai intellectual property",
            "ai labor", "ai job displacement", "ai workforce",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "ai_sector_impact": {
        "description": (
            "Artificial intelligence applied in specific industries — "
            "healthcare, finance, education, manufacturing, agriculture, law, "
            "creative work: sector-specific AI products, adoption, and impact."
        ),
        "keywords": [
            "ai in healthcare", "ai drug discovery", "ai diagnosis",
            "ai radiology", "ai pathology",
            "ai finance", "algorithmic trading", "ai fraud detection",
            "ai lending", "ai underwriting",
            "ai education", "ai tutoring",
            "ai manufacturing", "ai robotics",
            "ai agriculture", "precision agriculture",
            "ai climate", "ai energy", "ai logistics",
            "ai legal", "ai coding", "ai programming",
            "copilot", "code generation",
            "ai art", "ai music", "ai video generation",
            "ai content creation", "sora", "midjourney", "stable diffusion",
        ],
        "gkg_theme_prefixes": [
            "WB_676_CLOUD_COMPUTING", "WB_661_BIG_DATA",
            "WB_665_SOFTWARE_AS_A_SERVICE",
            "WB_1084_TECHNOLOGY_TRANSFER_AND_DIFFUSION",
        ],
        "scan_description": True,
    },
    "oss_vulnerabilities": {
        "judge_strict": True,
        "description": (
            "Security vulnerabilities and exploits in open source software and "
            "package ecosystems: CVEs, malicious packages, software supply "
            "chain attacks, security advisories and patches."
        ),
        "keywords": [
            "CVE-20", "CVSS score", "NVD vulnerability", "NVD database",
            "open source vulnerability", "open source exploit", "oss vulnerability", "oss exploit",
            "open source security flaw", "open source security issue",
            "npm vulnerability", "npm malicious", "malicious npm", "malicious package",
            "PyPI vulnerability", "PyPI malicious", "malicious PyPI", "malicious dependency",
            "malicious module", "malicious library", "malicious python package",
            "Ruby gem vulnerability", "Maven vulnerability", "cargo vulnerability",
            "dependency confusion", "typosquatting package",
            "software supply chain attack", "software supply chain compromise",
            "Log4Shell", "log4j vulnerability", "XZ backdoor", "XZ Utils backdoor",
            "Heartbleed", "ShellShock",
            "vulnerability disclosure", "responsible disclosure", "bug bounty",
            "security advisory", "GitHub Advisory", "GHSA-", "GitHub security advisory",
            "critical vulnerability", "critical patch", "vulnerability patch",
            "RCE vulnerability", "remote code execution vulnerability",
            "memory corruption vulnerability", "buffer overflow vulnerability",
            "path traversal vulnerability", "SQL injection vulnerability",
            "zero-day vulnerability", "zero-day exploit", "proof of concept exploit",
            "patch released", "security fix released", "software composition analysis",
            "vulnerable dependency", "vulnerable package",
        ],
        "gkg_theme_prefixes": ["CYBER_ATTACK"],
        "scan_description": True,
    },
    "cyber_attacks": {
        "description": (
            "Cyberattack incidents and campaigns: data breaches, ransomware, "
            "nation-state cyber operations, hacking groups, and major "
            "cybersecurity incidents."
        ),
        "keywords": [
            "cyberattack", "cyber attack", "cyber-attack",
            "data breach", "security breach", "massive breach",
            "ransomware attack", "ransomware gang",
            "malware attack", "phishing campaign", "spear phishing",
            "DDoS attack", "distributed denial", "denial-of-service attack",
            "nation-state hack", "nation-state attack", "state-sponsored hack",
            "state-sponsored cyberattack", "state-sponsored malware",
            "APT group", "APT28", "APT29", "APT41",
            "Lazarus Group", "Volt Typhoon", "Fancy Bear", "Cozy Bear", "Sandworm", "Kimsuky",
            "cyberespionage", "cyber espionage", "cyber warfare", "cyberwarfare",
            "hacktivist", "hacktivism",
            "critical infrastructure attack", "hospital ransomware", "healthcare ransomware",
            "pipeline ransomware", "power grid hack", "water treatment hack",
            "CISA alert", "CISA advisory", "FBI cyber", "NSA advisory", "NCSC warning",
            "data exfiltrated", "data stolen", "systems encrypted",
            "network intrusion", "unauthorized access", "hacking group",
            "threat actor", "cybercriminal gang", "cybercrime ring",
            "dark web marketplace", "darknet marketplace",
        ],
        "gkg_theme_prefixes": ["CYBER_ATTACK"],
        "scan_description": True,
    },
    "geopolitics_conflict": {
        "description": (
            "Geopolitics and armed conflict: wars and military operations, "
            "ceasefires and peace talks, sanctions, coups, interstate tensions, "
            "major diplomacy, defense alliances and arms transfers."
        ),
        "keywords": [
            "ceasefire", "cease-fire", "airstrike", "air strike", "missile strike",
            "drone strike", "invasion", "military offensive", "military operation",
            "artillery", "mobilization", "peace talks", "peace deal", "armistice",
            "coup", "martial law", "sanctions", "arms deal", "arms transfer",
            "nato", "security council", "military exercise", "border clash",
            "territorial dispute", "annexation", "insurgency", "militia",
            "war crimes", "prisoner exchange", "no-fly zone",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "energy_climate": {
        "description": (
            "Energy and climate: oil and gas markets, OPEC, LNG, electricity "
            "grids and blackouts, renewables, nuclear power, energy prices and "
            "security, climate policy and emissions targets, and extreme "
            "weather framed by its energy or infrastructure impact."
        ),
        "keywords": [
            "oil prices", "crude oil", "opec", "natural gas", "lng",
            "power grid", "blackout", "power outage", "electricity prices",
            "renewable energy", "solar power", "wind power", "wind farm",
            "nuclear plant", "nuclear power", "nuclear reactor",
            "coal plant", "energy crisis", "energy security",
            "carbon emissions", "emissions target", "net zero", "net-zero",
            "climate policy", "climate summit", "climate agreement",
            "heatwave", "heat wave", "drought", "grid operator",
            "energy transition", "battery storage", "hydrogen energy",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "public_health": {
        "description": (
            "Public health: disease outbreaks, epidemics, vaccines and drug "
            "approvals, health agencies and policy, hospital systems, and "
            "population health threats."
        ),
        "keywords": [
            "outbreak", "epidemic", "pandemic", "vaccine", "vaccination",
            "drug approval", "fda approval", "clinical trial",
            "cdc", "world health organization", "public health",
            "measles", "cholera", "bird flu", "h5n1", "avian influenza",
            "mpox", "dengue", "malaria", "tuberculosis", "polio",
            "hospital capacity", "health ministry", "health emergency",
            "disease surveillance", "quarantine", "antimicrobial resistance",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    # FDA-registered device makers acting AS device companies. Candidates come
    # from fda_match_cache (name matches), NOT keywords — pill_scorer.py
    # special-cases candidate_source and applies the semantic gate + anti-query.
    "meddev_companies": {
        "judge_strict": True,
        "description": (
            "News about medical device manufacturers acting as medical device "
            "companies: product launches and clearances, device recalls, "
            "clinical trials, FDA regulatory actions, medtech industry deals "
            "and partnerships."
        ),
        "neg_description": (
            "Routine stock-market coverage: shares bought or sold by funds, "
            "13F filings, analyst ratings and price targets, shareholder "
            "lawsuits, dividend announcements, earnings-preview boilerplate."
        ),
        "keywords": [],
        "candidate_source": "fda_match_cache",
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    # --- Federal health agencies ---
    "fda_agency": {
        "description": (
            "News about the U.S. Food and Drug Administration as a regulatory "
            "body: drug and device approvals, recalls, warning letters, "
            "facility inspections, advisory committee meetings, policy "
            "changes and rulemaking, and FDA leadership decisions."
        ),
        "neg_description": (
            "Routine stock-market coverage, company earnings, or financial "
            "news that merely mentions FDA approval in passing without "
            "covering the agency's actions or decisions."
        ),
        "judge_strict": True,
        "keywords": [
            "fda approval", "fda clears", "fda approves", "fda recalls",
            "fda warning letter", "fda inspection", "fda commissioner",
            "fda advisory committee", "fda panel", "fda ban",
            "food and drug administration", "fda rejects", "fda denies",
            "fda fast track", "fda breakthrough", "fda priority review",
            "fda final rule", "fda proposed rule", "fda guidance",
            "fda enforcement", "fda consent decree", "fda import alert",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "nih_news": {
        "description": (
            "News about the National Institutes of Health: research funding "
            "and grants, major study findings and research breakthroughs from "
            "NIH-funded work, institute directors and leadership changes, "
            "budget and policy decisions affecting biomedical research."
        ),
        "judge_strict": True,
        "keywords": [
            "national institutes of health", "nih director", "nih funding",
            "nih grant", "nih study", "nih research", "nih budget",
            "ninds", "national cancer institute", "nida", "nhlbi", "niaid",
            "nih director", "nih policy", "nih director's blog",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "cms_news": {
        "description": (
            "News about the Centers for Medicare & Medicaid Services: "
            "rulemaking and final rules, reimbursement rate changes, "
            "Medicare and Medicaid policy decisions, enrollment changes, "
            "agency leadership, and program administration."
        ),
        "neg_description": (
            "General political debate about healthcare spending, entitlement "
            "reform, or budget politics that is not about CMS-specific "
            "actions, rules, or administration."
        ),
        "judge_strict": True,
        "keywords": [
            "centers for medicare", "cms administrator", "cms final rule",
            "cms proposed rule", "cms rulemaking", "cms reimbursement",
            "cms payment", "medicare advantage", "medicaid expansion",
            "cms policy", "cms enrollment", "cms chief",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
    "va_news": {
        "description": (
            "News about the U.S. Department of Veterans Affairs: VA healthcare "
            "and hospitals, veterans' benefits and claims processing, VA "
            "leadership and policy, VA modernization and IT, and veterans "
            "services administered by the VA."
        ),
        "neg_description": (
            "General military or veterans news not about the VA: VFW or "
            "American Legion advocacy, foreign countries' veterans, military "
            "records disputes not involving VA healthcare or benefits, "
            "political campaigns about veterans issues without VA action."
        ),
        "judge_strict": True,
        "keywords": [
            "department of veterans affairs", "va secretary",
            "va hospital", "veterans health", "va medical center",
            "va benefits", "va claims", "va disability",
            "veterans affairs", "va healthcare", "va secretary of",
            "veterans health administration", "va secretary of",
        ],
        "gkg_theme_prefixes": [],
        "scan_description": True,
    },
}


# ---------------------------------------------------------------------------
# Combined automaton builder
# ---------------------------------------------------------------------------

def _build_combined_automaton(
    categories: dict[str, dict] | None = None,
) -> ahocorasick.Automaton:
    """Build one automaton with all keywords from all categories. Each
    pattern maps to (keyword, category_name). One scan tags all categories."""
    cats = categories or CATEGORIES
    A = ahocorasick.Automaton()
    seen: set[str] = set()
    for cat_name, cat_conf in cats.items():
        for kw in cat_conf.get("keywords", []):
            low = kw.lower().strip()
            if not low or low in seen:
                continue
            seen.add(low)
            A.add_word(low, (low, cat_name))
    A.make_automaton()
    log.info("combined automaton: %d patterns across %d categories",
             len(seen), len(cats))
    return A


def _keyword_scan(automaton: ahocorasick.Automaton, text: str) -> tuple[str, str] | None:
    """First word-bounded keyword → (keyword, category) or None."""
    if not text:
        return None
    low = text.lower()
    n = len(low)
    for end_idx, value in automaton.iter(low):
        kw, category = value
        start = end_idx - len(kw) + 1
        if start > 0 and _WORD_RE.match(low[start - 1]):
            continue
        if end_idx + 1 < n and _WORD_RE.match(low[end_idx + 1]):
            continue
        return kw, category
    return None


def _keyword_scan_all(automaton: ahocorasick.Automaton, text: str) -> list[tuple[str, str]]:
    """All word-bounded keyword matches → [(keyword, category), ...].
    Returns at most one match per category (first hit wins)."""
    if not text:
        return []
    low = text.lower()
    n = len(low)
    found: dict[str, str] = {}
    for end_idx, value in automaton.iter(low):
        kw, category = value
        if category in found:
            continue
        start = end_idx - len(kw) + 1
        if start > 0 and _WORD_RE.match(low[start - 1]):
            continue
        if end_idx + 1 < n and _WORD_RE.match(low[end_idx + 1]):
            continue
        found[category] = kw
    return [(kw, cat) for cat, kw in found.items()]


# ---------------------------------------------------------------------------
# GKG theme matching
# ---------------------------------------------------------------------------

def _all_theme_prefixes() -> dict[str, list[str]]:
    """Invert: theme_prefix → list of categories that use it."""
    result: dict[str, list[str]] = {}
    for cat_name, cat_conf in CATEGORIES.items():
        for prefix in cat_conf.get("gkg_theme_prefixes", []):
            result.setdefault(prefix, []).append(cat_name)
    return result


def _themes_match_all(themes_field: str, prefix_map: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Check themes against all category prefixes. Returns [(theme, category), ...]."""
    if not themes_field:
        return []
    found: dict[str, str] = {}
    for entry in themes_field.split(";"):
        theme = entry.split(",")[0].strip()
        if not theme:
            continue
        for prefix, cats in prefix_map.items():
            if theme.startswith(prefix):
                for cat in cats:
                    if cat not in found:
                        found[cat] = theme
    return [(theme, cat) for cat, theme in found.items()]


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------

def _get_last_ts(con, category: str, source: str) -> int:
    row = con.execute(
        "SELECT last_crawled_at FROM tag_state "
        "WHERE category = ? AND source_type = ?",
        [category, source],
    ).fetchone()
    return row[0] if row else 0


def _set_last_ts(con, category: str, source: str, ts: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO tag_state "
        "(category, source_type, last_crawled_at, updated_at) "
        "VALUES (?, ?, ?, current_timestamp)",
        [category, source, ts],
    )


def _bulk_insert_tags(con, tags: list[tuple]) -> None:
    if not tags:
        return
    con.executemany(
        "INSERT INTO article_tags "
        "(article_id, source_type, category, matched_via, matched_detail, crawled_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        tags,
    )


# ---------------------------------------------------------------------------
# Stream scanners
# ---------------------------------------------------------------------------

def _tag_gkg_stream(con, prefix_map: dict, since_ts: int = 0) -> tuple[int, int, int]:
    """Scan GKG rows for supply chain themes across all categories."""
    where = 'WHERE "V1DATE" > ?' if since_ts else ""
    params: list = [since_ts] if since_ts else []
    scanned = inserted = 0
    max_ts = since_ts
    offset = 0

    while True:
        chunk = con.execute(
            f'SELECT "GKGRECORDID", "V1DATE", "V2ENHANCEDTHEMES" '
            f'FROM gkg {where} LIMIT ? OFFSET ?',
            params + [CHUNK_SIZE, offset],
        ).fetchall()
        if not chunk:
            break

        tags: list[tuple] = []
        for gkg_id, v1date, themes in chunk:
            scanned += 1
            if v1date and v1date > max_ts:
                max_ts = v1date
            for theme, cat in _themes_match_all(themes, prefix_map):
                tags.append((gkg_id, "gkg", cat, "theme", theme, v1date))

        if tags:
            _bulk_insert_tags(con, tags)
            inserted += len(tags)

        if scanned % 500_000 == 0:
            log.info("  GKG tagged %d, matched %d", scanned, inserted)

        if len(chunk) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE

    return scanned, inserted, max_ts


def _tag_gal_stream(con, automaton, since_ts: int = 0) -> tuple[int, int, int]:
    """Scan English GAL titles + descriptions for keywords across all categories."""
    conds = ["language = 'en'"]
    params: list = []
    if since_ts:
        conds.append("crawled_at > ?")
        params.append(since_ts)
    where = "WHERE " + " AND ".join(conds)

    scanned = inserted = 0
    max_ts = since_ts
    offset = 0

    while True:
        chunk = con.execute(
            f"SELECT url, crawled_at, title, description FROM gal {where} "
            f"ORDER BY crawled_at LIMIT ? OFFSET ?",
            params + [CHUNK_SIZE, offset],
        ).fetchall()
        if not chunk:
            break

        tags: list[tuple] = []
        for url, crawled_at, title, description in chunk:
            scanned += 1
            if crawled_at and crawled_at > max_ts:
                max_ts = crawled_at
            text = (title or "") + " " + (description or "")
            hits = _keyword_scan_all(automaton, text)
            for kw, cat in hits:
                tags.append((url, "gal", cat, "keyword", kw, crawled_at))

        if tags:
            _bulk_insert_tags(con, tags)
            inserted += len(tags)

        if scanned % 500_000 == 0:
            log.info("  GAL tagged %d, matched %d", scanned, inserted)

        if len(chunk) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE

    return scanned, inserted, max_ts


# ---------------------------------------------------------------------------
# Custom pill integration
# ---------------------------------------------------------------------------

USERS_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "users.db"


def _load_all_categories() -> dict[str, dict]:
    """Merge static CATEGORIES with custom pills from SQLite."""
    cats = dict(CATEGORIES)
    if not USERS_DB_PATH.exists():
        return cats
    try:
        import json as _json
        import sqlite3
        udb = sqlite3.connect(str(USERS_DB_PATH))
        udb.row_factory = sqlite3.Row
        # Keyword pills only — semantic pills have NULL/empty keywords_json and
        # are maintained by pill_scorer.py, not the keyword automaton. Per-row
        # guard so one malformed pill can't take down all custom pills.
        for row in udb.execute(
            "SELECT id, keywords_json, scan_description FROM custom_pills "
            "WHERE COALESCE(pill_type, 'keyword') = 'keyword'"
        ).fetchall():
            try:
                keywords = _json.loads(row["keywords_json"] or "[]")
                if not keywords:
                    continue
                cats[f"custom_{row['id']}"] = {
                    "keywords": keywords,
                    "gkg_theme_prefixes": [],
                    "scan_description": bool(row["scan_description"]),
                }
            except Exception:
                log.warning("skipping malformed custom pill %s", row["id"])
        udb.close()
    except Exception as e:
        log.warning("failed to load custom pills: %s", e)
    return cats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tag_new_rows(db_path=None) -> dict:
    """Incremental tag — scan only rows newer than the oldest watermark."""
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    con.execute("SET threads = 4")
    con.execute("CHECKPOINT")
    summary: dict = {}
    all_cats = _load_all_categories()
    try:
        automaton = _build_combined_automaton(all_cats)
        prefix_map = _all_theme_prefixes()

        # GKG: only categories with theme prefixes need GKG scanning.
        # Custom pills (keywords only) don't scan GKG, so exclude them
        # from the watermark min — otherwise their zero watermark forces
        # a full 6.6M row re-scan on every tick.
        gkg_cats = [c for c, conf in all_cats.items() if conf.get("gkg_theme_prefixes")]
        gkg_watermarks = [
            _get_last_ts(con, cat, "gkg")
            for cat in gkg_cats
            if _get_last_ts(con, cat, "gkg") > 0
        ]
        gkg_since = min(gkg_watermarks) if gkg_watermarks else -1
        if gkg_since >= 0:
            t0 = time.time()
            scanned, matched, max_ts = _tag_gkg_stream(con, prefix_map, since_ts=gkg_since)
            summary["gkg_scanned"] = scanned
            summary["gkg_matched"] = matched
            summary["gkg_elapsed_s"] = round(time.time() - t0, 2)
            for cat in gkg_cats:
                if max_ts > _get_last_ts(con, cat, "gkg"):
                    _set_last_ts(con, cat, "gkg", max_ts)
        else:
            summary["gkg_scanned"] = 0
            summary["gkg_matched"] = 0
            summary["gkg_elapsed_s"] = 0.0

        # GAL: use the oldest watermark, but ONLY from categories that
        # already have a watermark. Custom pills with no tag_state entry
        # (backfill pending/in-progress) are handled by the pill_worker,
        # not the incremental tagger — including them would force a full
        # 7.6M row re-scan every 15-min tick.
        gal_cats = [c for c, conf in all_cats.items() if conf.get("keywords")]
        gal_watermarks = [
            _get_last_ts(con, cat, "gal")
            for cat in gal_cats
            if _get_last_ts(con, cat, "gal") > 0
        ]
        gal_since = min(gal_watermarks) if gal_watermarks else -1
        t0 = time.time()
        scanned, matched, max_ts = _tag_gal_stream(con, automaton, since_ts=gal_since)
        summary["gal_scanned"] = scanned
        summary["gal_matched"] = matched
        summary["gal_elapsed_s"] = round(time.time() - t0, 2)
        for cat in all_cats:
            if max_ts > _get_last_ts(con, cat, "gal"):
                _set_last_ts(con, cat, "gal", max_ts)

        con.execute("CHECKPOINT")
    finally:
        con.close()
    return summary


def initial_tag(category: str | None = None, db_path=None) -> dict:
    """Full tag from scratch. Pass category to rebuild one, or None for all."""
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    con.execute("SET threads = 8")
    con.execute("CHECKPOINT")
    summary: dict = {}
    cats_to_build = {category: CATEGORIES[category]} if category else CATEGORIES
    try:
        for cat in cats_to_build:
            log.info("Clearing %s tags...", cat)
            con.execute("DELETE FROM article_tags WHERE category = ?", [cat])
            con.execute("DELETE FROM tag_state WHERE category = ?", [cat])

        prefix_map = _all_theme_prefixes()
        if any(c.get("gkg_theme_prefixes") for c in cats_to_build.values()):
            log.info("Tagging GKG (theme scan)...")
            t0 = time.time()
            scanned, matched, max_ts = _tag_gkg_stream(con, prefix_map, since_ts=0)
            summary["gkg_scanned"] = scanned
            summary["gkg_matched"] = matched
            summary["gkg_elapsed_s"] = round(time.time() - t0, 1)
            for cat in cats_to_build:
                _set_last_ts(con, cat, "gkg", max_ts)

        log.info("Tagging GAL (keyword scan, title + description)...")
        automaton = _build_combined_automaton(cats_to_build)
        t0 = time.time()
        scanned, matched, max_ts = _tag_gal_stream(con, automaton, since_ts=0)
        summary["gal_scanned"] = scanned
        summary["gal_matched"] = matched
        summary["gal_elapsed_s"] = round(time.time() - t0, 1)
        for cat in cats_to_build:
            _set_last_ts(con, cat, "gal", max_ts)

        con.execute("CHECKPOINT")
    finally:
        con.close()
    return summary
