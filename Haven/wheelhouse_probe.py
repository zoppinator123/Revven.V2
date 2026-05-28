#!/usr/bin/env python3
"""
Probe the Wheelhouse API to discover which endpoints return real data.
Run: python3 wheelhouse_probe.py <your-api-key>
"""
import sys
import json
import urllib.request
import urllib.error

if len(sys.argv) < 2:
    print("Usage: python3 wheelhouse_probe.py <your-wheelhouse-api-key>")
    sys.exit(1)

API_KEY = sys.argv[1]
BASE = "https://api.usewheelhouse.com"

HEADERS = {
    "X-User-API-Key": API_KEY,
    "Accept": "application/json",
    "Content-Type": "application/json",
}

def get(path):
    url = BASE + path
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
            data = json.loads(body)
            return r.status, data
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as ex:
        return 0, str(ex)

print(f"\nProbing Wheelhouse API with key: {API_KEY[:12]}...\n{'─'*60}")

endpoints = [
    # v1 Pro API
    "/ss_api/v1/listings",
    "/ss_api/v1/accounts",
    "/ss_api/v1/users/me",
    # v2 variations
    "/api/v2/listings",
    "/api/v2/properties",
    "/api/v2/reservations",
    "/api/v2/calendar",
    "/api/v2/rates",
    "/api/v2/user",
    "/api/v2/account",
    # v1 variations
    "/api/v1/listings",
    "/api/v1/properties",
    "/api/v1/user",
    # root
    "/api/listings",
    "/api/properties",
]

found = []
for ep in endpoints:
    status, data = get(ep)
    icon = "✅" if status == 200 else "❌"
    preview = ""
    if status == 200:
        if isinstance(data, list):
            preview = f"  → list with {len(data)} items"
        elif isinstance(data, dict):
            preview = f"  → keys: {list(data.keys())[:6]}"
        found.append((ep, data))
    else:
        preview = f"  → {status}"
    print(f"{icon} {ep}{preview}")

print(f"\n{'─'*60}")
print(f"Responding endpoints: {len(found)}")
if found:
    print("\nFull responses from working endpoints:")
    for ep, data in found:
        print(f"\n=== {ep} ===")
        print(json.dumps(data, indent=2)[:1000])
