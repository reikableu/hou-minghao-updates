import asyncio
import hashlib
import html
import json
import random
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

TARGET_WEIBO_UIDS = [
    "5763552342",
    "1831550987",
    "2356308454",
    "8450716290",
    "8004004308",
]

MAX_GALLERY_ITEMS = 150
MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024

PROFILE_DELAY_SECONDS = 2
DOWNLOAD_DELAY_SECONDS = 0.35
DOWNLOAD_TIMEOUT_MS = 45000
MAX_DOWNLOAD_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    """Return the current Singapore date and time."""

    return datetime.now(SINGAPORE).isoformat(
        timespec="seconds"
    )


def prepare_directories():
    """
    Create the gallery media directory.

    The .gitkeep file allows Git to retain gallery_media even
    when no image files have been downloaded yet.
    """

    GALLERY_MEDIA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not GITKEEP_FILE.exists():
        GITKEEP_FILE.write_text(
            "This file keeps the gallery_media directory in Git.\n",
            encoding="utf-8",
        )

    print("=" * 72)
    print("GALLERY DIRECTORY PREPARATION")
    print("=" * 72)
    print(f"Working directory: {Path.cwd()}")
    print(f"Script directory: {BASE_DIR}")
    print(f"Gallery JSON: {GALLERY_FILE}")
    print(f"Gallery media: {GALLERY_MEDIA_DIR}")
    print(
        "Gallery media exists: "
        f"{GALLERY_MEDIA_DIR.exists()}"
    )
    print(f".gitkeep exists: {GITKEEP_FILE.exists()}")
    print("=" * 72)


def clean_text(value):
    """Remove HTML tags and normalize whitespace."""

    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def load_existing_items():
    """Load existing gallery records from gallery.json."""

    if not GALLERY_FILE.exists():
        print("No existing gallery.json was found.")
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
# WEIBO PROFILE COLLECTION
# ============================================================

async def fetch_profile_candidates(page, uid):
    """
    Retrieve image candidates from one Weibo profile.
    """

    container_id = f"107603{uid}"

    target_url = (
        "https://m.weibo.cn/api/container/getIndex"
        f"?type=uid"
        f"&value={uid}"
        f"&containerid={container_id}"
    )

    print()
    print("=" * 72)
    print(f"FETCHING WEIBO PROFILE UID: {uid}")
    print("=" * 72)
    print(f"Endpoint: {target_url}")

    candidates = []

    try:
        response = await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if not response:
            print(f"No response received for UID {uid}.")
            return []

        print(f"HTTP status for UID {uid}: {response.status}")

        if response.status != 200:
            print(
                f"Weibo returned HTTP {response.status} "
                f"for UID {uid}."
            )
            return []

        try:
            body_text = await response.text()
        except Exception:
            body_text = ""

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
                f"Invalid JSON response for UID {uid}: "
                f"{error}"
            )
            print(f"Response preview: {body_text[:1200]}")
            return []

        if not isinstance(payload, dict):
            print(
                f"Unexpected response structure for UID {uid}."
            )
            return []

        api_ok = payload.get("ok")
        api_message = payload.get("msg") or ""

        print(
            f"Weibo API result for UID {uid}: "
            f"ok={api_ok}, msg={api_message}"
        )

        if api_ok not in (1, "1", True):
            print(
                f"Weibo API returned no usable data "
                f"for UID {uid}."
            )
            print(
                "Payload preview: "
                f"{json.dumps(payload, ensure_ascii=False)[:1500]}"
            )
            return []

        data = payload.get("data") or {}

        if not isinstance(data, dict):
            print(
                f"No valid data object returned for UID {uid}."
            )
            return []

        cards = data.get("cards") or []

        if not isinstance(cards, list):
            cards = []

        print(f"Found {len(cards)} cards for UID {uid}.")

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
                    continue

                if image_url.startswith("//"):
                    image_url = "https:" + image_url

                if not image_url.startswith(
                    ("http://", "https://")
                ):
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
            f"Error fetching UID {uid}: "
            f"{type(error).__name__}: {error}"
        )

    return candidates


# ============================================================
# IMAGE URL VARIANTS
# ============================================================

def normalize_sina_image_url(url):
    """Normalize a Sina image URL."""

    url = str(url or "").strip()

    if url.startswith("//"):
        url = "https:" + url

    return url


