import asyncio
import hashlib
import html
import json
import random
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright


# ============================================================
# PATHS AND CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

GALLERY_FILE = BASE_DIR / "gallery.json"
GALLERY_MEDIA_DIR = BASE_DIR / "gallery_media"
GITKEEP_FILE = GALLERY_MEDIA_DIR / ".gitkeep"

RAW_GALLERY_BASE = (
    "https://raw.githubusercontent.com/"
    "reikableu/hou-minghao-updates/main/gallery_media"
)

SINGAPORE = ZoneInfo("Asia/Singapore")

# Weibo profile UIDs to collect
TARGET_WEIBO_UIDS = [
    "5763552342",
    "1831550987",
    "2356308454",
    "8450716290",
    "8004004308",
]

MAX_GALLERY_ITEMS = 150
REQUEST_TIMEOUT = 30
PROFILE_DELAY_SECONDS = 2
DOWNLOAD_DELAY_SECONDS = 0.25
MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    """Return the current Singapore time in ISO format."""

    return datetime.now(SINGAPORE).isoformat(timespec="seconds")


def prepare_directories():
    """
    Create the gallery media directory before collection starts.

    A .gitkeep file is added so Git can track the directory even
    when the collector does not find any images.
    """

    GALLERY_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    if not GITKEEP_FILE.exists():
        GITKEEP_FILE.write_text(
            "This file keeps the gallery_media directory in Git.\n",
            encoding="utf-8",
        )

    print("=" * 70)
    print("GALLERY DIRECTORY PREPARATION")
    print("=" * 70)
    print(f"Working directory: {Path.cwd()}")
    print(f"Script directory: {BASE_DIR}")
    print(f"Gallery JSON file: {GALLERY_FILE}")
    print(f"Gallery media directory: {GALLERY_MEDIA_DIR}")
    print(
        "Gallery media directory exists: "
        f"{GALLERY_MEDIA_DIR.exists()}"
    )
    print(f".gitkeep exists: {GITKEEP_FILE.exists()}")
    print("=" * 70)


