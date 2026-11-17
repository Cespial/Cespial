#!/usr/bin/env python3
"""Build the profile record card.

Reads aggregate numbers from the GitHub GraphQL API and writes one card, in a
light and a dark version, plus stats.json.

Only totals leave this script. It never requests repository names, so nothing
about private work beyond counts and language shares can end up in the output.

Usage:
    STATS_TOKEN=<token> python3 scripts/build_stats.py --out dist
    python3 scripts/build_stats.py --out dist --data dist/stats.json   # re-render, no API
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "stats.config.json"
API = "https://api.github.com/graphql"

# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats",
        },
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            if payload.get("errors"):
                raise RuntimeError(json.dumps(payload["errors"])[:500])
            return payload["data"]
        except urllib.error.HTTPError as error:
            # 4xx other than rate limiting will not fix itself.
            if error.code < 500 and error.code not in (403, 429):
                raise
            last_error = error
        except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
            last_error = error
        time.sleep(2**attempt)
    raise SystemExit(f"GitHub API failed after retries: {last_error}")


Q_VIEWER = """
query {
  viewer {
    login
    createdAt
    public: repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
    private: repositories(ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount }
  }
}"""

Q_CALENDAR = """
query($from: DateTime!, $to: DateTime!) {
  viewer {
    contributionsCollection(from: $from, to: $to) {
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}"""

Q_DEPLOY_REPOS = """
query($cursor: String) {
  viewer {
    repositories(first: 50, after: $cursor, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes { id deployments { totalCount } }
    }
  }
}"""

Q_DEPLOYMENTS = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Repository {
      deployments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { environment createdAt latestStatus { state } }
      }
    }
  }
}"""


def production_deployments(token: str) -> dict:
    """Successful deployments to a production environment, across owned repositories.

    Failed builds are not counted. Environment names vary by host ("Production",
    "Production - app-name"), so anything that starts with "production" counts.
    """
    count, projects, first = 0, 0, None
    repo_cursor = None
    while True:
        page = gql(token, Q_DEPLOY_REPOS, {"cursor": repo_cursor})["viewer"]["repositories"]
        for repo in page["nodes"]:
            if not repo["deployments"]["totalCount"]:
                continue
            shipped, cursor = 0, None
            while True:
                batch = gql(token, Q_DEPLOYMENTS, {"id": repo["id"], "cursor": cursor})
                batch = batch["node"]["deployments"]
                for item in batch["nodes"]:
                    environment = (item["environment"] or "").lower()
                    status = (item["latestStatus"] or {}).get("state")
                    if environment.startswith("production") and status == "SUCCESS":
                        shipped += 1
                        if first is None or item["createdAt"] < first:
                            first = item["createdAt"]
                if not batch["pageInfo"]["hasNextPage"]:
                    break
                cursor = batch["pageInfo"]["endCursor"]
            count += shipped
            projects += 1 if shipped else 0
        if not page["pageInfo"]["hasNextPage"]:
            break
        repo_cursor = page["pageInfo"]["endCursor"]
    if not count:
        raise SystemExit(
            "No production deployments came back. A fine-grained token needs the "
            "'Deployments: read' permission as well as 'Metadata: read'."
        )
    return {"successful": count, "projects": projects, "since": first[:10]}


def calendar(token: str, start: dt.date, end: dt.date) -> tuple[dict[str, int], int]:
    """Daily counts between two dates (at most one year apart) and the private total."""
    data = gql(
        token,
        Q_CALENDAR,
        {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"},
    )["viewer"]["contributionsCollection"]
    days = {
        day["date"]: day["contributionCount"]
        for week in data["contributionCalendar"]["weeks"]
        for day in week["contributionDays"]
        if start.isoformat() <= day["date"] <= end.isoformat()
    }
    return days, data["restrictedContributionsCount"]


def collect(token: str, config: dict) -> dict:
    viewer = gql(token, Q_VIEWER)["viewer"]
    if viewer["login"].lower() != config["login"].lower():
        raise SystemExit(
            f"The token belongs to '{viewer['login']}', not '{config['login']}'. "
            "Private totals are only visible to the account owner's own token."
        )

    today = dt.datetime.now(dt.timezone.utc).date()
    window_start = today - dt.timedelta(days=364)
    window, private_count = calendar(token, window_start, today)
    total = sum(window.values())
    if total == 0:
        raise SystemExit("The API returned zero contributions; refusing to publish.")

    return {
        "generated": today.isoformat(),
        "window": {"from": window_start.isoformat(), "to": today.isoformat()},
        "contributions": total,
        "private_share": round(private_count * 100 / total),
        "active_days": sum(1 for count in window.values() if count > 0),
        "window_days": len(window),
        "repositories": {
            "total": viewer["public"]["totalCount"] + viewer["private"]["totalCount"],
            "public": viewer["public"]["totalCount"],
            "private": viewer["private"]["totalCount"],
        },
        "deployments": production_deployments(token),
        "days": [window[date] for date in sorted(window)],
    }


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #

WIDTH = 720
PAD = 28
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

# Surfaces are GitHub's own page backgrounds, so a card stays legible even when
# the reader's GitHub theme and operating-system theme disagree.
THEMES = {
    "light": {
        "surface": "#ffffff",
        "border": "#d1d9e0",
        "ink": "#1f2328",
        "ink2": "#59636e",
        "muted": "#6e7781",
        "axis": "#c8d1da",
        "accent": "#2a78d6",
    },
    "dark": {
        "surface": "#0d1117",
        "border": "#3d444d",
        "ink": "#f0f6fc",
        "ink2": "#c1c9d2",
        "muted": "#9198a1",
        "axis": "#3d444d",
        "accent": "#3987e5",
    },
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt(number: int) -> str:
    return f"{number:,}"


def long_date(iso: str) -> str:
    date = dt.date.fromisoformat(iso)
    return f"{date.day} {MONTHS[date.month - 1]} {date.year}"


def text(x, y, string, size, fill, weight=400, anchor="start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{escape(str(string))}</text>'
    )


# Marks fade up in sequence when the image loads. They start faint, not absent,
# so a renderer that freezes the first frame still shows the full chart.
MOTION = (
    "@keyframes on{from{opacity:.22}}"
    ".day{animation:on .6s ease-out both}"
    "@media (prefers-reduced-motion:reduce){.day{animation:none}}"
)


def frame(height: int, theme: dict, title: str, description: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
            f'width="{WIDTH}" height="{height}" role="img" aria-labelledby="t d" '
            f'font-family="{FONT}">',
            f'<title id="t">{escape(title)}</title>',
            f'<desc id="d">{escape(description)}</desc>',
            f"<style>{MOTION}</style>",
            f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="8" '
            f'fill="{theme["surface"]}" stroke="{theme["border"]}"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def record(stats: dict, theme: dict) -> str:
    """Three totals over the year drawn as a signal, one line per day.

    Line height follows the square root of the day's count, so that one very
    busy day does not flatten the rest. The signal shows rhythm; the totals
    above it carry the numbers.
    """
    height = 304
    body: list[str] = []
    shipped = stats["deployments"]
    since = dt.date.fromisoformat(shipped["since"])

    tiles = [
        (
            fmt(shipped["successful"]),
            "production deploys",
            f"{shipped['projects']} projects since {MONTHS[since.month - 1]} {since.year}",
        ),
        (fmt(stats["contributions"]), "contributions", "in the last 12 months"),
        (fmt(stats["active_days"]), "active days", f"of the last {stats['window_days']}"),
    ]
    for x, (value, label, sub) in zip((PAD, PAD + 244, PAD + 464), tiles):
        body.append(text(x, 70, value, 40, theme["ink"], 600))
        body.append(text(x, 96, label, 15, theme["ink"], 600))
        body.append(text(x, 116, sub, 13, theme["ink2"]))

    days = stats["days"]
    start = dt.date.fromisoformat(stats["window"]["from"])
    left, right, centre, reach = PAD, WIDTH - PAD, 196, 36
    slot = (right - left) / len(days)
    peak = max(days) ** 0.5 or 1
    for index, count in enumerate(days):
        x = left + slot * index
        date = start + dt.timedelta(days=index)
        if date.day == 1:
            body.append(text(x, 256, MONTHS[date.month - 1], 12, theme["muted"]))
        if count <= 0:
            body.append(
                f'<rect x="{x:.2f}" y="{centre - 0.5}" width="{slot * 0.66:.2f}" height="1" fill="{theme["axis"]}"/>'
            )
            continue
        half = max(reach * count**0.5 / peak, 1.2)
        body.append(
            f'<rect x="{x:.2f}" y="{centre - half:.2f}" width="{slot * 0.66:.2f}" height="{half * 2:.2f}" '
            f'rx="{slot * 0.33:.2f}" fill="{theme["accent"]}" class="day" '
            f'style="animation-delay:{index * 3}ms"/>'
        )

    footer = f"One line per day. {stats['private_share']}% of the work is in private repositories; only totals are shown."
    body.append(text(PAD, height - 18, footer, 12, theme["muted"]))
    body.append(
        text(WIDTH - PAD, height - 18, long_date(stats["generated"]), 12, theme["muted"], anchor="end")
    )

    description = (
        f"{fmt(shipped['successful'])} successful production deployments across "
        f"{shipped['projects']} projects since {long_date(shipped['since'])}. "
        f"{fmt(stats['contributions'])} contributions in the last 12 months, "
        f"{stats['private_share']}% of them in private repositories. "
        f"{stats['active_days']} active days of the last {stats['window_days']}. "
        "Below the totals, daily activity is drawn as a waveform with one line per day."
    )
    return frame(height, theme, "Production record, last 12 months", description, body)


def render(stats: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        (out / f"record-{name}.svg").write_text(record(stats, theme), encoding="utf-8")
    kept = ("generated", "window", "contributions", "private_share", "active_days",
            "window_days", "repositories", "deployments", "days")
    data = {key: stats[key] for key in kept}
    (out / "stats.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="dist", type=Path)
    parser.add_argument("--data", type=Path, help="render from a saved stats.json instead of the API")
    args = parser.parse_args()

    if args.data:
        stats = json.loads(args.data.read_text(encoding="utf-8"))
    else:
        token = os.environ.get("STATS_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise SystemExit("Set STATS_TOKEN (or GH_TOKEN) to a token of the account owner.")
        stats = collect(token, json.loads(CONFIG.read_text(encoding="utf-8")))

    render(stats, args.out)
    print(json.dumps({k: v for k, v in stats.items() if k != "days"}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
