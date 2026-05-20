#!/usr/bin/env python3
"""
Meta Ads Bulk Launcher

Replaces N8N workflow + Adnova for bulk Meta ad creation:
  Google Drive folder → Upload to Meta → Create creatives/adsets/ads

Features beyond N8N:
  - Multi-placement creatives (4:5 feed + 9:16 story/reels auto-paired)
  - Auto-thumbnail generation per video (ffmpeg or Meta API fallback)
  - Combinatorial creative testing (N creatives x M texts x P headlines)
  - Naming convention engine with template variables
  - Post ID preservation for scaling winners
  - Dry run preview + rollback capability
  - API v25.0 (N8N uses deprecated v22.0)

Usage (CLI):
    python execution/meta_ads_launcher.py --account "Name" --drive-link "URL" \\
        --campaign-id "123" --adset-name "Test" --url "https://..." \\
        --primary-text "Ad copy" --headline "Headline" --description "Desc" \\
        --budget "5000" --country "US" --optimization "7dc"

Usage (programmatic):
    from execution.meta_ads_launcher import launch_ads, list_campaigns
    campaigns = list_campaigns("123456789")
    result = launch_ads(account_name="Name", drive_link="URL", ...)
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime
from itertools import product as itertools_product
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# Google imports
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─── Paths ───────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
load_dotenv(WORKSPACE / ".env")
TMP_DIR = WORKSPACE / ".tmp"
CREDENTIALS_FILE = WORKSPACE / "credentials.json"
TOKEN_FILE = WORKSPACE / "token.json"
CONFIG_FILE = Path(__file__).parent / "meta_ads_config.json"

# ─── Google OAuth Scopes ─────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]

# ─── Meta API ────────────────────────────────────────────────────────
META_API_VERSION = "v25.0"
META_GRAPH_URL = f"https://graph.facebook.com/{META_API_VERSION}"
META_VIDEO_URL = f"https://graph-video.facebook.com/{META_API_VERSION}"

# ─── File Extensions ─────────────────────────────────────────────────
IMAGE_EXTENSIONS = {".png", ".jpeg", ".jpg"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}

# ─── Aspect Ratio Patterns ───────────────────────────────────────────
FEED_PATTERNS = re.compile(
    r"[_\-](4x5|4_5|4-5|feed|portrait|1080x1350)\b", re.IGNORECASE
)
STORY_PATTERNS = re.compile(
    r"[_\-](9x16|9_16|9-16|story|reel|vertical|1080x1920)\b", re.IGNORECASE
)
SQUARE_PATTERNS = re.compile(
    r"[_\-](1x1|1_1|square|1080x1080)\b", re.IGNORECASE
)

# ─── Default UTM ─────────────────────────────────────────────────────
DEFAULT_UTM = (
    "utm_source={{site_source_name}}&utm_medium={{placement}}"
    "&utm_campaign={{campaign.name}}&utm_term={{adset.name}}"
    "&utm_content={{ad.name}}&fbadid={{ad.id}}"
)


# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AccountConfig:
    """Static account config from meta_ads_config.json."""
    account_name: str
    ad_account_id: str
    fb_page_id: str
    ig_page_id: str
    pixel_id: str


@dataclass
class CreativeGroup:
    """A group of files that form one ad creative."""
    concept: str                       # base name (e.g. "hero")
    media_type: str                    # "image" or "video"
    feed_file: Optional[dict] = None   # {id, name, mimeType} from Drive
    story_file: Optional[dict] = None  # {id, name, mimeType} from Drive
    is_multi_placement: bool = False

    # Populated after upload
    feed_image_hash: Optional[str] = None
    feed_video_id: Optional[str] = None
    story_image_hash: Optional[str] = None
    story_video_id: Optional[str] = None
    feed_thumbnail_hash: Optional[str] = None
    story_thumbnail_hash: Optional[str] = None


@dataclass
class LaunchResult:
    """Result of the full launch operation."""
    success: bool = False
    account_name: str = ""
    adset_id: Optional[str] = None
    adset_name: Optional[str] = None
    creative_ids: list = field(default_factory=list)
    ad_ids: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    uploaded_images: list = field(default_factory=list)
    uploaded_videos: list = field(default_factory=list)
    launch_file: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load meta_ads_config.json."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Config not found at {CONFIG_FILE}. "
            "Create it with your account details."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_account_config(account_name: str) -> AccountConfig:
    """Look up account by name in config.json."""
    config = load_config()
    accounts = config.get("accounts", {})

    if account_name not in accounts:
        available = list(accounts.keys())
        raise ValueError(
            f"Account '{account_name}' not found. Available: {available}"
        )

    acct = accounts[account_name]
    return AccountConfig(
        account_name=account_name,
        ad_account_id=acct["ad_account_id"],
        fb_page_id=acct["fb_page_id"],
        ig_page_id=acct["ig_page_id"],
        pixel_id=acct["pixel_id"],
    )


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE AUTH
# ═══════════════════════════════════════════════════════════════════════

def get_google_credentials() -> Credentials:
    """Load or create Google OAuth credentials with Drive scope."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Check for missing Drive scope
    if creds and creds.scopes:
        has_drive = any("drive" in s for s in creds.scopes)
        if not has_drive:
            print("[AUTH] Existing token missing Drive scope. Re-authenticating...")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleAuthRequest())
            except Exception as e:
                print(f"[AUTH] Token refresh failed: {e}. Re-authenticating...")
                creds = None

        if not creds:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=8080)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


# ═══════════════════════════════════════════════════════════════════════
# META API - DISCOVERY
# ═══════════════════════════════════════════════════════════════════════

def _get_meta_token() -> str:
    """Get Meta access token from env."""
    token = os.getenv("META_ACCESS_TOKEN", "")
    if not token:
        raise ValueError(
            "META_ACCESS_TOKEN not set in .env. "
            "Generate at: developers.facebook.com -> Marketing API -> Tools"
        )
    return token


