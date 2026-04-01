#!/usr/bin/env python3
"""saver.py — Fetch, summarize with Gemini Flash, and save a URL to Notion."""

import os, sys, json, re, argparse
from datetime import date
from html.parser import HTMLParser
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv(path=".env"):
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(root):
        return
    with open(root) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip("\"'")
            os.environ.setdefault(k.strip(), v)


# ── HTML → plain text ─────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside"}
    VOID = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed",
            "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self._depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            t = data.strip()
            if t:
                self.parts.append(t)


def extract_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = " ".join(p.parts)
    return re.sub(r"\s+", " ", text).strip()


# ── Fetch URL ─────────────────────────────────────────────────────────────────

BLOCKED_DOMAINS = {"x.com", "twitter.com", "instagram.com", "facebook.com"}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_url(url: str) -> requests.Response:
    return requests.get(url, headers=BROWSER_HEADERS, timeout=15, verify=False, allow_redirects=True)


def fetch_text(url: str, max_chars: int = 30000) -> tuple:
    """Returns (full_text, truncated_text, final_url). full_text for reading time, truncated for Gemini."""
    if not url.startswith("http"):
        url = "https://" + url

    r = _fetch_url(url)
    final_url = r.url

    # If blocked, try archive.ph
    if r.status_code in (401, 403, 429):
        try:
            ar = _fetch_url(f"https://archive.ph/{final_url}")
            if ar.ok:
                text = extract_text(ar.text)
                if len(text) > 200:
                    return text, text[:8000], final_url
        except Exception:
            pass
        fallback = f"URL: {final_url}\nNote: Could not access page content. Summarize based on the URL alone."
        return fallback, fallback, final_url

    r.raise_for_status()
    domain = re.sub(r"^www\.", "", final_url.split("/")[2].lower())
    if domain in BLOCKED_DOMAINS:
        fallback = f"URL: {final_url}\nNote: This is a {domain} link. Summarize based on the URL alone."
        return fallback, fallback, final_url

    ct = r.headers.get("content-type", "")
    if "html" in ct:
        full_text = extract_text(r.text)
        return full_text, full_text[:8000], final_url
    return r.text, r.text[:8000], final_url


def reading_time(text: str) -> str:
    words = len(text.split())
    minutes = max(1, round(words / 200))
    return f"{minutes} min read"


# ── Gemini Flash ──────────────────────────────────────────────────────────────

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"


def summarize(text: str, api_key: str):
    prompt = (
        "You are a helpful assistant. Given the following web page content, "
        "return ONLY a JSON object with three keys:\n"
        '  "title": a headline in "[Category] - Topic" format, max 100 characters '
        '(e.g. "Cybersecurity - New Attack Targets npm Package axios")\n'
        '  "summary": a 2-3 sentence summary of the page\n'
        '  "tags": a list of 3-5 short relevant tags (lowercase, no # symbol)\n\n'
        f"Content:\n{text}\n\n"
        "Respond with valid JSON only. No markdown fences, no extra text."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    r = requests.post(GEMINI_URL, params={"key": api_key}, json=payload, timeout=60)
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    data = json.loads(raw)
    title = str(data.get("title", "")).strip()[:100]
    summary = str(data["summary"])
    tags = [str(t).lower().strip() for t in data["tags"]][:5]
    return title, summary, tags


# ── Notion helpers ────────────────────────────────────────────────────────────

NOTION_BASE = "https://api.notion.com/v1"


def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def find_database(token: str, name: str) -> Optional[str]:
    r = requests.post(
        f"{NOTION_BASE}/search",
        headers=_notion_headers(token),
        json={"query": name, "filter": {"value": "database", "property": "object"}},
    )
    r.raise_for_status()
    for db in r.json().get("results", []):
        title_parts = db.get("title", [])
        title = "".join(p.get("plain_text", "") for p in title_parts)
        if title.strip().lower() == name.lower():
            return db["id"]
    return None


def create_database(token: str, parent_page_id: str) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "Link Library"}}],
        "properties": {
            "Title":      {"title": {}},
            "Summary":    {"rich_text": {}},
            "Tags":       {"multi_select": {}},
            "Source":     {"url": {}},
            "Date Saved": {"date": {}},
            "Read Time":  {"rich_text": {}},
            "Read":       {"checkbox": {}},
        },
    }
    r = requests.post(f"{NOTION_BASE}/databases", headers=_notion_headers(token), json=body)
    if not r.ok:
        raise RuntimeError(f"Failed to create database: {r.status_code} {r.text}")
    return r.json()["id"]


def find_duplicate(token: str, db_id: str, clean_url: str) -> Optional[str]:
    """Return existing Notion page URL if this source URL was already saved."""
    r = requests.post(
        f"{NOTION_BASE}/databases/{db_id}/query",
        headers=_notion_headers(token),
        json={"filter": {"property": "Source", "url": {"equals": clean_url}}},
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0].get("url") if results else None


