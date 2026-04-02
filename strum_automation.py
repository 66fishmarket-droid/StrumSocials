"""
Strum! Social Media Automation Script
======================================
Handles the non-Canva parts of the image generation pipeline:
  - Reads seed data from Google Sheets
  - Identifies items needing images
  - Downloads exported images from URLs
  - Updates Google Sheets with image status
  - Commits and pushes to GitHub

Canva MCP integration:
  The script generates a batch JSON file that Claude Code consumes
  to drive the Canva edit-export-cancel loop efficiently.

Usage:
  python strum_automation.py scan          # Show what needs images
  python strum_automation.py batch <type>  # Generate batch file for Claude
  python strum_automation.py update <type> # Update Sheets after generation
  python strum_automation.py download      # Download images from export URLs
  python strum_automation.py commit        # Git add, commit, push new images
  python strum_automation.py status        # Full pipeline status report
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

# ── Config ──────────────────────────────────────────────────────────────────

SPREADSHEET_ID = "17IHjmhrVcTZGLRQfXAZfJ86w1X91awYOZBp8-jUTMW8"
SERVICE_ACCOUNT_PATH = os.path.expanduser("~/.claude/google-service-account.json")
REPO_DIR = Path(__file__).parent
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/66fishmarket-droid/StrumSocials/main"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Template definitions: maps content type to Canva template + element IDs
TEMPLATES = {
    "otd": {
        "sheet": "OnThisDay Seeds",
        "design_id": "DAG4Sliy2Zg",
        "elements": {
            "factoid": "PBjpwX1gm3tngz74-LBNG7Rtxtyzxqt2m",
            "date": "PBjpwX1gm3tngz74-LBR4TWdPrsSlKCqS",
        },
        "columns": {
            "status": 0,       # A
            "post_date": 1,    # B
            "event_text": 2,   # C
            "caption": 3,      # D
            "image_url": 4,    # E
            "filename": 5,     # F
            "source_url": 6,   # G
            "created_at": 7,   # H
            "used_on": 8,      # I
            "hashtags": 9,     # J
            "image_created": 10,  # K
        },
        "date_format": lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%B %-d")
            if sys.platform != "win32"
            else datetime.strptime(d, "%Y-%m-%d").strftime("%B %#d"),
        "filename_col": 5,
        "image_created_col": 10,
        "image_url_col": 4,
        "event_text_col": 2,
        "post_date_col": 1,
    },
    "sotd": {
        "sheet": "Song Seeds",
        "design_id": "DAG92AXOLnE",
        "elements": {
            "song_title": "PBjpwX1gm3tngz74-LBNG7Rtxtyzxqt2m",
            "artist": "PBjpwX1gm3tngz74-LB5YKvfczJMPGd5K",
            "mood_tags": "PBjpwX1gm3tngz74-LB3MbCD0NYZyjQmv",
        },
        "columns": {
            "title": 0,         # A
            "artist": 1,        # B
            "mood_note": 4,     # E
            "status": 5,        # F
            "image_created": 8, # I
            "filename": 9,      # J
            "image_url": 10,    # K
        },
        "filename_col": 9,
        "image_created_col": 8,
        "image_url_col": 10,
        "event_text_col": 0,    # title used as primary text
        "post_date_col": 1,     # artist used as secondary (reusing field)
    },
    "trivia": {
        "sheet": "Trivia Seeds",
        "design_id": "DAG32T-T8gU",
        "elements": {},  # not yet mapped
    },
    "event": {
        "sheet": "Event Promo",
        "design_id": "DAG3d3prj-Y",
        "elements": {},  # not yet mapped
    },
}


# ── Google Sheets client ────────────────────────────────────────────────────

def get_sheets_client():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(client, sheet_name):
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(sheet_name)


# ── Scan: find items needing images ─────────────────────────────────────────

def scan_pending(content_type="otd"):
    tmpl = TEMPLATES[content_type]
    client = get_sheets_client()
    ws = get_worksheet(client, tmpl["sheet"])
    all_rows = ws.get_all_values()

    header = all_rows[0]
    pending = []

    for i, row in enumerate(all_rows[1:], start=2):  # 1-indexed, skip header
        # Pad row if shorter than expected
        while len(row) <= tmpl["image_created_col"]:
            row.append("")

        image_created = row[tmpl["image_created_col"]].strip().lower()
        filename = row[tmpl["filename_col"]].strip() if tmpl["filename_col"] < len(row) else ""
        post_date = row[tmpl["post_date_col"]].strip() if tmpl["post_date_col"] < len(row) else ""
        event_text = row[tmpl["event_text_col"]].strip() if tmpl["event_text_col"] < len(row) else ""

        if image_created not in ("yes",) and filename and event_text:
            # Check if image file already exists locally
            jpg_path = REPO_DIR / f"{filename}.jpg"
            exists_locally = jpg_path.exists()

            pending.append({
                "row": i,
                "post_date": post_date,
                "event_text": event_text,
                "filename": filename,
                "exists_locally": exists_locally,
            })

    return pending, header


def cmd_scan(args):
    content_type = args.type
    print(f"\nScanning '{TEMPLATES[content_type]['sheet']}' for items needing images...\n")

    pending, _ = scan_pending(content_type)

    if not pending:
        print("All items have images! Nothing to do.")
        return

    needs_generation = [p for p in pending if not p["exists_locally"]]
    needs_sheet_update = [p for p in pending if p["exists_locally"]]

    if needs_generation:
        print(f"Need Canva generation ({len(needs_generation)}):")
        for item in needs_generation:
            print(f"  Row {item['row']:>3} | {item['post_date']:>10} | {item['filename']}")

    if needs_sheet_update:
        print(f"\nImage exists but sheet not updated ({len(needs_sheet_update)}):")
        for item in needs_sheet_update:
            print(f"  Row {item['row']:>3} | {item['post_date']:>10} | {item['filename']}")

    print(f"\nTotal: {len(needs_generation)} to generate, {len(needs_sheet_update)} to mark in sheet")


# ── Batch: generate JSON for Claude Code ────────────────────────────────────

def cmd_batch(args):
    content_type = args.type
    tmpl = TEMPLATES[content_type]
    pending, _ = scan_pending(content_type)

    # Only items that need Canva generation
    needs_generation = [p for p in pending if not p["exists_locally"]]

    if not needs_generation:
        print("No items need Canva generation.")
        return

    batch = {
        "content_type": content_type,
        "design_id": tmpl["design_id"],
        "elements": tmpl["elements"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [],
    }

    for item in needs_generation:
        entry = {
            "row": item["row"],
            "post_date": item["post_date"],
            "filename": item["filename"],
            "operations": [],
        }

        if content_type == "otd":
            # Format date for display
            try:
                display_date = tmpl["date_format"](item["post_date"])
            except (ValueError, KeyError):
                display_date = item["post_date"]

            entry["operations"] = [
                {
                    "type": "replace_text",
                    "element_id": tmpl["elements"]["factoid"],
                    "text": item["event_text"],
                },
                {
                    "type": "replace_text",
                    "element_id": tmpl["elements"]["date"],
                    "text": display_date,
                },
            ]

        batch["items"].append(entry)

    outfile = REPO_DIR / f"{content_type}_batch.json"
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(batch, f, indent=2, ensure_ascii=False)

    print(f"Batch file written: {outfile}")
    print(f"Items: {len(batch['items'])}")
    print(f"\nTo process in Claude Code, use:")
    print(f"  'Process the batch file {outfile.name} using the Canva MCP tools'")


# ── Update: mark items as done in Google Sheets ────────────────────────────

def cmd_update(args):
    content_type = args.type
    tmpl = TEMPLATES[content_type]
    client = get_sheets_client()
    ws = get_worksheet(client, tmpl["sheet"])

    pending, _ = scan_pending(content_type)
    # Items where file exists locally but sheet says No
    to_update = [p for p in pending if p["exists_locally"]]

    if not to_update:
        print("No sheet updates needed — all existing images already marked.")
        return

    print(f"Updating {len(to_update)} rows in '{tmpl['sheet']}'...")

    # Batch update: set image_created = "Yes" for all matching rows
    # Column K = column 11 (1-indexed in gspread)
    col_letter = chr(ord('A') + tmpl["image_created_col"])

    cells_to_update = []
    for item in to_update:
        cell_ref = f"{col_letter}{item['row']}"
        cells_to_update.append(gspread.Cell(
            row=item["row"],
            col=tmpl["image_created_col"] + 1,  # gspread is 1-indexed
            value="Yes"
        ))

    ws.update_cells(cells_to_update)

    print(f"Done! Updated {len(cells_to_update)} rows to 'Yes'.")
    for item in to_update:
        print(f"  Row {item['row']:>3} | {item['filename']}")


# ── Download: fetch images from export URLs file ───────────────────────────

def cmd_download(args):
    exports_file = REPO_DIR / "exports.json"
    if not exports_file.exists():
        print("No exports.json found. This file should contain:")
        print('  [{"filename": "otd_2026-...", "url": "https://..."}]')
        return

    with open(exports_file, encoding="utf-8") as f:
        exports = json.load(f)

    downloaded = 0
    for item in exports:
        filename = item["filename"]
        url = item["url"]
        outpath = REPO_DIR / f"{filename}.jpg"

        if outpath.exists():
            print(f"  Skip (exists): {filename}")
            continue

        print(f"  Downloading: {filename}...")
        result = subprocess.run(
            ["curl", "-sL", "-o", str(outpath), url],
            capture_output=True, text=True
        )
        if result.returncode == 0 and outpath.exists() and outpath.stat().st_size > 1000:
            downloaded += 1
            print(f"    OK ({outpath.stat().st_size:,} bytes)")
        else:
            print(f"    FAILED")

    print(f"\nDownloaded {downloaded}/{len(exports)} images.")


# ── Commit: git add + commit + push new images ─────────────────────────────

def cmd_commit(args):
    content_type = args.type
    os.chdir(REPO_DIR)

    # Find untracked/modified image files
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True
    )

    new_images = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        status = line[:2].strip()
        filepath = line[3:].strip().strip('"')
        if filepath.endswith(".jpg") and status in ("??", "M", "A"):
            new_images.append(filepath)

    if not new_images:
        print("No new images to commit.")
        return

    print(f"Found {len(new_images)} new/modified images:")
    for img in new_images:
        print(f"  {img}")

    # Determine commit message based on content type and count
    type_labels = {
        "otd": "On This Day",
        "sotd": "Song of the Day",
        "trivia": "Trivia",
        "event": "Event Promo",
    }
    label = type_labels.get(content_type, content_type)

    msg = f"Add {len(new_images)} {label} images via automated Canva pipeline"

    if not args.yes:
        confirm = input(f"\nCommit message: {msg}\nPush to main? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            return

    # Git add
    subprocess.run(["git", "add"] + new_images, check=True)

    # Git commit
    subprocess.run(
        ["git", "commit", "-m", msg],
        check=True
    )

    # Git push
    subprocess.run(["git", "push", "origin", "main"], check=True)
    print(f"\nCommitted and pushed {len(new_images)} images.")


# ── Status: full pipeline report ────────────────────────────────────────────

def cmd_status(args):
    print("=" * 60)
    print("Strum! Social Media Automation — Pipeline Status")
    print("=" * 60)

    for ctype, tmpl in TEMPLATES.items():
        if not tmpl.get("filename_col"):
            continue  # skip types without full column mapping

        try:
            pending, header = scan_pending(ctype)
        except Exception as e:
            print(f"\n{ctype.upper()}: Error reading sheet — {e}")
            continue

        needs_gen = len([p for p in pending if not p["exists_locally"]])
        needs_update = len([p for p in pending if p["exists_locally"]])

        status_icon = "[DONE]" if needs_gen == 0 and needs_update == 0 else "[TODO]"
        print(f"\n{status_icon} {tmpl['sheet']}")
        print(f"   Need generation: {needs_gen}")
        print(f"   Need sheet update: {needs_update}")

    # Count local images
    otd_count = len(list(REPO_DIR.glob("otd_*.jpg")))
    sotd_count = len(list(REPO_DIR.glob("sotd_*.jpg")))
    trivia_count = len(list(REPO_DIR.glob("trivia_*.jpg")))

    print(f"\nLocal images: {otd_count} OTD, {sotd_count} SOTD, {trivia_count} Trivia")
    print()


# ── CLI entry point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Strum! Social Media Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Show items needing images")
    p_scan.add_argument("-t", "--type", default="otd", choices=TEMPLATES.keys())
    p_scan.set_defaults(func=cmd_scan)

    # batch
    p_batch = sub.add_parser("batch", help="Generate batch file for Claude Code")
    p_batch.add_argument("-t", "--type", default="otd", choices=TEMPLATES.keys())
    p_batch.set_defaults(func=cmd_batch)

    # update
    p_update = sub.add_parser("update", help="Mark generated images in Sheets")
    p_update.add_argument("-t", "--type", default="otd", choices=TEMPLATES.keys())
    p_update.set_defaults(func=cmd_update)

    # download
    p_download = sub.add_parser("download", help="Download images from exports.json")
    p_download.set_defaults(func=cmd_download)

    # commit
    p_commit = sub.add_parser("commit", help="Git commit and push new images")
    p_commit.add_argument("-t", "--type", default="otd", choices=TEMPLATES.keys())
    p_commit.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_commit.set_defaults(func=cmd_commit)

    # status
    p_status = sub.add_parser("status", help="Full pipeline status report")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
