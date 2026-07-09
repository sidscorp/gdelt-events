"""Semantic pill scorer — the hybrid keyword+semantic membership engine.

Every pill (curated CATEGORIES entry with a `description`, plus custom
semantic pills from users.db) gets its description embedded once (cached in
data/embeddings/pill_vectors.npz, keyed by sha1 of the description). Article
vectors from the embedding store are cosine-scored against all pill vectors
in one numpy matmul.

Membership rule per curated pill:
    tag if  sem >= sem_hi
        OR  (existing keyword tag AND sem >= sem_lo)
    and, when the pill has a neg_description:
        reject/demote if sem_neg >= sem_pos
Custom semantic pills: tag if sem >= pill threshold.
`meddev_companies` (candidate_source='fda_match_cache'): candidates are FDA
name matches; tag if sem >= sem_lo AND sem_pos > sem_neg.

Articles without vectors (non-English / embed lag) keep keyword-only tags —
demotion only ever touches articles that HAVE a vector.

Runs incrementally (watermark on store row_index) chained at the end of
embed_new_articles.py; pipeline/rescore_pills.py uses the same machinery for
full-corpus backfills into shadow (`__v2`) categories.
"""

import hashlib
import json
import logging
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np

# Dual-mode imports: package-relative when run via `-m pipeline.X`; when
# imported from script-mode pipeline files (embed_new_articles.py runs as a
# plain script), put the repo root on sys.path and import as the package —
# tagger.py etc. use relative imports and only work as package members.
try:
    from .config import DATA_DIR, DB_PATH
    from .loader import _open_connection
    from . import embedding_store
    from .tagger import CATEGORIES, SEM_HI_DEFAULT, SEM_LO_DEFAULT
except ImportError:
    import sys as _sys
    _repo = str(Path(__file__).resolve().parent.parent)
    if _repo not in _sys.path:
        _sys.path.insert(0, _repo)
    from pipeline.config import DATA_DIR, DB_PATH
    from pipeline.loader import _open_connection
    from pipeline import embedding_store
    from pipeline.tagger import CATEGORIES, SEM_HI_DEFAULT, SEM_LO_DEFAULT

log = logging.getLogger("pill_scorer")

USERS_DB = DATA_DIR / "users.db"
VEC_CACHE = embedding_store.BASE_DIR / "pill_vectors.npz"
VEC_META = embedding_store.BASE_DIR / "pill_vectors_meta.json"
WATERMARK = embedding_store.BASE_DIR / ".pill_scorer_row"

# Set to "" at flip time; "__v2" writes shadow categories for evaluation.
SUFFIX = ""  # FLIPPED LIVE 2026-07-09 — judged tags write straight to the real categories

IN_BATCH = 400  # ≤500 per the GAL point-lookup gotcha


# ---------------------------------------------------------------------------
# Pill registry: (key, description, neg_description, hi, lo, kind)
# ---------------------------------------------------------------------------

def load_pill_defs() -> list[dict]:
    pills = []
    for cat, conf in CATEGORIES.items():
        desc = conf.get("description")
        if not desc:
            continue
        pills.append({
            "key": cat,
            "description": desc,
            "neg_description": conf.get("neg_description"),
            "hi": conf.get("sem_hi", SEM_HI_DEFAULT),
            "lo": conf.get("sem_lo", SEM_LO_DEFAULT),
            "strict": bool(conf.get("judge_strict")),
            "kind": ("fda" if conf.get("candidate_source") == "fda_match_cache"
                     else "curated"),
        })
    # Custom semantic pills (embedding blob stored at creation)
    if USERS_DB.exists():
        try:
            con = sqlite3.connect(str(USERS_DB))
            con.row_factory = sqlite3.Row
            for row in con.execute(
                "SELECT id, description_text, similarity_threshold FROM custom_pills "
                "WHERE pill_type = 'semantic' AND description_text IS NOT NULL"
            ).fetchall():
                pills.append({
                    "key": f"custom_{row['id']}",
                    "description": row["description_text"],
                    "neg_description": None,
                    "hi": row["similarity_threshold"] or 0.55,
                    "lo": None,  # pure semantic — no keyword path
                    "kind": "custom",
                })
            con.close()
        except Exception:
            log.exception("failed to load custom semantic pills (non-fatal)")
    return pills


