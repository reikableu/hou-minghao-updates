import asyncio
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

# ============================================================
# PATHS AND CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

GIFS_FILE = BASE_DIR / "gifs.json"
GIFS_MEDIA_DIR = BASE_DIR / "gifs_media"
GITKEEP_FILE = GIFS_MEDIA_DIR / ".gitkeep"

RAW_GIFS_BASE = (
    "https://raw.githubusercontent.com/"
    "reikableu/hou-minghao-updates/main/gifs_media"
)

SINGAPORE = ZoneInfo("Asia/Singapore")

# Keyword search is login-gated on Weibo, so it is OFF by default.
SEARCH_KEYWORDS = []

# Tenor search keywords (No API key required, scraped via Playwright)
TENOR_KEYWORDS = ["HouMinghao", "侯明昊"]

# Optional: Weibo UIDs of fan / official accounts that post lots of Hou Minghao content
TARGET_UIDS = [
    "7971490580",
    "7987922088",
    "8290189779",
    "6362579474",
    "6242319711",
    "7970616874",
    "1747335284",
    "8001514587",
    "2431320290",
    "8004004308",
    "8450716290",
    "2356308454",
    "5763552342",
    "1831550987",
]

# Login cookie, stored as a GitHub secret named WEIBO_COOKIE
WEIBO_COOKIE = os.environ.get("WEIBO_COOKIE", "").strip()

MAX_GIF_ITEMS = 150
MAX_GIF_SIZE_BYTES = 20 * 1024 * 1024    # 20MB limit for final GIFs
MAX_VIDEO_SIZE_BYTES = 40 * 1024 * 1024  # 40MB limit for live photo videos
LIVE_PHOTO_MAX_SECONDS = 6               # trim live photo videos to this length
DOWNLOAD_BUDGET_SECONDS = 20 * 60        # updated to 20 mins budget limit
REQUEST_TIMEOUT_MS = 20000               # per download attempt

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(SINGAPORE).isoformat(timespec="seconds")


def prepare_directories():
    GIFS_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if not GITKEEP_FILE.exists():
        GITKEEP_FILE.write_text("Keeps gifs_media in Git.\n", encoding="utf-8")


