"""
GDELT Full-Text Search CLI

Searches actual article content via the GDELT DOC API — finds keywords
anywhere in the article body. Use this for product/device searches,
general phrases, or any keyword query.

Modes:
  articles  - List matching articles (default)
  tone      - Sentiment/tone analysis over time
  timeline  - Coverage volume chart over time

Spaces between words = AND (both must appear). Use quotes for exact phrases.
Boolean operators (and/or/not) are auto-uppercased.
No stemming: "regulate" won't match "regulation".
Max 3-month lookback, max 250 results per query.

Usage:
  python search.py "surgical mask"
  python search.py "pacemaker recall" --days 30
  python search.py "insulin pump" --country "China, India"
  python search.py "medtronic" --source US --limit 50
  python search.py "stent" --domain reuters.com
  python search.py "FDA recall" --csv results.csv
  python search.py "recall or warning"
  python search.py "medical device" --mode tone --timespan 30d
  python search.py "medical device" --mode timeline --timespan 30d
  python search.py "test" --json
  python search.py "FDA recall" --sort date --timespan 2w
"""

import argparse
import csv
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from gdelt_client import (
    build_query, search_gdelt, parse_date,
    normalize_query, interpret_tone, SORT_OPTIONS,
)

console = Console()


def safe_text(val):
    """Strip characters that Windows cp1252 can't encode."""
    if not val:
        return ""
    return val.encode("cp1252", errors="replace").decode("cp1252")


def display_article(i, article):
    domain = safe_text(article.get("domain", "Unknown"))
    dt = parse_date(article.get("seendate", ""))
    date_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "N/A"

    header = Text()
    header.append(f"[{i}] ", style="bold dim")
    header.append(domain, style="bold")
    header.append(f"  {date_str}", style="dim")

    lines = []
    title = safe_text(article.get("title", ""))
    if title:
        lines.append(f"[bold white]{title}[/]")

    source_country = safe_text(article.get("sourcecountry", ""))
    if source_country:
        lines.append(f"[bold]Source:[/] {source_country}")

    lines.append(f"[blue]{safe_text(article.get('url', ''))}[/]")

    body = "\n".join(lines)
    console.print(Panel(body, title=header, border_style="dim", padding=(0, 1)))


def display_tone(data, search_terms, display_timespan):
    """Display tone/sentiment analysis from timelinetone response."""
    timeline = data.get("timeline", [])
    if not timeline:
        console.print("[yellow]No tone data found.[/]")
        return

    # Extract tone values from all series
    all_tones = []
    for series in timeline:
        for entry in series.get("data", []):
            try:
                all_tones.append(float(entry["value"]))
            except (KeyError, TypeError, ValueError):
                continue

    if not all_tones:
        console.print("[yellow]No tone values found in response.[/]")
        return

    avg_tone = sum(all_tones) / len(all_tones)
    min_tone = min(all_tones)
    max_tone = max(all_tones)

    label, style = interpret_tone(avg_tone)

    console.print(Rule(f"Tone Analysis: {search_terms} | Past {display_timespan}"))
    console.print()
    console.print(f"  Average Tone:  [bold]{avg_tone:+.2f}[/]")
    console.print(f"  Range:         {min_tone:+.2f} to {max_tone:+.2f}")
    console.print(f"  Data Points:   {len(all_tones)}")
    console.print()
    console.print(f"  Sentiment:     [{style}]{label}[/{style}]")
    console.print()
    console.print(
        "[dim]GDELT tone ranges from -100 to +100. "
        "Most news tends to be slightly negative (-5 to 0).[/]"
    )


def display_timeline(data, search_terms, display_timespan):
    """Display timeline volume as ASCII bar chart."""
    timeline = data.get("timeline", [])
    if not timeline:
        console.print("[yellow]No timeline data found.[/]")
        return

    # Extract date/value pairs from first series
    points = []
    for entry in timeline[0].get("data", []):
        try:
            date_str = entry["date"]
            value = int(float(entry["value"]))
            points.append((date_str, value))
        except (KeyError, TypeError, ValueError):
            continue

    if not points:
        console.print("[yellow]No timeline values found in response.[/]")
        return

    # Show last 15 data points
    points = points[-15:]
    max_val = max(v for _, v in points)
    bar_width = 40

    console.print(Rule(f"Coverage Volume: {search_terms} | Past {display_timespan}"))
    console.print()

    for date_str, value in points:
        # Format date: 20260224T163000Z -> 2026-02-24
        display_date = date_str[:8] if len(date_str) >= 8 else date_str
        if len(display_date) == 8:
            display_date = f"{display_date[:4]}-{display_date[4:6]}-{display_date[6:8]}"

        bar_len = int((value / max_val) * bar_width) if max_val > 0 else 0
        bar = "\u2588" * bar_len
        console.print(f"  {display_date} | {value:5d} | [cyan]{bar}[/]")

    # Summary
    total = sum(v for _, v in points)
    avg = total / len(points)
    peak_date, peak_val = max(points, key=lambda x: x[1])
    if len(peak_date) >= 8:
        peak_date = f"{peak_date[:4]}-{peak_date[4:6]}-{peak_date[6:8]}"

    console.print()
    console.print(f"  Total Articles:   {total}")
    console.print(f"  Peak:             {peak_val} ({peak_date})")
    console.print(f"  Average/Period:   {avg:.0f}")
    console.print(f"  Data Points:      {len(points)}")