# ---------------------------------------------------------------------------
# Pill vector cache
# ---------------------------------------------------------------------------

def _desc_hash(desc: str) -> str:
    return hashlib.sha1(desc.encode("utf-8")).hexdigest()


def get_pill_vectors(pills: list[dict]) -> dict[str, np.ndarray]:
    """Return {vec_key: unit vector}. vec_key is '<pill_key>' for the positive
    description and '<pill_key>__neg' for the anti-query. Re-embeds only when
    a description's hash changed."""
    try:
        from .embedder import embed_query
    except ImportError:
        from pipeline.embedder import embed_query

    cached: dict[str, np.ndarray] = {}
    meta: dict[str, str] = {}
    if VEC_CACHE.exists() and VEC_META.exists():
        try:
            npz = np.load(VEC_CACHE)
            cached = {k: npz[k] for k in npz.files}
            meta = json.loads(VEC_META.read_text())
        except Exception:
            cached, meta = {}, {}

    out: dict[str, np.ndarray] = {}
    new_meta: dict[str, str] = {}
    dirty = False
    wanted: list[tuple[str, str]] = []
    for p in pills:
        wanted.append((p["key"], p["description"]))
        if p.get("neg_description"):
            wanted.append((p["key"] + "__neg", p["neg_description"]))

    for key, desc in wanted:
        h = _desc_hash(desc)
        new_meta[key] = h
        if key in cached and meta.get(key) == h:
            out[key] = cached[key]
            continue
        log.info("embedding pill query: %s", key)
        vec = np.asarray(embed_query(desc), dtype="float32")
        vec = vec / (np.linalg.norm(vec) or 1.0)
        out[key] = vec
        dirty = True

    if dirty or set(new_meta) != set(meta):
        np.savez(VEC_CACHE, **out)
        VEC_META.write_text(json.dumps(new_meta, indent=1))
    return out


# ---------------------------------------------------------------------------
# Scoring + membership
# ---------------------------------------------------------------------------

