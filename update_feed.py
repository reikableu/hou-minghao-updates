import hashlib
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator


POSTS_FILE = Path("posts.json")
SINGAPORE = ZoneInfo("Asia/Singapore")
UTC = ZoneInfo("UTC")
MAX_POSTS = 200
MAX_TRANSLATED_POSTS_PER_RUN = 5
TRANSLATION_DELAY_SECONDS = 3
REQUEST_DELAY_SECONDS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SOURCES = [
    {
        "name": "Hou Minghao Weibo",
        "source": "侯明昊",
        "platform": "Weibo",
        "url": "https://sina.cn/media/1831550987",
        "official": True,
        "type": "sina_account",
    },
    {
        "name": "Hou Minghao Studio Weibo",
        "source": "侯明昊工作室",
        "platform": "Weibo",
        "url": "https://sina.cn/media/5763552342",
        "official": True,
        "type": "sina_account",
    },
    {
        "name": "Google News",
        "source": "Google News",
        "platform": "News",
        "query": "侯明昊",
        "official": False,
        "type": "google_news",
    },
    {
        "name": "Hou Minghao X List",
        "source": "Hou Minghao X Updates",
        "platform": "X",
        "url": "https://rss.app/feeds/k375DGhHRMrZ3Ju2.xml",
        "official": False,
        "type": "rss_feed",
    },
]


def singapore_now():
    return datetime.now(SINGAPORE)


def clean_text(value):
    if not value:
        return ""

    value = unescape(str(value))
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def shorten(value, maximum=220):
    value = clean_text(value)
    if len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"