def clean_text(value):
    if not value:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def load_existing_gifs():
    if not GIFS_FILE.exists():
        return []
    try:
        data = json.loads(GIFS_FILE.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [item for item in items if isinstance(item, dict)]
    except Exception:
        return []


def build_cookies():
    """Turn 'A=1; B=2' into Playwright cookie dicts for the Weibo domains."""
    cookies = []
    if not WEIBO_COOKIE:
        return cookies
    for part in WEIBO_COOKIE.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        for domain in (".weibo.cn", ".weibo.com"):
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return cookies


def host_variants(url):
    """Sina images can be served from wx1-wx4; try them all."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    variants = [url]
    if re.fullmatch(r"wx[1-4]\.sinaimg\.cn", host):
        for n in range(1, 5):
            alt_host = f"wx{n}.sinaimg.cn"
            if alt_host != host:
                variants.append(urllib.parse.urlunsplit(
                    (parsed.scheme or "https", alt_host, parsed.path, parsed.query, parsed.fragment)
                ))
    return variants


async def download_bytes(context, url, post_url, size_limit):
    """Download a file, trying alternate image hosts and referers (bounded)."""
    referers = [post_url or "https://m.weibo.cn/", "https://weibo.com/", "https://tenor.com/"]
    last_status = None
    attempts = 0
    max_attempts = 8

    for candidate_url in host_variants(url):
        for referer in referers:
            if attempts >= max_attempts:
                print(f"  Gave up after {attempts} attempts (last status: {last_status}).")
                return None
            attempts += 1
            try:
                response = await context.request.get(
                    candidate_url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": referer,
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                    },
                    timeout=REQUEST_TIMEOUT_MS,
                    fail_on_status_code=False,
                )
                last_status = response.status
                if response.status == 200:
                    body = await response.body()
                    if body and len(body) <= size_limit:
                        return body
                    print(f"  Empty or oversized file ({len(body):,} bytes).")
                    return None
            except Exception as error:
                last_status = type(error).__name__
                break

    print(f"  All download attempts failed (last status: {last_status}).")
    return None


def convert_video_to_gif(video_bytes):
    """Convert a short video (mov/mp4) to GIF bytes using ffmpeg with 120s timeout."""
    settings = [(480, 12), (360, 10), (320, 8)]
    last_error = None

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input_video")
        dst = os.path.join(tmp, "output.gif")
        with open(src, "wb") as f:
            f.write(video_bytes)

        for width, fps in settings:
            vf = (
                f"fps={fps},scale={width}:-1:flags=lanczos,"
                f"split[a][b];[a]palettegen[p];[b][p]paletteuse"
            )
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", src,
                        "-t", str(LIVE_PHOTO_MAX_SECONDS),
                        "-vf", vf, "-loop", "0", dst,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,  # Increased timeout to 120 seconds
                )
                with open(dst, "rb") as f:
                    data = f.read()
                if data and len(data) <= MAX_GIF_SIZE_BYTES:
                    return data
                last_error = "GIF too large"
            except subprocess.TimeoutExpired:
                raise RuntimeError("ffmpeg timed out")
            except Exception as error:
                last_error = str(error)

    raise RuntimeError(f"ffmpeg conversion failed: {last_error}")


# ============================================================
# WEIBO API FETCHING + PARSING
# ============================================================

def extract_candidates_from_cards(cards, stats, debug_state):
    candidates = []

    for card in cards:
        if not isinstance(card, dict):
            continue

        mblogs = []
        if isinstance(card.get("mblog"), dict):
            mblogs.append(card["mblog"])
        elif isinstance(card.get("card_group"), list):
            for sub_card in card["card_group"]:
                if isinstance(sub_card, dict) and isinstance(sub_card.get("mblog"), dict):
                    mblogs.append(sub_card["mblog"])

        for mblog in mblogs:
            stats["posts"] += 1
            post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
            if not post_id:
                continue

            user = mblog.get("user") or {}
            user_name = user.get("screen_name") or "Weibo User"
            raw_text = clean_text(mblog.get("raw_text") or mblog.get("text") or "")
            title = f"GIF by @{user_name}: {raw_text[:80]}" if raw_text else f"GIF by @{user_name}"
            post_url = f"https://m.weibo.cn/status/{post_id}"

            pics = mblog.get("pics") or []
            for pic_idx, picture in enumerate(pics):
                if not isinstance(picture, dict):
                    continue
                stats["pics"] += 1

                video_url = normalize_url(picture.get("videoSrc"))
                if picture.get("type") == "livephoto" or video_url:
                    if not debug_state["printed_live_keys"]:
                        print(f"[debug] Live photo keys: {list(picture.keys())}")
                        debug_state["printed_live_keys"] = True
                    if video_url:
                        stats["live"] += 1
                        candidates.append({
                            "photo_id": f"wb_live_{post_id}_{pic_idx}",
                            "post_url": post_url,
                            "image_url": video_url,
                            "title": title,
                            "author": user_name,
                            "is_video": True,
                        })
                        continue

                large_pic = picture.get("large") or {}
                image_url = normalize_url(large_pic.get("url") or picture.get("url"))
                if not image_url:
                    continue

                if ".gif" in image_url.lower() or picture.get("type") == "gif":
                    stats["gifs"] += 1
                    candidates.append({
                        "photo_id": f"wb_gif_{post_id}_{pic_idx}",
                        "post_url": post_url,
                        "image_url": image_url,
                        "title": title,
                        "author": user_name,
                        "is_video": False,
                    })

    return candidates


async def fetch_api_pages(page, label, url_for_page, max_pages=3):
    candidates = []
    stats = {"posts": 0, "pics": 0, "gifs": 0, "live": 0}
    debug_state = {"printed_live_keys": False}

    for page_num in range(1, max_pages + 1):
        target_url = url_for_page(page_num)
        print(f"Fetching {label}, page {page_num}.")

        try:
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            if not response or response.status != 200:
                print(f"  No valid response for {label}. Stopping.")
                break

            body_text = await response.text()
            try:
                payload = json.loads(body_text)
            except Exception:
                print(f"  Response was not JSON. Start of body: {body_text[:200]!r}")
                break

            if payload.get("ok") not in (1, "1", True):
                print(f"  API returned ok != 1 for {label}")
                break

            cards = (payload.get("data") or {}).get("cards") or []
            if not cards:
                print(f"  No more cards for {label}.")
                break

            candidates.extend(extract_candidates_from_cards(cards, stats, debug_state))
            await asyncio.sleep(1.5)

        except Exception as error:
            print(f"  Error fetching {label}: {error}")
            break

    print(
        f"  {label}: {stats['posts']} posts, {stats['pics']} pics, "
        f"{stats['gifs']} GIFs, {stats['live']} live photos."
    )
    return candidates


async def fetch_search_candidates(page, keyword, max_pages=3):
    encoded_keyword = urllib.parse.quote(keyword)

    def url_for_page(n):
        return (
            "https://m.weibo.cn/api/container/getIndex"
            f"?containerid=100103type%3D1%26q%3D{encoded_keyword}"
            "&page_type=searchall"
            f"&page={n}"
        )

    return await fetch_api_pages(page, f"search '{keyword}'", url_for_page, max_pages)


async def fetch_profile_candidates(page, uid, max_pages=3):
    def url_for_page(n):
        return (
            "https://m.weibo.cn/api/container/getIndex"
            f"?type=uid&value={uid}&containerid=107603{uid}&page={n}"
        )

    return await fetch_api_pages(page, f"profile {uid}", url_for_page, max_pages)


# ============================================================
# TENOR WEB SCRAPING FETCHING (FIXED SELECTORS)
# ============================================================

async def fetch_tenor_candidates(page, keyword):
    encoded_keyword = urllib.parse.quote(keyword)
    target_url = f"https://tenor.com/search/{encoded_keyword}-gifs"
    candidates = []
    print(f"Fetching Tenor search for keyword: '{keyword}'")

    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

        # Scroll down dynamically to trigger lazy-loaded GIF items
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2)

        # Updated resilient selector logic targeting generic figures, videos, and image cards on Tenor
        media_items = await page.evaluate("""() => {
            const results = [];
            // Target all potential container elements or media items on Tenor search grid
            const elements = document.querySelectorAll('figure, img, video, source');
            
            elements.forEach((el) => {
                let mediaUrl = '';
                let altText = '';

                if (el.tagName === 'VIDEO' || el.tagName === 'SOURCE') {
                    mediaUrl = el.src || el.currentSrc;
                } else if (el.tagName === 'IMG') {
                    mediaUrl = el.src;
                    altText = el.alt;
                } else {
                    const img = el.querySelector('img');
                    const video = el.querySelector('video') || el.querySelector('source');
                    if (video) mediaUrl = video.src || video.currentSrc;
                    else if (img) {
                        mediaUrl = img.src;
                        altText = img.alt;
                    }
                }

                if (mediaUrl && (mediaUrl.includes('tenor.com') || mediaUrl.includes('media.tenor.co') || mediaUrl.includes('cgw.tenor.com'))) {
                    results.push({
                        mediaUrl: mediaUrl,
                        title: altText || 'Tenor GIF'
                    });
                }
            });
            return results;
        }""")

        seen_urls = set()
        for idx, item in enumerate(media_items):
            media_url = normalize_url(item["mediaUrl"])
            if not media_url or media_url in seen_urls:
                continue
            
            # Map preview image links to direct gif/mp4 outputs if possible, or keep original link
            if ".gif" not in media_url and ".mp4" not in media_url and ".webm" not in media_url:
                if "images/" in media_url:
                    media_url = media_url.replace("/images/", "/media/")

            seen_urls.add(media_url)
            url_hash = hashlib.md5(media_url.encode()).hexdigest()[:10]
            photo_id = f"tenor_{keyword.lower()}_{idx}_{url_hash}"
            is_video = media_url.endswith(".mp4") or media_url.endswith(".webm")

            candidates.append({
                "photo_id": photo_id,
                "post_url": target_url,
                "image_url": media_url,
                "title": f"Tenor ({keyword}): {item['title'][:60]}",
                "author": "Tenor User",
                "is_video": is_video,
            })

        print(f"  Tenor '{keyword}': found {len(candidates)} items.")
    except Exception as error:
        print(f"  Error scraping Tenor for '{keyword}': {error}")

    return candidates


# ============================================================
# CLEANUP
# ============================================================

def remove_unused_files(items):
    if not GIFS_MEDIA_DIR.exists():
        return

    if not items:
        print("No items to keep; skipping cleanup of gifs_media.")
        return

    keep_filenames = set()
    for item in items:
        image_url = str(item.get("image") or "").strip()
        if image_url:
            name = Path(urllib.parse.urlsplit(image_url).path).name
            if name:
                keep_filenames.add(name)

    removed_count = 0
    for path in GIFS_MEDIA_DIR.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name not in keep_filenames:
            path.unlink(missing_ok=True)
            removed_count += 1
            print(f"Removed unused GIF: {path.name}")

    print(f"Unused GIF files removed: {removed_count}")


# ============================================================
# MAIN SCRIPT EXECUTION
# ============================================================

async def main_async():
    prepare_directories()
    existing_items = load_existing_gifs()

    existing_ids = {str(item.get("id")) for item in existing_items if item.get("id")}
    existing_hashes = {str(item.get("imageHash")) for item in existing_items if item.get("imageHash")}

    print(f"Cookie provided: {'yes' if WEIBO_COOKIE else 'no'}")
    print(f"ffmpeg available: {'yes' if FFMPEG_AVAILABLE else 'NO'}")
    print(f"Profiles to scrape: {len(TARGET_UIDS)}")
    print(f"Tenor keywords to scrape: {len(TENOR_KEYWORDS)}")

    all_candidates = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1365, "height": 768},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
        )

        cookies = build_cookies()
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        try:
            await page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        except Exception as error:
            print(f"Warm-up visit failed: {error}")

        for uid in TARGET_UIDS:
            all_candidates.extend(await fetch_profile_candidates(page, uid, max_pages=3))
            await asyncio.sleep(2)

        for keyword in SEARCH_KEYWORDS:
            all_candidates.extend(await fetch_search_candidates(page, keyword, max_pages=3))
            await asyncio.sleep(2)

        for keyword in TENOR_KEYWORDS:
            all_candidates.extend(await fetch_tenor_candidates(page, keyword))
            await asyncio.sleep(2)

        unique_candidates = []
        seen_candidate_ids = set()
        seen_candidate_urls = set()
        for cand in all_candidates:
            if cand["photo_id"] in seen_candidate_ids or cand["image_url"] in seen_candidate_urls:
                continue
            seen_candidate_ids.add(cand["photo_id"])
            seen_candidate_urls.add(cand["image_url"])
            unique_candidates.append(cand)

        print(f"\nTotal unique candidates collected: {len(unique_candidates)}")

        new_items = []
        downloaded_hashes = set()
        failed_downloads = 0

        download_start = time.monotonic()
        total_candidates = len(unique_candidates)

        for index, item in enumerate(unique_candidates, start=1):
            if time.monotonic() - download_start > DOWNLOAD_BUDGET_SECONDS:
                print(f"::warning::Download budget of 20 minutes reached at {index}/{total_candidates}.")
                break

            print(f"[{index}/{total_candidates}] {item['photo_id']}")

            if item["photo_id"] in existing_ids:
                print(f"Skipping {item['photo_id']} because it is already in gifs.json.")
                continue

            is_video = item.get("is_video", False)
            if is_video and not FFMPEG_AVAILABLE:
                print(f"Skipping {item['photo_id']}: ffmpeg not installed.")
                failed_downloads += 1
                continue

            try:
                size_limit = MAX_VIDEO_SIZE_BYTES if is_video else MAX_GIF_SIZE_BYTES
                content = await download_bytes(
                    context, item["image_url"], item["post_url"], size_limit
                )
                if not content:
                    print(f"Download failed for {item['photo_id']}.")
                    failed_downloads += 1
                    continue

                if is_video:
                    try:
                        content = await asyncio.to_thread(convert_video_to_gif, content)
                    except Exception as error:
                        print(f"Conversion failed for {item['photo_id']}: {error}")
                        failed_downloads += 1
                        continue

                if not (content.startswith(b"GIF87a") or content.startswith(b"GIF89a") or content.startswith(b"RIFF")):
                    print(f"{item['photo_id']} is not a valid format. Skipping.")
                    failed_downloads += 1
                    continue

                content_hash = hashlib.sha256(content).hexdigest()
                if content_hash in downloaded_hashes or content_hash in existing_hashes:
                    print(f"Duplicate content hash skipped for {item['photo_id']}.")
                    continue

                downloaded_hashes.add(content_hash)
                filename = f"{item['photo_id']}_{content_hash[:12]}.gif"
                path = GIFS_MEDIA_DIR / filename
                path.write_bytes(content)

                platform_name = "Tenor" if "tenor" in item["photo_id"] else "Weibo"
                new_items.append({
                    "id": item["photo_id"],
                    "image": f"{RAW_GIFS_BASE}/{filename}",
                    "remoteImage": item["image_url"],
                    "sourceUrl": item["post_url"],
                    "platform": platform_name,
                    "source": f"{platform_name} (@{item['author']})",
                    "title": item["title"],
                    "date": "",
                    "origin": f"{platform_name} Media",
                    "imageHash": content_hash,
                    "collectedAt": now_iso(),
                })
                print(f"Saved: {filename}")

            except Exception as error:
                failed_downloads += 1
                print(f"Download failed for {item['photo_id']}: {error}")

            await asyncio.sleep(0.5)

        await browser.close()

    combined = new_items + existing_items
    random.shuffle(combined)
    final_items = combined[:MAX_GIF_ITEMS]

    remove_unused_files(final_items)

    output = {
        "lastUpdated": now_iso(),
        "status": (
            f"GIF update: {len(new_items)} new, "
            f"{failed_downloads} failed, {len(final_items)} total items"
        ),
        "total": len(final_items),
        "items": final_items,
    }

    tmp = GIFS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GIFS_FILE)
    print(f"\nUpdate complete. {len(new_items)} new, {len(final_items)} total items in gifs.json.")


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
