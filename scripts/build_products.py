#!/usr/bin/env python3
"""Build the product cards: a real screenshot of each live site in a browser frame.

For every product in data/stats.config.json this opens the public site in
headless Chrome, checks that the page really is the product (not an error or a
bot-check page), takes a screenshot and wraps it in an SVG frame, in a light
and a dark version.

A card is only written when the site answered with the expected content, so
"Live · checked <date>" is always a statement that was verified on that date.
If a site cannot be verified, its previous card is left untouched.

Needs Chrome (or Chromium) and Pillow.

Usage:
    python3 scripts/build_products.py --out dist
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "stats.config.json"

VIEW_WIDTH, CROP_HEIGHT = 1440, 900  # captured, then cropped to the top 16:10
SHOT_WIDTH, SHOT_HEIGHT = 960, 600  # stored size; about 2x the size it is shown at
WIDTH, BAR, RADIUS = 720, 54, 12
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

THEMES = {
    "light": {"surface": "#ffffff", "border": "#d1d9e0", "bar": "#f6f8fa", "pill": "#ffffff",
              "ink": "#1f2328", "muted": "#59636e", "dot": "#c8d1da", "good": "#0ca30c"},
    "dark": {"surface": "#0d1117", "border": "#3d444d", "bar": "#151b23", "pill": "#0d1117",
             "ink": "#f0f6fc", "muted": "#9198a1", "dot": "#3d444d", "good": "#0ca30c"},
}


def find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("Chrome was not found. Set CHROME to the browser binary.")


def chrome(binary: str, url: str, height: int, *extra: str) -> str:
    """Run headless Chrome once and return what it printed."""
    with tempfile.TemporaryDirectory() as profile:
        command = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            f"--user-data-dir={profile}",
            f"--window-size={VIEW_WIDTH},{height}",
            "--virtual-time-budget=12000",
            *extra,
            url,
        ]
        if os.environ.get("CI"):
            command.insert(1, "--no-sandbox")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            return result.stdout
        except subprocess.TimeoutExpired as error:
            # Chrome sometimes lingers after it has already done the work.
            output = error.stdout or ""
            return output.decode("utf-8", "ignore") if isinstance(output, bytes) else output


def capture(binary: str, product: dict) -> bytes | None:
    """A JPEG of the top of the page, or None when the product could not be verified."""
    height = product.get("viewport_height", CROP_HEIGHT)
    for attempt in range(3):
        page = chrome(binary, product["url"], height, "--dump-dom")
        if product["expect"].lower() not in page.lower():
            print(f"  {product['slug']}: expected text not found (attempt {attempt + 1})")
            continue
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "shot.png"
            chrome(binary, product["url"], height, f"--screenshot={target}")
            if not target.exists():
                print(f"  {product['slug']}: no screenshot written (attempt {attempt + 1})")
                continue
            image = Image.open(target).convert("RGB")
        # A taller viewport pushes fixed banners (cookie notices) below the crop.
        image = image.crop((0, 0, image.width, min(image.height, CROP_HEIGHT)))
        image = image.resize((SHOT_WIDTH, SHOT_HEIGHT), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=82, optimize=True, progressive=True)
        return buffer.getvalue()
    return None


def card(product: dict, jpeg: bytes, today: dt.date, theme: dict) -> str:
    shot_height = WIDTH * SHOT_HEIGHT // SHOT_WIDTH
    height = BAR + shot_height
    checked = f"checked {today.day} {MONTHS[today.month - 1]}"
    image = base64.b64encode(jpeg).decode("ascii")
    domain = escape(product["domain"])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" height="{height}" role="img" aria-labelledby="t" font-family="{FONT}">
<title id="t">{domain}: screenshot of the live site, {checked} {today.year}</title>
<defs><clipPath id="c"><rect x="1" y="1" width="{WIDTH - 2}" height="{height - 2}" rx="{RADIUS - 1}"/></clipPath></defs>
<g clip-path="url(#c)">
<rect width="{WIDTH}" height="{height}" fill="{theme['surface']}"/>
<image x="0" y="{BAR}" width="{WIDTH}" height="{shot_height}" preserveAspectRatio="xMidYMin slice" href="data:image/jpeg;base64,{image}"/>
<rect width="{WIDTH}" height="{BAR}" fill="{theme['bar']}"/>
<line x1="0" x2="{WIDTH}" y1="{BAR}" y2="{BAR}" stroke="{theme['border']}"/>
</g>
<circle cx="26" cy="{BAR / 2}" r="6" fill="{theme['dot']}"/><circle cx="46" cy="{BAR / 2}" r="6" fill="{theme['dot']}"/><circle cx="66" cy="{BAR / 2}" r="6" fill="{theme['dot']}"/>
<rect x="96" y="12" width="250" height="30" rx="15" fill="{theme['pill']}" stroke="{theme['border']}"/>
<text x="221" y="33.5" font-size="18" fill="{theme['muted']}" text-anchor="middle">{domain}</text>
<circle cx="{WIDTH - 228}" cy="{BAR / 2}" r="6" fill="{theme['good']}"/>
<text x="{WIDTH - 214}" y="33.5" font-size="18" fill="{theme['ink']}" font-weight="600">Live<tspan fill="{theme['muted']}" font-weight="400"> · {checked}</tspan></text>
<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="{RADIUS}" fill="none" stroke="{theme['border']}"/>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="dist", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    binary = find_chrome()
    today = dt.datetime.now(dt.timezone.utc).date()
    products = json.loads(CONFIG.read_text(encoding="utf-8"))["products"]
    missing = []

    for product in products:
        files = {name: args.out / f"product-{product['slug']}-{name}.svg" for name in THEMES}
        jpeg = capture(binary, product)
        if jpeg is None:
            kept = all(path.exists() for path in files.values())
            note = "keeping the previous card" if kept else "and there is no previous card"
            print(f"::warning::{product['domain']} could not be verified; {note}.")
            if not kept:
                missing.append(product["slug"])
            continue
        for name, theme in THEMES.items():
            files[name].write_text(card(product, jpeg, today, theme), encoding="utf-8")
        print(f"  {product['slug']}: live, {len(jpeg) // 1024} KB screenshot")

    if missing:
        print(f"No card at all for: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