def contains_chinese(text):
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def make_id(platform, source, title, url):
    value = "|".join(
        [platform or "", source or "", title or "", url or ""]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def determine_category(text, platform):
    text = text or ""
    lowered = text.lower()

    if platform == "X":
        return "X Update"
    if platform == "News":
        return "News"
    if any(word in text for word in ["演唱会", "活动", "见面会", "盛典"]):
        return "Event"
    if any(word in text for word in ["代言", "品牌", "大使"]):
        return "Endorsement"
    if any(word in text for word in ["歌曲", "新歌", "音乐", "上线"]):
        return "Music"
    if any(word in text for word in ["视频", "花絮", "直播"]):
        return "Video"
    if any(word in text for word in ["照片", "写真", "大片", "造型"]):
        return "Photos"
    if any(word in text for word in ["电视剧", "电影", "角色", "定档"]):
        return "Drama"
    if "interview" in lowered or "采访" in text:
        return "Interview"
    return "Official"


def load_existing_data():
    default = {
        "lastUpdated": None,
        "status": "Initial setup",
        "translationStatus": {},
        "sourceStatus": [],
        "posts": [],
    }

    if not POSTS_FILE.exists():
        return default

    try:
        with POSTS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("posts.json must contain a JSON object")
        data.setdefault("posts", [])
        data.setdefault("sourceStatus", [])
        return data
    except Exception as error:
        print("Could not read posts.json:", error)
        return default


def parse_possible_date(text):
    now = singapore_now()

    full_date = re.search(
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text or ""
    )
    if full_date:
        year, month, day = map(int, full_date.groups())
        try:
            return datetime(year, month, day, 12, 0, tzinfo=SINGAPORE).isoformat()
        except ValueError:
            pass

    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", text or "")
    if month_day:
        month, day = map(int, month_day.groups())
        try:
            candidate = datetime(now.year, month, day, 12, 0, tzinfo=SINGAPORE)
            if candidate > now.replace(hour=23, minute=59):
                candidate = candidate.replace(year=now.year - 1)
            return candidate.isoformat()
        except ValueError:
            pass

    return now.isoformat()


def parse_feed_date(value):
    value = clean_text(value)
    if not value:
        return singapore_now().isoformat()

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(SINGAPORE).isoformat()
    except Exception:
        pass

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(SINGAPORE).isoformat()
    except Exception:
        return singapore_now().isoformat()


def collect_sina_account(source_config):
    response = requests.get(
        source_config["url"], headers=HEADERS, timeout=30
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    selectors = [
        "article",
        ".card",
        ".feed-card",
        ".weibo-item",
        ".m-panel",
        ".content",
        "section",
    ]

    for selector in selectors:
        candidates.extend(soup.select(selector))

    if not candidates:
        candidates = soup.find_all(["div", "li"])

    collected = []
    seen_text = set()

    for element in candidates:
        text = clean_text(element.get_text(" ", strip=True))

        if len(text) < 25 or len(text) > 1800:
            continue
        if text in seen_text:
            continue
        if "侯明昊" not in text and source_config["source"] not in text:
            continue

        seen_text.add(text)
        link_element = element.find("a", href=True)
        post_url = source_config["url"]

        if link_element:
            post_url = urllib.parse.urljoin(
                source_config["url"], link_element.get("href")
            )

        title_text = re.sub(
            r"^\s*(侯明昊工作室|侯明昊)\s*", "", text
        )
        title = shorten(title_text, 120)
        excerpt = shorten(text, 320)

        collected.append(
            {
                "id": make_id(
                    source_config["platform"],
                    source_config["source"],
                    title,
                    post_url,
                ),
                "platform": source_config["platform"],
                "source": source_config["source"],
                "title": title or "New official Hou Minghao update",
                "excerpt": excerpt,
                "category": determine_category(
                    text, source_config["platform"]
                ),
                "date": parse_possible_date(text),
                "url": post_url,
                "official": source_config["official"],
                "verified": True,
                "collectionMethod": "Automatic",
            }
        )

        if len(collected) >= 15:
            break

    return collected


def collect_google_news(source_config):
    encoded_query = urllib.parse.quote(source_config["query"])
    feed_url = (
        "https://news.google.com/rss/search"
        f"?q={encoded_query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )

    response = requests.get(feed_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    collected = []

    for item in root.findall(".//item")[:25]:
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        description = clean_text(item.findtext("description"))
        publication_date = clean_text(item.findtext("pubDate"))
        source_element = item.find("source")
        publisher = (
            clean_text(source_element.text)
            if source_element is not None and source_element.text
            else "News source"
        )

        if not title or not link:
            continue

        collected.append(
            {
                "id": make_id("News", publisher, title, link),
                "platform": "News",
                "source": publisher,
                "title": shorten(title, 150),
                "excerpt": shorten(description, 300),
                "category": "News",
                "date": parse_feed_date(publication_date),
                "url": link,
                "official": False,
                "verified": True,
                "collectionMethod": "Automatic",
            }
        )

    return collected


def find_atom_text(item, field):
    atom = "{http://www.w3.org/2005/Atom}"
    return item.findtext(atom + field) or item.findtext(field)


def collect_rss_feed(source_config):
    response = requests.get(
        source_config["url"], headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)

    items = root.findall(".//item")
    is_atom = False

    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        is_atom = True

    collected = []

    for item in items[:25]:
        if is_atom:
            title = clean_text(find_atom_text(item, "title"))
            description = clean_text(
                find_atom_text(item, "content")
                or find_atom_text(item, "summary")
            )
            publication_date = clean_text(
                find_atom_text(item, "published")
                or find_atom_text(item, "updated")
            )
            author = clean_text(find_atom_text(item, "author"))
            link = ""
            for link_element in item.findall(
                "{http://www.w3.org/2005/Atom}link"
            ):
                candidate = clean_text(link_element.get("href", ""))
                if candidate:
                    link = candidate
                    break
        else:
            title = clean_text(item.findtext("title"))
            description = clean_text(
                item.findtext("description") or item.findtext("content")
            )
            publication_date = clean_text(
                item.findtext("pubDate")
                or item.findtext("published")
                or item.findtext("updated")
            )
            author = clean_text(
                item.findtext("author")
                or item.findtext(
                    "{http://purl.org/dc/elements/1.1/}creator"
                )
            )
            link = clean_text(item.findtext("link"))

        if not title and description:
            title = shorten(description, 150)
        if not title or not link:
            continue

        account_match = re.search(
            r"@([A-Za-z0-9_]+)", f"{author} {title} {description}"
        )
        source_name = (
            "@" + account_match.group(1)
            if account_match
            else author or source_config["source"]
        )

        collected.append(
            {
                "id": make_id("X", source_name, title, link),
                "platform": "X",
                "source": source_name,
                "title": shorten(title, 180),
                "excerpt": shorten(description, 350),
                "category": "X Update",
                "date": parse_feed_date(publication_date),
                "url": link,
                "official": False,
                "verified": True,
                "collectionMethod": "Automatic",
            }
        )

    return collected


def collect_source(source_config):
    if source_config["type"] == "sina_account":
        return collect_sina_account(source_config)
    if source_config["type"] == "google_news":
        return collect_google_news(source_config)
    if source_config["type"] == "rss_feed":
        return collect_rss_feed(source_config)
    return []


def normalize_existing_post(post):
    post.setdefault(
        "id",
        make_id(
            post.get("platform", ""),
            post.get("source", ""),
            post.get("title", ""),
            post.get("url", ""),
        ),
    )
    post.setdefault("excerpt", "")
    post.setdefault("category", "Other")
    post.setdefault("official", False)
    post.setdefault("verified", False)
    post.setdefault("collectionMethod", "Manual")
    return post


def post_timestamp(post):
    try:
        return datetime.fromisoformat(
            post.get("date", "").replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return 0


def translate_to_english(text):
    """Translate one field at a time so separators cannot be mistranslated."""
    text = clean_text(text)

    if not text:
        return {"translation": "", "rate_limited": False}
    if not contains_chinese(text):
        return {"translation": text, "rate_limited": False}

    last_error = None
    for attempt in range(2):
        try:
            translated = GoogleTranslator(source="auto", target="en").translate(text)
            translated = clean_text(translated)
            if translated:
                return {"translation": translated, "rate_limited": False}
            last_error = "Translation service returned an empty result"
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
            message = str(error).lower()
            rate_limited = (
                type(error).__name__ == "TooManyRequests"
                or "too many requests" in message
                or "429" in message
                or "rate limit" in message
            )
            print("Translation failed:", last_error[:200])
            if rate_limited:
                return {"translation": "", "rate_limited": True}
        if attempt == 0:
            time.sleep(2)

    if last_error:
        print("Giving up on this field:", last_error[:200])
    return {"translation": "", "rate_limited": False}


def add_translations(posts):
    translations_added = 0
    posts_translated = 0
    rate_limited = False

    print()
    print("Checking English translations...")

    for index, post in enumerate(posts, start=1):
        original_title = clean_text(
            post.get("titleOriginal") or post.get("title", "")
        )
        original_excerpt = clean_text(
            post.get("excerptOriginal") or post.get("excerpt", "")
        )

        post["titleOriginal"] = original_title
        post["excerptOriginal"] = original_excerpt

        title_english = clean_text(post.get("titleEnglish", ""))
        excerpt_english = clean_text(post.get("excerptEnglish", ""))

        title_needs_translation = contains_chinese(original_title) and not title_english
        excerpt_needs_translation = contains_chinese(original_excerpt) and not excerpt_english

        if not contains_chinese(original_title) and not title_english:
            post["titleEnglish"] = original_title
        if not contains_chinese(original_excerpt) and not excerpt_english:
            post["excerptEnglish"] = original_excerpt

        if not title_needs_translation and not excerpt_needs_translation:
            continue

        if posts_translated >= MAX_TRANSLATED_POSTS_PER_RUN:
            print(
                "Translation limit reached. Remaining posts will be "
                "handled during a future run."
            )
            break

        print(f"Translating post {index}/{len(posts)}")
        post_had_translation = False

        # Translate title and excerpt separately; Google Translate can alter
        # marker strings when both fields are sent as one combined request.
        for field_name, original_text, needs_translation in (
            ("titleEnglish", original_title, title_needs_translation),
            ("excerptEnglish", original_excerpt, excerpt_needs_translation),
        ):
            if not needs_translation:
                continue

            result = translate_to_english(original_text)
            if result["rate_limited"]:
                rate_limited = True
                print("Translation service rate limit reached; stopping this run.")
                break

            translated_text = result["translation"]
            if translated_text:
                post[field_name] = translated_text
                translations_added += 1
                post_had_translation = True
            time.sleep(TRANSLATION_DELAY_SECONDS)

        if rate_limited:
            break
        if post_had_translation:
            posts_translated += 1

    return translations_added, posts_translated, rate_limited


def main():
    data = load_existing_data()
    existing_posts = [
        normalize_existing_post(post)
        for post in data.get("posts", [])
        if isinstance(post, dict)
    ]

    collected_posts = []
    source_status = []

    for source_config in SOURCES:
        checked_at = singapore_now()
        print(f"Checking {source_config['name']}...")

        try:
            source_posts = collect_source(source_config)
            collected_posts.extend(source_posts)
            source_status.append(
                {
                    "source": source_config["name"],
                    "status": "Success",
                    "itemsFound": len(source_posts),
                    "checkedAt": checked_at.isoformat(),
                }
            )
            print(
                f"{source_config['name']}: "
                f"{len(source_posts)} items found"
            )
        except Exception as error:
            print(
                f"{source_config['name']} failed: "
                f"{type(error).__name__}: {error}"
            )
            source_status.append(
                {
                    "source": source_config["name"],
                    "status": "Failed",
                    "itemsFound": 0,
                    "checkedAt": checked_at.isoformat(),
                    "message": str(error)[:200],
                }
            )

        time.sleep(REQUEST_DELAY_SECONDS)

    combined = existing_posts + collected_posts
    deduplicated = {}

    for post in combined:
        post = normalize_existing_post(post)
        post_id = post["id"]
        if post_id not in deduplicated:
            deduplicated[post_id] = post
        else:
            # Preserve previously stored translations when a fresh copy is found.
            existing = deduplicated[post_id]
            for field in [
                "titleOriginal",
                "excerptOriginal",
                "titleEnglish",
                "excerptEnglish",
            ]:
                if existing.get(field) and not post.get(field):
                    post[field] = existing[field]
            deduplicated[post_id] = post

    final_posts = sorted(
        deduplicated.values(), key=post_timestamp, reverse=True
    )[:MAX_POSTS]

    translations_added, posts_translated, translation_rate_limited = (
        add_translations(final_posts)
    )

    successful_sources = sum(
        1 for source in source_status if source["status"] == "Success"
    )
    failed_sources = sum(
        1 for source in source_status if source["status"] == "Failed"
    )

    output = {
        "lastUpdated": singapore_now().isoformat(timespec="seconds"),
        "status": (
            f"Completed: {successful_sources} sources successful, "
            f"{failed_sources} failed"
        ),
        "translationStatus": {
            "translationsAdded": translations_added,
            "postsTranslatedThisRun": posts_translated,
            "maximumPostsPerRun": MAX_TRANSLATED_POSTS_PER_RUN,
            "rateLimited": translation_rate_limited,
        },
        "sourceStatus": source_status,
        "posts": final_posts,
    }

    with POSTS_FILE.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print()
    print("Collection completed")
    print("Newly collected:", len(collected_posts))
    print("Total saved:", len(final_posts))
    print("Successful sources:", successful_sources)
    print("Failed sources:", failed_sources)
    print("Translations added:", translations_added)
    print("Posts translated this run:", posts_translated)
    print("Translation rate limited:", translation_rate_limited)


if __name__ == "__main__":
    main()
