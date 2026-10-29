import hashlib
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator


POSTS_FILE = Path("posts.json")
SINGAPORE = ZoneInfo("Asia/Singapore")
MAX_POSTS = 200
MAX_TRANSLATIONS_PER_RUN = 5
TRANSLATION_DELAY_SECONDS = 8

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
]


def singapore_now():
    return datetime.now(SINGAPORE)


def clean_text(value):
    if not value:
        return ""

    value = unescape(value)
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def shorten(value, maximum=220):
    value = clean_text(value)

    if len(value) <= maximum:
        return value

    return value[: maximum - 1].rstrip() + "…"

def contains_chinese(text):
    if not text:
        return False

    return bool(
        re.search(
            r"[\u3400-\u4dbf\u4e00-\u9fff]",
            text,
        )
    )

def translate_to_english(text):
    text = clean_text(text)

    if not text:
        return {
            "translation": "",
            "rate_limited": False,
        }

    if not contains_chinese(text):
        return {
            "translation": text,
            "rate_limited": False,
        }

    try:
        translated = GoogleTranslator(
            source="auto",
            target="en",
        ).translate(text=text)

        return {
            "translation": clean_text(translated),
            "rate_limited": False,
        }

    except Exception as error:
        error_name = type(error).__name__
        error_message = str(error)

        print(
            "Translation failed:",
            error_name,
            error_message[:200],
        )

        is_rate_limited = (
            error_name == "TooManyRequests"
            or "too many requests" in error_message.lower()
            or "429" in error_message
        )

        return {
            "translation": "",
            "rate_limited": is_rate_limited,
        }
    text = clean_text(text)

    if not text:
        return ""

    if not contains_chinese(text):
        return text

    try:
        translated = GoogleTranslator(
            source="auto",
            target="en",
        ).translate(text=text)

        return clean_text(translated)

    except Exception as error:
        print(
            "Translation failed:",
            type(error).__name__,
            str(error)[:150],
        )

        return ""
        
def make_id(platform, source, title, url):
    value = "|".join(
        [
            platform or "",
            source or "",
            title or "",
            url or "",
        ]
    )

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def determine_category(text, platform):
    lowered = text.lower()

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

    if platform == "News":
        return "News"

    return "Official"


def load_existing_data():
    if not POSTS_FILE.exists():
        return {
            "lastUpdated": None,
            "status": "Initial setup",
            "sourceStatus": [],
            "posts": [],
        }

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

        return {
            "lastUpdated": None,
            "status": "Recovered from an invalid posts.json file",
            "sourceStatus": [],
            "posts": [],
        }


def parse_possible_date(text):
    now = singapore_now()

    patterns = [
        r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})",
        r"(\d{1,2})月(\d{1,2})日",
    ]

    full_date = re.search(patterns[0], text)

    if full_date:
        year, month, day = map(int, full_date.groups())

        try:
            return datetime(
                year,
                month,
                day,
                12,
                0,
                tzinfo=SINGAPORE,
            ).isoformat()
        except ValueError:
            pass

    month_day = re.search(patterns[1], text)

    if month_day:
        month, day = map(int, month_day.groups())
        year = now.year

        try:
            candidate = datetime(
                year,
                month,
                day,
                12,
                0,
                tzinfo=SINGAPORE,
            )

            if candidate > now.replace(hour=23, minute=59):
                candidate = candidate.replace(year=year - 1)

            return candidate.isoformat()
        except ValueError:
            pass

    return now.isoformat()


def collect_sina_account(source_config):
    response = requests.get(
        source_config["url"],
        headers=HEADERS,
        timeout=30,
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

        if len(text) < 25 or len(text) > 1500:
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
                source_config["url"],
                link_element.get("href"),
            )

        title_text = re.sub(
            r"^\s*(侯明昊工作室|侯明昊)\s*",
            "",
            text,
        )

        title = shorten(title_text, 90)
        excerpt = shorten(text, 240)
        date_value = parse_possible_date(text)

        post = {
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
                text,
                source_config["platform"],
            ),
            "date": date_value,
            "url": post_url,
            "official": source_config["official"],
            "verified": True,
            "collectionMethod": "Automatic",
        }

        collected.append(post)

        if len(collected) >= 15:
            break

    return collected


