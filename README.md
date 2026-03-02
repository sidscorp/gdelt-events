# gdelt-events

CLI toolkit for searching global news via the [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). No API key required.

## Setup

```
pip install -r requirements.txt
```

## Usage

### Article search (default)

```bash
python search.py "surgical mask"
python search.py "pacemaker recall" --days 30
python search.py "insulin pump" --country "China, India"
python search.py "medtronic" --source US --limit 50
python search.py "stent" --domain reuters.com
```

### Tone/sentiment analysis

```bash
python search.py "medical device" --mode tone --timespan 30d
```

### Coverage volume timeline

```bash
python search.py "medical device" --mode timeline --timespan 30d
```

### Other options

```bash
# Flexible timespans: 7d, 2w, 1m, 24h, 15min
python search.py "FDA recall" --timespan 2w

# Sort: relevance (default), date, date-asc, tone-asc, tone-desc
python search.py "medical device" --sort date

# JSON output
python search.py "test" --json

# CSV export (articles mode)
python search.py "FDA recall" --csv results.csv

# Boolean operators are auto-normalized
python search.py "recall or warning"
# -> Normalized: recall or warning -> (recall OR warning)
```

## Files

| File | Purpose |
|------|---------|
| `search.py` | CLI entry point |
| `gdelt_client.py` | API client library (no display logic) |
| `gdelt_explorer.ipynb` | Jupyter notebook for exploration |
| `gdelt-api-reference.md` | GDELT DOC API reference notes |
