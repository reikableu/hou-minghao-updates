import hashlib
import json
import os
import re
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

SEARCH_QUERIES = ['Hou Minghao', 'HouMinghao', '侯明昊']
MAX_GALLERY_ITEMS = 150
REQUEST_TIMEOUT = 30

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/126.0.0.0 Safari/537.36'
)

HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en-US,en;q=0.9',
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


def collect_query_direct(query):
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.pinterest.com/resource/BaseSearchResource/get/?source_url=/search/pins/?q={encoded_query}&data="
    data_param = json.dumps({
        "options": {
            "isPrefetch": False,
            "query": query,
            "scope": "pins",
            "no_fetch_context_on_resource": False
        },
        "context": {}
    })
    
    full_url = url + urllib.parse.quote(data_param)
    print(f'Fetching Pinterest public search for: {query}')
    
    candidates = []
    try:
        response = requests.get(full_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        print(f"Pinterest search response status for '{query}': {response.status_code}")
        if response.status_code != 200:
            print(f"Response text snippet: {response.text[:200]}")
            return []
            
        payload = response.json()
        results = payload.get('resource_response', {}).get('data', {}).get('results', [])
        print(f"Found {len(results)} raw pins for query '{query}'")
        
        for pin in results:
            pin_id = str(pin.get('id') or '')
            if not pin_id:
                continue
                
            images = pin.get('images', {})
            original_img = images.get('orig', {}).get('url') or images.get('736x', {}).get('url') or ''
            if not original_img:
                continue
                
            title = pin.get('grid_title') or pin.get('pindoc_title') or f"Hou Minghao Pinterest photo - {query}"
            pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
            
            candidates.append({
                'pin_id': pin_id,
                'pin_url': pin_url,
                'image_url': original_img,
                'title': str(title)[:250],
                'query': query,
            })
    except Exception as error:
        print(f"Error fetching Pinterest data for {query}: {type(error).__name__} - {error}")
        
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
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': item['pin_url'],
    }
    response = session.get(item['image_url'], headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get('Content-Type') or '').lower()
    if not content_type.startswith('image/') or not response.content or len(response.content) > 15 * 1024 * 1024:
        raise ValueError('Invalid or oversized image response')
    content_hash = hashlib.sha256(response.content).hexdigest()
    filename = f"{item['pin_id']}_{content_hash[:12]}{extension_for(content_type, item['image_url'])}"
    
    GALLERY_MEDIA_DIR.mkdir(exist_ok=True)
    path = GALLERY_MEDIA_DIR / filename
    if not path.exists():
        path.write_bytes(response.content)
        
    return {
        'id': item['pin_id'],
        'image': f'{RAW_GALLERY_BASE}/{filename}',
        'remoteImage': item['image_url'],
        'sourceUrl': item['pin_url'],
        'platform': 'Pinterest',
        'source': 'Pinterest',
        'title': item['title'],
        'date': '',
        'origin': 'Pinterest',
        'query': item['query'],
        'imageHash': content_hash,
        'collectedAt': now_iso(),
    }


def merge_items(new_items, old_items):
    merged = []
    seen_ids = set()
    seen_hashes = set()
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
        merged.append(item)
        if len(merged) >= MAX_GALLERY_ITEMS:
            break
    return merged


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
    all_candidates = {}
    
    for query in SEARCH_QUERIES:
        for item in collect_query_direct(query):
            all_candidates[item['pin_id']] = item

    print(f"Total unique candidates collected: {len(all_candidates)}")
    session = requests.Session()
    new_items = []
    seen_hashes = set()
    for index, item in enumerate(all_candidates.values(), 1):
        try:
            record = download_candidate(session, item)
            if record['imageHash'] in seen_hashes:
                Path('gallery_media', Path(urllib.parse.urlsplit(record['image']).path).name).unlink(missing_ok=True)
                continue
            seen_hashes.add(record['imageHash'])
            new_items.append(record)
            print(f"Downloaded {index}/{len(all_candidates)}: {item['pin_id']}")
        except Exception as error:
            print(f"Image skipped {item['pin_id']}: {error}")
        time.sleep(0.2)

    final_items = merge_items(new_items, existing)
    remove_unused_files(final_items)
    output = {
        'lastUpdated': now_iso(),
        'status': f'Pinterest gallery completed: {len(new_items)} new, {len(final_items)} total',
        'queries': SEARCH_QUERIES,
        'total': len(final_items),
        'items': final_items,
    }
    temporary = GALLERY_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(GALLERY_FILE)
    print(f'New Pinterest images: {len(new_items)}')
    print(f'Final Pinterest gallery: {len(final_items)}')


if __name__ == '__main__':
    main()
