import requests
from bs4 import BeautifulSoup
import csv
import time
import random
import re
import os

# ── Config ──────────────────────────────────────────────────────────────────
BASE_URL = "https://www.ambitionbox.com"
LISTING_URL = f"{BASE_URL}/list-of-companies"
NUM_PAGES = 5
OUTPUT_FILE = "ambitionbox_companies.csv"
DELAY_MIN = 1.5   # seconds between requests (min)
DELAY_MAX = 3.0   # seconds between requests (max)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CSV_COLUMNS = [
    "company_name",
    "profile_url",
    "overall_rating",
    "total_reviews",
    "industry",
    "description",
    "salary_benefits_rating",
    "job_security_rating",
    "work_life_balance_rating",
    "skill_development_rating",
    "company_culture_rating",
    "work_satisfaction_rating",
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_page(url, retries=3):
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            print(f"  [!] Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(2)
    return None


def polite_delay():
    """Sleep a random amount to avoid hammering the server."""
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def safe_text(tag, default="N/A"):
    """Extract stripped text from a BS4 tag, returning default if None."""
    if tag is None:
        return default
    text = tag.get_text(separator=" ", strip=True)
    return text if text else default


# ── Listing page scraper ─────────────────────────────────────────────────────

def scrape_listing_page(page_num):
    """
    Return a list of (company_name, profile_url) tuples from one listing page.
    AmbitionBox pagination uses ?page=N query param.
    """
    url = LISTING_URL if page_num == 1 else f"{LISTING_URL}?page={page_num}"
    print(f"[*] Scraping listing page {page_num}: {url}")
    soup = get_page(url)
    if soup is None:
        print(f"  [!] Could not fetch listing page {page_num}")
        return []

    companies = []

    # Company cards are anchor tags that link to /reviews/ or /info/ pages
    # Try multiple selector strategies for robustness
    cards = soup.select("a[href*='/reviews/']")
    if not cards:
        cards = soup.select("a[href*='/info/']")

    seen = set()
    for a in cards:
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        # Build full URL
        profile_url = href if href.startswith("http") else BASE_URL + href

        # Company name: try the anchor text, or a child element
        name_tag = a.select_one(".companyCardWrapper__companyName, .company-name, h2, h3, strong")
        if name_tag:
            name = safe_text(name_tag)
        else:
            name = safe_text(a)

        # Skip generic/empty names
        if not name or name in ("N/A", ""):
            continue

        companies.append((name, profile_url))

    print(f"  Found {len(companies)} companies on page {page_num}")
    return companies


# ── Company profile scraper ──────────────────────────────────────────────────

def parse_rating_value(text):
    """Extract a numeric rating like '3.8' from a string."""
    if not text:
        return "N/A"
    match = re.search(r"\d+(\.\d+)?", text)
    return match.group() if match else "N/A"


def scrape_company_profile(company_name, profile_url):
    """
    Visit a company profile page and extract all required fields.
    Returns a dict with CSV_COLUMNS as keys.
    """
    print(f"  [>] Scraping: {company_name}")
    soup = get_page(profile_url)

    row = {col: "N/A" for col in CSV_COLUMNS}
    row["company_name"] = company_name
    row["profile_url"] = profile_url

    if soup is None:
        return row

    # ── Overall rating ────────────────────────────────────────────────────
    rating_tag = (
        soup.select_one(".overallRating, .ratingValue, [class*='overallRating'], [class*='rating__overall']")
        or soup.select_one("span.ratingBadge")
    )
    row["overall_rating"] = parse_rating_value(safe_text(rating_tag))

    # ── Total reviews ─────────────────────────────────────────────────────
    review_tag = soup.select_one(
        "[class*='reviewCount'], [class*='review-count'], a[href*='reviews'] span"
    )
    if review_tag:
        row["total_reviews"] = safe_text(review_tag).split()[0].replace(",", "")

    # Fallback: search for text containing "reviews"
    if row["total_reviews"] == "N/A":
        for tag in soup.find_all(string=re.compile(r"\d[\d,]*\s*reviews", re.I)):
            match = re.search(r"([\d,]+)\s*reviews", tag, re.I)
            if match:
                row["total_reviews"] = match.group(1).replace(",", "")
                break

    # ── Industry ─────────────────────────────────────────────────────────
    industry_tag = soup.select_one(
        "[class*='industry'], [class*='Industry'], [data-testid*='industry']"
    )
    if industry_tag:
        row["industry"] = safe_text(industry_tag)
    else:
        # Look for a label "Industry" near a value
        for tag in soup.find_all(string=re.compile(r"^industry$", re.I)):
            parent = tag.parent
            if parent and parent.find_next_sibling():
                row["industry"] = safe_text(parent.find_next_sibling())
                break

    # ── Description / About ───────────────────────────────────────────────
    desc_tag = (
        soup.select_one("[class*='aboutDescription'], [class*='about-description'], [class*='companyDescription']")
        or soup.select_one("section#about p, div#about p")
    )
    if desc_tag:
        row["description"] = safe_text(desc_tag)[:500]  # cap at 500 chars

    # ── Key sub-ratings ───────────────────────────────────────────────────
    # Map readable names → CSV column names
    RATING_MAP = {
        "salary":           "salary_benefits_rating",
        "benefit":          "salary_benefits_rating",
        "job security":     "job_security_rating",
        "work-life":        "work_life_balance_rating",
        "work life":        "work_life_balance_rating",
        "skill":            "skill_development_rating",
        "career":           "skill_development_rating",
        "culture":          "company_culture_rating",
        "work satisfaction":"work_satisfaction_rating",
        "satisfaction":     "work_satisfaction_rating",
    }

    # Strategy 1: look for labelled rating rows/items
    rating_items = soup.select("[class*='ratingRow'], [class*='rating-item'], [class*='subRating'], li[class*='rating']")
    for item in rating_items:
        label_tag = item.select_one("[class*='label'], [class*='Label'], span:first-child")
        value_tag = item.select_one("[class*='value'], [class*='Value'], [class*='rating'], span:last-child")
        if not label_tag or not value_tag:
            continue
        label = safe_text(label_tag).lower()
        value = parse_rating_value(safe_text(value_tag))
        for key, col in RATING_MAP.items():
            if key in label and value != "N/A":
                row[col] = value

    # Strategy 2: find all <dt>/<dd> pairs or labelled sections
    for dt in soup.select("dt, [class*='paramLabel'], [class*='param-label']"):
        label = safe_text(dt).lower()
        dd = dt.find_next_sibling()
        if dd:
            value = parse_rating_value(safe_text(dd))
            for key, col in RATING_MAP.items():
                if key in label and value != "N/A":
                    row[col] = value

    return row


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("AmbitionBox Company Scraper")
    print("=" * 60)

    # Step 1: collect company links from listing pages
    all_companies = []
    for page in range(1, NUM_PAGES + 1):
        companies = scrape_listing_page(page)
        all_companies.extend(companies)
        polite_delay()

    # Deduplicate by URL
    seen_urls = set()
    unique_companies = []
    for name, url in all_companies:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_companies.append((name, url))

    print(f"\n[*] Total unique companies found: {len(unique_companies)}")
    target = unique_companies[:50]  # cap at 50

    # Step 2: scrape each company profile
    rows = []
    for i, (name, url) in enumerate(target, 1):
        print(f"[{i}/{len(target)}] {name}")
        row = scrape_company_profile(name, url)
        rows.append(row)
        polite_delay()

    # Step 3: write CSV
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n[✓] Done! {len(rows)} companies saved to: {output_path}")


if __name__ == "__main__":
    main()