# GDELT Monitor

Real-time global news intelligence dashboard. Continuously ingests the GDELT Project's news feed -- over 25 million articles from 44,000+ sources worldwide -- and surfaces signals that matter through curated topic filters, custom keyword monitoring, and semantic search.

Live at [gdeltmonitor.com](https://gdeltmonitor.com)

## Features

- **8 preset monitoring views**: Medical Device Companies (FDA matcher), Medical Devices, Supply Chain Alerts, Semiconductor & Chip Geopolitics, AI & Machine Learning, AI in Defense & Intelligence, AI Regulation & Policy, AI Sector Impact
- **Custom keyword pills**: Define your own keyword sets; the system builds an Aho-Corasick automaton and scans the full article corpus
- **Semantic pills**: Describe what you want to monitor in natural language; articles are matched by embedding similarity using nomic-embed-text
- **Semantic search**: FAISS-indexed vector search over 7M+ article embeddings (IVF-PQ, ~400MB index)
- **Dual data sources**: GDELT Global Article List (GAL) for broad coverage + Global Knowledge Graph (GKG) for entity-enriched analysis
- **15-minute update cycle**: New articles ingested, tagged, and searchable within minutes of publication
- **60-day rolling window**: Automatic pruning keeps storage bounded

## Architecture

```
GDELT feeds (every 15 min)
    |
    v
Downloader -> Parser -> DuckDB (25M+ articles)
    |
    +--> FDA Matcher (Aho-Corasick, 8500 companies)
    +--> Tagger (Aho-Corasick, 270+ keyword patterns)
    +--> Embedder (nomic-embed-text via Ollama)
    |
    v
Flask Dashboard (Waitress WSGI)
    |
    +--> Preset views (pre-computed tag/match caches)
    +--> Custom pills (per-user keyword or semantic filters)
    +--> Semantic search (FAISS IVF-PQ index)
    +--> Auth (Flask-Login, SQLite user store)
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Database | DuckDB 1.5+ (analytical queries) + SQLite (user data) |
| Pattern matching | Aho-Corasick via pyahocorasick |
| Embeddings | nomic-embed-text v1.5 via Ollama |
| Vector search | FAISS (IVF-PQ index) |
| Web framework | Flask + Waitress |
| Data source | GDELT Project v2 (GKG) + v3 (GAL) |

## Setup

```bash
# Clone and install
git clone https://github.com/sidscorp/gdelt-events.git
cd gdelt-events
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Initial data download (takes ~30 min)
python gdelt_ingest.py

# Start the dashboard
python dashboard/serve.py
# -> http://localhost:8015

# Optional: set up Ollama for semantic features
# Install Ollama, pull nomic-embed-text:v1.5
# Set OLLAMA_URL env var if not localhost
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434/api/embed` | Ollama embedding API endpoint |

## Project Structure

```
pipeline/           # Data ingestion and processing
  config.py         # GDELT feed URLs, retention settings
  downloader.py     # GDELT v2 file downloader
  gal_downloader.py # GAL feed downloader
  loader.py         # CSV parser -> DuckDB loader
  schema.py         # DuckDB table definitions
  tagger.py         # Aho-Corasick keyword classifier
  fda_matcher.py    # FDA company name matcher
  pruner.py         # 60-day retention pruner
  embedder.py       # Ollama embedding client
  embedding_store.py # Vector storage (manifest + binary)
  build_faiss_index.py # FAISS IVF-PQ index builder

dashboard/          # Flask web application
  app.py            # API endpoints and query routing
  serve.py          # Waitress WSGI server
  models.py         # SQLite user/pill models
  views.py          # Preset view registry
  auth.py           # Flask-Login integration
  pill_worker.py    # Background pill backfill daemon
  semantic_search.py # FAISS search backend
  templates/        # Jinja2 HTML templates
  static/           # Favicon, assets

tests/              # Test suite
  smoke.sh          # 20 curl-based endpoint checks
  test_queries.py   # Parametrized query tests
  golden_queries.json # Query corpus with assertions
```

## License

MIT
