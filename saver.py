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

def fetch_text(url: str, max_chars: int = 8000) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; saver-bot/1.0)"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "html" in ct:
        return extract_text(r.text)[:max_chars]
    return r.text[:max_chars]


# ── Gemini Flash ──────────────────────────────────────────────────────────────

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

def summarize(text: str, api_key: str):
    prompt = (
        "You are a helpful assistant. Given the following web page content, "
        "return ONLY a JSON object with two keys:\n"
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
    summary = str(data["summary"])
    tags = [str(t).lower().strip() for t in data["tags"]][:5]
    return summary, tags


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
            "URL":        {"title": {}},
            "Summary":    {"rich_text": {}},
            "Tags":       {"multi_select": {}},
            "Source":     {"url": {}},
            "Date Saved": {"date": {}},
        },
    }
    r = requests.post(f"{NOTION_BASE}/databases", headers=_notion_headers(token), json=body)
    if not r.ok:
        sys.exit(f"Failed to create database: {r.status_code} {r.text}")
    return r.json()["id"]


def add_entry(token: str, db_id: str, url: str, summary: str, tags: list) -> str:
    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "URL":        {"title": [{"text": {"content": url}}]},
            "Summary":    {"rich_text": [{"text": {"content": summary}}]},
            "Tags":       {"multi_select": [{"name": t} for t in tags]},
            "Source":     {"url": url},
            "Date Saved": {"date": {"start": date.today().isoformat()}},
        },
    }
    r = requests.post(f"{NOTION_BASE}/pages", headers=_notion_headers(token), json=body)
    if not r.ok:
        sys.exit(f"Failed to add entry: {r.status_code} {r.text}")
    return r.json().get("url", "")


# ── Core logic (shared by CLI and server) ─────────────────────────────────────

def save_url(url: str) -> dict:
    """Fetch, summarize, and save a URL. Returns {"summary", "tags", "notion_url"}."""
    load_dotenv()

    notion_token   = os.environ.get("NOTION_TOKEN")
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or None
    gemini_key     = os.environ.get("GEMINI_API_KEY")

    if not notion_token:
        raise ValueError("NOTION_TOKEN is not set.")
    if not gemini_key:
        raise ValueError("GEMINI_API_KEY is not set.")

    text = fetch_text(url)
    summary, tags = summarize(text, gemini_key)

    db_id = find_database(notion_token, "Link Library")
    if not db_id:
        if not parent_page_id:
            raise ValueError(
                "'Link Library' database not found and NOTION_PARENT_PAGE_ID is not set."
            )
        db_id = create_database(notion_token, parent_page_id)

    notion_url = add_entry(notion_token, db_id, url, summary, tags)
    return {"summary": summary, "tags": tags, "notion_url": notion_url}


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

    print(f"  Summary : {result['summary'][:100]}{'...' if len(result['summary']) > 100 else ''}")
    print(f"  Tags    : {', '.join(result['tags'])}")
    print(f"\nSaved! {result['notion_url']}")


if __name__ == "__main__":
    main()
