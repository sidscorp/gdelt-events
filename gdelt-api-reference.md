# GDELT API Reference

Comprehensive reference for the GDELT 2.0 public REST APIs. These are free, unauthenticated endpoints
that search a rolling window of global news coverage updated every 15 minutes.

Source: [GDELT DOC 2.0 API Debuts](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)

---

## Table of Contents

1. [API Overview](#api-overview)
2. [DOC API — Full-Text Article Search](#doc-api)
   - [Endpoint & URL Structure](#endpoint--url-structure)
   - [Query Operators](#query-operators)
   - [Output Modes](#output-modes)
   - [Output Formats](#output-formats)
   - [Time Parameters](#time-parameters)
   - [Sorting](#sorting)
   - [Other Parameters](#other-parameters)
   - [JSON Response Structures](#json-response-structures)
3. [Context API — Sentence-Level Search](#context-api)
4. [GEO API — Geographic Visualization](#geo-api)
5. [TV API — Television Monitoring](#tv-api)
6. [Rate Limits & Practical Constraints](#rate-limits--practical-constraints)
7. [Medical Device Search Strategy](#medical-device-search-strategy)
8. [GKG Themes Reference](#gkg-themes-reference)
9. [Example Queries](#example-queries)

---

## API Overview

| API | Endpoint | Searches | Time Window | Best For |
|-----|----------|----------|-------------|----------|
| **DOC** | `/api/v2/doc/doc` | Full article text | Rolling 3 months | Article discovery, timelines, tone analysis |
| **Context** | `/api/v2/context/context` | Individual sentences | Past 72 hours | Finding exact quotes, precise context |
| **GEO** | `/api/v2/geo/geo` | Full article text | Rolling 7 days | Geographic mapping of coverage |
| **TV** | `/api/v2/tv/tv` | TV closed captions | July 2009–present | Television broadcast monitoring |

All APIs:
- Base URL: `https://api.gdeltproject.org`
- Free, no API key required
- CORS enabled (embeddable via iframe)
- Rate limit: **1 request per 5 seconds** (or contact kalev.leetaru5@gmail.com for higher)

---

## DOC API

The primary API for article search. Searches full-text content of articles across 65 machine-translated
languages. All search terms must appear somewhere in the article body (not just headlines).

### Endpoint & URL Structure

```
https://api.gdeltproject.org/api/v2/doc/doc?query=QUERY&mode=MODE&format=FORMAT&timespan=TIMESPAN&maxrecords=N&sort=SORT
```

Everything after `query=` is a single string with operators embedded inline (space-separated).

### Query Operators

#### Text Search

| Operator | Description | Example |
|----------|-------------|---------|
| `keyword` | Words separated by spaces = implicit AND. All must appear in article. | `medical device recall` |
| `"exact phrase"` | Exact phrase match (no stemming — "regulate" won't match "regulation") | `"pacemaker recall"` |
| `(a OR b OR c)` | Boolean OR — at least one must match. Must use capitalized OR. | `(medtronic OR "boston scientific" OR stryker)` |
| `-term` | Exclude articles containing this term | `-politics` |
| `near#:"a b"` | Proximity — words must appear within N words of each other | `near10:"FDA recall"` |
| `repeat#:"term"` | Term must appear at least N times in the article | `repeat3:"medical device"` |

**Important**: Boolean OR cannot be nested. You can combine AND + OR like:
```
"medical device" (recall OR warning OR alert) sourcelang:english
```
This means: article must contain "medical device" AND at least one of (recall, warning, alert).

#### Metadata Filters

All filters are embedded in the `query` parameter, not as separate URL params.

| Operator | Description | Example |
|----------|-------------|---------|
| `sourcelang:CODE` | Article's original language (even if machine-translated to English) | `sourcelang:english` |
| `sourcecountry:NAME` | Country where the news outlet is based. Spaces removed, lowercase. | `sourcecountry:unitedstates` |
| `domain:DOMAIN` | Partial domain match (cnn.com also matches subdomain.cnn.com) | `domain:reuters.com` |
| `domainis:DOMAIN` | Exact domain match only | `domainis:fda.gov` |
| `theme:THEME` | GKG thematic category (see [GKG Themes](#gkg-themes-reference)) | `theme:HEALTH_PANDEMIC` |
| `tone<N` or `tone>N` | Filter by sentiment score (negative = critical, positive = favorable) | `tone<-5` |
| `toneabs>N` | Filter by emotional intensity regardless of positive/negative | `toneabs>10` |

#### Image Operators (Visual GKG)

These filter based on Google Cloud Vision deep learning analysis of article images.

| Operator | Description | Example |
|----------|-------------|---------|
| `imagetag:"label"` | Object/activity recognition (10,000+ labels) | `imagetag:"hospital"` |
| `imageocrmeta:"text"` | OCR text extracted from images, metadata, captions | `imageocrmeta:"FDA"` |
| `imagefacetone<N` | Facial emotion in images (-2 to +2 scale) | `imagefacetone<-1.5` |
| `imagenumfaces>N` | Number of visible faces | `imagenumfaces>3` |
| `imagewebcount<N` | How many sites use this image (reverse image search) | `imagewebcount<10` |
| `imagewebtag:"topic"` | Crowdsourced web descriptions of the image | `imagewebtag:"medical"` |

### Output Modes

Set via `mode=` parameter.

#### Article Modes

| Mode | Description | Supports maxrecords | Supports format |
|------|-------------|--------------------:|-----------------|
| `artlist` | List of articles with title, URL, date, metadata | Yes (1–250) | json, csv, html, rss, jsonfeed |
| `artgallery` | Magazine-style visual layout with social images | Yes | html |

#### Timeline Modes

All timeline modes return time-series data. Resolution adapts automatically:
- **< 72 hours**: 15-minute intervals
- **72 hours – 1 week**: hourly intervals
- **> 1 week**: daily intervals

| Mode | Series Label | Value | Extra Fields |
|------|-------------|-------|-------------|
| `timelinevol` | "Volume Intensity" | % of total GDELT coverage | — |
| `timelinevolraw` | "Article Count" | Raw article count | `norm` (total articles monitored) |
| `timelinevolinfo` | "Volume Intensity" | % of total coverage | Top 10 articles per timestep |
| `timelinetone` | "Average Tone" | Average sentiment score | — |
| `timelinelang` | Per-language series | % of total coverage | One series per detected language |
| `timelinesourcecountry` | Per-country series | % of total coverage | One series per source country |

#### Other Modes

| Mode | Description |
|------|-------------|
| `tonechart` | Histogram of sentiment distribution (-100 to +100) |
| `imagecollage` | Grid of images from matched articles |
| `imagecollageinfo` | Image grid + web popularity + metadata |
| `imagecollagesharei` | Social sharing images from articles |
| `imagegallery` | Magazine-style image display |
| `wordcloudimagewebtags` | Word cloud of image-related web tags |
| `wordcloudimagetags` | Word cloud of deep learning image labels |

### Output Formats

Set via `format=` parameter.

| Format | Description | Notes |
|--------|-------------|-------|
| `html` | Interactive browser visualization | Default. Embeddable via iframe. |
| `json` | UTF-8 JSON | Best for programmatic access |
| `csv` | UTF-8 CSV with BOM | Excel-compatible |
| `jsonp` | JSON with callback wrapper | Specify `callback=functionName` |
| `rss` | RSS 2.0 feed | artlist mode only |
| `rssarchive` | RSS with desktop + mobile/AMP URLs | artlist mode only |
| `jsonfeed` | JSONFeed 1.0 | artlist mode only |

### Time Parameters

You must use **either** `timespan` **or** `startdatetime`/`enddatetime`, not both.

#### Relative: timespan

Offset backwards from the current moment.

| Format | Example | Notes |
|--------|---------|-------|
| Minutes | `15min` | Minimum 15 minutes |
| Hours | `24h` or `24hours` | |
| Days | `7d` or `7days` | |
| Weeks | `4w` or `4weeks` | |
| Months | `3m` or `3months` | Maximum ~3 months (default) |

#### Absolute: startdatetime / enddatetime

Format: `YYYYMMDDHHMMSS` — must be within the last 3 months.

```
&startdatetime=20260101000000&enddatetime=20260115235959
```

### Sorting

Set via `sort=` parameter.

| Value | Description |
|-------|-------------|
| `hybridrel` | Relevance + outlet popularity (default) |
| `datedesc` | Most recent first |
| `dateasc` | Oldest first |
| `tonedesc` | Most positive sentiment first |
| `toneasc` | Most negative sentiment first |

### Other Parameters

| Parameter | Values | Description |
|-----------|--------|-------------|
| `maxrecords` | 1–250 | Max articles returned (artlist/image modes only). Default: 75. |
| `timelinesmooth` | 1–30 | Moving average window for timeline modes (smooths spikes) |
| `trans` | `googtrans` | Embeds Google Translate widget (HTML mode) |
| `timezoom` | `yes` | Interactive timeline zoom (HTML timeline modes) |

### JSON Response Structures

#### artlist Mode

```json
{
  "articles": [
    {
      "url": "https://example.com/article",
      "url_mobile": "https://example.com/article?outputType=amp",
      "title": "Article Title Here",
      "seendate": "20260226T170000Z",
      "socialimage": "https://example.com/image.jpg",
      "domain": "example.com",
      "language": "English",
      "sourcecountry": "United States"
    }
  ]
}
```

**Article fields:**

| Field | Description |
|-------|-------------|
| `url` | Full article URL |
| `url_mobile` | Mobile/AMP URL (may be empty) |
| `title` | Article headline |
| `seendate` | When GDELT first saw the article (`YYYYMMDDTHHMMSSZ`) |
| `socialimage` | Featured/social sharing image URL |
| `domain` | Publishing domain |
| `language` | Article language |
| `sourcecountry` | Country of the news outlet |

**Note**: The DOC API does **not** return article body text, sentiment score, themes, organizations,
or locations in the artlist response. It only returns the metadata above. To get richer metadata,
you need BigQuery or the GKG datasets. The DOC API is for *discovery* — finding relevant articles —
not for deep metadata extraction.

#### timelinevol Mode

```json
{
  "query_details": {
    "title": "search terms here",
    "date_resolution": "hour"
  },
  "timeline": [
    {
      "series": "Volume Intensity",
      "data": [
        {"date": "20260226T170000Z", "value": 0.0032}
      ]
    }
  ]
}
```

`value` = percentage of total GDELT coverage at that timestep.

#### timelinevolraw Mode

```json
{
  "query_details": {
    "title": "search terms here",
    "date_resolution": "hour"
  },
  "timeline": [
    {
      "series": "Article Count",
      "data": [
        {"date": "20260226T170000Z", "value": 7, "norm": 7257}
      ]
    }
  ]
}
```

`value` = raw article count matching your query. `norm` = total articles GDELT monitored in that
timestep (divide value/norm for percentage).

#### timelinetone Mode

```json
{
  "query_details": {
    "title": "search terms here",
    "date_resolution": "hour"
  },
  "timeline": [
    {
      "series": "Average Tone",
      "data": [
        {"date": "20260226T170000Z", "value": -2.45}
      ]
    }
  ]
}
```

`value` = average sentiment. Negative = critical/negative coverage, positive = favorable.

#### timelinelang / timelinesourcecountry Modes

Same structure but with **multiple series** — one per language or country:

```json
{
  "timeline": [
    {"series": "English", "data": [...]},
    {"series": "Spanish", "data": [...]},
    {"series": "Chinese", "data": [...]}
  ]
}
```

---

## Context API

Searches **individual sentences** within articles (not whole documents). Returns the matching sentence
as a text snippet. All search terms must appear in the **same sentence**.

Source: [Context 2.0 API](https://blog.gdeltproject.org/announcing-the-gdelt-context-2-0-api/)

### Endpoint

```
https://api.gdeltproject.org/api/v2/context/context
```

### Key Differences from DOC API

| | DOC API | Context API |
|-|---------|-------------|
| Search unit | Whole article | Single sentence |
| Returns | URL + metadata | URL + sentence snippet |
| Time window | 3 months | 72 hours only |
| Max results | 250 | 200 |
| Term matching | Anywhere in article | Must be in same sentence |

### Parameters

Same query operators as DOC API, plus:

| Parameter | Description |
|-----------|-------------|
| `isquote=1` | Only return sentences that are direct quotes |
| `mode` | Only `artlist` is available |
| `format` | json, csv, rss, jsonfeed, jsonp |
| `maxrecords` | Default 75, max 200 |
| `sort` | Default relevance, or `datedesc`, `dateasc` |
| `timespan` | Same as DOC API but max 72 hours |
| `startdatetime` / `enddatetime` | Must be within 72 hours |

### When to Use Context vs DOC

- Use **DOC** for broad discovery: "find all articles about medical device recalls"
- Use **Context** for precision: "find sentences where 'FDA' and 'recall' appear together"
- Context is great for validating whether articles actually discuss your topic in a meaningful way

---

## GEO API

Creates geographic visualizations of news coverage. Maps every location mentioned near your keywords.

Source: [GEO 2.0 API Debuts](https://blog.gdeltproject.org/gdelt-geo-2-0-api-debuts/)

### Endpoint

```
https://api.gdeltproject.org/api/v2/geo/geo
```

### Time Window

Rolling **7 days** only (not 3 months like DOC).

### Query Operators

Same as DOC API, plus geographic-specific operators:

| Operator | Description | Example |
|----------|-------------|---------|
| `location:"name"` | Formal location name | `location:"new york"` |
| `locationcc:CODE` | Country code (FIPS 2-char or name, no spaces) | `locationcc:france` |
| `locationadm1:CODE` | First-order admin division (4-char: country+state) | `locationadm1:USTX` |
| `near:LAT,LON,RADIUS` | Radius search in miles (append `km` for kilometers) | `near:38.9,-77.0,50` |

### Modes

**Point modes** (individual locations):

| Mode | Description | Max Points |
|------|-------------|-----------|
| `pointdata` | Location dots with up to 5 articles each (default) | 1,000 |
| `pointheatmap` | Heatmap without article lists (GeoJSON only) | 25,000 |
| `pointanimation` | Animated 15-min increments over 7 days (GeoJSON only) | 10,000/step |

**Aggregate modes** (rolled up to boundaries):

| Mode | Description |
|------|-------------|
| `country` | Normalized by total country mentions (% density) |
| `adm1` | First-order admin division aggregation |
| `sourcecountry` | Origin country of publishing outlets |

Each mode has an `image` variant (e.g., `imagepointdata`, `imagecountry`) for image-based searches.

### Key Parameters

| Parameter | Values | Description |
|-----------|--------|-------------|
| `geores` | 0, 1, 2 | 0=all locations, 1=exclude countries, 2=cities only |
| `maxpoints` | varies | Max locations returned (depends on mode) |
| `format` | html, geojson, csv, rss, jsonfeed | Output format |
| `sortby` | date, tonedesc, toneasc | Sort order (default: relevance) |
| `timespan` | Up to 7d | Time window |
| `zoomwheel` | 0/false | Disable mousewheel zoom on HTML maps |

---

## TV API

Searches closed captions from US television broadcasts (163+ stations, July 2009–present,
2+ million hours of content).

Source: [TV API Debuts](https://blog.gdeltproject.org/gdelt-2-0-television-api-debuts/)

### Endpoint

```
https://api.gdeltproject.org/api/v2/tv/tv
```

### Query Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `context:"term"` | Match in 15-second clip or adjacent clips | `context:"FDA"` |
| `station:CODE` | Filter by station | `station:CNN` |
| `network:NAME` | Filter by network | `network:CBS` |
| `show:"name"` | Filter by show name | `show:"60 Minutes"` |
| `market:"name"` | Filter by TV market | `market:"National"` |

Plus standard text operators: `"exact phrase"`, `(a OR b)`, `-exclusion`.

### Modes

| Mode | Description |
|------|-------------|
| `clipgallery` | Top 50 clips with thumbnails and snippets |
| `timelinevol` | Coverage volume over time |
| `timelinevolnorm` | Total monitored airtime by station |
| `showchart` | Top 10 shows mentioning query |
| `stationchart` | Relative attention by station |
| `stationdetails` | All available stations (JSON only) |
| `trendingtopics` | Currently trending topics (JSON only) |
| `wordcloud` | Top 200 words from relevant clips |

### Key Parameters

| Parameter | Description |
|-----------|-------------|
| `timespan` | Up to `2y` (years!) — much longer than DOC API |
| `startdatetime`/`enddatetime` | July 2, 2009 to present |
| `datanorm` | `perc` (default) or `raw` for absolute counts |
| `datacomb` | `combined` to merge all stations into one series |
| `maxrecords` | Default 50 (HTML), up to 3,000 (JSON/CSV) |
| `timelinesmooth` | 0–30 timestep moving average |
| `dateres` | `hour`, `day`, `week`, `month`, `year` |

---

## Rate Limits & Practical Constraints

### Rate Limiting
- **1 request per 5 seconds** for all APIs
- Exceeding this returns HTTP 429 with message: "Please limit requests to one every 5 seconds"
- For higher throughput, contact kalev.leetaru5@gmail.com

### Data Constraints
- **DOC API**: Rolling 3-month window back to Jan 1, 2017. Data older than 3 months drops off.
- **Context API**: 72 hours only.
- **GEO API**: 7 days only.
- **TV API**: Full archive back to July 2009.
- **No stemming**: "regulate" will NOT match "regulation", "regulatory", etc.
  You must search for each word form separately using OR.
- **Max 250 results** per artlist query. For comprehensive collection, use overlapping time windows.
- **No body text** in DOC API responses — only metadata. You get URLs, not article content.
- Articles appear in GDELT within minutes of publication but may take up to ~1 hour.

### Handling the 250-Result Cap

To collect more than 250 articles on a topic:
1. Use `timelinevolraw` first to see which time periods have the most coverage
2. Break your collection into smaller time windows using `startdatetime`/`enddatetime`
3. Query each window separately with `maxrecords=250`
4. Deduplicate by URL across windows

---

## Medical Device Search Strategy

This section outlines how to construct queries for monitoring medical device events.

### The Challenge

GDELT has no stemming, so you need explicit OR lists for word variants. There's also no
"medical device" category — you have to build it from keywords and domain knowledge.

### Query Building Blocks

#### Core Medical Device Terms
```
("medical device" OR "medical devices")
```

#### Expand with Event Types
```
("medical device" OR "medical devices") (recall OR warning OR alert OR "safety notice" OR "adverse event" OR defect OR malfunction OR shortage)
```

#### Specific Device Categories
```
("pacemaker" OR "defibrillator" OR "insulin pump" OR "ventilator" OR "surgical robot" OR "hip implant" OR "knee implant" OR "stent" OR "catheter")
```

#### Major Manufacturers
```
(medtronic OR "boston scientific" OR stryker OR "johnson & johnson" OR "abbott laboratories" OR "becton dickinson" OR "baxter international" OR zimmer OR "edwards lifesciences" OR philips)
```

#### Regulatory / Supply Chain
```
("FDA recall" OR "FDA warning" OR "510k" OR "class I recall" OR "class II recall" OR "supply chain disruption" OR "manufacturing defect" OR "quality control")
```

### Recommended Query Patterns

**Broad medical device monitoring** (cast a wide net):
```
query=("medical device" OR "medical devices") sourcelang:english&mode=artlist&maxrecords=250&timespan=7d&format=json&sort=datedesc
```

**Recalls and safety events** (high signal):
```
query=("medical device" OR "medical devices") (recall OR "safety alert" OR "adverse event" OR warning) sourcelang:english&mode=artlist&maxrecords=250&timespan=7d&format=json&sort=datedesc
```

**Specific manufacturer monitoring**:
```
query=(medtronic OR "boston scientific" OR stryker) (recall OR warning OR "FDA") sourcelang:english&mode=artlist&maxrecords=250&timespan=7d&format=json&sort=datedesc
```

**Supply chain disruptions**:
```
query=("medical device" OR "medical devices" OR "medical equipment") ("supply chain" OR shortage OR disruption OR tariff OR manufacturing) sourcelang:english&mode=artlist&maxrecords=250&timespan=7d&format=json
```

**Coverage volume over time** (are events spiking?):
```
query=("medical device" OR "medical devices") (recall OR warning) sourcelang:english&mode=timelinevolraw&timespan=3m&format=json
```

**Sentiment tracking** (is coverage turning negative?):
```
query=("medical device" OR "medical devices") sourcelang:english&mode=timelinetone&timespan=30d&format=json
```

**Geographic spread** (where are events being reported from?):
```
query=("medical device" OR "medical devices") (recall OR warning) sourcelang:english&mode=timelinesourcecountry&timespan=30d&format=json
```

### Domain Filtering for Quality

Restrict to high-quality sources to reduce noise:

```
# FDA's own domain
domainis:fda.gov

# Major wire services
(domain:reuters.com OR domain:apnews.com)

# Medical trade press
(domain:meddeviceonline.com OR domain:massdevice.com OR domain:medtechdive.com)
```

### Dealing with No Stemming

Since GDELT does not stem, you need to handle word forms explicitly:

```
# Instead of just "regulate":
(regulate OR regulation OR regulations OR regulatory OR regulator OR regulators)

# Instead of just "manufacture":
(manufacture OR manufacturer OR manufacturers OR manufacturing OR manufactured)
```

### Using near# for Precision

The proximity operator helps when simple AND is too loose:

```
# "FDA" and "recall" must appear within 10 words of each other
near10:"FDA recall"

# "supply" and "chain" within 3 words (catches "supply chain", "supply-chain", "chain of supply")
near3:"supply chain"
```

### Using repeat# for Signal Strength

Articles that mention your topic repeatedly are more likely to be about it:

```
# Article must mention "medical device" at least 3 times
repeat3:"medical device"
```

### Combining tone with search

Find specifically negative coverage about medical devices:

```
query=("medical device" OR "medical devices") tone<-5 sourcelang:english
```

---

## GKG Themes Reference

GDELT tags articles with Global Knowledge Graph (GKG) themes. You can filter by theme in any query
using `theme:THEME_NAME`. The full list is at:

```
http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT
```

### Potentially Relevant Themes for Medical Device Monitoring

Based on the GKG taxonomy, themes that may be relevant (verify against the lookup file):

| Theme | Likely Coverage |
|-------|----------------|
| `HEALTH_PANDEMIC` | Disease outbreaks affecting device demand |
| `HEALTH_SEXTRANSDISEASE` | STD-related health coverage |
| `MEDICAL` | General medical coverage |
| `SCIENCE_TECHNOLOGY` | Technology and innovation |
| `RECALL` | Product recalls |
| `SAFETY` | Safety-related coverage |
| `TRADE` | Trade policy affecting supply chains |
| `TARIFF` | Tariff-related coverage |
| `MANUFACTURING` | Manufacturing and production |
| `REGULATION` | Regulatory actions |
| `TERROR` | Could affect supply chain disruption analysis |
| `CRISISLEX_*` | Crisis-related language |

**Important**: The `theme:` operator may return broad results. Combining it with keyword search
is usually more effective:

```
query="medical device" theme:RECALL sourcelang:english
```

### Finding More Themes

Use the DOC API's word cloud modes or the lookup file to discover which themes commonly co-occur
with medical device coverage. You can also use BigQuery to mine the GKG for theme associations
(but that's outside the scope of the REST APIs).

---

## Example Queries

### 1. Basic Article Search

Find recent articles about medical device recalls, sorted by date:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=%22medical+device%22+recall+sourcelang%3Aenglish\
&mode=artlist&maxrecords=25&timespan=7d&format=json&sort=datedesc"
```

### 2. Timeline of Coverage Volume

How much coverage has "medical device recall" gotten over the past 3 months?

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=%22medical+device%22+recall+sourcelang%3Aenglish\
&mode=timelinevolraw&timespan=3m&format=json"
```

### 3. Sentiment Tracking

Track average tone of medical device coverage over 30 days:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=%22medical+device%22+sourcelang%3Aenglish\
&mode=timelinetone&timespan=30d&format=json"
```

### 4. Coverage by Source Country

Where is medical device news being published from?

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=%22medical+device%22+sourcelang%3Aenglish\
&mode=timelinesourcecountry&timespan=30d&format=json"
```

### 5. Only Negative Coverage

Find articles with strongly negative tone about medical devices:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=%22medical+device%22+tone%3C-5+sourcelang%3Aenglish\
&mode=artlist&maxrecords=50&timespan=30d&format=json&sort=toneasc"
```

### 6. Manufacturer-Specific Search

Monitor Medtronic across multiple event types:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=medtronic+(recall+OR+warning+OR+%22FDA%22+OR+%22adverse+event%22)+sourcelang%3Aenglish\
&mode=artlist&maxrecords=100&timespan=30d&format=json&sort=datedesc"
```

### 7. Proximity Search for Precision

FDA and recall must appear within 10 words of each other:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?\
query=near10%3A%22FDA+recall%22+%22medical+device%22+sourcelang%3Aenglish\
&mode=artlist&maxrecords=50&timespan=30d&format=json"
```

### 8. Context API — Sentence-Level

Find exact sentences where "medical device" and "recall" appear together:

```bash
curl -s "https://api.gdeltproject.org/api/v2/context/context?\
query=%22medical+device%22+recall\
&mode=artlist&maxrecords=75&timespan=72h&format=json"
```

### 9. Geographic Visualization

Map all locations mentioned in medical device recall coverage:

```bash
curl -s "https://api.gdeltproject.org/api/v2/geo/geo?\
query=%22medical+device%22+recall+sourcelang%3Aenglish\
&mode=pointdata&format=geojson&timespan=7d&geores=2"
```

### 10. TV Coverage

Search television broadcasts for medical device mentions:

```bash
curl -s "https://api.gdeltproject.org/api/v2/tv/tv?\
query=%22medical+device%22+market%3A%22National%22\
&mode=timelinevol&format=json&timespan=6m"
```

### 11. Using search.py (Our CLI)

```bash
# Basic search
python search.py "medical device recall" --days 7 --limit 50

# With country context
python search.py "pacemaker recall" --country "United States" --days 30

# Export to CSV
python search.py "medtronic recall" --days 30 --limit 250 --csv medtronic_recalls.csv

# Specific source country
python search.py "medical device" --source US --domain fda.gov --days 30
```

Note: `search.py` currently only uses `artlist` mode. The other modes (timeline, tone, geo)
would need to be added to `gdelt_client.py` to use from the CLI.
