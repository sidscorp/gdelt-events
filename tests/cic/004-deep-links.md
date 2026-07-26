# 004 — Deep links reproduce the sender's view

Issue: #5 — Make the URL authoritative for feed state

Target: `http://localhost:8016` (prod: `https://gdeltmonitor.com`)

Baseline as of 2026-07-26: step 1 **FAILS** — loading with `hours=24` leaves the 3h
chip active. This script should go green only once #5 ships.

1. Load `{TARGET}/?view=geopolitics-conflict&hours=24`
   EXPECT: the "24h" chip is visually active (filled background) and "3h" is not.

2. Read the "CURATED TOPICS" row.
   EXPECT: the "Geopolitics & Conflict" pill is active; no other topic pill is.

3. Read the article-count line directly above the first card.
   EXPECT: a count consistent with a 24-hour window — materially larger than the same
   view at `hours=3`.

4. Load `{TARGET}/?q=semiconductor&sort=recent`
   EXPECT: the search box contains `semiconductor`, and the SORT control reads a
   recency option rather than "Importance".

5. From step 4, click the "24h" chip, then press the browser Back button.
   EXPECT: the previous state from step 4 is restored — same query, same sort.

6. Load `{TARGET}/?hours=999&view=not-a-real-view`
   EXPECT: the page renders normally on its defaults. No blank page, no console
   exception, no spinner that never resolves.

FAIL if: any control's state contradicts its URL parameter, Back does not restore the
prior state, or a malformed parameter throws instead of falling back.