def collect_google_news(source_config):
    encoded_query = urllib.parse.quote(source_config["query"])

    feed_url = (
        "https://news.google.com/rss/search"
        f"?q={encoded_query}"
        "&hl=zh-CN"
        "&gl=CN"
        "&ceid=CN:zh-Hans"
    )

    response = requests.get(
        feed_url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    collected = []

    for item in root.findall(".//item")[:25]:
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        description = clean_text(item.findtext("description"))
        publication_date = clean_text(item.findtext("pubDate"))
        publisher = "News source"

        source_element = item.find("source")

        if source_element is not None and source_element.text:
            publisher = clean_text(source_element.text)

        try:
            parsed_date = datetime.strptime(
                publication_date,
                "%a, %d %b %Y %H:%M:%S %Z",
            )
            parsed_date = parsed_date.replace(
                tzinfo=ZoneInfo("UTC")
            ).astimezone(SINGAPORE)
            date_value = parsed_date.isoformat()
        except Exception:
            date_value = singapore_now().isoformat()

        if not title or not link:
            continue

        post = {
            "id": make_id(
                source_config["platform"],
                publisher,
                title,
                link,
            ),
            "platform": "News",
            "source": publisher,
            "title": shorten(title, 130),
            "excerpt": shorten(description, 240),
            "category": determine_category(title, "News"),
            "date": date_value,
            "url": link,
            "official": False,
            "verified": True,
            "collectionMethod": "Automatic",
        }

        collected.append(post)

    return collected


def collect_source(source_config):
    if source_config["type"] == "sina_account":
        return collect_sina_account(source_config)

    if source_config["type"] == "google_news":
        return collect_google_news(source_config)

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
        started = singapore_now()

        try:
            print(f"Checking {source_config['name']}...")

            posts = collect_source(source_config)
            collected_posts.extend(posts)

            source_status.append(
                {
                    "source": source_config["name"],
                    "status": "Success",
                    "itemsFound": len(posts),
                    "checkedAt": started.isoformat(),
                }
            )

            print(
                f"{source_config['name']}: "
                f"{len(posts)} items found"
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
                    "checkedAt": started.isoformat(),
                    "message": str(error)[:200],
                }
            )

        time.sleep(2)

    combined = existing_posts + collected_posts
    deduplicated = {}

    for post in combined:
        post_id = post.get("id")

        if not post_id:
            post_id = make_id(
                post.get("platform", ""),
                post.get("source", ""),
                post.get("title", ""),
                post.get("url", ""),
            )
            post["id"] = post_id

        if post_id not in deduplicated:
            deduplicated[post_id] = post

    final_posts = sorted(
        deduplicated.values(),
        key=post_timestamp,
        reverse=True,
    )[:MAX_POSTS]

    print()
    print("Checking English translations...")

    translations_added = 0
    posts_translated_this_run = 0
    translation_rate_limited = False

    for index, post in enumerate(final_posts, start=1):
        original_title = clean_text(
            post.get("titleOriginal")
            or post.get("title", "")
        )

        original_excerpt = clean_text(
            post.get("excerptOriginal")
            or post.get("excerpt", "")
        )

        post["titleOriginal"] = original_title
        post["excerptOriginal"] = original_excerpt

        existing_title_translation = clean_text(
            post.get("titleEnglish", "")
        )

        existing_excerpt_translation = clean_text(
            post.get("excerptEnglish", "")
        )

        title_needs_translation = (
            contains_chinese(original_title)
            and not existing_title_translation
        )

        excerpt_needs_translation = (
            contains_chinese(original_excerpt)
            and not existing_excerpt_translation
        )

        if (
            not contains_chinese(original_title)
            and not existing_title_translation
        ):
            post["titleEnglish"] = original_title

        if (
            not contains_chinese(original_excerpt)
            and not existing_excerpt_translation
        ):
            post["excerptEnglish"] = original_excerpt

        if not (
            title_needs_translation
            or excerpt_needs_translation
        ):
            continue

        if (
            posts_translated_this_run
            >= MAX_TRANSLATIONS_PER_RUN
        ):
            print(
                "Translation limit reached. "
                "Remaining posts will be handled "
                "during a future run."
            )
            break

        print(
            f"Translating post "
            f"{index}/{len(final_posts)}"
        )

        separator = "\n\n---EXCERPT---\n\n"

        combined_parts = []

        if title_needs_translation:
            combined_parts.append(original_title)
        else:
            combined_parts.append("")

        if excerpt_needs_translation:
            combined_parts.append(original_excerpt)
        else:
            combined_parts.append("")

        combined_text = separator.join(combined_parts)

        result = translate_to_english(combined_text)

        if result["rate_limited"]:
            translation_rate_limited = True

            print(
                "Translation service rate limit reached. "
                "Stopping translations for this run."
            )

            break

        translated_text = result["translation"]

        if not translated_text:
            print(
                "No translation returned. "
                "Moving to the next post."
            )

            time.sleep(
                TRANSLATION_DELAY_SECONDS
            )

            continue

        translated_parts = translated_text.split(
            "---EXCERPT---",
            1,
        )

        translated_title = clean_text(
            translated_parts[0]
            if translated_parts
            else ""
        )

        translated_excerpt = clean_text(
            translated_parts[1]
            if len(translated_parts) > 1
            else ""
        )

        if title_needs_translation and translated_title:
            post["titleEnglish"] = translated_title
            translations_added += 1

        if (
            excerpt_needs_translation
            and translated_excerpt
        ):
            post["excerptEnglish"] = translated_excerpt
            translations_added += 1

        posts_translated_this_run += 1

        time.sleep(
            TRANSLATION_DELAY_SECONDS
        )

    successful_sources = sum(
        1
        for source in source_status
        if source["status"] == "Success"
    )

    failed_sources = sum(
        1
        for source in source_status
        if source["status"] == "Failed"
    )

    output = {
        "lastUpdated": singapore_now().isoformat(
            timespec="seconds"
        ),
        "status": (
            f"Completed: {successful_sources} sources successful, "
            f"{failed_sources} failed"
        ),
        "translationStatus": {
            "translationsAdded": translations_added,
            "postsTranslatedThisRun": (
                posts_translated_this_run
            ),
            "maximumPostsPerRun": (
                MAX_TRANSLATIONS_PER_RUN
            ),
            "rateLimited": translation_rate_limited,
        },
        "sourceStatus": source_status,
        "posts": final_posts,
    }

    with POSTS_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            output,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("Collection completed")
    print("Newly collected:", len(collected_posts))
    print("Total saved:", len(final_posts))
    print("Successful sources:", successful_sources)
    print("Failed sources:", failed_sources)
    print("Translations added:", translations_added)
    print(
        "Posts translated this run:",
        posts_translated_this_run,
    )

    print(
        "Translation rate limited:",
        translation_rate_limited,
    )

if __name__ == "__main__":
    main()
