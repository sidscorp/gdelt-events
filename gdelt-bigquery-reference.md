# GDELT BigQuery Reference

GDELT's full dataset is publicly available in Google BigQuery under `gdelt-bq`. Free tier: 1 TB/month of queries, no billing required.

## Setup

1. Create a free GCP project at [console.cloud.google.com](https://console.cloud.google.com)
2. Use the [BigQuery console](https://console.cloud.google.com/bigquery) directly, or from Python:

```bash
pip install google-cloud-bigquery pyarrow
gcloud auth application-default login
```

## Tables

Always prefer `_partitioned` tables with `_PARTITIONTIME` filters to control costs.

| Table | Description | Size |
|-------|-------------|------|
| `gdelt-bq.gdeltv2.events_partitioned` | Events, 2015-present (updated every 15 min) | ~63 GB |
| `gdelt-bq.gdeltv2.eventmentions_partitioned` | Event mentions | ~104 GB |
| `gdelt-bq.gdeltv2.gkg_partitioned` | Global Knowledge Graph (themes, people, orgs) | ~3.6 TB |
| `gdelt-bq.full.events` | GDELT 1.0 events, 1979-present | ~144 GB |
| `gdelt-bq.full.crosswalk_geocountrycodetohuman` | FIPS country code lookup | tiny |

**Cost:** $6.25/TB after the free tier. The GKG table unfiltered costs ~$22.50 per scan — always filter by partition.

## Key Fields (Events Table)

| Field | Description |
|-------|-------------|
| `SQLDATE` | Date as integer `YYYYMMDD` — convert with `PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING))` |
| `Actor1Name`, `Actor2Name` | Actors involved |
| `Actor1CountryCode`, `Actor2CountryCode` | FIPS country codes |
| `EventCode` / `EventRootCode` | CAMEO event code (e.g. `14` = protest) |
| `QuadClass` | 1=Verbal Coop, 2=Material Coop, 3=Verbal Conflict, 4=Material Conflict |
| `GoldsteinScale` | -10 (conflict) to +10 (cooperation) |
| `AvgTone` | Sentiment, -100 to +100 |
| `NumMentions`, `NumArticles` | Coverage volume |
| `ActionGeo_CountryCode`, `ActionGeo_FullName` | Where the event occurred |
| `ActionGeo_Lat`, `ActionGeo_Long` | Event coordinates |
| `SOURCEURL` | Originating article URL |

## Example Queries

### Events by country for a month

```sql
SELECT
  ActionGeo_CountryCode AS country,
  COUNT(*) AS event_count,
  AVG(GoldsteinScale) AS avg_goldstein,
  AVG(AvgTone) AS avg_tone
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP("2026-02-01")
  AND _PARTITIONTIME < TIMESTAMP("2026-03-01")
  AND ActionGeo_CountryCode IS NOT NULL
GROUP BY country
ORDER BY event_count DESC
LIMIT 25
```

### Filter by event type (protests)

```sql
SELECT
  PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING)) AS event_date,
  Actor1Name,
  ActionGeo_FullName,
  NumMentions,
  AvgTone,
  SOURCEURL
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP("2026-02-01")
  AND _PARTITIONTIME < TIMESTAMP("2026-03-01")
  AND EventRootCode = '14'
ORDER BY NumMentions DESC
LIMIT 100
```

### Conflict events timeline

```sql
SELECT
  PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING)) AS event_date,
  COUNT(*) AS conflict_events,
  SUM(NumArticles) AS total_articles,
  AVG(GoldsteinScale) AS avg_goldstein
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP("2026-01-01")
  AND _PARTITIONTIME < TIMESTAMP("2026-04-01")
  AND QuadClass = 4
GROUP BY event_date
ORDER BY event_date
```

### Events involving a specific country

```sql
SELECT
  SQLDATE, Actor1Name, Actor2Name,
  EventCode, GoldsteinScale, AvgTone,
  NumMentions, SOURCEURL
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP("2026-02-01")
  AND _PARTITIONTIME < TIMESTAMP("2026-03-01")
  AND (Actor1CountryCode = 'IND' OR Actor2CountryCode = 'IND')
ORDER BY NumMentions DESC
LIMIT 100
```

### Python client

```python
from google.cloud import bigquery

client = bigquery.Client(project="your-gcp-project-id")
query = """
SELECT PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING)) AS event_date,
       COUNT(*) AS events, AVG(AvgTone) AS avg_tone
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP("2026-02-01")
  AND _PARTITIONTIME < TIMESTAMP("2026-03-01")
GROUP BY event_date ORDER BY event_date
"""
df = client.query(query).to_dataframe()
```

## Gotchas

- **Country codes are FIPS, not ISO** — Iraq is `IZ` not `IQ`. Use `crosswalk_geocountrycodetohuman` for lookups.
- **`SQLDATE` is an integer**, not a date — always cast it.
- **GKG is huge** (3.6 TB) — never query without `_PARTITIONTIME` filters.
- GDELT blog examples use legacy SQL syntax (`[square.brackets]`). Standard SQL uses backticks.

## Documentation

- [GDELT BigQuery Demos](https://blog.gdeltproject.org/a-compilation-of-gdelt-bigquery-demos/) — tutorial index
- [Partitioned Tables Guide](https://blog.gdeltproject.org/announcing-partitioned-gdelt-bigquery-tables/) — cost control
- [Event Codebook V2.0 (PDF)](http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf) — full schema
- [CAMEO Manual (PDF)](http://data.gdeltproject.org/documentation/CAMEO.Manual.1.1b3.pdf) — event codes