def export_csv(articles, path):
    fields = ["Date", "Title", "Source", "SourceCountry", "Language", "URL"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for article in articles:
            dt = parse_date(article.get("seendate", ""))
            writer.writerow({
                "Date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "Title": article.get("title", ""),
                "Source": article.get("domain", ""),
                "SourceCountry": article.get("sourcecountry", ""),
                "Language": article.get("language", ""),
                "URL": article.get("url", ""),
            })

    console.print(f"\nExported {len(articles)} articles to [bold]{path}[/]")


def main():
    parser = argparse.ArgumentParser(
        description="Search GDELT articles by full-text content (DOC API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("search", help="Search terms (spaces = AND, use quotes for exact phrases)")
    parser.add_argument("--country", help="Location context: articles must mention this place (e.g. 'China, India')")
    parser.add_argument("--source", help="Source country filter: outlet's country (e.g. US, UK, unitedstates)")
    parser.add_argument("--domain", help="Limit to specific news domain (e.g. reuters.com)")
    parser.add_argument("--days", type=int, default=None, help="Look back N days (default: 7, max: ~90)")
    parser.add_argument("--timespan", "-t", default=None, help="Timespan: 7d, 2w, 1m, 24h, 15min (default: 7d)")
    parser.add_argument("--limit", type=int, default=25, help="Max results (default: 25, max: 250)")
    parser.add_argument("--mode", "-m", default="articles", choices=["articles", "tone", "timeline"],
                        help="Output mode (default: articles)")
    parser.add_argument("--sort", "-s", default="relevance", choices=list(SORT_OPTIONS),
                        help="Sort order (default: relevance)")
    parser.add_argument("--csv", metavar="FILE", help="Export results to CSV file (articles mode only)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output raw JSON")

    args = parser.parse_args()

    # Resolve timespan: --timespan takes precedence, then --days, then default
    if args.timespan:
        timespan = args.timespan
    elif args.days is not None:
        timespan = f"{args.days}d"
    else:
        timespan = "7d"

    display_timespan = timespan

    # Normalize query
    raw_search = args.search
    normalized_search = normalize_query(raw_search)

    # Show normalization (unless JSON output)
    if normalized_search != raw_search and not args.json_output:
        console.print(f"[dim]Normalized: {raw_search} -> {normalized_search}[/]")

    query = build_query(normalized_search, args.country, args.source, args.domain)
    sort_value = SORT_OPTIONS[args.sort]

    # Map CLI mode to GDELT API mode
    mode_map = {
        "articles": "artlist",
        "tone": "timelinetone",
        "timeline": "timelinevolraw",
    }
    api_mode = mode_map[args.mode]

    # Warn if --csv used with non-articles mode
    if args.csv and args.mode != "articles":
        console.print("[yellow]Warning: --csv is only supported for articles mode, ignoring.[/]")

    # Display header (unless JSON output)
    if not args.json_output:
        label = f"GDELT: {normalized_search}"
        if args.country:
            label += f" | Mentions: {args.country}"
        if args.source:
            label += f" | Source: {args.source}"
        if args.domain:
            label += f" | Domain: {args.domain}"
        label += f" | {display_timespan} | {args.mode}"
        if args.mode == "articles":
            label += f" | {args.sort} | limit {args.limit}"
        console.print(Rule(label))
        console.print(f"[dim]Query: {query}[/]\n")

    with console.status(f"Searching GDELT ({args.mode} mode)..."):
        data = search_gdelt(query, timespan=timespan, limit=args.limit, sort=sort_value, mode=api_mode)

    if data is None:
        console.print("[red]Error: Failed to get response from GDELT API.[/]")
        sys.exit(1)

    # JSON output
    if args.json_output:
        if args.mode == "articles":
            articles = data.get("articles", [])
            output = {
                "query": query,
                "mode": args.mode,
                "timespan": display_timespan,
                "sort": args.sort,
                "count": len(articles),
                "articles": articles,
            }
            print(json.dumps(output, indent=2))
        else:
            print(json.dumps(data, indent=2))
        return

    # Mode-specific display
    if args.mode == "tone":
        display_tone(data, normalized_search, display_timespan)
    elif args.mode == "timeline":
        display_timeline(data, normalized_search, display_timespan)
    else:
        # Articles mode
        articles = data.get("articles", [])
        if not articles:
            console.print("[yellow]No results found.[/] Try broadening your search or increasing --timespan.")
            return

        console.print(f"[bold]{len(articles)}[/] articles found.\n")

        if args.csv:
            export_csv(articles, args.csv)
        else:
            for i, article in enumerate(articles, 1):
                display_article(i, article)


if __name__ == "__main__":
    main()