def build_image_url_variants(original_url):
    """
    Build alternate paths for a Sina image.

    Weibo images may be accessible through different size paths
    and different wx1-wx4 Sina image hosts.
    """

    original_url = normalize_sina_image_url(
        original_url
    )

    if not original_url:
        return []

    variants = []
    seen = set()

    def add_variant(value):
        value = normalize_sina_image_url(value)

        if not value:
            return

        if value not in seen:
            seen.add(value)
            variants.append(value)

    add_variant(original_url)

    size_paths = [
        "large",
        "original",
        "mw2000",
        "orj1080",
        "bmiddle",
    ]

    size_pattern = re.compile(
        (
            r"/(?:mw2000|large|original|orj1080|"
            r"orj360|bmiddle)/"
        ),
        re.IGNORECASE,
    )

    for size_name in size_paths:
        if size_pattern.search(original_url):
            variant = size_pattern.sub(
                f"/{size_name}/",
                original_url,
                count=1,
            )

            add_variant(variant)

    parsed = urllib.parse.urlsplit(original_url)
    host = parsed.netloc.lower()

    if re.fullmatch(r"wx[1-4]\.sinaimg\.cn", host):
        for number in range(1, 5):
            alternate_host = (
                f"wx{number}.sinaimg.cn"
            )

            variant = urllib.parse.urlunsplit(
                (
                    parsed.scheme or "https",
                    alternate_host,
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )

            add_variant(variant)

    return variants


# ============================================================
# IMAGE VALIDATION
# ============================================================

def image_extension(content_type, url, content):
    """
    Determine the image extension from the response headers,
    file signature, or source URL.
    """

    content_type = (content_type or "").lower()

    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"

    if "png" in content_type:
        return ".png"

    if "webp" in content_type:
        return ".webp"

    if "gif" in content_type:
        return ".gif"

    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    if (
        content.startswith(b"GIF87a")
        or content.startswith(b"GIF89a")
    ):
        return ".gif"

    if (
        len(content) >= 12
        and content[0:4] == b"RIFF"
        and content[8:12] == b"WEBP"
    ):
        return ".webp"

    url_path = urllib.parse.urlsplit(url).path
    suffix = Path(url_path).suffix.lower()

    if suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }:
        return suffix

    return ".jpg"


def is_valid_image_content(content, content_type):
    """Confirm that the downloaded body is an image."""

    if not content:
        return False

    if len(content) > MAX_IMAGE_SIZE_BYTES:
        return False

    content_type = (content_type or "").lower()

    if content_type.startswith("image/"):
        return True

    if content.startswith(b"\xff\xd8\xff"):
        return True

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True

    if content.startswith(b"GIF87a"):
        return True

    if content.startswith(b"GIF89a"):
        return True

    if (
        len(content) >= 12
        and content[0:4] == b"RIFF"
        and content[8:12] == b"WEBP"
    ):
        return True

    return False


# ============================================================
# PLAYWRIGHT IMAGE DOWNLOAD
# ============================================================

async def playwright_image_request(
    browser_context,
    image_url,
    referer,
):
    """
    Retrieve an image through the active Playwright request
    context.

    APIResponse uses response.headers as a property. It does
    not provide response.all_headers().
    """

    response = None

    try:
        response = await browser_context.request.get(
            image_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": referer,
                "Accept": (
                    "image/avif,image/webp,image/apng,"
                    "image/svg+xml,image/*,*/*;q=0.8"
                ),
                "Accept-Language": (
                    "en-US,en;q=0.9,zh-CN;q=0.8"
                ),
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            timeout=DOWNLOAD_TIMEOUT_MS,
            fail_on_status_code=False,
        )

        status = response.status

        # Correct APIResponse header access.
        headers = response.headers or {}

        content_type = (
            headers.get("content-type")
            or headers.get("Content-Type")
            or ""
        ).lower()

        content = b""

        if status == 200:
            try:
                content = await response.body()

            except Exception as error:
                print(
                    "  Could not read response body: "
                    f"{type(error).__name__}: {error}"
                )

        success = (
            status == 200
            and is_valid_image_content(
                content,
                content_type,
            )
        )

        return {
            "success": success,
            "status": status,
            "content_type": content_type,
            "content": content,
        }

    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass


