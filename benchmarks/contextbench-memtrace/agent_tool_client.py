#!/usr/bin/env python3
"""Small container-side client for the ContextBench Memtrace tool bridge."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--file", dest="file_path")
    search.add_argument("--limit", type=int, default=20)

    shortlist = subparsers.add_parser("shortlist")
    shortlist.add_argument("--file", dest="files", action="append", required=True)

    symbol = subparsers.add_parser("symbol")
    symbol.add_argument("--name", dest="symbol", required=True)
    symbol.add_argument("--file", dest="file_path")

    cochange = subparsers.add_parser("cochange")
    cochange.add_argument("--target", required=True)
    cochange.add_argument("--days", dest="window_days", type=int, default=365)
    cochange.add_argument("--limit", type=int, default=10)

    history = subparsers.add_parser("history")
    history.add_argument("--target")
    history.add_argument("--from", dest="from_time", default="365d ago")
    history.add_argument("--limit", type=int, default=20)

    rank = subparsers.add_parser("rank")
    rank.add_argument(
        "--candidate",
        dest="candidates",
        action="append",
        required=True,
        help="Ranked context span in relative/path:start-end form",
    )

    subparsers.add_parser("verify")

    args = parser.parse_args()
    url = os.environ.get("MEMTRACE_AGENT_URL", "")
    token = os.environ.get("MEMTRACE_AGENT_TOKEN", "")
    if not url or not token:
        parser.error("MEMTRACE_AGENT_URL and MEMTRACE_AGENT_TOKEN are required")
    payload = {key: value for key, value in vars(args).items() if value is not None}
    payload["token"] = token
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print(body or str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Memtrace bridge request failed: {error}", file=sys.stderr)
        return 2
    if not result.get("ok"):
        print(
            result.get("error", "Memtrace bridge rejected the request"), file=sys.stderr
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
