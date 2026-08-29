# Phase 1 ship gate — hand audit of 200 auto-join news edges

Sample: `pipeline/entity_registry.py --audit-sample 200`, seed 1, drawn uniformly from the 5,479
auto-join alias rows carrying news evidence (`auto_join AND gkg_mentions > 0`). Identity aliases
(an entity's own SEC/FDA name matching itself) are excluded — auditing
`ABBOTT LABORATORIES -> ABBOTT LABORATORIES` measures nothing.
Adjudicated by hand 2026-08-29 against the entity's name, ticker, exchange and SIC.
Raw sample kept at `data/entity_audit.tsv` on rainbow-boi.

## Two different things get measured, and they must not be conflated

**Link precision** — does this alias *denote* this entity? That is what a registry is for, and
what the >=97% gate is about.

**Alias ambiguity** — the alias denotes the entity in some mentions and something else in others
("Nasdaq" the listed company vs. the exchange as a venue). The link is right; the *mention* may
not be about the company. This is a query-time ranking problem for Phase 2, not a build-time
identity error, and it is reported separately rather than hidden inside the headline number.

## Result

| measure | count | rate |
|---|---|---|
| **Link precision** (wrong denotation) | 3 wrong / 200 | **98.5%** — clears the >=97% gate |
| Alias ambiguity (right entity, mixed mentions) | 8 / 200 | 4.0% — carried into Phase 2 |
| Clean | 189 / 200 | 94.5% |

### The 3 wrong links
| # | alias | mentions | joined to | why it is wrong |
|---|---|---|---|---|
| 91 | `SU` | 26 | SU Group Holdings Ltd (SUGP) | A 2-character token. GKG emits it as an org string from acronyms and name fragments; almost none of it is this company. |
| 156 | `DOVER` | 33 | DOVER Corp (DOV) | Dover is a town in Delaware, Kent and New Hampshire, and a port. The place dominates the mentions. |
| 194 | `GLOBAL ENTERTAINMENT` | 2 | Global Entertainment Holdings, Inc. | Generic noun phrase; a shell filer with no exchange. Multi-token, so it bypassed the liveness test. |

### The 8 ambiguous aliases (link kept, flagged)
`NASDAQ` (38,931 mentions — by far the largest; most refer to the exchange or the index, not to
Nasdaq, Inc. the company), `WATERS` (62, Waters Corp vs. the common noun/surname),
`HOWARD HUGHES` (32, the corporation vs. the person and the airport), `AMERICAN FARMLAND` (27,
dead filer, generic phrase), `KARMAN` (34), `SERES` (11, Seres Group the Chinese EV maker vs.
Seres Therapeutics), `AIAI` (7), `BARK` (2).

## What the audit changed in the build

Two fixes, applied *after* the measurement above so the 98.5% is not circular:

1. `MIN_SINGLE_TOKEN_LEN = 3` — a one-token alias shorter than 3 characters never auto-joins.
   Removes `SU`. Verified not to touch the correct short names in the sample (`RXO`, `DHT`,
   `IDT` are all 3 characters and survive).
2. `ALIAS_BLOCKLIST` — the confirmed-wrong aliases, listed by name with the reason inline, so the
   next reader sees the evidence rather than a bare set literal.

Post-fix numbers are reported in the CHANGELOG as a separate build; the 98.5% belongs to the
build that was actually sampled.

## Honest limitation

The ambiguity class is not solved by anything available at build time. `NASDAQ` alone carries
38,931 mentions — more than any other alias — and no name-level rule can tell "shares fell on the
Nasdaq" from "Nasdaq Inc. reported earnings". Phase 2 must disambiguate at mention level (article
context vs. the entity's sector) before a follow loop sends anyone a digest, or a follower of
NDAQ gets every market story on the site. This is recorded as a Phase 2 entry condition.