async def download_candidate(
    browser_context,
    item,
):
    """
    Download one Weibo image.

    The downloader tests:
    - Alternate image size paths
    - Alternate wx1-wx4 image hosts
    - The post URL as Referer
    - weibo.com as Referer
    - m.weibo.cn as Referer
    """

    image_variants = build_image_url_variants(
        item["image_url"]
    )

    referers = [
        item.get("post_url")
        or "https://m.weibo.cn/",
        "https://weibo.com/",
        "https://m.weibo.cn/",
    ]

    attempt_count = 0
    last_status = None
    last_error = None

    for image_url in image_variants:
        for referer in referers:
            for retry_number in range(
                1,
                MAX_DOWNLOAD_RETRIES + 1,
            ):
                attempt_count += 1

                try:
                    result = await playwright_image_request(
                        browser_context,
                        image_url,
                        referer,
                    )

                    last_status = result["status"]

                    print(
                        f"  Attempt {attempt_count}: "
                        f"HTTP {result['status']}, "
                        f"size={len(result['content']):,}, "
                        f"referer={referer}"
                    )

                    if result["success"]:
                        content = result["content"]
                        content_type = (
                            result["content_type"]
                        )

                        content_hash = hashlib.sha256(
                            content
                        ).hexdigest()

                        extension = image_extension(
                            content_type,
                            image_url,
                            content,
                        )

                        filename = (
                            f"{item['photo_id']}_"
                            f"{content_hash[:12]}"
                            f"{extension}"
                        )

                        path = (
                            GALLERY_MEDIA_DIR / filename
                        )

                        if not path.exists():
                            path.write_bytes(content)

                        print(
                            f"  Saved image: {filename} "
                            f"({len(content):,} bytes)"
                        )

                        return {
                            "id": item["photo_id"],
                            "image": (
                                f"{RAW_GALLERY_BASE}/"
                                f"{filename}"
                            ),
                            "remoteImage": image_url,
                            "originalRemoteImage": (
                                item["image_url"]
                            ),
                            "sourceUrl": item["post_url"],
                            "platform": "Weibo",
                            "source": (
                                f"Weibo (@{item['author']})"
                            ),
                            "title": item["title"],
                            "date": "",
                            "origin": "Weibo Profile",
                            "imageHash": content_hash,
                            "collectedAt": now_iso(),
                        }

                    if result["status"] in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        wait_seconds = retry_number * 2

                        print(
                            f"  Temporary HTTP "
                            f"{result['status']}; "
                            f"waiting {wait_seconds} seconds."
                        )

                        await asyncio.sleep(wait_seconds)

                    else:
                        break

                except Exception as error:
                    last_error = (
                        f"{type(error).__name__}: {error}"
                    )

                    print(
                        f"  Download attempt failed: "
                        f"{last_error}"
                    )

                    if retry_number < MAX_DOWNLOAD_RETRIES:
                        await asyncio.sleep(
                            retry_number * 2
                        )

    error_details = (
        "All image variants failed. "
        f"Last HTTP status: {last_status}"
    )

    if last_error:
        error_details += (
            f". Last error: {last_error}"
        )

    raise RuntimeError(error_details)


# ============================================================
# GALLERY MERGING
# ============================================================

