#!/usr/bin/env python3
"""Backfill slug column for existing positions.

For crypto updown markets, derives the slug from the question text.
For other markets, searches Gamma events API by question.

Usage:
    python scripts/backfill_slugs.py
"""
import re
import sqlite3
import requests
import time
from pathlib import Path

GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_DIR = Path("data")

# Slugify: lowercase, replace non-alphanumeric with hyphens, collapse, strip
def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _add_slug_column(conn: sqlite3.Connection):
    """Add slug column if it doesn't exist."""
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN slug TEXT DEFAULT ''")
        conn.commit()
        print("  Added slug column")
    except sqlite3.OperationalError:
        pass  # already exists


def _derive_slug_from_question(question: str) -> str:
    """Try to derive the Gamma event slug from the position's question text.

    Gamma event slugs for crypto updown follow a pattern like:
    - bitcoin-up-or-down-march-15-2026-9pm-et
    - btc-updown-5m-1773622800
    The question text is the event title, so we can derive the slug.
    """
    slug = _slugify(question)

    # Crypto updown 1h events need the year inserted before the time component.
    # Question: "BNB Up or Down - March 15, 9PM ET"
    # Slug needs: "bnb-up-or-down-march-15-2026-9pm-et"
    month_pattern = re.compile(
        r"(up-or-down-(?:january|february|march|april|may|june|july|august|september|october|november|december)-\d+)-(\d+(?:am|pm))"
    )
    m = month_pattern.search(slug)
    if m:
        # Insert current year between date and time
        from datetime import datetime
        year = datetime.now().year
        slug = slug[:m.end(1)] + f"-{year}-" + slug[m.start(2):]

    return slug


def _verify_slug_on_gamma(slug: str) -> str:
    """Verify a slug exists on Gamma. Returns the confirmed slug or empty string."""
    try:
        resp = requests.get(
            f"{GAMMA_API_URL}/events",
            params={"slug": slug},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0].get("slug", "")
    except Exception:
        pass
    return ""


def _search_event_by_title(title: str) -> str:
    """Search Gamma events by title text to find slug."""
    try:
        # Search by the first meaningful words
        resp = requests.get(
            f"{GAMMA_API_URL}/events",
            params={"title": title, "limit": 5},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Find exact title match
            for event in data:
                if event.get("title", "").strip() == title.strip():
                    return event.get("slug", "")
            # Fuzzy: return first result if title is close enough
            if data and _slugify(data[0].get("title", "")) == _slugify(title):
                return data[0].get("slug", "")
    except Exception:
        pass
    return ""


def _find_slug_for_position(question: str) -> str:
    """Find the event slug for a position, using multiple strategies."""
    if not question:
        return ""

    # Strategy 1: Derive slug from question text and verify on Gamma
    derived = _derive_slug_from_question(question)
    if derived:
        verified = _verify_slug_on_gamma(derived)
        if verified:
            return verified

    # Strategy 2: Search by title
    slug = _search_event_by_title(question)
    if slug:
        return slug

    # Strategy 3: Return the derived slug even if unverified
    return derived


def backfill_db(db_path: Path):
    """Backfill slugs for all positions missing them in a single DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    _add_slug_column(conn)

    rows = conn.execute(
        "SELECT id, market_id, market_question FROM positions "
        "WHERE slug IS NULL OR slug = '' OR slug = 'will-joe-biden-get-coronavirus-before-the-election'"
    ).fetchall()

    if not rows:
        print("  No positions need backfill")
        conn.close()
        return

    print(f"  {len(rows)} positions need slug backfill")

    # Deduplicate by question to minimize API calls
    question_slugs: dict[str, str] = {}

    for row in rows:
        d = dict(row)
        question = d.get("market_question", "")

        if question not in question_slugs:
            slug = _find_slug_for_position(question)
            question_slugs[question] = slug
            if slug:
                print(f"  '{question[:50]}' -> {slug}")
            else:
                print(f"  '{question[:50]}' -> (not found)")
            time.sleep(0.3)  # rate limit

    # Update positions
    updated = 0
    for row in rows:
        d = dict(row)
        slug = question_slugs.get(d.get("market_question", ""), "")
        if slug:
            conn.execute("UPDATE positions SET slug = ? WHERE id = ?", (slug, d["id"]))
            updated += 1

    conn.commit()
    conn.close()
    print(f"  Updated {updated}/{len(rows)} positions")


def main():
    if not DATA_DIR.exists():
        print("No data/ directory found.")
        return

    for db_path in sorted(DATA_DIR.glob("poly_trade*.db")):
        if db_path.name.endswith(".migrated"):
            continue
        print(f"\n{db_path.name}:")
        backfill_db(db_path)

    print("\nDone.")


if __name__ == "__main__":
    main()
