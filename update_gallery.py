import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


POSTS_FILE = Path("posts.json")
GALLERY_FILE = Path("gallery.json")

SINGAPORE = ZoneInfo("Asia/Singapore")

# Keep a larger pool so the dashboard can randomly choose 18.
MAX_GALLERY_ITEMS = 150

# Do not include extremely old entries if they no longer have images.
REQUIRE_IMAGE = True


def singapore_now():
    return datetime.now(SINGAPORE)


def make_gallery_id(image_url, post_url):
    value = f"{image_url}|{post_url}"
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:24]


def load_posts():
    if not POSTS_FILE.exists():
        raise FileNotFoundError(
            "posts.json was not found. Run update_feed.py first."
        )

    with POSTS_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("posts.json must contain a JSON object.")

    posts = data.get("posts", [])

    if not isinstance(posts, list):
        raise ValueError(
            "The posts field in posts.json must be an array."
        )

    return posts


def normalize_image_url(value):
    if not value:
        return ""

    value = str(value).strip()

    if value.startswith(("https://", "http://")):
        return value

    return ""


def build_gallery(posts):
    gallery_items = []
    seen_images = set()

    for post in posts:
        if not isinstance(post, dict):
            continue

        images = post.get("images", [])

        if not isinstance(images, list):
            images = [images] if images else []

        post_url = str(post.get("url", "")).strip()
        platform = str(
            post.get("platform", "Update")
        ).strip()
        source = str(
            post.get("source", platform)
        ).strip()
        title = str(
            post.get("titleOriginal")
            or post.get("title")
            or "Hou Minghao gallery photo"
        ).strip()
        post_date = str(post.get("date", "")).strip()

        for raw_image in images:
            image_url = normalize_image_url(raw_image)

            if REQUIRE_IMAGE and not image_url:
                continue

            if image_url in seen_images:
                continue

            seen_images.add(image_url)

            gallery_items.append(
                {
                    "id": make_gallery_id(
                        image_url,
                        post_url,
                    ),
                    "image": image_url,
                    "sourceUrl": post_url,
                    "platform": platform,
                    "source": source,
                    "title": title,
                    "date": post_date,
                    "origin": "Feed",
                }
            )

    # posts.json is already sorted newest first, so this keeps
    # newer gallery images ahead of older ones.
    return gallery_items[:MAX_GALLERY_ITEMS]


def save_gallery(items):
    output = {
        "lastUpdated": singapore_now().isoformat(
            timespec="seconds"
        ),
        "status": "Gallery generated successfully",
        "total": len(items),
        "items": items,
    }

    temporary_file = Path("gallery.json.tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            output,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_file.replace(GALLERY_FILE)


def main():
    print("=" * 60)
    print("Hou Minghao Gallery Generator")
    print("=" * 60)

    posts = load_posts()

    print(f"Posts loaded: {len(posts)}")

    gallery_items = build_gallery(posts)

    print(f"Unique gallery images: {len(gallery_items)}")

    save_gallery(gallery_items)

    print("gallery.json created successfully.")


if __name__ == "__main__":
    main()
