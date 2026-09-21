"""Moderate the community statistics on routeai.bais.info.

    python scripts/stats-admin.py list
    python scripts/stats-admin.py ban gh:1a2b3c... --days 7 --reason "made up numbers"
    python scripts/stats-admin.py ban anon:9f8e... --permanent --reason "spam"
    python scripts/stats-admin.py unban gh:1a2b3c...

The admin token is read from ROUTEAI_ADMIN_TOKEN, or from ~/.secrets/routeai-admin-token.
A ban hides that submitter's results from the site right away and refuses their next submission,
with the reason and, for a temporary ban, the date it ends.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SITE = os.environ.get("ROUTEAI_COMMUNITY_URL", "https://routeai.bais.info").rstrip("/")


def token() -> str:
    value = os.environ.get("ROUTEAI_ADMIN_TOKEN", "").strip()
    if value:
        return value
    path = Path.home() / ".secrets" / "routeai-admin-token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    sys.exit("no admin token: set ROUTEAI_ADMIN_TOKEN or write it to ~/.secrets/routeai-admin-token")


def call(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{SITE}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as exc:
        sys.exit(f"{SITE}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the last 200 submissions, newest first")
    ban = sub.add_parser("ban", help="hide a submitter's results and refuse their next submission")
    ban.add_argument("subject", help="the id shown by `list`, e.g. gh:1a2b… or anon:9f8e…")
    ban.add_argument("--days", type=int, help="temporary ban")
    ban.add_argument("--permanent", action="store_true")
    ban.add_argument("--reason", required=True, help="told to the submitter when they try again")
    unban = sub.add_parser("unban", help="lift a ban and clear its strikes")
    unban.add_argument("subject")
    args = parser.parse_args()

    if args.cmd == "list":
        rows = call("/api/admin/submissions")["submissions"]
        print(f"{'subject':<40} {'cert':<5} {'model':<28} {'hw':<8} {'cat':<8} {'score':>6} {'tok/s':>7}  flag")
        for r in rows:
            flag = "OUT OF SCALE" if r["outlier"] else ""
            print(f"{r['subject']:<40} {'yes' if r['certified'] else 'no':<5} {r['model'][:28]:<28} "
                  f"{r['hw']:<8} {r['category']:<8} {r['score']:>6.2f} {r['gen_tps']:>7.1f}  {flag}")
        print(f"\n{len(rows)} submission(s)")
        return 0

    if args.cmd == "ban":
        if not args.permanent and not args.days:
            sys.exit("choose --days N or --permanent")
        answer = call("/api/admin/ban", {"subject": args.subject, "days": None if args.permanent else args.days,
                                         "reason": args.reason})
        until = answer.get("until")
        print(f"{args.subject} blocked " + ("permanently" if not until else f"until {until}"))
        return 0

    call("/api/admin/unban", {"subject": args.subject})
    print(f"{args.subject} unblocked; its results are public again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