def merge_and_shuffle_items(
    new_items,
    old_items,
):
    """
    Merge new and existing items while removing duplicates.
    """

    combined = []
    seen_ids = set()
    seen_hashes = set()
    seen_images = set()

    for item in new_items + old_items:
        if not isinstance(item, dict):
            continue

        item_id = str(
            item.get("id") or ""
        ).strip()

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
    Remove downloaded images no longer referenced by
    gallery.json.

    Hidden files such as .gitkeep are preserved.
    """

    if not GALLERY_MEDIA_DIR.exists():
        return

    keep_filenames = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        image_url = str(
            item.get("image") or ""
        ).strip()

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

        if path.name.startswith("."):
            continue

        if path.name not in keep_filenames:
            path.unlink(missing_ok=True)
            removed_count += 1

            print(
                f"Removed unused image: {path.name}"
            )

    print(
        f"Unused local images removed: {removed_count}"
    )


def write_gallery_file(
    final_items,
    new_items_count,
    failed_downloads,
):
    """Write gallery.json using an atomic replacement."""

    output = {
        "lastUpdated": now_iso(),
        "status": (
            "Weibo gallery updated: "
            f"{new_items_count} downloaded, "
            f"{failed_downloads} failed, "
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

    print(
        f"Gallery JSON written to: {GALLERY_FILE}"
    )


# ============================================================
# MAIN PROCESS
# ============================================================

async def main_async():
    prepare_directories()

    existing_items = load_existing_items()

    existing_ids = {
        str(item.get("id") or "").strip()
        for item in existing_items
        if isinstance(item, dict)
        and item.get("id")
    }

    existing_hashes = {
        str(item.get("imageHash") or "").strip()
        for item in existing_items
        if isinstance(item, dict)
        and item.get("imageHash")
    }

    all_candidates = []

    successful_profiles = 0
    empty_profiles = 0

    unique_candidates = []
    new_items = []

    failed_downloads = 0
    duplicate_downloads = 0
    previously_collected = 0

    print()
    print("=" * 72)
    print("STARTING WEIBO PROFILE COLLECTION")
    print("=" * 72)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                (
                    "--disable-blink-features="
                    "AutomationControlled"
                ),
            ],
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

        try:
            print(
                "Warming up the Weibo browser session..."
            )

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

            await asyncio.sleep(
                PROFILE_DELAY_SECONDS
            )

        print()
        print("=" * 72)
        print("PROFILE COLLECTION SUMMARY")
        print("=" * 72)
        print(
            f"Profiles with photos: "
            f"{successful_profiles}"
        )
        print(
            f"Profiles without photos: "
            f"{empty_profiles}"
        )
        print(
            f"Total candidates collected: "
            f"{len(all_candidates)}"
        )

        seen_candidate_ids = set()
        seen_candidate_urls = set()

        for candidate in all_candidates:
            photo_id = str(
                candidate.get("photo_id") or ""
            ).strip()

            image_url = str(
                candidate.get("image_url") or ""
            ).strip()

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
            f"Unique photo candidates: "
            f"{len(unique_candidates)}"
        )

        print()
        print("=" * 72)
        print("STARTING IMAGE DOWNLOAD")
        print("=" * 72)

        downloaded_hashes = set()

        for index, item in enumerate(
            unique_candidates,
            start=1,
        ):
            photo_id = item.get(
                "photo_id",
                "unknown",
            )

            print()
            print(
                f"Processing image {index}/"
                f"{len(unique_candidates)}: "
                f"{photo_id}"
            )

            if photo_id in existing_ids:
                previously_collected += 1

                print(
                    "Already present in gallery.json; "
                    "download skipped."
                )

                continue

            try:
                record = await download_candidate(
                    context,
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

                    duplicate_path.unlink(
                        missing_ok=True
                    )

                    print(
                        f"Duplicate image skipped: "
                        f"{photo_id}"
                    )

                else:
                    downloaded_hashes.add(
                        image_hash
                    )

                    new_items.append(record)

                    print(
                        f"Downloaded successfully: "
                        f"{photo_id}"
                    )

            except Exception as error:
                failed_downloads += 1

                print(
                    f"Image skipped {photo_id}: "
                    f"{type(error).__name__}: {error}"
                )

            await asyncio.sleep(
                DOWNLOAD_DELAY_SECONDS
            )

        await context.close()
        await browser.close()

    final_items = merge_and_shuffle_items(
        new_items,
        existing_items,
    )

    remove_unused_files(final_items)

    write_gallery_file(
        final_items,
        len(new_items),
        failed_downloads,
    )

    print()
    print("=" * 72)
    print("FINAL GALLERY SUMMARY")
    print("=" * 72)
    print(
        f"Collected candidates: "
        f"{len(all_candidates)}"
    )
    print(
        f"Unique candidates: "
        f"{len(unique_candidates)}"
    )
    print(
        f"Previously collected: "
        f"{previously_collected}"
    )
    print(
        f"New downloaded images: "
        f"{len(new_items)}"
    )
    print(
        f"Duplicate images skipped: "
        f"{duplicate_downloads}"
    )
    print(
        f"Failed image downloads: "
        f"{failed_downloads}"
    )
    print(
        f"Final gallery items: "
        f"{len(final_items)}"
    )
    print(
        "Gallery media directory exists: "
        f"{GALLERY_MEDIA_DIR.exists()}"
    )
    print("Gallery process completed.")
    print("=" * 72)


def main():
    try:
        asyncio.run(main_async())

    except KeyboardInterrupt:
        print(
            "Gallery process was stopped manually."
        )

    except Exception as error:
        print()
        print("=" * 72)
        print("GALLERY PROCESS FAILED")
        print("=" * 72)
        print(
            f"{type(error).__name__}: {error}"
        )
        raise


if __name__ == "__main__":
    main()
