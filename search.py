"""
GDELT Full-Text Search CLI

Searches actual article content via the GDELT DOC API — finds keywords
anywhere in the article body. Use this for product/device searches,
general phrases, or any keyword query.

Spaces between words = AND (both must appear). Use quotes for exact phrases.
No stemming: "regulate" won't match "regulation".
Max 3-month lookback, max 250 results per query.

Usage:
  python search.py "surgical mask"
  python search.py "pacemaker recall" --days 30
  python search.py "insulin pump" --country "China, India"
  python search.py "medtronic" --source US --limit 50
  python search.py "stent" --domain reuters.com
  python search.py "FDA recall" --csv results.csv
"""

import argparse
import csv

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from gdelt_client import build_query, search_gdelt, parse_date
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
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7, max: ~90)")
    parser.add_argument("--limit", type=int, default=25, help="Max results (default: 25, max: 250)")
    parser.add_argument("--csv", metavar="FILE", help="Export results to CSV file")

    args = parser.parse_args()

    query = build_query(args.search, args.country, args.source, args.domain)

    # Display header
    label = f"GDELT Full-Text: {args.search}"
    if args.country:
        label += f" | Mentions: {args.country}"
    if args.source:
        label += f" | Source: {args.source}"
    if args.domain:
        label += f" | Domain: {args.domain}"
    label += f" | Past {args.days} days | Limit {args.limit}"
    console.print(Rule(label))
    console.print(f"[dim]Query: {query}[/]\n")

    with console.status("Searching GDELT DOC API..."):
        articles = search_gdelt(query, args.days, args.limit)

    if not articles:
        console.print("[yellow]No results found.[/] Try broadening your search or increasing --days.")
        return

    console.print(f"[bold]{len(articles)}[/] articles found.\n")

    if args.csv:
        export_csv(articles, args.csv)
    else:
        for i, article in enumerate(articles, 1):
            display_article(i, article)


if __name__ == "__main__":
    main()
