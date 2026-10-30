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
from playwright.sync_api import sync_playwright

GALLERY_FILE = Path('gallery.json')
GALLERY_MEDIA_DIR = Path('gallery_media')
RAW_GALLERY_BASE = 'https://raw.githubusercontent.com/reikableu/hou-minghao-updates/main/gallery_media'
SINGAPORE = ZoneInfo('Asia/Singapore')

SEARCH_QUERIES = ['Hou Minghao', 'HouMinhao', '侯明昊']
MAX_ITEMS_PER_QUERY = 45
MAX_GALLERY_ITEMS = 150
MAX_SCROLLS_PER_QUERY = 12
MIN_WIDTH = 300
MIN_HEIGHT = 300
REQUEST_TIMEOUT = 30

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/126.0.0.0 Safari/537.36'
)


def now_iso():
    return datetime.now(SINGAPORE).isoformat(timespec='seconds')


def load_pinterest_cookies():
    raw = os.environ.get('PINTEREST_COOKIES_JSON', '').strip()
    if not raw:
        return []
    cookies = json.loads(raw)
    if not isinstance(cookies, list):
        raise ValueError('PINTEREST_COOKIES_JSON must be a JSON array.')
    allowed = {'name', 'value', 'url', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}
    output = []
    for item in cookies:
        if not isinstance(item, dict) or not item.get('name') or item.get('value') is None:
            continue
        cookie = {k: v for k, v in item.items() if k in allowed}
        if not cookie.get('url') and not cookie.get('domain'):
            cookie['domain'] = '.pinterest.com'
            cookie['path'] = '/'
        same_site = str(cookie.get('sameSite', 'Lax')).capitalize()
        cookie['sameSite'] = same_site if same_site in {'Strict', 'Lax', 'None'} else 'Lax'
        output.append(cookie)
    return output


def normalize_pin_url(value):
    value = str(value or '').strip()
    if value.startswith('/'):
        value = urllib.parse.urljoin('https://www.pinterest.com', value)
    match = re.search(r'https?://(?:www\.)?pinterest\.[^/]+/pin/(\d+)', value, re.I)
    return f'https://www.pinterest.com/pin/{match.group(1)}/' if match else ''


def normalize_image_url(value):
    value = str(value or '').strip().replace('\\/', '/')
    if value.startswith('//'):
        value = 'https:' + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc.lower().endswith('pinimg.com'):
        return ''
    path = re.sub(r'/(?:\d+x|originals)/', '/originals/', parsed.path, count=1, flags=re.I)
    return urllib.parse.urlunsplit(('https', parsed.netloc, path, '', ''))


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


def collect_query(page, query):
    url = 'https://www.pinterest.com/search/pins/?q=' + urllib.parse.quote_plus(query) + '&rs=typed'
    print(f'Pinterest search: {query}')
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    found = {}
    stagnant = 0
    previous = 0
    for scroll in range(MAX_SCROLLS_PER_QUERY):
        raw = page.evaluate('''() => Array.from(document.querySelectorAll('a[href*="/pin/"]')).map(a => {
            const img = a.querySelector('img');
            if (!img) return null;
            let src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
            const srcset = img.srcset || '';
            if (srcset) {
                const choices = srcset.split(',').map(x => x.trim().split(/\\s+/)).map(x => ({url:x[0]||'', size:parseInt(x[1])||0})).sort((a,b)=>b.size-a.size);
                if (choices.length && choices[0].url) src = choices[0].url;
            }
            return {href:a.href, image:src, title:img.alt||'', width:img.naturalWidth||img.width||0, height:img.naturalHeight||img.height||0};
        }).filter(Boolean)''')
        for item in raw:
            pin_url = normalize_pin_url(item.get('href'))
            image_url = normalize_image_url(item.get('image'))
            width = int(item.get('width') or 0)
            height = int(item.get('height') or 0)
            if not pin_url or not image_url:
                continue
            if width and width < MIN_WIDTH:
                continue
            if height and height < MIN_HEIGHT:
                continue
            pin_id = re.search(r'/pin/(\d+)', pin_url).group(1)
            found[pin_id] = {
                'pin_id': pin_id,
                'pin_url': pin_url,
                'image_url': image_url,
                'title': str(item.get('title') or f'Hou Minghao Pinterest photo - {query}')[:250],
                'query': query,
                'width': width,
                'height': height,
            }
        print(f'  Scroll {scroll + 1}: {len(found)} unique pins')
        if len(found) >= MAX_ITEMS_PER_QUERY:
            break
        stagnant = stagnant + 1 if len(found) == previous else 0
        previous = len(found)
        if stagnant >= 3:
            break
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(1800)
    return list(found.values())[:MAX_ITEMS_PER_QUERY]


def extension_for(content_type, url):
    content_type = (content_type or '').lower()
    if 'png' in content_type:
        return '.png'
    if 'webp' in content_type:
        return '.webp'
    if 'gif' in content_type:
        return '.gif'
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else '.jpg'


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
        'width': item['width'],
        'height': item['height'],
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
        if item.get('origin') not in {'Pinterest', None} and item.get('platform') != 'Pinterest':
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
    cookies = load_pinterest_cookies()
    all_candidates = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'])
        context = browser.new_context(user_agent=USER_AGENT, locale='en-US', timezone_id='Asia/Singapore', viewport={'width': 1440, 'height': 1200})
        if cookies:
            context.add_cookies(cookies)
            print(f'Pinterest cookies loaded: {len(cookies)}')
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        for query in SEARCH_QUERIES:
            try:
                for item in collect_query(page, query):
                    all_candidates[item['pin_id']] = item
            except Exception as error:
                print(f'Pinterest search failed for {query}: {type(error).__name__}: {error}')
        browser.close()

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
            print(f"Image skipped {item['pin_id']}: {type(error).__name__}: {error}")
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
    print(f'Pinterest candidates: {len(all_candidates)}')
    print(f'New Pinterest images: {len(new_items)}')
    print(f'Final Pinterest gallery: {len(final_items)}')


if __name__ == '__main__':
    main()
