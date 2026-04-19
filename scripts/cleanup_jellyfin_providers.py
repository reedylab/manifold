#!/usr/bin/env python3
"""Drain duplicate Jellyfin XMLTV listings providers pointing at manifold.xml
down to a single entry. Safe to run anytime — it queries the live config,
loops DELETE calls, and stops when only one matching provider remains.

Usage:
  scripts/cleanup_jellyfin_providers.py <JELLYFIN_URL> <API_KEY> [--keep-path PATTERN]

The --keep-path pattern defaults to "manifold.xml" (substring, case-insensitive).
"""
import argparse
import sys
import time

import requests


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jellyfin_url")
    ap.add_argument("api_key")
    ap.add_argument("--keep-path", default="manifold.xml",
                    help="Substring (case-insensitive) that matches providers to collapse")
    ap.add_argument("--dry-run", action="store_true", help="Report counts, don't delete")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    base = args.jellyfin_url.rstrip("/")
    headers = {"X-MediaBrowser-Token": args.api_key, "Content-Type": "application/json"}
    needle = args.keep_path.lower()

    def fetch_providers():
        r = requests.get(f"{base}/System/Configuration/livetv", headers=headers, timeout=args.timeout)
        r.raise_for_status()
        all_p = r.json().get("ListingProviders", []) or []
        matching = [p for p in all_p
                    if (p.get("Type") or "").lower() == "xmltv"
                    and needle in (p.get("Path") or "").lower()
                    and p.get("Id")]
        return all_p, matching

    all_p, matches = fetch_providers()
    print(f"Total providers: {len(all_p)}")
    print(f"Matching {args.keep_path!r}: {len(matches)}")

    if len(matches) <= 1:
        print("Nothing to collapse.")
        return 0

    # Keep the first one, delete everything else
    keeper = matches[0]
    to_delete = matches[1:]
    print(f"Keeping: {keeper['Id']} ({keeper.get('Path')})")
    print(f"Deleting: {len(to_delete)}")

    if args.dry_run:
        print("(dry run — no deletions)")
        return 0

    deleted = 0
    failed = 0
    t0 = time.time()
    last_print = t0

    for p in to_delete:
        pid = p["Id"]
        try:
            r = requests.delete(f"{base}/LiveTv/ListingProviders", headers=headers,
                                params={"id": pid}, timeout=args.timeout)
            r.raise_for_status()
            deleted += 1
        except Exception as e:
            failed += 1
            if failed < 10:
                print(f"  delete {pid} failed: {e}", file=sys.stderr)

        now = time.time()
        if now - last_print >= 5:
            rate = deleted / max(now - t0, 1)
            remaining = len(to_delete) - deleted - failed
            eta = int(remaining / max(rate, 0.1))
            print(f"  deleted={deleted} failed={failed} rate={rate:.1f}/s eta={eta}s")
            last_print = now

    print(f"\nDone in {int(time.time() - t0)}s: deleted={deleted}, failed={failed}")

    # Verify
    _, matches_after = fetch_providers()
    print(f"Matching providers after: {len(matches_after)}")
    return 0 if len(matches_after) <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