def _score_matrix(vectors: np.ndarray, pill_vecs: dict[str, np.ndarray],
                  keys: list[str]) -> np.ndarray:
    """(n_articles, n_keys) cosine matrix. Article vectors are normalized here."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vectors / norms
    mat = np.stack([pill_vecs[k] for k in keys], axis=1)  # (768, n_keys)
    return unit @ mat


def _crawled_at_for(con, urls: list[str]) -> dict[str, int]:
    """url -> crawled_at via gal_recent (fast) then gal fallback."""
    out: dict[str, int] = {}
    for tbl in ("gal_recent", "gal"):
        missing = [u for u in urls if u not in out]
        if not missing:
            break
        for i in range(0, len(missing), IN_BATCH):
            chunk = missing[i:i + IN_BATCH]
            ph = ",".join(["?"] * len(chunk))
            try:
                for url, ts in con.execute(
                    f"SELECT url, max(crawled_at) FROM {tbl} WHERE url IN ({ph}) GROUP BY url",
                    chunk,
                ).fetchall():
                    out[url] = ts
            except Exception as e:
                # gal_recent may legitimately be absent on dev; anything else
                # must be VISIBLE — a swallowed error here once nulled an
                # entire backfill (0 dated -> 0 judged).
                log.warning("crawled_at lookup failed on %s (batch %d): %s", tbl, i, e)
                break
    return out


def _existing_tags(con, category: str, urls: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(urls), IN_BATCH):
        chunk = urls[i:i + IN_BATCH]
        ph = ",".join(["?"] * len(chunk))
        for (u,) in con.execute(
            f"SELECT DISTINCT article_id FROM article_tags "
            f"WHERE category = ? AND source_type='gal' AND article_id IN ({ph})",
            [category] + chunk,
        ).fetchall():
            found.add(u)
    return found


def _fda_candidates(con, urls: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(urls), IN_BATCH):
        chunk = urls[i:i + IN_BATCH]
        ph = ",".join(["?"] * len(chunk))
        for (u,) in con.execute(
            f"SELECT DISTINCT article_id FROM fda_match_cache "
            f"WHERE source_type='gal' AND match_type IN ('legal','contextual','stripped') "
            f"AND article_id IN ({ph})",
            chunk,
        ).fetchall():
            found.add(u)
    return found


# Semantic candidate net: loose on purpose — it only nominates keyword-less
# articles for the LLM judge; the judge is the precision layer.
SEM_NET = 0.66


def _titles_for(con, urls: list[str]) -> dict[str, tuple[str, str]]:
    """url -> (title, description) via gal_recent then gal."""
    out: dict[str, tuple[str, str]] = {}
    for tbl in ("gal_recent", "gal"):
        missing = [u for u in urls if u not in out]
        if not missing:
            break
        for i in range(0, len(missing), IN_BATCH):
            chunk = missing[i:i + IN_BATCH]
            ph = ",".join(["?"] * len(chunk))
            try:
                for u, t, d in con.execute(
                    f"SELECT url, any_value(title), any_value(description) "
                    f"FROM {tbl} WHERE url IN ({ph}) GROUP BY url",
                    chunk,
                ).fetchall():
                    if t and len(t.strip()) > 10:
                        out[u] = (t.strip(), (d or "").strip())
            except Exception as e:
                log.warning("title lookup failed on %s (batch %d): %s", tbl, i, e)
                break
    return out


def _open_read(retries: int = 30):
    """Read-only connection (coexists with the dashboard). Retries through
    write-lock windows; sets the Windows spill dir (db.py gotcha)."""
    import duckdb
    last = None
    for _ in range(retries):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            con.execute("SET threads = 2")
            con.execute("SET memory_limit = '2GB'")
            con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
            return con
        except duckdb.IOException as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not open read-only connection: {last}")


def stage_batch(con, urls: list[str], vectors: np.ndarray,
                pills: list[dict], pill_vecs: dict[str, np.ndarray],
                suffix: str = SUFFIX):
    """Judge-gated membership for one batch of articles — STAGES operations
    without writing. `con` must be a READ-ONLY connection: judge calls take
    minutes and holding the write lock that long 503s the live dashboard.

    Returns (insert_rows, deletes, counters): insert_rows are article_tags
    tuples; deletes are (live_category, url) pairs for judged-irrelevant
    keyword tags. Custom pills stay pure-cosine (user-tuned)."""
    try:
        from . import pill_judge
    except ImportError:
        from pipeline import pill_judge

    keys = [p["key"] for p in pills]
    scores = _score_matrix(vectors, pill_vecs, keys)
    col = {k: i for i, k in enumerate(keys)}
    idx_of = {u: i for i, u in enumerate(urls)}

    crawled = None   # lazy
    titles = None    # lazy
    inserts: list[tuple] = []
    deletes: list[tuple] = []
    counters = {"judged": 0}

    def _ensure_lookups(cand_urls):
        nonlocal crawled, titles
        if crawled is None:
            crawled = _crawled_at_for(con, urls)
        if titles is None:
            titles = _titles_for(con, cand_urls)
        else:
            missing = [u for u in cand_urls if u not in titles]
            if missing:
                titles.update(_titles_for(con, missing))

    fda_cand = None
    for p in pills:
        k = p["key"]
        s = scores[:, col[k]]

        # Custom pills: pure cosine at the user's threshold, no judge.
        if p["kind"] == "custom":
            already = _existing_tags(con, k, urls)
            member_idx = [i for i in np.nonzero(s >= p["hi"])[0]
                          if urls[i] not in already]
            if member_idx:
                if crawled is None:
                    crawled = _crawled_at_for(con, urls)
                inserts.extend(
                    (urls[i], "gal", k, "semantic", f"{s[i]:.3f}", crawled.get(urls[i]))
                    for i in member_idx if crawled.get(urls[i])
                )
            continue

        # Curated/FDA: gather candidates for the judge.
        target_cat = k + suffix
        if p["kind"] == "fda":
            if fda_cand is None:
                fda_cand = _fda_candidates(con, urls)
            cand = set(fda_cand)
            kw_tagged = set()
        else:
            kw_tagged = _existing_tags(con, k, urls)  # live keyword tags
            cand = set(kw_tagged)
            cand.update(urls[i] for i in np.nonzero(s >= SEM_NET)[0])

        already = _existing_tags(con, target_cat, urls)
        to_judge = sorted(cand - already)
        if not to_judge:
            continue
        _ensure_lookups(to_judge)
        items = [{"url": u, "title": titles[u][0], "desc": titles[u][1]}
                 for u in to_judge if u in titles]
        verdicts = pill_judge.judge(target_cat, items)
        if verdicts is None:
            continue  # gateway down: leave keyword tags as-is, no demotion
        counters["judged"] += len(verdicts)

        accept = {"relevant"} if p.get("strict") else pill_judge.ACCEPT
        approved = [u for u, v in verdicts.items() if v in accept]
        inserts.extend(
            (u, "gal", target_cat, "judge",
             f"{verdicts[u]}|{s[idx_of[u]]:.3f}" if u in idx_of else verdicts[u],
             crawled.get(u))
            for u in approved if crawled.get(u))

        # Demote judged-irrelevant keyword tags from the LIVE category so a
        # keyword false-positive shows for at most one cycle (post-flip,
        # suffix == "" makes live and target the same category).
        if suffix == "":
            deletes.extend((k, u) for u, v in verdicts.items()
                           if v == "irrelevant" and u in kw_tagged)
    return inserts, deletes, counters


# ---------------------------------------------------------------------------
# Incremental entry point (chained from embed_new_articles.py)
# ---------------------------------------------------------------------------

def score_new(suffix: str = SUFFIX) -> dict:
    t0 = time.time()
    pills = load_pill_defs()
    if not pills:
        return {"skipped": "no pills"}
    pill_vecs = get_pill_vectors(pills)

    try:
        next_row = int(WATERMARK.read_text().strip())
    except (OSError, ValueError):
        next_row = embedding_store.total_rows()  # first run: start from now

    # Phase 1: stage everything on a READ-ONLY connection (judge calls take
    # minutes — never hold the write lock through them).
    totals = {"inserted": 0, "demoted": 0, "articles": 0, "judged": 0}
    all_inserts: list[tuple] = []
    all_deletes: list[tuple] = []
    max_row = next_row - 1
    rcon = _open_read()
    try:
        for urls, vectors, last_row in embedding_store.iter_active_chunks(
                chunk_rows=50_000, min_row_index=next_row):
            ins, dels, c = stage_batch(rcon, urls, vectors, pills, pill_vecs, suffix=suffix)
            all_inserts.extend(ins)
            all_deletes.extend(dels)
            totals["judged"] += c["judged"]
            totals["articles"] += len(urls)
            max_row = last_row
    finally:
        rcon.close()

    # Phase 2: one brief write burst.
    if all_inserts or all_deletes:
        wcon = _open_connection(DB_PATH)
        try:
            for i in range(0, len(all_inserts), 1000):
                wcon.executemany(
                    "INSERT INTO article_tags (article_id, source_type, category, "
                    "matched_via, matched_detail, crawled_at) VALUES (?, ?, ?, ?, ?, ?)",
                    all_inserts[i:i + 1000],
                )
            by_cat: dict[str, list] = {}
            for cat, u in all_deletes:
                by_cat.setdefault(cat, []).append(u)
            for cat, del_urls in by_cat.items():
                for i in range(0, len(del_urls), IN_BATCH):
                    chunk = del_urls[i:i + IN_BATCH]
                    ph = ",".join(["?"] * len(chunk))
                    wcon.execute(
                        f"DELETE FROM article_tags WHERE category = ? AND source_type='gal' "
                        f"AND article_id IN ({ph})",
                        [cat] + chunk,
                    )
            wcon.execute("CHECKPOINT")
        finally:
            wcon.close()
        totals["inserted"] = len(all_inserts)
        totals["demoted"] = len(all_deletes)

    WATERMARK.write_text(str(max_row + 1))
    totals["elapsed_s"] = round(time.time() - t0, 1)
    log.info("pill_scorer: %s", totals)
    return totals