def meta_api_request(
    method: str,
    url: str,
    access_token: str,
    params: dict = None,
    files: dict = None,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> dict:
    """Make a Meta Graph API request with retry logic."""
    params = dict(params or {})
    params["access_token"] = access_token

    for attempt in range(1, retries + 1):
        try:
            if method.upper() == "POST":
                resp = requests.post(url, data=params, files=files, timeout=120)
            else:
                resp = requests.get(url, params=params, timeout=60)

            if resp.status_code == 401:
                raise RuntimeError(
                    "Meta API 401 (Unauthorized). Your META_ACCESS_TOKEN has expired. "
                    "Regenerate at developers.facebook.com -> Marketing API -> Tools"
                )

            data = resp.json()

            if "error" in data:
                error_msg = data["error"].get("message", str(data["error"]))
                if attempt < retries:
                    print(f"  [META] Attempt {attempt} failed: {error_msg}. Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
                raise RuntimeError(f"Meta API error: {error_msg}")

            return data

        except requests.exceptions.RequestException as e:
            if attempt < retries:
                print(f"  [META] Network error (attempt {attempt}): {e}. Retrying...")
                time.sleep(retry_delay)
            else:
                raise RuntimeError(f"Meta API network error after {retries} attempts: {e}")

    raise RuntimeError("Meta API request failed unexpectedly")


def list_ad_accounts(token: str = None) -> list[dict]:
    """List all ad accounts accessible with the token."""
    token = token or _get_meta_token()
    url = f"{META_GRAPH_URL}/me/adaccounts"
    result = meta_api_request("GET", url, token, params={
        "fields": "name,account_id,account_status,currency,timezone_name",
        "limit": "100",
    })
    accounts = result.get("data", [])
    for a in accounts:
        status_map = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW"}
        a["status_label"] = status_map.get(a.get("account_status"), "UNKNOWN")
    return accounts


def list_campaigns(ad_account_id: str, token: str = None) -> list[dict]:
    """List campaigns for an ad account."""
    token = token or _get_meta_token()
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/campaigns"
    result = meta_api_request("GET", url, token, params={
        "fields": "name,status,objective,daily_budget,lifetime_budget",
        "limit": "100",
    })
    return result.get("data", [])


def list_pixels(ad_account_id: str, token: str = None) -> list[dict]:
    """List pixels for an ad account."""
    token = token or _get_meta_token()
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adspixels"
    result = meta_api_request("GET", url, token, params={
        "fields": "name,id",
        "limit": "50",
    })
    return result.get("data", [])


def get_account_pages(ad_account_id: str, token: str = None) -> list[dict]:
    """List pages that can be used for ads."""
    token = token or _get_meta_token()
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/promote_pages"
    result = meta_api_request("GET", url, token, params={
        "fields": "name,id,instagram_accounts{id,username}",
        "limit": "50",
    })
    return result.get("data", [])


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE DRIVE
# ═══════════════════════════════════════════════════════════════════════

def extract_folder_id(drive_link: str) -> str:
    """Extract folder ID from Google Drive URL or raw ID."""
    # /folders/FOLDER_ID format
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", drive_link)
    if match:
        return match.group(1)
    # ?id=FOLDER_ID format (Google Drive "open" links)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", drive_link)
    if match:
        return match.group(1)
    # Raw ID
    if re.match(r"^[a-zA-Z0-9_-]+$", drive_link):
        return drive_link
    raise ValueError(f"Could not extract folder ID from: {drive_link}")


def list_drive_files(folder_id: str) -> list[dict]:
    """List all files in a Google Drive folder."""
    creds = get_google_credentials()
    service = build("drive", "v3", credentials=creds)

    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, size)",
        pageSize=100,
    ).execute()
    files = results.get("files", [])

    if not files:
        raise ValueError(
            f"No files found in Drive folder {folder_id}. "
            "Check permissions and that files exist."
        )

    print(f"[DRIVE] Found {len(files)} files in folder")
    for f in files:
        print(f"  - {f['name']} ({f.get('mimeType', 'unknown')})")
    return files


def download_to_memory(file_id: str) -> bytes:
    """Download a file from Drive into memory (for images)."""
    creds = get_google_credentials()
    service = build("drive", "v3", credentials=creds)
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def download_to_file(file_id: str, dest_path: Path) -> int:
    """Stream a Drive file to disk. Returns file size in bytes."""
    creds = get_google_credentials()
    service = build("drive", "v3", credentials=creds)
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"    Drive download: {int(status.progress() * 100)}%")
    return dest_path.stat().st_size


def make_temporarily_shareable(file_id: str) -> tuple[str, str]:
    """
    Make a Drive file temporarily accessible via link.
    Returns (download_url, permission_id).
    """
    creds = get_google_credentials()
    service = build("drive", "v3", credentials=creds)

    permission = service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
    ).execute()

    permission_id = permission["id"]

    # Get the webContentLink (direct download URL that works for Meta)
    file_meta = service.files().get(
        fileId=file_id, fields="webContentLink"
    ).execute()
    download_url = file_meta.get("webContentLink")

    if not download_url:
        # Fallback: construct the API download URL
        download_url = f"https://drive.google.com/uc?id={file_id}&export=download&confirm=t"

    return download_url, permission_id


