"""Rebuild article_tags for one or all categories.

Usage:
    python rebuild_tags.py                    # all categories
    python rebuild_tags.py --category supply_chain
    python rebuild_tags.py --category medical_devices
Run with dashboard stopped (needs writer access).
"""
import argparse
import logging
from pipeline.tagger import initial_tag, CATEGORIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=list(CATEGORIES.keys()),
                        help="Rebuild one category. Default: all.")
    args = parser.parse_args()

    summary = initial_tag(category=args.category)
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}")
