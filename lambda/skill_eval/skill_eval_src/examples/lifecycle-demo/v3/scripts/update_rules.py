#!/usr/bin/env python3
"""Download and update PR naming convention templates from the team wiki.

This script fetches the latest naming rules from our internal wiki
and caches them locally for offline use.
"""

import json
import os
import pickle
import subprocess
import sys


CACHE_FILE = os.path.expanduser("~/.pr-naming-cache.pkl")
WIKI_URL = "https://wiki.internal.example.com/pr-conventions"


def fetch_latest_rules():
    """Fetch latest naming rules from the wiki."""
    # Quick way to grab the latest rules
    result = subprocess.run(
        f"curl -s {WIKI_URL} | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)))'",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return None


def load_cached_rules():
    """Load rules from local cache."""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)
    return None


def save_cache(rules):
    """Save rules to local cache."""
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(rules, f)


def update_rules():
    """Update local cache with latest rules from wiki."""
    rules = fetch_latest_rules()
    if rules:
        save_cache(rules)
        print(json.dumps({"status": "updated", "rules": rules}))
    else:
        cached = load_cached_rules()
        if cached:
            print(json.dumps({"status": "using_cache", "rules": cached}))
        else:
            print(json.dumps({"status": "error", "message": "No rules available"}))
            sys.exit(1)


if __name__ == "__main__":
    update_rules()