def remove_sharing(file_id: str, permission_id: str):
    """Remove temporary sharing permission."""
    try:
        creds = get_google_credentials()
        service = build("drive", "v3", credentials=creds)
        service.permissions().delete(
            fileId=file_id, permissionId=permission_id
        ).execute()
    except Exception as e:
        print(f"  [DRIVE] Warning: could not remove sharing for {file_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# FILE INTELLIGENCE
# ═══════════════════════════════════════════════════════════════════════

def classify_file(filename: str) -> str:
    """Classify file as 'image' or 'video' by extension."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in VIDEO_EXTENSIONS:
        return "video"
    else:
        raise ValueError(
            f"Unrecognized extension '{ext}' for '{filename}'. "
            f"Expected image ({IMAGE_EXTENSIONS}) or video ({VIDEO_EXTENSIONS})"
        )


def detect_aspect_ratio(filename: str) -> str:
    """
    Detect aspect ratio from filename patterns.
    Returns 'feed', 'story', 'square', or 'unknown'.
    """
    stem = Path(filename).stem
    if STORY_PATTERNS.search(stem):
        return "story"
    if FEED_PATTERNS.search(stem):
        return "feed"
    if SQUARE_PATTERNS.search(stem):
        return "square"
    return "unknown"


def get_base_concept(filename: str) -> str:
    """
    Extract base concept name from filename by removing ratio suffixes.
    e.g. "hero_4x5.mp4" → "hero", "ugc-review_9x16.mov" → "ugc-review"
    Handles double extensions like .jpg.jpeg → strips inner extension from stem.
    """
    stem = Path(filename).stem
    # Handle double extensions (e.g. file.jpg.jpeg → stem is "file.jpg")
    stem = re.sub(r"\.(jpg|jpeg|png|mp4|mov)$", "", stem, flags=re.IGNORECASE)
    # Remove known ratio patterns
    for pattern in [FEED_PATTERNS, STORY_PATTERNS, SQUARE_PATTERNS]:
        stem = pattern.sub("", stem)
    # Clean trailing separators
    stem = re.sub(r"[_\-]+$", "", stem)
    return stem or Path(filename).stem


def _strip_timestamp(concept: str) -> str:
    """Strip trailing timestamp-like digits (10+) from a concept name."""
    # Also strip residual extension artifacts (from double extensions)
    cleaned = re.sub(r"\.(jpg|jpeg|png|mp4|mov)$", "", concept, flags=re.IGNORECASE)
    return re.sub(r"[_\-]?\d{10,}$", "", cleaned).rstrip("-_") or concept


def _get_timestamp(concept: str) -> int:
    """Extract trailing timestamp digits from a concept name."""
    m = re.search(r"(\d{10,})$", concept)
    return int(m.group(1)) if m else 0


def pair_creatives(files: list[dict]) -> list[CreativeGroup]:
    """
    Group files by base concept name and pair feed/story versions.
    Returns list of CreativeGroup objects.

    Two-pass pairing:
      1. Exact concept match (standard suffix-based: hero_4x5 + hero_9x16)
      2. Prefix+timestamp match for files like ad-1-{ts}.png + ad-1-story-{ts}.png
         where feed/story have different timestamps
    """
    groups = {}

    # Classify all files
    classified = []
    for f in files:
        filename = f["name"]
        try:
            media_type = classify_file(filename)
        except ValueError:
            print(f"  [WARN] Skipping unrecognized file: {filename}")
            continue
        concept = get_base_concept(filename)
        ratio = detect_aspect_ratio(filename)
        classified.append({"file": f, "media_type": media_type,
                           "concept": concept, "ratio": ratio})

    # Pass 1: exact concept match (existing logic)
    for item in classified:
        key = (item["concept"], item["media_type"])
        if key not in groups:
            groups[key] = CreativeGroup(concept=item["concept"],
                                        media_type=item["media_type"])
        group = groups[key]
        if item["ratio"] == "story":
            group.story_file = item["file"]
        else:
            group.feed_file = item["file"]

    # Pass 2: prefix-based matching for unpaired files with timestamps
    feed_only = {k: g for k, g in groups.items()
                 if g.feed_file and not g.story_file}
    story_only = {k: g for k, g in groups.items()
                  if g.story_file and not g.feed_file}

    if feed_only and story_only:
        # Group by (prefix, media_type)
        feed_by_prefix = {}
        for k, g in feed_only.items():
            prefix = _strip_timestamp(g.concept)
            pkey = (prefix, g.media_type)
            feed_by_prefix.setdefault(pkey, []).append(k)

        story_by_prefix = {}
        for k, g in story_only.items():
            prefix = _strip_timestamp(g.concept)
            pkey = (prefix, g.media_type)
            story_by_prefix.setdefault(pkey, []).append(k)

        for pkey in story_by_prefix:
            if pkey not in feed_by_prefix:
                continue
            # Sort both by timestamp and pair positionally
            s_keys = sorted(story_by_prefix[pkey],
                            key=lambda k: _get_timestamp(groups[k].concept))
            f_keys = sorted(feed_by_prefix[pkey],
                            key=lambda k: _get_timestamp(groups[k].concept))
            for sk, fk in zip(s_keys, f_keys):
                # Merge story into feed group
                groups[fk].story_file = groups[sk].story_file
                groups[fk].is_multi_placement = True
                del groups[sk]
                print(f"  [PAIR] Matched {groups[fk].concept} ↔ story by prefix '{pkey[0]}'")

    # Mark remaining multi-placement / handle story-only
    for group in groups.values():
        if group.feed_file and group.story_file:
            group.is_multi_placement = True
        elif not group.feed_file and group.story_file:
            group.feed_file = group.story_file
            group.story_file = None

    result = list(groups.values())
    print(f"\n[PAIRING] {len(result)} creative group(s):")
    for g in result:
        if g.is_multi_placement:
            print(f"  - {g.concept} ({g.media_type}) [MULTI-PLACEMENT: feed + story]")
        else:
            print(f"  - {g.concept} ({g.media_type}) [single placement]")
    return result


def generate_name(template: str, context: dict) -> str:
    """Apply naming convention template with context variables."""
    result = template
    for key, value in context.items():
        result = result.replace(f"{{{key}}}", str(value))
    # Remove any unreplaced placeholders
    result = re.sub(r"\{[^}]+\}", "", result)
    # Clean double separators
    result = re.sub(r"[_\-]{2,}", "_", result).strip("_- ")
    return result


# ═══════════════════════════════════════════════════════════════════════
# THUMBNAILS
# ═══════════════════════════════════════════════════════════════════════

def _ffmpeg_available() -> bool:
    """Check if ffmpeg is installed."""
    return shutil.which("ffmpeg") is not None


def extract_thumbnail_ffmpeg(video_url: str, output_name: str) -> Optional[bytes]:
    """
    Extract frame at ~2 seconds from video URL using ffmpeg.
    Returns JPEG bytes or None on failure.
    """
    if not _ffmpeg_available():
        return None

    TMP_DIR.mkdir(exist_ok=True)
    output_path = TMP_DIR / f"thumb_{output_name}.jpg"

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "2",
                "-i", video_url,
                "-frames:v", "1",
                "-q:v", "2",
                str(output_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and output_path.exists():
            thumb_bytes = output_path.read_bytes()
            output_path.unlink()
            return thumb_bytes
        else:
            print(f"  [THUMB] ffmpeg failed: {result.stderr.decode()[:200]}")
            return None
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"  [THUMB] ffmpeg error: {e}")
        return None


def get_meta_auto_thumbnail(video_id: str, access_token: str) -> Optional[str]:
    """
    Get Meta's auto-generated thumbnail URL for a video.
    Returns thumbnail URL or None.
    """
    try:
        url = f"{META_GRAPH_URL}/{video_id}/thumbnails"
        result = meta_api_request("GET", url, access_token, params={
            "fields": "id,uri,height,width,is_preferred",
        })
        thumbnails = result.get("data", [])
        if not thumbnails:
            return None

        # Prefer the is_preferred one
        for t in thumbnails:
            if t.get("is_preferred"):
                return t.get("uri")
        return thumbnails[0].get("uri")
    except Exception as e:
        print(f"  [THUMB] Meta thumbnail fetch failed: {e}")
        return None


def generate_thumbnail(
    video_url: str,
    video_id: str,
    filename: str,
    ad_account_id: str,
    access_token: str,
) -> Optional[str]:
    """
    Generate and upload a thumbnail for a video.
    Returns image_hash or None.

    Strategy:
    1. Try ffmpeg (extracts hook frame at 2s)
    2. Fallback to Meta's auto-generated thumbnails
    """
    # Try ffmpeg first
    thumb_bytes = extract_thumbnail_ffmpeg(video_url, Path(filename).stem)
    if thumb_bytes:
        print(f"  [THUMB] Extracted frame via ffmpeg for {filename}")
        return upload_image_bytes(thumb_bytes, f"thumb_{filename}.jpg", ad_account_id, access_token)

    # Fallback: Meta auto-generated
    thumb_url = get_meta_auto_thumbnail(video_id, access_token)
    if thumb_url:
        print(f"  [THUMB] Using Meta auto-thumbnail for {filename}")
        try:
            resp = requests.get(thumb_url, timeout=30)
            if resp.status_code == 200:
                return upload_image_bytes(resp.content, f"thumb_{filename}.jpg", ad_account_id, access_token)
        except Exception as e:
            print(f"  [THUMB] Failed to download Meta thumbnail: {e}")

    print(f"  [THUMB] No thumbnail generated for {filename} - Meta will auto-select")
    return None


# ═══════════════════════════════════════════════════════════════════════
# META UPLOAD
# ═══════════════════════════════════════════════════════════════════════

def upload_image_bytes(
    image_bytes: bytes,
    filename: str,
    ad_account_id: str,
    access_token: str,
) -> str:
    """Upload image bytes to Meta. Returns image_hash."""
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adimages"
    files = {"filename": (filename, io.BytesIO(image_bytes), "image/jpeg")}
    result = meta_api_request("POST", url, access_token, files=files)

    images = result.get("images", {})
    if not images:
        raise RuntimeError(f"Image upload returned no hash. Response: {result}")

    first_key = list(images.keys())[0]
    image_hash = images[first_key].get("hash")
    if not image_hash:
        raise RuntimeError(f"No hash in image upload response: {result}")

    print(f"  [UPLOAD] Image {filename} -> hash: {image_hash}")
    return image_hash


def _poll_video_status(
    video_id: str,
    access_token: str,
    poll_interval: float = 10.0,
    poll_timeout: float = 300.0,
) -> str:
    """Poll Meta until video processing is complete. Returns video_id."""
    print(f"  [UPLOAD] Waiting for processing (timeout: {poll_timeout}s)...")
    status_url = f"{META_GRAPH_URL}/{video_id}"
    start_time = time.time()

    while time.time() - start_time < poll_timeout:
        time.sleep(poll_interval)
        try:
            status_resp = meta_api_request(
                "GET", status_url, access_token,
                params={"fields": "status"},
                retries=1,
            )
            video_status = status_resp.get("status", {}).get("video_status", "processing")
            elapsed = int(time.time() - start_time)
            print(f"    Video {video_id} status: {video_status} ({elapsed}s)")

            if video_status == "ready":
                return video_id
            elif video_status == "error":
                raise RuntimeError(
                    f"Video {video_id} processing failed: {status_resp.get('status', {})}"
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # Transient error during polling, keep trying

    raise RuntimeError(
        f"Video {video_id} processing timed out after {poll_timeout}s. "
        "It may still be processing - check Meta Ads Manager."
    )


CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB chunks for Meta chunked upload


def upload_video_direct(
    file_id: str,
    filename: str,
    ad_account_id: str,
    access_token: str,
    poll_interval: float = 10.0,
    poll_timeout: float = 300.0,
) -> str:
    """
    Upload video: Drive → temp file → Meta.
    Uses chunked upload for files > 100 MB, single POST for smaller.
    Temp file is deleted after upload.
    """
    TMP_DIR.mkdir(exist_ok=True)
    tmp_path = TMP_DIR / f"upload_{filename}"

    try:
        print(f"  [UPLOAD] Downloading {filename} from Drive...")
        file_size = download_to_file(file_id, tmp_path)
        size_mb = file_size / 1024 / 1024
        print(f"  [UPLOAD] Downloaded {size_mb:.1f} MB to temp file")

        if file_size > 100 * 1024 * 1024:
            video_id = _chunked_upload(tmp_path, filename, file_size, ad_account_id, access_token)
        else:
            video_id = _simple_upload(tmp_path, filename, ad_account_id, access_token)

        print(f"  [UPLOAD] Video submitted -> id: {video_id}")
        return _poll_video_status(video_id, access_token, poll_interval, poll_timeout)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _simple_upload(
    file_path: Path, filename: str, ad_account_id: str, access_token: str,
) -> str:
    """Single-request upload for smaller videos."""
    url = f"{META_VIDEO_URL}/act_{ad_account_id}/advideos"
    with open(file_path, "rb") as f:
        files_data = {"source": (filename, f, "video/mp4")}
        result = meta_api_request("POST", url, access_token, files=files_data)
    video_id = result.get("id")
    if not video_id:
        raise RuntimeError(f"Video upload returned no ID: {result}")
    return video_id


def _chunked_upload(
    file_path: Path, filename: str, file_size: int,
    ad_account_id: str, access_token: str,
) -> str:
    """
    Meta chunked upload for large videos.
    1. Start session → get upload_session_id
    2. Upload chunks with start_offset/end_offset
    3. Final chunk returns video_id
    """
    base_url = f"{META_VIDEO_URL}/act_{ad_account_id}/advideos"

    # Start upload session
    start_resp = meta_api_request("POST", base_url, access_token, params={
        "upload_phase": "start",
        "file_size": str(file_size),
    })
    session_id = start_resp.get("upload_session_id")
    video_id = start_resp.get("video_id")
    if not session_id:
        raise RuntimeError(f"Chunked upload start failed: {start_resp}")

    start_offset = int(start_resp.get("start_offset", 0))
    end_offset = int(start_resp.get("end_offset", CHUNK_SIZE))
    chunk_num = 0

    print(f"  [UPLOAD] Chunked upload started (video_id: {video_id})")

    # Upload chunks
    with open(file_path, "rb") as f:
        while start_offset < file_size:
            chunk_num += 1
            chunk_len = end_offset - start_offset
            f.seek(start_offset)
            chunk_data = f.read(chunk_len)

            print(f"    Chunk {chunk_num} ({start_offset // 1024 // 1024}-{end_offset // 1024 // 1024} MB)")

            chunk_resp = requests.post(
                base_url,
                data={
                    "access_token": access_token,
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": str(start_offset),
                },
                files={"video_file_chunk": (filename, io.BytesIO(chunk_data), "video/mp4")},
                timeout=300,
            )
            chunk_result = chunk_resp.json()
            if "error" in chunk_result:
                raise RuntimeError(f"Chunk upload failed: {chunk_result['error'].get('message')}")

            start_offset = int(chunk_result.get("start_offset", file_size))
            end_offset = int(chunk_result.get("end_offset", file_size))

    # Finish upload
    finish_resp = meta_api_request("POST", base_url, access_token, params={
        "upload_phase": "finish",
        "upload_session_id": session_id,
    })
    if not finish_resp.get("success"):
        raise RuntimeError(f"Chunked upload finish failed: {finish_resp}")

    # video_id comes from start phase; finish just confirms success
    if not video_id:
        raise RuntimeError(f"No video_id from chunked upload start phase")

    return video_id


def upload_video_by_url(
    file_url: str,
    ad_account_id: str,
    access_token: str,
    file_id: str = None,
    filename: str = None,
    poll_interval: float = 10.0,
    poll_timeout: float = 300.0,
) -> str:
    """
    Upload video via URL passthrough (server-to-server).
    Falls back to direct upload if URL approach fails.
    Polls until processing complete. Returns video_id.
    """
    url = f"{META_VIDEO_URL}/act_{ad_account_id}/advideos"

    try:
        result = meta_api_request("POST", url, access_token, params={
            "file_url": file_url,
        }, retries=1)
    except RuntimeError as e:
        if file_id and filename:
            print(f"  [UPLOAD] URL passthrough failed ({e}), falling back to direct upload...")
            return upload_video_direct(file_id, filename, ad_account_id, access_token,
                                       poll_interval, poll_timeout)
        raise

    video_id = result.get("id")
    if not video_id:
        raise RuntimeError(f"Video upload returned no ID. Response: {result}")

    print(f"  [UPLOAD] Video submitted -> id: {video_id}")
    return _poll_video_status(video_id, access_token, poll_interval, poll_timeout)


# ═══════════════════════════════════════════════════════════════════════
# META CREATION
# ═══════════════════════════════════════════════════════════════════════

def _build_enhancements_spec(enable: bool) -> dict:
    """
    Build degrees_of_freedom_spec with individual enhancement toggles.
    Keys are lowercase snake_case per the official SDK:
    github.com/facebook/facebook-python-business-sdk → AdCreativeFeaturesSpec
    """
    status = "OPT_IN" if enable else "OPT_OUT"
    s = {"enroll_status": status}
    return {
        "creative_features_spec": {
            "standard_enhancements_catalog": s,
            "image_touchups": s,                    # Visual touch-ups
            "image_brightness_and_contrast": s,
            "image_uncrop": s,
            "enhance_cta": s,                       # Enhance CTA
            "cv_transformation": s,                 # Add video effects
            "text_optimizations": s,
            "text_generation": s,
            "description_automation": s,
            "image_templates": s,
            "video_auto_crop": s,
            "image_animation": s,
            "image_auto_crop": s,
            "product_extensions": s,
            "profile_card": s,
            "inline_comment": s,
            "site_extensions": s,
        },
    }

def create_single_image_creative(
    ad_account_id: str,
    access_token: str,
    creative_name: str,
    fb_page_id: str,
    ig_page_id: str,
    image_hash: str,
    landing_url: str,
    primary_text: str,
    headline: str,
    description: str,
    utm_tags: str,
    enable_enhancements: bool = False,
) -> str:
    """Create a standard image ad creative. Returns creative_id."""
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adcreatives"

    object_story_spec = {
        "page_id": fb_page_id,
        "instagram_user_id": ig_page_id,
        "link_data": {
            "image_hash": image_hash,
            "link": landing_url,
            "message": primary_text,
            "name": headline,
            "description": description,
            "call_to_action": {
                "type": "LEARN_MORE",
                "value": {"link": landing_url},
            },
        },
    }

    params = {
        "name": creative_name,
        "object_story_spec": json.dumps(object_story_spec),
        "contextual_multi_ads": json.dumps({"enroll_status": "OPT_OUT"}),
        "degrees_of_freedom_spec": json.dumps(_build_enhancements_spec(enable_enhancements)),
        "url_tags": utm_tags,
    }

    result = meta_api_request("POST", url, access_token, params=params)
    creative_id = result.get("id")
    if not creative_id:
        raise RuntimeError(f"Image creative creation failed: {result}")

    print(f"  [CREATIVE] Image: {creative_id} ({creative_name})")
    return creative_id


def create_single_video_creative(
    ad_account_id: str,
    access_token: str,
    creative_name: str,
    fb_page_id: str,
    ig_page_id: str,
    video_id: str,
    thumbnail_hash: Optional[str],
    landing_url: str,
    primary_text: str,
    headline: str,
    description: str,
    utm_tags: str,
    enable_enhancements: bool = False,
) -> str:
    """Create a standard video ad creative. Returns creative_id."""
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adcreatives"

    video_data = {
        "video_id": video_id,
        "message": primary_text,
        "title": headline,
        "link_description": description,
        "call_to_action": {
            "type": "LEARN_MORE",
            "value": {"link": landing_url},
        },
    }
    if thumbnail_hash:
        video_data["image_hash"] = thumbnail_hash

    object_story_spec = {
        "page_id": fb_page_id,
        "instagram_user_id": ig_page_id,
        "video_data": video_data,
    }

    params = {
        "name": creative_name,
        "object_story_spec": json.dumps(object_story_spec),
        "contextual_multi_ads": json.dumps({"enroll_status": "OPT_OUT"}),
        "degrees_of_freedom_spec": json.dumps(_build_enhancements_spec(enable_enhancements)),
        "url_tags": utm_tags,
    }

    result = meta_api_request("POST", url, access_token, params=params)
    creative_id = result.get("id")
    if not creative_id:
        raise RuntimeError(f"Video creative creation failed: {result}")

    print(f"  [CREATIVE] Video: {creative_id} ({creative_name})")
    return creative_id


def create_multi_placement_creative(
    ad_account_id: str,
    access_token: str,
    creative_name: str,
    fb_page_id: str,
    ig_page_id: str,
    media_type: str,
    feed_asset_id: str,
    story_asset_id: str,
    feed_thumbnail_hash: Optional[str],
    story_thumbnail_hash: Optional[str],
    landing_url: str,
    primary_text: str,
    headline: str,
    description: str,
    utm_tags: str,
    enable_enhancements: bool = False,
) -> str:
    """
    Create a multi-placement creative using asset_feed_spec.
    Maps feed asset to feed placements, story asset to story/reels.
    Returns creative_id.
    """
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adcreatives"

    if media_type == "video":
        feed_asset = {"video_id": feed_asset_id, "adlabels": [{"name": "feed"}]}
        story_asset = {"video_id": story_asset_id, "adlabels": [{"name": "story"}]}
        if feed_thumbnail_hash:
            feed_asset["thumbnail_hash"] = feed_thumbnail_hash
        if story_thumbnail_hash:
            story_asset["thumbnail_hash"] = story_thumbnail_hash
        asset_key = "videos"
        label_key = "video_label"
        ad_format = "SINGLE_VIDEO"
    else:
        feed_asset = {"hash": feed_asset_id, "adlabels": [{"name": "feed"}]}
        story_asset = {"hash": story_asset_id, "adlabels": [{"name": "story"}]}
        asset_key = "images"
        label_key = "image_label"
        ad_format = "SINGLE_IMAGE"

    asset_feed_spec = {
        asset_key: [feed_asset, story_asset],
        "bodies": [{"text": primary_text}],
        "titles": [{"text": headline}],
        "descriptions": [{"text": description}],
        "link_urls": [{"website_url": landing_url}],
        "call_to_action_types": ["LEARN_MORE"],
        "ad_formats": [ad_format],
        "asset_customization_rules": [
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram"],
                    "facebook_positions": ["feed"],
                    "instagram_positions": ["stream"],
                },
                label_key: {"name": "feed"},
            },
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram"],
                    "facebook_positions": ["story", "facebook_reels"],
                    "instagram_positions": ["story", "reels"],
                },
                label_key: {"name": "story"},
            },
        ],
    }

    params = {
        "name": creative_name,
        "asset_feed_spec": json.dumps(asset_feed_spec),
        "object_story_spec": json.dumps({
            "page_id": fb_page_id,
            "instagram_user_id": ig_page_id,
        }),
        "contextual_multi_ads": json.dumps({"enroll_status": "OPT_OUT"}),
        "degrees_of_freedom_spec": json.dumps(_build_enhancements_spec(enable_enhancements)),
        "url_tags": utm_tags,
    }

    result = meta_api_request("POST", url, access_token, params=params)
    creative_id = result.get("id")
    if not creative_id:
        raise RuntimeError(f"Multi-placement creative creation failed: {result}")

    print(f"  [CREATIVE] Multi-placement: {creative_id} ({creative_name})")
    return creative_id


def build_attribution_spec(optimization: str) -> list[dict]:
    """Map optimization string to attribution_spec."""
    opt = optimization.lower().strip()
    if opt == "1dc":
        return [{"event_type": "CLICK_THROUGH", "window_days": 1}]
    elif opt == "7dc":
        return [{"event_type": "CLICK_THROUGH", "window_days": 7}]
    elif opt == "7dc1dv":
        return [
            {"event_type": "CLICK_THROUGH", "window_days": 7},
            {"event_type": "VIEW_THROUGH", "window_days": 1},
        ]
    elif opt == "7dc1dv1ev":
        return [
            {"event_type": "CLICK_THROUGH", "window_days": 7},
            {"event_type": "VIEW_THROUGH", "window_days": 1},
            {"event_type": "ENGAGED_VIDEO_VIEW", "window_days": 1},
        ]
    else:
        raise ValueError(f"Unknown optimization: '{opt}'. Expected '1dc', '7dc', '7dc1dv', or '7dc1dv1ev'")


def create_adset(
    ad_account_id: str,
    access_token: str,
    adset_name: str,
    campaign_id: str,
    daily_budget: str,
    pixel_id: str,
    destination_type: str,
    country: str,
    optimization: str,
    dsa_beneficiary: str = None,
    dsa_payor: str = None,
) -> str:
    """Create an ad set (PAUSED). Returns adset_id."""
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/adsets"

    attribution_spec = build_attribution_spec(optimization)

    params = {
        "name": adset_name,
        "campaign_id": campaign_id,
        "daily_budget": daily_budget,
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "promoted_object": json.dumps({
            "pixel_id": pixel_id,
            "custom_event_type": "PURCHASE",
        }),
        "destination_type": destination_type,
        "targeting": json.dumps({
            "geo_locations": {"countries": [country]},
            "targeting_automation": {"advantage_audience": 1},
        }),
        "attribution_spec": json.dumps(attribution_spec),
        "status": "PAUSED",
    }
    if dsa_beneficiary:
        params["dsa_beneficiary"] = dsa_beneficiary
    if dsa_payor:
        params["dsa_payor"] = dsa_payor

    result = meta_api_request("POST", url, access_token, params=params)
    adset_id = result.get("id")
    if not adset_id:
        raise RuntimeError(f"Ad set creation failed: {result}")

    print(f"  [ADSET] Created: {adset_id} ({adset_name}) [PAUSED]")
    return adset_id


def create_ad(
    ad_account_id: str,
    access_token: str,
    ad_name: str,
    adset_id: str,
    creative_id: str,
    source_ad_id: str = None,
) -> str:
    """Create an ad linking creative to adset. Returns ad_id."""
    url = f"{META_GRAPH_URL}/act_{ad_account_id}/ads"

    params = {
        "name": ad_name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status": "ACTIVE",
    }
    if source_ad_id:
        params["source_ad_id"] = source_ad_id

    result = meta_api_request("POST", url, access_token, params=params, retries=5)
    ad_id = result.get("id")
    if not ad_id:
        raise RuntimeError(f"Ad creation failed: {result}")

    print(f"  [AD] Created: {ad_id} ({ad_name})")
    return ad_id


# ═══════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

def launch_ads(
    account_name: str,
    drive_link: str,
    adset_name: str,
    campaign_id: str,
    daily_budget: str,
    landing_url: str,
    primary_text,           # str or list[str] for combinatorial
    headline,               # str or list[str] for combinatorial
    description: str,
    country: str,
    optimization: str,
    destination_type: str = "WEBSITE",
    utm_tags: str = DEFAULT_UTM,
    naming_template: str = None,
    source_ad_id: str = None,
    enable_enhancements: bool = False,
    dsa_beneficiary: str = None,
    dsa_payor: str = None,
    dry_run: bool = False,
) -> LaunchResult:
    """
    Main orchestrator. Runs the full pipeline:
    1. Load account config
    2. List & pair Drive files
    3. Upload assets to Meta
    4. Generate thumbnails
    5. Create creatives (single or multi-placement)
    6. Create ad set
    7. Create ads (with combinatorial text/headline variations)
    """
    result = LaunchResult(account_name=account_name)
    TMP_DIR.mkdir(exist_ok=True)

    # Normalize text inputs to lists for combinatorial
    primary_texts = [primary_text] if isinstance(primary_text, str) else list(primary_text)
    headlines = [headline] if isinstance(headline, str) else list(headline)

    # Load naming template
    config = load_config()
    naming_tpl = naming_template or config.get("naming_template", "{concept}")
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'=' * 60}")
    print(f"META ADS BULK LAUNCHER")
    print(f"{'=' * 60}")
    print(f"Account: {account_name}")
    print(f"Ad Set:  {adset_name}")
    print(f"Copy variations: {len(primary_texts)} texts x {len(headlines)} headlines")
    print(f"{'=' * 60}\n")

    # ── Step 1: Account Config ──
    try:
        print("[STEP 1] Loading account config...")
        acct = get_account_config(account_name)
        print(f"  Ad Account: {acct.ad_account_id}")
        print(f"  FB Page: {acct.fb_page_id}")
        print(f"  IG Page: {acct.ig_page_id}")
        print(f"  Pixel: {acct.pixel_id}")
    except Exception as e:
        result.errors.append(f"Config: {e}")
        _save_result(result)
        return result

    # ── Step 2: Meta Token ──
    access_token = os.getenv("META_ACCESS_TOKEN", "")
    if not access_token:
        result.errors.append(
            "META_ACCESS_TOKEN not set in .env. "
            "Generate at developers.facebook.com -> Marketing API -> Tools"
        )
        _save_result(result)
        return result

    # ── Step 3: List & Pair Drive Files ──
    try:
        print("\n[STEP 2] Listing Google Drive files...")
        folder_id = extract_folder_id(drive_link)
        drive_files = list_drive_files(folder_id)
        creative_groups = pair_creatives(drive_files)
    except Exception as e:
        result.errors.append(f"Drive: {e}")
        _save_result(result)
        return result

    # ── Dry Run Preview ──
    if dry_run:
        total_creatives = len(creative_groups)
        total_ads = total_creatives * len(primary_texts) * len(headlines)
        print(f"\n{'=' * 60}")
        print(f"DRY RUN PREVIEW")
        print(f"{'=' * 60}")
        print(f"Ad Set: \"{adset_name}\" (PAUSED, ${int(daily_budget)/100:.0f}/day, {country}, {optimization})")
        for g in creative_groups:
            name_ctx = {"date": today, "account": account_name, "concept": g.concept,
                        "format": g.media_type, "ratio": "multi" if g.is_multi_placement else "feed"}
            ad_name = generate_name(naming_tpl, name_ctx)
            placement = "feed + stories/reels" if g.is_multi_placement else "feed only"
            print(f"  Creative: \"{ad_name}\" ({g.media_type}, {placement})")
            if len(primary_texts) > 1 or len(headlines) > 1:
                for pt_idx, hl_idx in itertools_product(range(len(primary_texts)), range(len(headlines))):
                    print(f"    Ad variation: text#{pt_idx+1} x headline#{hl_idx+1}")
        print(f"\nTotal: {total_creatives} creatives x {len(primary_texts)*len(headlines)} variations = {total_ads} ads")
        print(f"{'=' * 60}\n")
        result.success = True
        return result

    # ── Step 4: Upload Assets ──
    for group in creative_groups:
        # Upload feed asset
        if group.feed_file:
            try:
                print(f"\n[STEP 3] Uploading feed asset: {group.feed_file['name']}")
                if group.media_type == "image":
                    img_bytes = download_to_memory(group.feed_file["id"])
                    group.feed_image_hash = upload_image_bytes(
                        img_bytes, group.feed_file["name"], acct.ad_account_id, access_token
                    )
                    result.uploaded_images.append({"name": group.feed_file["name"], "hash": group.feed_image_hash})
                else:
                    # Upload video directly (Drive URLs don't work for Meta URL passthrough)
                    group.feed_video_id = upload_video_direct(
                        group.feed_file["id"], group.feed_file["name"],
                        acct.ad_account_id, access_token,
                    )
                    # Generate thumbnail from Meta auto-thumbnails
                    group.feed_thumbnail_hash = generate_thumbnail(
                        None, group.feed_video_id, group.feed_file["name"],
                        acct.ad_account_id, access_token
                    )
                    result.uploaded_videos.append({"name": group.feed_file["name"], "id": group.feed_video_id})
            except Exception as e:
                result.errors.append(f"Upload feed {group.feed_file['name']}: {e}")
                continue

        # Upload story asset (for multi-placement)
        if group.is_multi_placement and group.story_file:
            try:
                print(f"\n[STEP 3] Uploading story asset: {group.story_file['name']}")
                if group.media_type == "image":
                    img_bytes = download_to_memory(group.story_file["id"])
                    group.story_image_hash = upload_image_bytes(
                        img_bytes, group.story_file["name"], acct.ad_account_id, access_token
                    )
                    result.uploaded_images.append({"name": group.story_file["name"], "hash": group.story_image_hash})
                else:
                    # Upload video directly (Drive URLs don't work for Meta URL passthrough)
                    group.story_video_id = upload_video_direct(
                        group.story_file["id"], group.story_file["name"],
                        acct.ad_account_id, access_token,
                    )
                    # Generate thumbnail from Meta auto-thumbnails
                    group.story_thumbnail_hash = generate_thumbnail(
                        None, group.story_video_id, group.story_file["name"],
                        acct.ad_account_id, access_token
                    )
                    result.uploaded_videos.append({"name": group.story_file["name"], "id": group.story_video_id})
            except Exception as e:
                result.errors.append(f"Upload story {group.story_file['name']}: {e}")
                group.is_multi_placement = False  # Downgrade to single


    # ── Step 5: Create Creatives (combinatorial) ──
    creative_entries = []  # list of (creative_id, ad_name)

    for group in creative_groups:
        for pt_idx, hl_idx in itertools_product(range(len(primary_texts)), range(len(headlines))):
            pt = primary_texts[pt_idx]
            hl = headlines[hl_idx]

            name_ctx = {
                "date": today, "account": account_name, "concept": group.concept,
                "format": group.media_type,
                "ratio": "multi" if group.is_multi_placement else "feed",
                "headline": hl[:20], "adset": adset_name,
            }
            creative_name = generate_name(naming_tpl, name_ctx)
            if len(primary_texts) > 1 or len(headlines) > 1:
                creative_name += f"_t{pt_idx+1}h{hl_idx+1}"

            try:
                print(f"\n[STEP 4] Creating creative: {creative_name}")

                if group.is_multi_placement:
                    feed_id = group.feed_image_hash if group.media_type == "image" else group.feed_video_id
                    story_id = group.story_image_hash if group.media_type == "image" else group.story_video_id
                    if feed_id and story_id:
                        cid = create_multi_placement_creative(
                            ad_account_id=acct.ad_account_id,
                            access_token=access_token,
                            creative_name=creative_name,
                            fb_page_id=acct.fb_page_id,
                            ig_page_id=acct.ig_page_id,
                            media_type=group.media_type,
                            feed_asset_id=feed_id,
                            story_asset_id=story_id,
                            feed_thumbnail_hash=group.feed_thumbnail_hash,
                            story_thumbnail_hash=group.story_thumbnail_hash,
                            landing_url=landing_url,
                            primary_text=pt,
                            headline=hl,
                            description=description,
                            utm_tags=utm_tags,
                            enable_enhancements=enable_enhancements,
                        )
                        creative_entries.append((cid, creative_name))
                        result.creative_ids.append(cid)
                    else:
                        result.errors.append(f"Missing assets for multi-placement: {group.concept}")

                elif group.media_type == "image" and group.feed_image_hash:
                    cid = create_single_image_creative(
                        ad_account_id=acct.ad_account_id,
                        access_token=access_token,
                        creative_name=creative_name,
                        fb_page_id=acct.fb_page_id,
                        ig_page_id=acct.ig_page_id,
                        image_hash=group.feed_image_hash,
                        landing_url=landing_url,
                        primary_text=pt,
                        headline=hl,
                        description=description,
                        utm_tags=utm_tags,
                        enable_enhancements=enable_enhancements,
                    )
                    creative_entries.append((cid, creative_name))
                    result.creative_ids.append(cid)

                elif group.media_type == "video" and group.feed_video_id:
                    cid = create_single_video_creative(
                        ad_account_id=acct.ad_account_id,
                        access_token=access_token,
                        creative_name=creative_name,
                        fb_page_id=acct.fb_page_id,
                        ig_page_id=acct.ig_page_id,
                        video_id=group.feed_video_id,
                        thumbnail_hash=group.feed_thumbnail_hash,
                        landing_url=landing_url,
                        primary_text=pt,
                        headline=hl,
                        description=description,
                        utm_tags=utm_tags,
                        enable_enhancements=enable_enhancements,
                    )
                    creative_entries.append((cid, creative_name))
                    result.creative_ids.append(cid)
                else:
                    result.errors.append(f"No uploaded asset for: {group.concept}")

            except Exception as e:
                result.errors.append(f"Creative {creative_name}: {e}")

    if not creative_entries:
        result.errors.append("No creatives were created. Cannot proceed.")
        _save_result(result)
        return result

    # ── Step 6: Create Ad Set ──
    try:
        print(f"\n[STEP 5] Creating ad set: {adset_name}")
        adset_id = create_adset(
            ad_account_id=acct.ad_account_id,
            access_token=access_token,
            adset_name=adset_name,
            campaign_id=campaign_id,
            daily_budget=daily_budget,
            pixel_id=acct.pixel_id,
            destination_type=destination_type,
            country=country,
            optimization=optimization,
            dsa_beneficiary=dsa_beneficiary,
            dsa_payor=dsa_payor,
        )
        result.adset_id = adset_id
        result.adset_name = adset_name
    except Exception as e:
        result.errors.append(f"Ad set: {e}")
        _save_result(result)
        return result

    # ── Step 7: Create Ads ──
    for creative_id, ad_name in creative_entries:
        try:
            print(f"\n[STEP 6] Creating ad: {ad_name}")
            ad_id = create_ad(
                ad_account_id=acct.ad_account_id,
                access_token=access_token,
                ad_name=ad_name,
                adset_id=adset_id,
                creative_id=creative_id,
                source_ad_id=source_ad_id,
            )
            result.ad_ids.append(ad_id)
        except Exception as e:
            result.errors.append(f"Ad {ad_name}: {e}")

    result.success = len(result.ad_ids) > 0
    _save_result(result)
    _print_summary(result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# ROLLBACK
# ═══════════════════════════════════════════════════════════════════════

def rollback_launch(launch_file: str, token: str = None):
    """Bulk-pause all entities from a launch result file."""
    token = token or _get_meta_token()

    with open(launch_file) as f:
        data = json.load(f)

    print(f"\n[ROLLBACK] Pausing entities from {launch_file}")

    # Pause ads
    for ad_id in data.get("ad_ids", []):
        try:
            url = f"{META_GRAPH_URL}/{ad_id}"
            meta_api_request("POST", url, token, params={"status": "PAUSED"})
            print(f"  Paused ad: {ad_id}")
        except Exception as e:
            print(f"  Failed to pause ad {ad_id}: {e}")

    # Pause ad set
    adset_id = data.get("adset_id")
    if adset_id:
        try:
            url = f"{META_GRAPH_URL}/{adset_id}"
            meta_api_request("POST", url, token, params={"status": "PAUSED"})
            print(f"  Paused ad set: {adset_id}")
        except Exception as e:
            print(f"  Failed to pause ad set {adset_id}: {e}")

    print("[ROLLBACK] Complete")


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _save_result(result: LaunchResult):
    """Save launch result to .tmp/ for debugging and rollback."""
    TMP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    launch_file = TMP_DIR / f"launch_{timestamp}.json"
    result.launch_file = str(launch_file)
    with open(launch_file, "w") as f:
        json.dump(asdict(result), f, indent=2, default=str)


def _print_summary(result: LaunchResult):
    """Print final summary."""
    print(f"\n{'=' * 60}")
    if result.success:
        status = "COMPLETE" if not result.errors else "PARTIAL SUCCESS"
    else:
        status = "FAILED"
    print(f"LAUNCH {status}")
    print(f"{'=' * 60}")
    print(f"  Ad Set:    {result.adset_id or 'NOT CREATED'} ({result.adset_name or '-'})")
    print(f"  Creatives: {len(result.creative_ids)}")
    print(f"  Ads:       {len(result.ad_ids)}")
    if result.errors:
        print(f"  Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"    - {err}")
    if result.launch_file:
        print(f"  Log:       {result.launch_file}")
    print(f"{'=' * 60}\n")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Meta Ads Bulk Launcher - Upload creatives and create ads"
    )
    parser.add_argument("--account", required=True, help="Account name from config")
    parser.add_argument("--drive-link", required=True, help="Google Drive folder URL or ID")
    parser.add_argument("--campaign-id", required=True, help="Meta campaign ID")
    parser.add_argument("--adset-name", required=True, help="Name for the new ad set")
    parser.add_argument("--url", required=True, help="Landing page URL")
    parser.add_argument("--primary-text", required=True, nargs="+", help="Ad copy (multiple for variations)")
    parser.add_argument("--headline", required=True, nargs="+", help="Headline (multiple for variations)")
    parser.add_argument("--description", required=True, help="Ad description")
    parser.add_argument("--budget", required=True, help="Daily budget in cents (5000 = $50)")
    parser.add_argument("--country", required=True, help="Country code (US, CA, etc.)")
    parser.add_argument("--optimization", required=True, choices=["1dc", "7dc", "7dc1dv", "7dc1dv1ev"],
                        help="Attribution window")
    parser.add_argument("--destination-type", default="WEBSITE", help="WEBSITE or UNDEFINED")
    parser.add_argument("--utm-tags", default=DEFAULT_UTM, help="UTM tags")
    parser.add_argument("--naming-template", help="Naming convention template")
    parser.add_argument("--source-ad-id", help="Source ad ID for post ID preservation")
    parser.add_argument("--enable-enhancements", action="store_true",
                        help="Enable Advantage+ creative enhancements")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating anything")

    args = parser.parse_args()

    texts = args.primary_text if len(args.primary_text) > 1 else args.primary_text[0]
    hdls = args.headline if len(args.headline) > 1 else args.headline[0]

    result = launch_ads(
        account_name=args.account,
        drive_link=args.drive_link,
        adset_name=args.adset_name,
        campaign_id=args.campaign_id,
        daily_budget=args.budget,
        landing_url=args.url,
        primary_text=texts,
        headline=hdls,
        description=args.description,
        country=args.country,
        optimization=args.optimization,
        destination_type=args.destination_type,
        utm_tags=args.utm_tags,
        naming_template=args.naming_template,
        source_ad_id=args.source_ad_id,
        enable_enhancements=args.enable_enhancements,
        dry_run=args.dry_run,
    )

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
