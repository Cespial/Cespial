#!/usr/bin/env python3
"""Build the profile statistics cards.

Reads aggregate numbers from the GitHub GraphQL API and writes four SVG files
(two cards, each in a light and a dark version) plus stats.json.

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

Q_LANGUAGES = """
query($cursor: String) {
  viewer {
    repositories(first: 40, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
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


def longest_streak(days: dict[str, int]) -> tuple[int, int]:
    """Longest run of consecutive active days, and the run that is still open."""
    best = current = 0
    for date in sorted(days):
        current = current + 1 if days[date] > 0 else 0
        best = max(best, current)
    return best, current


def month_keys(today: dt.date, count: int = 12) -> list[str]:
    keys = []
    year, month = today.year, today.month
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys[::-1]


def round_shares(values: list[float]) -> list[int]:
    """Whole percentages that add up to 100 (largest remainder)."""
    total = sum(values)
    if total <= 0:
        return [0 for _ in values]
    exact = [value * 100 / total for value in values]
    floors = [int(value) for value in exact]
    order = sorted(range(len(values)), key=lambda i: exact[i] - floors[i], reverse=True)
    for index in order[: 100 - sum(floors)]:
        floors[index] += 1
    return floors


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

    # Every year since the account was created, for the all-time streak.
    all_days: dict[str, int] = {}
    for year in range(int(viewer["createdAt"][:4]), today.year + 1):
        year_end = min(dt.date(year, 12, 31), today)
        all_days.update(calendar(token, dt.date(year, 1, 1), year_end)[0])
    streak, open_streak = longest_streak(all_days)

    months = [
        {"month": key, "count": sum(n for date, n in window.items() if date.startswith(key))}
        for key in month_keys(today)
    ]

    sizes: dict[str, int] = {}
    cursor = None
    while True:
        page = gql(token, Q_LANGUAGES, {"cursor": cursor})["viewer"]["repositories"]
        for repo in page["nodes"]:
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                sizes[name] = sizes.get(name, 0) + edge["size"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    excluded = set(config["languages"]["exclude"])
    ranked = sorted(
        ((name, size) for name, size in sizes.items() if name not in excluded),
        key=lambda item: item[1],
        reverse=True,
    )
    top = ranked[: config["languages"]["top"]]
    rest = sum(size for _, size in ranked[config["languages"]["top"] :])
    names = [name for name, _ in top] + (["Other"] if rest else [])
    shares = round_shares([size for _, size in top] + ([rest] if rest else []))

    return {
        "generated": today.isoformat(),
        "window": {"from": window_start.isoformat(), "to": today.isoformat()},
        "contributions": total,
        "private_share": round(private_count * 100 / total),
        "active_days": sum(1 for count in window.values() if count > 0),
        "window_days": len(window),
        "longest_streak": streak,
        "current_streak": open_streak,
        "repositories": {
            "total": viewer["public"]["totalCount"] + viewer["private"]["totalCount"],
            "public": viewer["public"]["totalCount"],
            "private": viewer["private"]["totalCount"],
        },
        "deployments": production_deployments(token),
        "months": months,
        "days": [window[date] for date in sorted(window)],
        "languages": [{"name": n, "share": s} for n, s in zip(names, shares)],
        "languages_excluded": sorted(excluded),
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
        "grid": "#e6eaef",
        "axis": "#c8d1da",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
        "other": "#8c959f",
        "dark_label_on": {"#eda100"},
    },
    "dark": {
        "surface": "#0d1117",
        "border": "#3d444d",
        "ink": "#f0f6fc",
        "ink2": "#c1c9d2",
        "muted": "#9198a1",
        "grid": "#21262d",
        "axis": "#3d444d",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
        "other": "#6e7681",
        "dark_label_on": set(),
    },
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt(number: int) -> str:
    return f"{number:,}"


def long_date(iso: str) -> str:
    date = dt.date.fromisoformat(iso)
    return f"{date.day} {MONTHS[date.month - 1]} {date.year}"


def text_width(string: str, size: float, bold: bool = False) -> float:
    """Rough advance width for the system sans; errs on the wide side."""
    return len(string) * size * (0.6 if bold else 0.56)


def text(x, y, string, size, fill, weight=400, anchor="start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{escape(str(string))}</text>'
    )


# Marks fade up in sequence when the image loads. They start faint, not absent,
# so a renderer that freezes the first frame still shows the full chart.
MOTION = (
    "@keyframes on{from{opacity:.22}}"
    ".up,.mid{animation:on .6s ease-out both}"
    "@media (prefers-reduced-motion:reduce){.up,.mid{animation:none}}"
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


def tick_step(maximum: int) -> int:
    """Smallest round step that gives at most four intervals."""
    magnitude = 1
    while True:
        for factor in (1, 2, 2.5, 5):
            step = int(factor * magnitude)
            if step and maximum / step <= 4:
                return step
        magnitude *= 10


def pulse(stats: dict, theme: dict) -> str:
    height = 356
    body: list[str] = []
    repos = stats["repositories"]

    shipped = stats["deployments"]
    since = dt.date.fromisoformat(shipped["since"])
    tiles = [
        (
            fmt(shipped["successful"]),
            "production deploys",
            f"{shipped['projects']} projects since {MONTHS[since.month - 1]} {since.year}",
            208,
        ),
        (fmt(stats["contributions"]), "contributions", "in the last 12 months", 172),
        (fmt(stats["active_days"]), "active days", f"of the last {stats['window_days']}", 146),
        (fmt(stats["longest_streak"]), "days in a row", "longest streak", 0),
    ]
    x = PAD
    for value, label, sub, advance in tiles:
        body.append(text(x, 70, value, 40, theme["ink"], 600))
        body.append(text(x, 96, label, 15, theme["ink"], 600))
        body.append(text(x, 116, sub, 13, theme["ink2"]))
        x += advance

    # Monthly columns: one series, so the title names it and no legend is needed.
    body.append(text(PAD, 160, "Contributions per month", 13, theme["ink2"], 600))
    left, right, top, base = PAD + 40, WIDTH - PAD, 182, 296
    counts = [month["count"] for month in stats["months"]]
    peak = max(counts)
    domain = max(peak * 1.14, 1)
    scale = (base - top) / domain

    step = tick_step(peak) if peak else 1
    tick = 0
    while tick <= domain:
        y = base - tick * scale
        color = theme["axis"] if tick == 0 else theme["grid"]
        body.append(f'<line x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{color}"/>')
        body.append(text(left - 8, y + 4, fmt(tick), 12, theme["muted"], anchor="end"))
        tick += step

    slot = (right - left) / len(counts)
    bar = 24
    peak_index = counts.index(peak)
    for index, month in enumerate(stats["months"]):
        centre = left + slot * (index + 0.5)
        count = month["count"]
        label = MONTHS[int(month["month"][5:]) - 1]
        body.append(text(centre, base + 18, label, 12, theme["muted"], anchor="middle"))
        if count <= 0:
            continue
        bar_height = max(count * scale, 2)
        x, y, r = centre - bar / 2, base - bar_height, min(4, bar_height)
        body.append(
            f'<path d="M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
            f'H{x + bar - r:.1f} Q{x + bar:.1f},{y:.1f} {x + bar:.1f},{y + r:.1f} V{base} Z" '
            f'fill="{theme["series"][0]}" class="up" style="animation-delay:{index * 45}ms"/>'
        )
        if index == peak_index:
            body.append(text(centre, y - 7, fmt(count), 12, theme["ink"], 600, "middle"))

    footer = (
        f"{stats['private_share']}% of this work is in private repositories; only totals are shown."
    )
    body.append(text(PAD, height - 18, footer, 12, theme["muted"]))
    body.append(
        text(WIDTH - PAD, height - 18, f"Updated {long_date(stats['generated'])}", 12, theme["muted"], anchor="end")
    )

    description = (
        f"{fmt(shipped['successful'])} successful production deployments across "
        f"{shipped['projects']} projects since {long_date(shipped['since'])}. "
        f"{fmt(stats['contributions'])} contributions in the last 12 months, "
        f"{stats['private_share']}% of them in private repositories. "
        f"{stats['active_days']} active days of the last {stats['window_days']}. "
        f"Longest streak {stats['longest_streak']} days. "
        f"{repos['total']} repositories ({repos['public']} public, {repos['private']} private). "
        "Per month: "
        + ", ".join(f"{m['month']} {fmt(m['count'])}" for m in stats["months"])
        + "."
    )
    return frame(height, theme, "GitHub activity, last 12 months", description, body)


def wave(stats: dict, theme: dict) -> str:
    """The year as a signal: one line per day, mirrored around a centre line.

    Height follows the square root of the day's count so that one very busy day
    does not flatten the rest. It is a picture of rhythm, not a chart to read
    values from; the monthly chart carries the numbers.
    """
    height = 150
    body: list[str] = []
    days = stats["days"]
    start = dt.date.fromisoformat(stats["window"]["from"])
    left, right, centre, reach = PAD, WIDTH - PAD, 78, 34
    slot = (right - left) / len(days)
    peak = max(days) ** 0.5 or 1

    body.append(text(PAD, 28, "The last 12 months, one line per day", 13, theme["ink2"], 600))
    body.append(text(WIDTH - PAD, 28, "taller means a busier day", 12, theme["muted"], anchor="end"))

    for index, count in enumerate(days):
        x = left + slot * index
        date = start + dt.timedelta(days=index)
        if date.day == 1:
            body.append(text(x, height - 14, MONTHS[date.month - 1], 12, theme["muted"]))
        if count <= 0:
            body.append(
                f'<rect x="{x:.2f}" y="{centre - 0.5}" width="{slot * 0.66:.2f}" height="1" fill="{theme["axis"]}"/>'
            )
            continue
        half = max(reach * count**0.5 / peak, 1.2)
        body.append(
            f'<rect x="{x:.2f}" y="{centre - half:.2f}" width="{slot * 0.66:.2f}" height="{half * 2:.2f}" '
            f'rx="{slot * 0.33:.2f}" fill="{theme["series"][0]}" class="mid" '
            f'style="animation-delay:{index * 3}ms"/>'
        )

    description = (
        f"Daily activity over the last 12 months: {stats['active_days']} active days "
        f"of {stats['window_days']}, {fmt(stats['contributions'])} contributions in total."
    )
    return frame(height, theme, "A year of work, day by day", description, body)


def stack(stats: dict, theme: dict) -> str:
    height = 140
    body: list[str] = []
    languages = [item for item in stats["languages"] if item["share"] > 0]
    repos = stats["repositories"]["total"]

    body.append(text(PAD, 36, "Code by language", 13, theme["ink2"], 600))
    body.append(
        text(WIDTH - PAD, 36, f"share of bytes across {repos} repositories", 12, theme["muted"], anchor="end")
    )

    gap, bar_top, bar_height = 2, 52, 28
    usable = WIDTH - 2 * PAD - gap * (len(languages) - 1)
    x = float(PAD)
    printed: set[str] = set()
    colours = {}
    for index, item in enumerate(languages):
        is_other = item["name"] == "Other"
        colour = theme["other"] if is_other else theme["series"][index % len(theme["series"])]
        colours[item["name"]] = colour
        width = usable * item["share"] / 100
        body.append(
            f'<rect x="{x:.1f}" y="{bar_top}" width="{width:.1f}" height="{bar_height}" rx="4" fill="{colour}"/>'
        )
        label = f"{item['share']}%"
        if width >= text_width(label, 13, bold=True) + 18:
            ink = "#0b0b0b" if colour in theme["dark_label_on"] else "#ffffff"
            body.append(text(x + 9, bar_top + 19, label, 13, ink, 600))
            printed.add(item["name"])
        x += width + gap

    # Legend in block order, in equal columns. A share the bar could not print
    # goes beside the name, in the same text run so the spacing never drifts.
    column = min((WIDTH - 2 * PAD) / len(languages), 132)
    for index, item in enumerate(languages):
        x = PAD + index * column
        body.append(
            f'<rect x="{x:.1f}" y="103" width="10" height="10" rx="2" fill="{colours[item["name"]]}"/>'
        )
        share = (
            ""
            if item["name"] in printed
            else f'<tspan dx="6" fill="{theme["muted"]}">{item["share"]}%</tspan>'
        )
        body.append(
            f'<text x="{x + 16:.1f}" y="113" font-size="13" fill="{theme["ink"]}">'
            f'{escape(item["name"])}{share}</text>'
        )

    description = "Share of code bytes: " + ", ".join(
        f"{item['name']} {item['share']}%" for item in languages
    ) + "."
    return frame(height, theme, "Code by language", description, body)


def render(stats: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        (out / f"wave-{name}.svg").write_text(wave(stats, theme), encoding="utf-8")
        (out / f"pulse-{name}.svg").write_text(pulse(stats, theme), encoding="utf-8")
        (out / f"stack-{name}.svg").write_text(stack(stats, theme), encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")


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
    summary = {k: v for k, v in stats.items() if k not in ("months", "languages", "days")}
    print(json.dumps(summary, indent=2))
    print("months:", ", ".join(f"{m['month'][5:]}={m['count']}" for m in stats["months"]))
    print("languages:", ", ".join(f"{l['name']} {l['share']}%" for l in stats["languages"]))


if __name__ == "__main__":
    sys.exit(main())
