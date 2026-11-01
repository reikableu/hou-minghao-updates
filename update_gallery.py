import hashlib
import json
import os
import random
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

GALLERY_FILE = Path('gallery.json')
GALLERY_MEDIA_DIR = Path('gallery_media')
RAW_GALLERY_BASE = 'https://raw.githubusercontent.com/reikableu/hou-minghao-updates/main/gallery_media'
SINGAPORE = ZoneInfo('Asia/Singapore')

# Target 5 Weibo Profile UIDs
TARGET_WEIBO_UIDS = [
    '5763552342',
    '1831550987',
    '2356308454',
    '8450716290',
    '8004004308',
]

MAX_GALLERY_ITEMS = 150
REQUEST_TIMEOUT = 30

USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
)

HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://m.weibo.cn/',
}


def now_iso():
    return datetime.now(SINGAPORE).isoformat(timespec='seconds')


def load_existing_items():
    if not GALLERY_FILE.exists():
        return []
    try:
        data = json.loads(GALLERY_FILE.read_text(encoding='utf-8'))
        items = data if isinstance(data, list) else data.get('items', [])
        return [x for x in items if isinstance(x, dict)]
    except Exception as error:
        print('Existing gallery could not be read:', error)
        return []


def collect_weibo_profile_images(uid):
    """Fetch recent post photos directly from a Weibo profile container."""
    container_id = f"107603{uid}"
    url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}"
    print(f"Fetching Weibo posts for UID: {uid}")
    
    candidates = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"Weibo API returned status {response.status_code} for UID {uid}")
            return []

        payload = response.json()
        cards = payload.get('data', {}).get('cards', [])

        for card in cards:
            mblog = card.get('mblog')
            if not mblog:
                continue

            post_id = str(mblog.get('id', ''))
            user_name = mblog.get('user', {}).get('screen_name', f'Weibo_{uid}')
            raw_text = mblog.get('raw_text') or mblog.get('text') or ''
            
            title = f"{user_name}: {raw_text[:80]}..." if raw_text else f"Photo by {user_name}"

            pics = mblog.get('pics', [])
            for index, pic in enumerate(pics):
                img_url = (
                    pic.get('large', {}).get('url') or 
                    pic.get('url') or 
                    ''
                )
                if not img_url:
                    continue

                if img_url.startswith('//'):
                    img_url = 'https:' + img_url

                photo_id = f"wb_{post_id}_{index}"
                candidates.append({
                    'photo_id': photo_id,
                    'post_url': f"https://m.weibo.cn/status/{post_id}",
                    'image_url': img_url,
                    'title': title,
                    'author': user_name,
                    'uid': uid,
                })
    except Exception as error:
        print(f"Error fetching Weibo profile {uid}: {error}")

    return candidates


def extension_for(content_type, url):
    content_type = (content_type or '').lower()
    if 'png' in content_type:
        return '.png'
    if 'webp' in content_type:
        return '.webp'
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp'} else '.jpg'


def download_candidate(session, item):
    headers = {
        'User-Agent': USER_AGENT,
        'Referer': 'https://m.weibo.cn/',
    }
    response = session.get(item['image_url'], headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get('Content-Type') or '').lower()
    
    if not content_type.startswith('image/') or not response.content or len(response.content) > 15 * 1024 * 1024:
        raise ValueError('Invalid or oversized image response')
        
    content_hash = hashlib.sha256(response.content).hexdigest()
    filename = f"{item['photo_id']}_{content_hash[:12]}{extension_for(content_type, item['image_url'])}"

    GALLERY_MEDIA_DIR.mkdir(exist_ok=True)
    path = GALLERY_MEDIA_DIR / filename
    if not path.exists():
        path.write_bytes(response.content)

    return {
        'id': item['photo_id'],
        'image': f'{RAW_GALLERY_BASE}/{filename}',
        'remoteImage': item['image_url'],
        'sourceUrl': item['post_url'],
        'platform': 'Weibo',
        'source': f"Weibo (@{item['author']})",
        'title': item['title'],
        'date': '',
        'origin': 'Weibo Profile',
        'imageHash': content_hash,
        'collectedAt': now_iso(),
    }


def merge_and_shuffle_items(new_items, old_items):
    """Combines new and old items, removes duplicates, and reshuffles display order."""
    combined = []
    seen_ids = set()
    seen_hashes = set()

    # Prioritize newly fetched images first
    for item in new_items + old_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get('id') or '')
        image_hash = str(item.get('imageHash') or '')
        image = str(item.get('image') or '')
        key = item_id or image

        if not key or key in seen_ids or (image_hash and image_hash in seen_hashes):
            continue

        seen_ids.add(key)
        if image_hash:
            seen_hashes.add(image_hash)
        combined.append(item)

    # Randomly shuffle the entire item pool so dashboard order changes every 4 hours
    random.shuffle(combined)
    return combined[:MAX_GALLERY_ITEMS]


def remove_unused_files(items):
    if not GALLERY_MEDIA_DIR.exists():
        return
    keep = {Path(urllib.parse.urlsplit(str(x.get('image') or '')).path).name for x in items}
    for path in GALLERY_MEDIA_DIR.iterdir():
        if path.is_file() and path.name not in keep:
            path.unlink()


def main():
    existing = load_existing_items()
    print(f"Loaded {len(existing)} existing gallery items.")
    
    all_candidates = []
    for uid in TARGET_WEIBO_UIDS:
        photos = collect_weibo_profile_images(uid)
        all_candidates.extend(photos)

    # Shuffle the fetched candidate pool randomly
    random.shuffle(all_candidates)
    print(f"Total photos collected across all profiles: {len(all_candidates)}")

    session = requests.Session()
    new_items = []
    seen_hashes = set()
    
    for index, item in enumerate(all_candidates, 1):
        try:
            record = download_candidate(session, item)
            if record['imageHash'] in seen_hashes:
                Path('gallery_media', Path(urllib.parse.urlsplit(record['image']).path).name).unlink(missing_ok=True)
                continue
            seen_hashes.add(record['imageHash'])
            new_items.append(record)
        except Exception as error:
            print(f"Image skipped {item['photo_id']}: {error}")
        time.sleep(0.2)

    # Merge with existing photos and randomly shuffle the display array
    final_items = merge_and_shuffle_items(new_items, existing)
    remove_unused_files(final_items)

    output = {
        'lastUpdated': now_iso(),
        'status': f'Weibo gallery updated: {len(new_items)} downloaded, {len(final_items)} display items',
        'uids': TARGET_WEIBO_UIDS,
        'total': len(final_items),
        'items': final_items,
    }

    temporary = GALLERY_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(GALLERY_FILE)
    print(f'New downloaded images: {len(new_items)}')
    print(f'Final randomized gallery items: {len(final_items)}')


if __name__ == '__main__':
    main()
