#!/usr/bin/env python3
"""server.py — Tiny Flask API so you can save URLs from anywhere (e.g. Android)."""

import os, sys, re
from urllib.parse import unquote
from saver import save_url, save_text, load_dotenv

try:
    from flask import Flask, request, jsonify
except ImportError:
    sys.exit("Missing dependency: pip3 install flask")

load_dotenv()

app = Flask(__name__)

# Optional: a shared secret to prevent random people from using your endpoint.
# Set SERVER_SECRET in .env — if unset, the endpoint is open.
SECRET = os.environ.get("SERVER_SECRET", "").strip()


@app.post("/save")
def save():
    if SECRET:
        token = request.headers.get("X-Secret", "")
        if token != SECRET:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    raw = data.get("url") or request.form.get("url") or request.args.get("url")

    if not raw:
        return jsonify({"error": "missing 'url' parameter"}), 400

    # Decode any double URL-encoding (e.g. https%3A%2F%2F → https://)
    raw = unquote(raw)

    # Try to extract a URL from the shared text
    url_match = re.search(r'https?://[^\s\)\]\>\"\']+', raw)
    try:
        if url_match:
            result = save_url(url_match.group(0).rstrip('.,;:!?'))
        else:
            result = save_text(raw)
        result["debug_raw"] = raw[:200]
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e), "debug_raw": raw[:200]}), 500


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