def clean_text(value):
    """Remove HTML tags and normalize whitespace."""

    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def load_existing_items():
    """Load existing gallery items from gallery.json."""

    if not GALLERY_FILE.exists():
        print("No existing gallery.json file was found.")
        return []

    try:
        data = json.loads(
            GALLERY_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or []
        else:
            items = []

        valid_items = [
            item
            for item in items
            if isinstance(item, dict)
        ]

        print(
            f"Loaded {len(valid_items)} existing gallery items."
        )

        return valid_items

    except Exception as error:
        print(
            "Existing gallery could not be read: "
            f"{type(error).__name__}: {error}"
        )

        return []


# ============================================================
# WEIBO COLLECTION
# ============================================================

async def fetch_profile_candidates(page, uid):
    """
    Fetch photo candidates from a Weibo profile.

    Playwright is used so the request is made through a browser
    context, which may handle cookies and visitor sessions better
    than a direct requests call.
    """

    container_id = f"107603{uid}"

    target_url = (
        "https://m.weibo.cn/api/container/getIndex"
        f"?type=uid"
        f"&value={uid}"
        f"&containerid={container_id}"
    )

    print()
    print("=" * 70)
    print(f"FETCHING WEIBO PROFILE UID: {uid}")
    print("=" * 70)
    print(f"Endpoint: {target_url}")

    candidates = []

    try:
        response = await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if not response:
            print(f"No browser response was received for UID {uid}.")
            return []

        print(f"HTTP status for UID {uid}: {response.status}")

        if response.status != 200:
            print(
                f"Weibo returned HTTP status {response.status} "
                f"for UID {uid}."
            )
            return []

        # First try reading the actual HTTP response body.
        try:
            body_text = await response.text()
        except Exception:
            body_text = ""

        # Fall back to the visible browser body.
        if not body_text or not body_text.strip():
            body_text = await page.evaluate(
                """
                () => {
                    if (!document.body) {
                        return "";
                    }

                    return document.body.innerText || "";
                }
                """
            )

        body_text = (body_text or "").strip()

        if not body_text:
            print(f"Empty response body for UID {uid}.")
            return []

        try:
            payload = json.loads(body_text)

        except json.JSONDecodeError as error:
            print(
                f"Response was not valid JSON for UID {uid}: "
                f"{error}"
            )
            print(f"Response preview: {body_text[:1000]}")
            return []

        if not isinstance(payload, dict):
            print(
                f"Unexpected response structure for UID {uid}. "
                f"Expected a JSON object."
            )
            print(f"Response preview: {body_text[:1000]}")
            return []

        api_ok = payload.get("ok")
        api_message = payload.get("msg") or ""

        print(
            f"Weibo API result for UID {uid}: "
            f"ok={api_ok}, msg={api_message}"
        )

        if api_ok not in (1, "1", True):
            print(
                f"Weibo API did not return usable data for UID {uid}."
            )
            print(
                "Payload preview: "
                f"{json.dumps(payload, ensure_ascii=False)[:1500]}"
            )
            return []

        data = payload.get("data") or {}

        if not isinstance(data, dict):
            print(f"No valid data object was returned for UID {uid}.")
            return []

        cards = data.get("cards") or []

        if not isinstance(cards, list):
            cards = []

        print(f"Found {len(cards)} cards for UID {uid}.")

        if not cards:
            print(f"No cards were returned for UID {uid}.")
            print(
                "Payload preview: "
                f"{json.dumps(payload, ensure_ascii=False)[:1500]}"
            )
            return []

        posts_found = 0
        pictures_found = 0

        for card in cards:
            if not isinstance(card, dict):
                continue

            mblog = card.get("mblog")

            if not isinstance(mblog, dict):
                continue

            posts_found += 1

            post_id = str(
                mblog.get("id")
                or mblog.get("mid")
                or ""
            ).strip()

            if not post_id:
                print("Skipped one card because it had no post ID.")
                continue

            user = mblog.get("user") or {}

            if not isinstance(user, dict):
                user = {}

            user_name = (
                user.get("screen_name")
                or f"Weibo_{uid}"
            )

            raw_text = (
                mblog.get("raw_text")
                or mblog.get("text")
                or ""
            )

            raw_text = clean_text(raw_text)

            if raw_text:
                title = f"{user_name}: {raw_text[:100]}"

                if len(raw_text) > 100:
                    title += "..."
            else:
                title = f"Photo by {user_name}"

            pics = mblog.get("pics") or []

            if not isinstance(pics, list):
                pics = []

            print(
                f"Post {post_id}: "
                f"{len(pics)} attached picture(s)"
            )

            for picture_index, picture in enumerate(pics):
                if not isinstance(picture, dict):
                    continue

                large_picture = picture.get("large") or {}

                if not isinstance(large_picture, dict):
                    large_picture = {}

                image_url = (
                    large_picture.get("url")
                    or picture.get("url")
                    or ""
                )

                image_url = str(image_url).strip()

                if not image_url:
                    print(
                        f"Post {post_id}, picture "
                        f"{picture_index}: no image URL found."
                    )
                    continue

                if image_url.startswith("//"):
                    image_url = "https:" + image_url

                if not image_url.startswith(
                    ("http://", "https://")
                ):
                    print(
                        f"Post {post_id}, picture "
                        f"{picture_index}: invalid image URL."
                    )
                    continue

                photo_id = (
                    f"wb_{post_id}_{picture_index}"
                )

                candidates.append(
                    {
                        "photo_id": photo_id,
                        "post_url": (
                            f"https://m.weibo.cn/status/{post_id}"
                        ),
                        "image_url": image_url,
                        "title": title,
                        "author": user_name,
                        "uid": uid,
                    }
                )

                pictures_found += 1

        print(f"Posts found for UID {uid}: {posts_found}")
        print(
            f"Usable photo candidates for UID {uid}: "
            f"{pictures_found}"
        )

    except Exception as error:
        print(
            f"Error fetching UID {uid} with Playwright: "
            f"{type(error).__name__}: {error}"
        )

    return candidates


# ============================================================
# IMAGE DOWNLOAD
# ============================================================

def extension_for(content_type, url):
    """Determine an appropriate image file extension."""

    content_type = (content_type or "").lower()

    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"

    if "png" in content_type:
        return ".png"

    if "webp" in content_type:
        return ".webp"

    if "gif" in content_type:
        return ".gif"

    url_path = urllib.parse.urlsplit(url).path
    suffix = Path(url_path).suffix.lower()

    allowed_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }

    if suffix in allowed_extensions:
        return suffix

    return ".jpg"


