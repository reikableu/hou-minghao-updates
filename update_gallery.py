import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

POSTS_FILE = Path('posts.json')
GALLERY_FILE = Path('gallery.json')
SINGAPORE = ZoneInfo('Asia/Singapore')
MAX_GALLERY_ITEMS = 150


def make_gallery_id(image_url, post_url):
    return hashlib.sha256(f'{image_url}|{post_url}'.encode('utf-8')).hexdigest()[:24]


def load_posts():
    if not POSTS_FILE.exists():
        raise FileNotFoundError('posts.json was not found. Run update_feed.py first.')
    with POSTS_FILE.open('r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get('posts', []), list):
        raise ValueError('posts.json must contain an object with a posts array.')
    return data.get('posts', [])


def build_gallery(posts):
    items = []
    seen = set()
    for post in posts:
        if not isinstance(post, dict):
            continue
        images = post.get('images', [])
        if not isinstance(images, list):
            images = [images] if images else []
        post_url = str(post.get('url', '')).strip()
        platform = str(post.get('platform', 'Update')).strip()
        source = str(post.get('source', platform)).strip()
        title = str(post.get('titleOriginal') or post.get('title') or 'Hou Minghao gallery photo').strip()
        post_date = str(post.get('date', '')).strip()
        for raw_image in images:
            image_url = str(raw_image or '').strip()
            if not image_url.startswith(('https://', 'http://')) or image_url in seen:
                continue
            seen.add(image_url)
            items.append({
                'id': make_gallery_id(image_url, post_url),
                'image': image_url,
                'sourceUrl': post_url or image_url,
                'platform': platform,
                'source': source,
                'title': title,
                'date': post_date,
                'origin': 'Feed',
            })
            if len(items) >= MAX_GALLERY_ITEMS:
                return items
    return items


def main():
    posts = load_posts()
    items = build_gallery(posts)
    output = {
        'lastUpdated': datetime.now(SINGAPORE).isoformat(timespec='seconds'),
        'status': 'Gallery generated successfully',
        'total': len(items),
        'items': items,
    }
    temporary = GALLERY_FILE.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    temporary.replace(GALLERY_FILE)
    print(f'Posts loaded: {len(posts)}')
    print(f'Unique gallery images: {len(items)}')
    print('gallery.json created successfully.')


if __name__ == '__main__':
    main()