def add_entry(token: str, db_id: str, url: str, title: str, summary: str,
              tags: list, read_time: str = "") -> tuple:
    """Create a Notion page and return (notion_url, page_id)."""
    source = url if url.startswith("http") else None
    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "Title":      {"title": [{"text": {"content": title}}]},
            "Summary":    {"rich_text": [{"text": {"content": summary}}]},
            "Tags":       {"multi_select": [{"name": t} for t in tags]},
            "Source":     {"url": source},
            "Date Saved": {"date": {"start": date.today().isoformat()}},
            "Read Time":  {"rich_text": [{"text": {"content": read_time}}]},
            "Read":       {"checkbox": False},
        },
    }
    r = requests.post(f"{NOTION_BASE}/pages", headers=_notion_headers(token), json=body)
    if not r.ok:
        raise RuntimeError(f"Failed to add entry: {r.status_code} {r.text}")
    page = r.json()
    return page.get("url", ""), page.get("id", "")


def append_article(token: str, page_id: str, text: str):
    """Append the full article text as paragraph blocks inside the Notion page."""
    # Split into 1900-char chunks (Notion block limit is 2000 chars)
    chunks = [text[i:i+1900] for i in range(0, min(len(text), 50000), 1900)][:50]
    blocks = [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}}
        for chunk in chunks
    ]
    r = requests.patch(
        f"{NOTION_BASE}/blocks/{page_id}/children",
        headers=_notion_headers(token),
        json={"children": blocks},
    )
    if not r.ok:
        raise RuntimeError(f"Failed to append article: {r.status_code} {r.text}")


# ── Core logic (shared by CLI and server) ─────────────────────────────────────

def _get_env():
    load_dotenv()
    notion_token   = os.environ.get("NOTION_TOKEN")
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or None
    gemini_key     = os.environ.get("GEMINI_API_KEY")
    db_id          = os.environ.get("NOTION_DATABASE_ID")
    if not notion_token:
        raise ValueError("NOTION_TOKEN is not set.")
    if not gemini_key:
        raise ValueError("GEMINI_API_KEY is not set.")
    return notion_token, parent_page_id, gemini_key, db_id


def _get_or_create_db(notion_token, db_id, parent_page_id):
    if not db_id:
        db_id = find_database(notion_token, "Link Library")
    if not db_id:
        if not parent_page_id:
            raise ValueError("'Link Library' database not found and NOTION_PARENT_PAGE_ID is not set.")
        db_id = create_database(notion_token, parent_page_id)
    return db_id


def save_url(url: str) -> dict:
    """Fetch, summarize, and save a URL. Returns result dict."""
    notion_token, parent_page_id, gemini_key, db_id = _get_env()
    db_id = _get_or_create_db(notion_token, db_id, parent_page_id)

    full_text, short_text, final_url = fetch_text(url)
    clean_url = final_url.strip().split("?")[0].split("#")[0]

    # Duplicate detection
    existing = find_duplicate(notion_token, db_id, clean_url)
    if existing:
        return {"duplicate": True, "notion_url": existing, "message": f"Already saved: {clean_url}"}

    read_time_str = reading_time(full_text)
    title, summary, tags = summarize(short_text, gemini_key)
    notion_url, page_id = add_entry(notion_token, db_id, clean_url, title, summary, tags, read_time_str)

    # Append full article as page content
    if page_id and len(full_text) > 200:
        try:
            append_article(notion_token, page_id, full_text)
        except Exception:
            pass  # Don't fail the save if article append fails

    return {"summary": summary, "tags": tags, "notion_url": notion_url,
            "source": clean_url, "read_time": read_time_str}


def save_text(text: str) -> dict:
    """Summarize and save plain text directly to Notion (no URL to fetch)."""
    notion_token, parent_page_id, gemini_key, db_id = _get_env()
    db_id = _get_or_create_db(notion_token, db_id, parent_page_id)

    read_time_str = reading_time(text)
    title, summary, tags = summarize(text[:8000], gemini_key)
    notion_url, page_id = add_entry(notion_token, db_id, "Note", title, summary, tags, read_time_str)

    if page_id and len(text) > 200:
        try:
            append_article(notion_token, page_id, text)
        except Exception:
            pass

    return {"summary": summary, "tags": tags, "notion_url": notion_url, "read_time": read_time_str}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch a URL, summarize it with Gemini Flash, and save it to Notion."
    )
    parser.add_argument("url", help="URL to fetch and save")
    args = parser.parse_args()

    print(f"Fetching {args.url} ...")
    try:
        result = save_url(args.url)
    except Exception as e:
        sys.exit(f"Error: {e}")

    if result.get("duplicate"):
        print(f"Already saved! {result['notion_url']}")
        return

    print(f"  Summary   : {result['summary'][:100]}{'...' if len(result['summary']) > 100 else ''}")
    print(f"  Tags      : {', '.join(result['tags'])}")
    print(f"  Read time : {result['read_time']}")
    print(f"\nSaved! {result['notion_url']}")


if __name__ == "__main__":
    main()