def download_candidate(session, item):
    """Download one image and return its gallery record."""

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://m.weibo.cn/",
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        ),
    }

    response = session.get(
        item["image_url"],
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = (
        response.headers.get("Content-Type") or ""
    ).lower()

    content_length = len(response.content)

    if not content_type.startswith("image/"):
        raise ValueError(
            f"Response was not an image. "
            f"Content-Type: {content_type or 'unknown'}"
        )

    if not response.content:
        raise ValueError("Image response was empty.")

    if content_length > MAX_IMAGE_SIZE_BYTES:
        raise ValueError(
            f"Image was larger than 15 MB: "
            f"{content_length} bytes"
        )

    content_hash = hashlib.sha256(
        response.content
    ).hexdigest()

    extension = extension_for(
        content_type,
        item["image_url"],
    )

    filename = (
        f"{item['photo_id']}_"
        f"{content_hash[:12]}"
        f"{extension}"
    )

    path = GALLERY_MEDIA_DIR / filename

    if not path.exists():
        path.write_bytes(response.content)
        print(
            f"Saved image: {path.name} "
            f"({content_length:,} bytes)"
        )
    else:
        print(f"Image already exists: {path.name}")

    return {
        "id": item["photo_id"],
        "image": f"{RAW_GALLERY_BASE}/{filename}",
        "remoteImage": item["image_url"],
        "sourceUrl": item["post_url"],
        "platform": "Weibo",
        "source": f"Weibo (@{item['author']})",
        "title": item["title"],
        "date": "",
        "origin": "Weibo Profile",
        "imageHash": content_hash,
        "collectedAt": now_iso(),
    }


# ============================================================
# GALLERY MERGING AND CLEANUP
# ============================================================

def merge_and_shuffle_items(new_items, old_items):
    """Merge new and old records while removing duplicates."""

    combined = []
    seen_ids = set()
    seen_hashes = set()
    seen_images = set()

    for item in new_items + old_items:
        if not isinstance(item, dict):
            continue

        item_id = str(item.get("id") or "").strip()
        image_hash = str(
            item.get("imageHash") or ""
        ).strip()
        image_url = str(
            item.get("image") or ""
        ).strip()

        if not item_id and not image_url:
            continue

        if item_id and item_id in seen_ids:
            continue

        if image_hash and image_hash in seen_hashes:
            continue

        if image_url and image_url in seen_images:
            continue

        if item_id:
            seen_ids.add(item_id)

        if image_hash:
            seen_hashes.add(image_hash)

        if image_url:
            seen_images.add(image_url)

        combined.append(item)

    random.shuffle(combined)

    return combined[:MAX_GALLERY_ITEMS]


def remove_unused_files(items):
    """
    Delete local image files that are no longer referenced.

    Hidden files, including .gitkeep, are always preserved.
    """

    if not GALLERY_MEDIA_DIR.exists():
        return

    keep_filenames = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        image_url = str(item.get("image") or "")

        if not image_url:
            continue

        filename = Path(
            urllib.parse.urlsplit(image_url).path
        ).name

        if filename:
            keep_filenames.add(filename)

    removed_count = 0

    for path in GALLERY_MEDIA_DIR.iterdir():
        if not path.is_file():
            continue

        # Preserve .gitkeep and any other hidden files.
        if path.name.startswith("."):
            continue

        if path.name not in keep_filenames:
            path.unlink(missing_ok=True)
            removed_count += 1
            print(f"Removed unused image: {path.name}")

    print(f"Unused local images removed: {removed_count}")


def write_gallery_file(final_items, new_items_count):
    """Write gallery.json atomically."""

    output = {
        "lastUpdated": now_iso(),
        "status": (
            "Weibo gallery updated: "
            f"{new_items_count} downloaded, "
            f"{len(final_items)} display items"
        ),
        "uids": TARGET_WEIBO_UIDS,
        "total": len(final_items),
        "items": final_items,
    }

    temporary_file = GALLERY_FILE.with_suffix(
        ".json.tmp"
    )

    temporary_file.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(GALLERY_FILE)

    print(f"Gallery JSON written to: {GALLERY_FILE}")


# ============================================================
# MAIN PROCESS
# ============================================================

async def main_async():
    prepare_directories()

    existing_items = load_existing_items()

    existing_hashes = {
        str(item.get("imageHash") or "")
        for item in existing_items
        if isinstance(item, dict)
        and item.get("imageHash")
    }

    all_candidates = []

    successful_profiles = 0
    empty_profiles = 0

    print()
    print("=" * 70)
    print("STARTING WEIBO PROFILE COLLECTION")
    print("=" * 70)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={
                "width": 1365,
                "height": 768,
            },
            extra_http_headers={
                "Accept-Language": (
                    "en-US,en;q=0.9,zh-CN;q=0.8"
                )
            },
        )

        page = await context.new_page()

        # Warm up the Weibo browser session.
        try:
            print("Warming up the Weibo browser session...")

            warmup_response = await page.goto(
                "https://m.weibo.cn/",
                wait_until="domcontentloaded",
                timeout=20000,
            )

            if warmup_response:
                print(
                    "Weibo warmup HTTP status: "
                    f"{warmup_response.status}"
                )
            else:
                print("Weibo warmup returned no response.")

            await asyncio.sleep(2)

        except Exception as error:
            print(
                "Weibo warmup notice: "
                f"{type(error).__name__}: {error}"
            )

        for uid in TARGET_WEIBO_UIDS:
            photos = await fetch_profile_candidates(
                page,
                uid,
            )

            if photos:
                successful_profiles += 1
                all_candidates.extend(photos)
            else:
                empty_profiles += 1

            await asyncio.sleep(PROFILE_DELAY_SECONDS)

        await context.close()
        await browser.close()

    print()
    print("=" * 70)
    print("PROFILE COLLECTION SUMMARY")
    print("=" * 70)
    print(
        "Profiles with usable photos: "
        f"{successful_profiles}"
    )
    print(
        "Profiles without usable photos: "
        f"{empty_profiles}"
    )
    print(
        "Total photo candidates collected: "
        f"{len(all_candidates)}"
    )

    # Remove duplicate candidates before downloading.
    unique_candidates = []
    seen_candidate_ids = set()
    seen_candidate_urls = set()

    for candidate in all_candidates:
        photo_id = candidate.get("photo_id")
        image_url = candidate.get("image_url")

        if not photo_id or not image_url:
            continue

        if photo_id in seen_candidate_ids:
            continue

        if image_url in seen_candidate_urls:
            continue

        seen_candidate_ids.add(photo_id)
        seen_candidate_urls.add(image_url)
        unique_candidates.append(candidate)

    random.shuffle(unique_candidates)

    print(
        "Unique photo candidates to process: "
        f"{len(unique_candidates)}"
    )

    print()
    print("=" * 70)
    print("STARTING IMAGE DOWNLOAD")
    print("=" * 70)

    session = requests.Session()

    new_items = []
    downloaded_hashes = set()
    failed_downloads = 0
    duplicate_downloads = 0

    for index, item in enumerate(
        unique_candidates,
        start=1,
    ):
        photo_id = item.get("photo_id", "unknown")

        try:
            print()
            print(
                f"Processing image {index}/"
                f"{len(unique_candidates)}: {photo_id}"
            )

            record = download_candidate(
                session,
                item,
            )

            image_hash = record["imageHash"]

            if (
                image_hash in downloaded_hashes
                or image_hash in existing_hashes
            ):
                duplicate_downloads += 1

                duplicate_filename = Path(
                    urllib.parse.urlsplit(
                        record["image"]
                    ).path
                ).name

                duplicate_path = (
                    GALLERY_MEDIA_DIR
                    / duplicate_filename
                )

                # Only delete the newly named duplicate file.
                duplicate_path.unlink(missing_ok=True)

                print(
                    f"Duplicate image skipped: {photo_id}"
                )
                continue

            downloaded_hashes.add(image_hash)
            new_items.append(record)

            print(
                f"Downloaded successfully: {photo_id}"
            )

        except Exception as error:
            failed_downloads += 1

            print(
                f"Image skipped {photo_id}: "
                f"{type(error).__name__}: {error}"
            )

        time.sleep(DOWNLOAD_DELAY_SECONDS)

    session.close()

    final_items = merge_and_shuffle_items(
        new_items,
        existing_items,
    )

    remove_unused_files(final_items)

    write_gallery_file(
        final_items,
        len(new_items),
    )

    print()
    print("=" * 70)
    print("FINAL GALLERY SUMMARY")
    print("=" * 70)
    print(
        f"Candidate images: {len(unique_candidates)}"
    )
    print(
        f"New downloaded images: {len(new_items)}"
    )
    print(
        f"Duplicate images skipped: {duplicate_downloads}"
    )
    print(
        f"Failed image downloads: {failed_downloads}"
    )
    print(
        f"Final gallery items: {len(final_items)}"
    )
    print(
        f"Gallery media directory: "
        f"{GALLERY_MEDIA_DIR}"
    )
    print(
        f"Gallery media directory exists: "
        f"{GALLERY_MEDIA_DIR.exists()}"
    )
    print(
        "Gallery process completed successfully."
    )
    print("=" * 70)


def main():
    try:
        asyncio.run(main_async())

    except KeyboardInterrupt:
        print("Gallery process was stopped manually.")

    except Exception as error:
        print()
        print("=" * 70)
        print("GALLERY PROCESS FAILED")
        print("=" * 70)
        print(
            f"{type(error).__name__}: {error}"
        )
        raise


if __name__ == "__main__":
    main()
