#!/usr/bin/env python3
"""
Meta Ads Library Tool (via ScrapeCreators API)

Fetches ads from Meta Ads Library including full creative content
when available (age-gated content like ED meds requires FB login).

Setup:
    1. Get an API key at https://scrapecreators.com/?via=chrisrudy (100 free calls, no CC)
    2. Get your API key
    3. Add to .env: SCRAPECREATORS_KEY=your_key

Usage:
    python execution/meta_ads_library.py bluechew
    python execution/meta_ads_library.py nike --limit 20
"""

import os
import sys
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv(Path(__file__).parent.parent / ".env")

SCRAPECREATORS_KEY = os.getenv("SCRAPECREATORS_KEY")
BASE_URL = "https://api.scrapecreators.com/v1/facebook/adLibrary"


def search_ads(query: str, country: str = "US", limit: int = 30) -> dict:
    """Search Meta Ads Library by keyword."""
    if not SCRAPECREATORS_KEY:
        return {"error": "SCRAPECREATORS_KEY not found in .env"}

    url = f"{BASE_URL}/search/ads"
    params = {"query": query, "country": country}
    headers = {"x-api-key": SCRAPECREATORS_KEY}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {str(e)}"}


def format_ad(ad: dict) -> dict:
    """Extract key info from an ad."""
    snapshot = ad.get("snapshot", {})
    body = snapshot.get("body", {})
    ad_copy = body.get("text", "") if isinstance(body, dict) else str(body)

    cards = snapshot.get("cards", [])
    images = snapshot.get("images", [])
    videos = snapshot.get("videos", [])

    # Determine creative type
    has_video = len(videos) > 0 or any(c.get("video_sd_url") for c in cards)
    if has_video:
        creative_type = "video"
    elif len(cards) > 1:
        creative_type = "carousel"
    elif len(cards) == 1 or len(images) > 0:
        creative_type = "static_image"
    else:
        creative_type = "unknown"

    # Check if content is gated
    gated_type = ad.get("gated_type", "UNKNOWN")
    is_gated = gated_type == "LOGGED_OUT"

    return {
        "ad_id": ad.get("ad_archive_id", ""),
        "page_name": snapshot.get("page_name") or ad.get("page_name", "Unknown"),
        "page_id": snapshot.get("page_id") or ad.get("page_id", ""),
        "start_date": ad.get("start_date_string", ""),
        "is_active": ad.get("is_active", False),
        "creative_type": creative_type,
        "ad_copy": ad_copy if not is_gated else "[AGE-GATED]",
        "cards_count": len(cards),
        "has_video": has_video,
        "platforms": ad.get("publisher_platform", []),
        "gated_type": gated_type,
        "is_gated": is_gated,
        "url": ad.get("url", f"https://www.facebook.com/ads/library/?id={ad.get('ad_archive_id', '')}")
    }


