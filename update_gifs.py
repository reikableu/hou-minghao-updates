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

GIFS_FILE = BASE_DIR / "gifs.json"
GIFS_MEDIA_DIR = BASE_DIR / "gifs_media"
GITKEEP_FILE = GIFS_MEDIA_DIR / ".gitkeep"

RAW_GIFS_BASE = (
    "https://raw.githubusercontent.com/"
    "reikableu/hou-minghao-updates/main/gifs_media"
)

SINGAPORE = ZoneInfo("Asia/Singapore")

# Keywords used to search the entire Weibo platform globally
SEARCH_KEYWORDS = [
    "侯明昊 gif",
    "侯明昊动图",
    "Neo Hou gif",
]

MAX_GIF_ITEMS = 150
MAX_GIF_SIZE_BYTES = 20 * 1024 * 1024  # 20MB safety limit for GIFs

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

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

def load_existing_gifs():
    if not GIFS_FILE.exists():
        return []
    try:
        data = json.loads(GIFS_FILE.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [item for item in items if isinstance(item, dict)]
    except Exception:
        return []

# ============================================================
# GLOBAL WEIBO KEYWORD SEARCH FOR GIFS
# ============================================================

async def fetch_search_candidates(page, keyword, max_pages=3):
    candidates = []
    encoded_keyword = urllib.parse.quote(keyword)
    
    for page_num in range(1, max_pages + 1):
        target_url = (
            f"https://m.weibo.cn/api/container/getIndex"
            f"?containerid=100103type%3D1%26q%3D{encoded_keyword}"
            f"&page_type=searchall"
            f"&page={page_num}"
        )
        
        print(f"Searching Weibo for '{keyword}' on page {page_num}.")
        
        try:
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            if not response or response.status != 200:
                break

            try:
                body_text = await response.text()
                payload = json.loads(body_text)
            except Exception:
                break

            if payload.get("ok") not in (1, "1", True):
                print(f"Reached end of results for keyword '{keyword}' on page {page_num}.")
                break

            data = payload.get("data") or {}
            cards = data.get("cards") or []
            
            if not cards:
                break

            posts_found = 0
            for card in cards:
                if not isinstance(card, dict):
                    continue
                
                mblogs = []
                if "mblog" in card and isinstance(card["mblog"], dict):
                    mblogs.append(card["mblog"])
                elif "card_group" in card and isinstance(card["card_group"], list):
                    for sub_card in card["card_group"]:
                        if isinstance(sub_card, dict) and "mblog" in sub_card:
                            mblogs.append(sub_card["mblog"])

                for mblog in mblogs:
                    posts_found += 1
                    post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
                    if not post_id:
                        continue

                    user = mblog.get("user") or {}
                    user_name = user.get("screen_name") or "Weibo User"
                    raw_text = clean_text(mblog.get("raw_text") or mblog.get("text") or "")
                    title = f"GIF by @{user_name}: {raw_text[:80]}" if raw_text else f"GIF by @{user_name}"

                    pics = mblog.get("pics") or []
                    for pic_idx, picture in enumerate(pics):
                        if not isinstance(picture, dict):
                            continue
                        
                        large_pic = picture.get("large") or {}
                        image_url = large_pic.get("url") or picture.get("url") or ""
                        image_url = str(image_url).strip()
                        
                        if not image_url:
                            continue
                        if image_url.startswith("//"):
                            image_url = "https:" + image_url

                        if ".gif" in image_url.lower() or picture.get("type") == "gif":
                            candidates.append({
                                "photo_id": f"wb_search_gif_{post_id}_{pic_idx}",
                                "post_url": f"https://m.weibo.cn/status/{post_id}",
                                "image_url": image_url,
                                "title": title,
                                "author": user_name,
                            })

            print(f"Found {posts_found} posts on page {page_num}. Total candidates so far: {len(candidates)}")
            await asyncio.sleep(1.5)

        except Exception as error:
            print(f"Error searching keyword '{keyword}': {error}")
            break

    return candidates

# ============================================================
# MAIN SCRIPT EXECUTION
# ============================================================

async def main_async():
    prepare_directories()
    existing_items = load_existing_gifs()
    
    existing_ids = {str(item.get("id")) for item in existing_items if item.get("id")}
    existing_hashes = {str(item.get("imageHash")) for item in existing_items if item.get("imageHash")}

    all_candidates = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1365, "height": 768}
        )
        page = await context.new_page()

        try:
            await page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        except Exception:
            pass

        for keyword in SEARCH_KEYWORDS:
            candidates = await fetch_search_candidates(page, keyword, max_pages=3)
            all_candidates.extend(candidates)
            await asyncio.sleep(2)

        print(f"\nTotal unique candidates collected from global search: {len(all_candidates)}")
        
        unique_candidates = []
        seen_candidate_ids = set()
        seen_candidate_urls = set()
        for cand in all_candidates:
            if cand["photo_id"] in seen_candidate_ids or cand["image_url"] in seen_candidate_urls:
                continue
            seen_candidate_ids.add(cand["photo_id"])
            seen_candidate_urls.add(cand["image_url"])
            unique_candidates.append(cand)

        new_items = []
        downloaded_hashes = set()
        failed_downloads = 0

        for item in unique_candidates:
            if item["photo_id"] in existing_ids:
                print(f"Skipping {item['photo_id']} because it is already in gifs.json.")
                continue

            try:
                response = await context.request.get(
                    item["image_url"],
                    headers={"Referer": "https://m.weibo.cn/"},
                    timeout=30000
                )
                if response.status != 200:
                    failed_downloads += 1
                    continue
                
                content = await response.body()
                if not content or len(content) > MAX_GIF_SIZE_BYTES:
                    failed_downloads += 1
                    continue
                
                if not (content.startswith(b"GIF87a") or content.startswith(b"GIF89a")):
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

                new_items.append({
                    "id": item["photo_id"],
                    "image": f"{RAW_GIFS_BASE}/{filename}",
                    "remoteImage": item["image_url"],
                    "sourceUrl": item["post_url"],
                    "platform": "Weibo",
                    "source": f"Weibo Search (@{item['author']})",
                    "title": item["title"],
                    "date": "",
                    "origin": "Weibo Global Search",
                    "imageHash": content_hash,
                    "collectedAt": now_iso(),
                })
                print(f"Successfully downloaded GIF: {filename}")

            except Exception as error:
                failed_downloads += 1
                print(f"Download failed for {item['photo_id']}: {error}")

            await asyncio.sleep(0.5)

        await browser.close()

    combined = new_items + existing_items
    random.shuffle(combined)
    final_items = combined[:MAX_GIF_ITEMS]

    output = {
        "lastUpdated": now_iso(),
        "status": f"Global Weibo GIF search updated: {len(new_items)} downloaded, {failed_downloads} failed, {len(final_items)} total items",
        "total": len(final_items),
        "items": final_items,
    }
    
    tmp = GIFS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GIFS_FILE)
    print(f"\nGIF update process complete. Total items in gifs.json: {len(final_items)}")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()

