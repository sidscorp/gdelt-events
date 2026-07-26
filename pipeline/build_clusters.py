"""Incremental materialized event clustering.

Rolls near-duplicate / same-event articles into persistent clusters with
stable IDs, so the dashboard can collapse them into one card and link to a
shareable /event/<id> page.

Approach (leader clustering, no chaining):
  - Process new embedded English GAL rows in crawled_at order (watermark).
  - For each article: FAISS top-K candidate neighbors, then EXACT-cosine
    re-rank against stored full-precision vectors (FAISS IVF-PQ is lossy and
    used only for candidate generation).
  - Join the best existing cluster whose centroid cosine >= threshold and
    whose time window/lifespan guards pass; else promote the best singleton
    neighbor into a new 2-article cluster; else leave as an implicit singleton.
  - Centroid = running mean of member unit vectors. Representative chosen by
    source authority -> has-image -> longest description -> earliest published.

Only multi-article clusters (size >= 2) are materialized. Cluster rows persist
indefinitely (members_json snapshot) so /event/<id> links survive pruning.

Writes are guarded by a PID lock and the DuckDB single-writer discipline.

Usage (prod venv python):
    python build_clusters.py                 # incremental from watermark
    python build_clusters.py --reset         # drop + rebuild from scratch
    python build_clusters.py --max-articles N # cap (debugging)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import embedding_store  # noqa: E402
import cluster_schema  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_clusters")

import duckdb  # noqa: E402

# --- Tunables ----------------------------------------------------------------
EVENT_THRESHOLD = 0.86      # exact cosine to join a cluster / form a pair
FAISS_K = 24                # candidate neighbors per article
FAISS_NPROBE = 64
WINDOW_HOURS = 72           # join only if within this of cluster latest_seen
MAX_SPAN_HOURS = 168        # a cluster's first->last span cap (7 days)
STALE_MINUTES = 60          # skip not-yet-embedded rows older than this; else wait
MEMBERS_CAP = 400           # cap members_json entries (size counter still counts all)
FLUSH_EVERY = 3000          # flush to DB every N processed articles
CHUNK = 20_000              # gal cursor page size
LOCK_FILE = config.DATA_DIR / ".cluster.lock"

# Source authority tiers for representative selection.
TIER1 = {
    "reuters.com", "apnews.com", "bloomberg.com", "nytimes.com", "wsj.com",
    "washingtonpost.com", "bbc.com", "bbc.co.uk", "theguardian.com", "ft.com",
    "cnn.com", "npr.org", "aljazeera.com", "abcnews.go.com", "cnbc.com",
    "politico.com", "axios.com", "economist.com", "apnews.com",
}
TIER2 = {
    "forbes.com", "businessinsider.com", "thehill.com", "usatoday.com",
    "latimes.com", "time.com", "newsweek.com", "fortune.com", "nbcnews.com",
    "cbsnews.com", "theverge.com", "techcrunch.com", "arstechnica.com",
}

_OUTLET_SUFFIX = re.compile(r"\s*[\|\-–—:]\s*[^|\-–—:]{1,40}$")
_NONWORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

# Bot-wall / paywall / error interstitial titles — skip so they don't form
# junk clusters (and would just be noise as singletons too).
_JUNK_TITLES = (
    "client challenge", "just a moment", "are you a robot", "access denied",
    "attention required", "403 forbidden", "page not found", "404 not found",
    "bot verification", "verifying you are human", "one moment please",
    "robot or human", "please verify you are a human", "security check",
    "access to this page has been denied", "site maintenance", "are you human",
)


def is_junk_title(title):
    if not title:
        return True
    t = _WS.sub(" ", title.lower()).strip()
    if len(t) < 6:
        return True
    return any(j in t for j in _JUNK_TITLES)


# --- Small helpers -----------------------------------------------------------
def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Clear a stale lock (process no longer alive / corrupt file).
    if LOCK_FILE.exists():
        try:
            if _pid_alive(int(LOCK_FILE.read_text().strip())):
                return None
            LOCK_FILE.unlink()
        except (ValueError, OSError):
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
    # Atomic create-if-absent: if two runs race, only one wins the O_EXCL open.
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return LOCK_FILE


def release_lock(handle):
    if handle and handle.exists():
        try:
            handle.unlink()
        except OSError:
            pass


def _to_dt(ts):
    s = str(int(ts)).zfill(14)
    return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                    int(s[8:10]), int(s[10:12]), int(s[12:14]))


def hours_between(a, b):
    return abs((_to_dt(a) - _to_dt(b)).total_seconds()) / 3600.0


def now_ts():
    return int(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))


def age_minutes(ts, now):
    return (_to_dt(now) - _to_dt(ts)).total_seconds() / 60.0


def title_fp(title):
    if not title:
        return None
    t = _OUTLET_SUFFIX.sub("", title.lower())
    t = _WS.sub(" ", _NONWORD.sub(" ", t)).strip()
    if len(t) < 8:
        t = _WS.sub(" ", title.lower()).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16] if t else None


def unit(v):
    v = np.asarray(v, dtype="float32")
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def authority(domain):
    d = (domain or "").lower()
    if d in TIER1:
        return 3
    if d in TIER2:
        return 2
    return 1


def rep_score(m):
    # higher is better; earliest published wins so negate the timestamp
    pub = m.get("published_at") or m.get("crawled_at") or 0
    return (authority(m.get("domain")), 1 if m.get("image") else 0,
            m.get("desclen", 0), -int(pub))


# --- Main clustering ---------------------------------------------------------
class Clusterer:
    def __init__(self, read_con, write_con):
        # read_con: the (read-only) source DB for gal + existing clusters.
        # write_con: the scratch DB where new/updated clusters accumulate.
        # Keeping reads read-only lets the live dashboard keep serving; only a
        # brief final merge writes the production DB.
        self.read_con = read_con
        self.write_con = write_con
        self.store = embedding_store
        # url -> row_index (full-precision vector lookup)
        self.manifest = {}
        # FAISS
        self.index = None
        self.faiss_urls = None
        # in-memory recent clusters
        self.clusters = {}      # cid -> dict(centroid_sum, size, first_seen, latest_seen,
                                #             title_fp, rep_url, title, image, members=list)
        self.url2cid = {}       # url -> cid (members of loaded/created clusters)
        self.fp_index = {}      # title_fp -> cid (recent)
        self.dirty = set()      # cids needing flush
        self.pending_members = []  # (url, cid, sim, crawled_at)
        self._vfile = None      # open handle to vectors.bin (fast repeated seeks)
        self._row_bytes = embedding_store.ROW_BYTES

    # ---- loading ----
    def load_manifest(self):
        mcon = self.store._conn()
        rows = mcon.execute(
            "SELECT url, row_index FROM embeddings WHERE status='active'"
        ).fetchall()
        mcon.close()
        self.manifest = {r[0]: r[1] for r in rows}
        log.info("manifest: %d active vectors", len(self.manifest))

    def open_vectors(self):
        self._vfile = open(self.store.BASE_DIR / "vectors.bin", "rb")

    def close(self):
        if self._vfile:
            try:
                self._vfile.close()
            except OSError:
                pass

    def load_faiss(self):
        import faiss
        idx_path = self.store.BASE_DIR / "articles.faiss"
        urls_path = self.store.BASE_DIR / "index_urls.txt"
        self.index = faiss.read_index(str(idx_path))
        try:
            self.index.nprobe = FAISS_NPROBE
        except Exception:
            pass
        with open(urls_path, encoding="utf-8") as f:
            self.faiss_urls = [ln.rstrip("\n") for ln in f]
        log.info("faiss: %d vectors", self.index.ntotal)

    def load_recent_clusters(self, now):
        # Load active clusters whose latest_seen is recent enough to still
        # accept new members (within WINDOW_HOURS of now).
        rows = self.read_con.execute(
            "SELECT cluster_id, centroid, size, first_seen, latest_seen, title_fp, "
            "rep_url, title, image, members_json FROM clusters WHERE status='active'"
        ).fetchall()
        loaded = 0
        for (cid, centroid, size, first_seen, latest_seen, fp, rep_url, title, image, mjson) in rows:
            if latest_seen and hours_between(now, latest_seen) > WINDOW_HOURS:
                continue  # too old to accept new members
            members = json.loads(mjson) if mjson else []
            cmean = np.frombuffer(centroid, dtype="float32").copy() if centroid else np.zeros(768, dtype="float32")
            self.clusters[cid] = {
                "centroid_sum": cmean * size,  # reconstruct running sum from stored mean
                "size": size,
                "first_seen": first_seen,
                "latest_seen": latest_seen,
                "title_fp": fp,
                "rep_url": rep_url, "title": title, "image": image,
                "members": members,
            }
            for m in members:
                self.url2cid[m["url"]] = cid
            if fp:
                self.fp_index[fp] = cid
            loaded += 1
        log.info("loaded %d recent clusters into memory", loaded)

    # ---- vector access ----
    def vec(self, url):
        ri = self.manifest.get(url)
        if ri is None:
            return None
        self._vfile.seek(ri * self._row_bytes)
        data = self._vfile.read(self._row_bytes)
        if len(data) != self._row_bytes:
            return None
        return unit(np.frombuffer(data, dtype="float32"))

    def faiss_search(self, uvec):
        q = np.asarray([uvec], dtype="float32")
        scores, idxs = self.index.search(q, FAISS_K)
        out = []
        for j in range(len(idxs[0])):
            i = int(idxs[0][j])
            if 0 <= i < len(self.faiss_urls):
                out.append((self.faiss_urls[i], float(scores[0][j])))
        return out

    def gal_meta(self, url):
        r = self.read_con.execute(
            "SELECT url, crawled_at, published_at, title, description, outlet_name, domain, image "
            "FROM gal WHERE url = ? LIMIT 1", [url]
        ).fetchone()
        if not r:
            return None
        return self._meta_from_row(r)

    @staticmethod
    def _meta_from_row(r):
        url, crawled_at, published_at, title, description, outlet, domain, image = r
        desc = (description or "")[:300]
        return {
            "url": url, "crawled_at": crawled_at, "published_at": published_at,
            "title": title, "outlet": outlet, "domain": domain, "image": image,
            "desc": desc, "desclen": len(description or ""),
        }

    # ---- cluster mutation ----
    def _recompute_rep(self, cl):
        best = max(cl["members"], key=rep_score)
        cl["rep_url"] = best["url"]
        cl["title"] = best["title"]
        cl["image"] = best.get("image")

    def join(self, cid, a_meta, uA, sim):
        cl = self.clusters[cid]
        cl["centroid_sum"] = cl["centroid_sum"] + uA
        cl["size"] += 1
        cl["latest_seen"] = max(cl["latest_seen"], a_meta["crawled_at"])
        cl["first_seen"] = min(cl["first_seen"], a_meta["crawled_at"])
        if len(cl["members"]) < MEMBERS_CAP:
            cl["members"].append(a_meta)
        self._recompute_rep(cl)
        self.url2cid[a_meta["url"]] = cid
        self.pending_members.append((a_meta["url"], cid, float(sim), a_meta["crawled_at"]))
        self.dirty.add(cid)

    def new_cluster(self, b_meta, uB, a_meta, uA, sim):
        seed_url = b_meta["url"]
        cid = "c" + hashlib.sha1(seed_url.encode("utf-8")).hexdigest()[:15]
        fp = title_fp(b_meta["title"]) or title_fp(a_meta["title"])
        cl = {
            "centroid_sum": uB + uA,
            "size": 2,
            "first_seen": min(b_meta["crawled_at"], a_meta["crawled_at"]),
            "latest_seen": max(b_meta["crawled_at"], a_meta["crawled_at"]),
            "title_fp": fp,
            "members": [b_meta, a_meta],
            "rep_url": None, "title": None, "image": None,
        }
        self._recompute_rep(cl)
        self.clusters[cid] = cl
        self.url2cid[b_meta["url"]] = cid
        self.url2cid[a_meta["url"]] = cid
        if fp:
            self.fp_index[fp] = cid
        self.pending_members.append((b_meta["url"], cid, float(sim), b_meta["crawled_at"]))
        self.pending_members.append((a_meta["url"], cid, float(sim), a_meta["crawled_at"]))
        self.dirty.add(cid)

    # ---- assignment ----
    def assign(self, a_meta, uA):
        a_crawled = a_meta["crawled_at"]
        cands = self.faiss_search(uA)

        # Tier A: exact-fingerprint shortcut to a recent cluster
        fp = title_fp(a_meta["title"])
        if fp and fp in self.fp_index:
            cid = self.fp_index[fp]
            cl = self.clusters.get(cid)
            if cl and hours_between(a_crawled, cl["latest_seen"]) <= WINDOW_HOURS \
                    and hours_between(a_crawled, cl["first_seen"]) <= MAX_SPAN_HOURS:
                cmean = unit(cl["centroid_sum"])
                self.join(cid, a_meta, uA, float(np.dot(uA, cmean)))
                return

        # Tier B: exact-rerank clustered candidates
        best_cid, best_c = None, EVENT_THRESHOLD
        for (curl, _score) in cands:
            if curl == a_meta["url"]:
                continue
            cid = self.url2cid.get(curl)
            if cid is None:
                continue
            cl = self.clusters.get(cid)
            if cl is None:
                continue  # evicted / too old
            if hours_between(a_crawled, cl["latest_seen"]) > WINDOW_HOURS:
                continue
            if hours_between(a_crawled, cl["first_seen"]) > MAX_SPAN_HOURS:
                continue
            c = float(np.dot(uA, unit(cl["centroid_sum"])))
            if c >= best_c:
                best_cid, best_c = cid, c
        if best_cid is not None:
            self.join(best_cid, a_meta, uA, best_c)
            return

        # Promote the best singleton neighbor into a new 2-cluster
        for (curl, _score) in cands:
            if curl == a_meta["url"] or curl in self.url2cid:
                continue
            uB = self.vec(curl)
            if uB is None:
                continue
            c = float(np.dot(uA, uB))
            if c < EVENT_THRESHOLD:
                break  # candidates sorted desc; no closer singleton
            b_meta = self.gal_meta(curl)
            if not b_meta or b_meta.get("crawled_at") is None:
                continue
            if hours_between(a_crawled, b_meta["crawled_at"]) > WINDOW_HOURS:
                continue
            self.new_cluster(b_meta, uB, a_meta, uA, c)
            return
        # else: implicit singleton (no row)

    # ---- flush ----
    def flush(self, watermark):
        if self.dirty:
            rows = []
            for cid in self.dirty:
                cl = self.clusters[cid]
                cmean = (cl["centroid_sum"] / cl["size"]).astype("float32")
                rows.append((
                    cid, cl["rep_url"], cl["title"], cl["image"],
                    cmean.tobytes(), cl["title_fp"], cl["size"],
                    cl["first_seen"], cl["latest_seen"],
                    json.dumps(cl["members"][:MEMBERS_CAP]),
                ))
            self.write_con.executemany(
                "INSERT OR REPLACE INTO clusters "
                "(cluster_id, rep_url, title, image, centroid, title_fp, size, "
                " first_seen, latest_seen, members_json, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', current_timestamp)",
                rows,
            )
            self.dirty.clear()
        if self.pending_members:
            self.write_con.executemany(
                "INSERT OR REPLACE INTO cluster_members "
                "(article_url, cluster_id, similarity, added_at) VALUES (?, ?, ?, ?)",
                self.pending_members,
            )
            self.pending_members.clear()
        self.write_con.execute("DELETE FROM cluster_state")
        self.write_con.execute(
            "INSERT INTO cluster_state (id, last_clustered_at, updated_at) "
            "VALUES (1, ?, current_timestamp)", [watermark],
        )


# Per-process scratch file so two runs (e.g. a manual backfill overlapping a
# scheduled run) never corrupt each other's staging DB.
SCRATCH_PATH = config.DATA_DIR / f".cluster_scratch_{os.getpid()}.duckdb"


def _tmp_dir():
    d = config.DATA_DIR / "duckdb_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d.as_posix()


def _connect_write(path, retries=40):
    """Open a read-write DuckDB connection, retrying on lock conflicts (the
    production merge briefly contends with the dashboard's read connections,
    same as the ingest job)."""
    last = None
    for i in range(retries):
        try:
            con = duckdb.connect(str(path))
            con.execute("SET threads = 4")
            con.execute("SET memory_limit = '10GB'")
            con.execute(f"SET temp_directory='{_tmp_dir()}'")
            return con
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not open write connection to {path}: {last}")


def _cleanup_scratch():
    for ext in ("", ".wal"):
        p = Path(str(SCRATCH_PATH) + ext)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def _merge_scratch_to_prod():
    """The ONLY production write: upsert this run's clusters/members from the
    scratch DB into prod and advance the watermark. Brief, so the dashboard's
    read-connection retries absorb it (the long scan was fully read-only)."""
    pcon = _connect_write(config.DB_PATH)
    try:
        cluster_schema.create_cluster_tables(pcon)
        pcon.execute(f"ATTACH '{SCRATCH_PATH.as_posix()}' AS scr (READ_ONLY)")
        nclu = pcon.execute("SELECT count(*) FROM scr.clusters").fetchone()[0]
        nmem = pcon.execute("SELECT count(*) FROM scr.cluster_members").fetchone()[0]
        if nclu:
            pcon.execute("INSERT OR REPLACE INTO clusters SELECT * FROM scr.clusters")
        if nmem:
            pcon.execute("INSERT OR REPLACE INTO cluster_members SELECT * FROM scr.cluster_members")
        pcon.execute("DELETE FROM cluster_state")
        pcon.execute("INSERT INTO cluster_state SELECT * FROM scr.cluster_state")
        pcon.execute("CHECKPOINT")
        pcon.execute("DETACH scr")
        log.info("merged %d clusters / %d members into prod", nclu, nmem)
    finally:
        pcon.close()


def run(max_articles=None):
    lock = acquire_lock()
    if not lock:
        log.warning("another build_clusters is running; exiting")
        return
    t0 = time.time()
    _cleanup_scratch()
    # Read production strictly read-only (coexists with the live dashboard);
    # all writes go to a private scratch DB and are merged in at the end.
    read_con = duckdb.connect(str(config.DB_PATH), read_only=True)
    read_con.execute("SET threads = 4")
    read_con.execute("SET memory_limit = '10GB'")
    read_con.execute(f"SET temp_directory='{_tmp_dir()}'")
    write_con = _connect_write(SCRATCH_PATH)
    try:
        cluster_schema.create_cluster_tables(write_con)

        # Watermark + existing recent clusters come from PROD (read-only).
        try:
            wm_row = read_con.execute("SELECT last_clustered_at FROM cluster_state WHERE id=1").fetchone()
        except Exception:
            wm_row = None
        watermark = (wm_row[0] if wm_row else 0) or 0
        now = now_ts()
        log.info("watermark=%s now=%s (read-only scan -> scratch)", watermark, now)

        cl = Clusterer(read_con, write_con)
        cl.load_manifest()
        cl.load_faiss()
        cl.open_vectors()
        cl.load_recent_clusters(now)

        processed = matched = 0
        new_wm = watermark
        cursor = watermark
        stop = False
        while not stop:
            page = read_con.execute(
                "SELECT url, crawled_at, published_at, title, description, outlet_name, domain, image "
                "FROM gal WHERE language='en' AND crawled_at > ? "
                "ORDER BY crawled_at LIMIT ?",
                [cursor, CHUNK],
            ).fetchall()
            if not page:
                break
            for r in page:
                url = r[0]
                crawled = r[1]
                cursor = crawled
                if url in cl.url2cid:
                    new_wm = max(new_wm, crawled)  # already clustered (e.g. pulled in early)
                    continue
                ri = cl.manifest.get(url)
                if ri is None:
                    # not embedded yet
                    if age_minutes(crawled, now) > STALE_MINUTES:
                        new_wm = max(new_wm, crawled)
                        continue
                    stop = True  # recent + unembedded: wait for embeddings next run
                    break
                if is_junk_title(r[3]):  # r[3] = title
                    new_wm = max(new_wm, crawled)
                    continue
                uA = cl.vec(url)
                if uA is None:
                    new_wm = max(new_wm, crawled)
                    continue
                a_meta = Clusterer._meta_from_row(r)
                before = len(cl.url2cid)
                cl.assign(a_meta, uA)
                if len(cl.url2cid) > before:
                    matched += 1
                processed += 1
                new_wm = max(new_wm, crawled)
                if processed % FLUSH_EVERY == 0:
                    cl.flush(new_wm)
                    log.info("processed=%d matched=%d clusters=%d", processed, matched, len(cl.clusters))
                if max_articles and processed >= max_articles:
                    stop = True
                    break
            if len(page) < CHUNK:
                break

        n_clusters = len(cl.clusters)
        cl.flush(new_wm)
        cl.close()
        write_con.close()
        write_con = None
        # Close the read-only prod connection BEFORE the merge: one process
        # cannot hold read-only + read-write to the same file concurrently.
        read_con.close()
        read_con = None
        # Brief production write (the scan above held no prod write lock).
        _merge_scratch_to_prod()
        log.info("DONE processed=%d matched(into clusters)=%d total_clusters=%d watermark=%s elapsed=%.0fs",
                 processed, matched, n_clusters, new_wm, time.time() - t0)
    finally:
        if read_con is not None:
            try:
                read_con.close()
            except Exception:
                pass
        if write_con is not None:
            try:
                write_con.close()
            except Exception:
                pass
        _cleanup_scratch()
        release_lock(lock)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-articles", type=int, default=None)
    args = ap.parse_args()
    run(max_articles=args.max_articles)


if __name__ == "__main__":
    main()