def analyze_ads(ads: list) -> dict:
    """Generate summary analysis."""
    if not ads:
        return {"error": "No ads to analyze"}

    total = len(ads)
    gated_count = sum(1 for ad in ads if ad.get("is_gated"))
    visible_ads = [ad for ad in ads if not ad.get("is_gated")]

    video_count = sum(1 for ad in ads if ad.get("creative_type") == "video")
    static_count = sum(1 for ad in ads if ad.get("creative_type") == "static_image")
    carousel_count = sum(1 for ad in ads if ad.get("creative_type") == "carousel")

    # Unique page names (whitelisting detection)
    page_names = list(set(ad.get("page_name", "") for ad in ads if ad.get("page_name")))

    # Ad copy samples (from visible ads only)
    copy_samples = []
    for ad in visible_ads:
        copy = ad.get("ad_copy", "").strip()
        if copy and len(copy) > 20 and copy != "[AGE-GATED]":
            copy_samples.append(copy)

    return {
        "total_ads": total,
        "gated_ads": gated_count,
        "visible_ads": len(visible_ads),
        "is_age_gated": gated_count > total * 0.5,
        "creative_mix": {
            "video": video_count,
            "static_image": static_count,
            "carousel": carousel_count,
        },
        "page_names": page_names,
        "uses_whitelisting": len(page_names) > 1,
        "copy_samples": list(set(copy_samples))[:10]
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python meta_ads_library.py <brand_name> [--limit N]")
        print("\nSetup: Add SCRAPECREATORS_KEY to .env")
        print("Get your key: https://scrapecreators.com/?via=chrisrudy")
        sys.exit(1)

    query = sys.argv[1]
    limit = 30
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        if idx + 1 < len(sys.argv):
            limit = int(sys.argv[idx + 1])

    print(f"\n{'='*60}")
    print(f"Meta Ads Library Search")
    print(f"{'='*60}")
    print(f"Query: {query}")
    print(f"{'='*60}\n")

    result = search_ads(query, limit=limit)

    if "error" in result:
        print(f"❌ Error: {result['error']}")
        sys.exit(1)

    print(f"Credits remaining: {result.get('credits_remaining', 'N/A')}")

    ads_raw = result.get("searchResults", [])
    if not ads_raw:
        print(f"No ads found for '{query}'")
        sys.exit(0)

    ads = [format_ad(ad) for ad in ads_raw[:limit]]
    analysis = analyze_ads(ads)

    # Output
    print(f"\n📊 SUMMARY")
    print(f"   Total ads found: {analysis['total_ads']}")
    print(f"   Age-gated (hidden): {analysis['gated_ads']}")
    print(f"   Visible content: {analysis['visible_ads']}")

    if analysis.get("is_age_gated"):
        print(f"\n   ⚠️  This advertiser's content is AGE-GATED")
        print(f"      (ED, alcohol, cannabis, gambling, etc.)")
        print(f"      Full creative requires Facebook login")

    print(f"\n📱 Pages running ads ({len(analysis['page_names'])}):")
    for page in analysis["page_names"][:10]:
        print(f"   - {page}")

    if analysis.get("uses_whitelisting"):
        print(f"\n   ⚡ Uses WHITELISTING/SPARK ADS: YES")

    print(f"\n📹 Creative Mix:")
    mix = analysis["creative_mix"]
    print(f"   - Video: {mix['video']}")
    print(f"   - Static/Image: {mix['static_image']}")
    print(f"   - Carousel: {mix['carousel']}")

    if analysis.get("copy_samples"):
        print(f"\n📝 Ad Copy Samples:")
        for i, copy in enumerate(analysis["copy_samples"][:5], 1):
            preview = copy[:200] + "..." if len(copy) > 200 else copy
            print(f"\n   [{i}] {preview}")

    # Detailed view
    print(f"\n{'='*60}")
    print(f"AD DETAILS (first 10)")
    print(f"{'='*60}")
    for i, ad in enumerate(ads[:10], 1):
        print(f"\n--- Ad {i} ---")
        print(f"Page: {ad['page_name']}")
        print(f"Type: {ad['creative_type']}")
        print(f"Cards: {ad['cards_count']}")
        print(f"Started: {ad['start_date'][:10] if ad['start_date'] else 'N/A'}")
        print(f"Platforms: {', '.join(ad['platforms']) if ad['platforms'] else 'N/A'}")
        print(f"Gated: {ad['gated_type']}")
        if ad.get("ad_copy") and ad["ad_copy"] != "[AGE-GATED]":
            print(f"Copy: {ad['ad_copy'][:150]}...")

    # Save results
    tmp_dir = Path(__file__).parent.parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    output_file = tmp_dir / f"meta_ads_{query.replace(' ', '_').lower()}.json"

    with open(output_file, "w") as f:
        json.dump({
            "query": query,
            "analysis": analysis,
            "ads": ads,
        }, f, indent=2)

    print(f"\n💾 Results saved: {output_file}")


if __name__ == "__main__":
    main()
