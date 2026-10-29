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
MAX_TRANSLATED_POSTS_PER_RUN = 10  # Successful posts; Weibo is prioritized so News cannot use the whole allowance.
TRANSLATION_DELAY_SECONDS = 5
REQUEST_DELAY_SECONDS = 2
WEIBO_POST_LIMIT = 15  # Maximum matching Weibo posts across all accounts per Singapore calendar day.

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
WEIBO_ACCOUNT_IDS = [
    "5763552342", "7954477778", "1831550987", "7431934486",
    "6941933193", "5919565877", "9072975151", "6609562381",
    "5894691419", "1882778030", "7728002071", "7854683575",
    "2682624851",
]

SOURCES = [
    {
        "name": f"Weibo account {account_id}",
        "source": f"Weibo @{account_id}",
        "platform": "Weibo",
        "url": f"https://sina.cn/media/{account_id}",
        "official": account_id in {"1831550987", "5763552342"},
        "type": "sina_account",
    }
    for account_id in WEIBO_ACCOUNT_IDS
] + [
    {"name": "Hou Minghao X List", "source": "Hou Minghao X Updates", "platform": "X",
     "url": "https://rss.app/feeds/k375DGhHRMrZ3Ju2.xml", "official": False, "type": "rss_feed"},
]


def singapore_now():
    return datetime.now(SINGAPORE)


def clean_text(value):
    if not value:
        return ""
    value = unescape(str(value))
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def shorten(value, maximum=220):
    value = clean_text(value)
    return value if len(value) <= maximum else value[:maximum - 1].rstrip() + "…"


def contains_chinese(text):
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def make_id(platform, source, title, url):
    value = "|".join([platform or "", source or "", title or "", url or ""])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def canonical_post_url(url):
    """Normalize URLs for duplicate detection without dropping meaningful IDs."""
    url = clean_text(url)
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    host = (parts.netloc or "").lower()
    # Unify Weibo domain variations to prevent mobile vs desktop duplicates
    if host in {"sina.cn", "m.weibo.cn", "weibo.com"}:
        host = "weibo.com"
    path = re.sub(r"/+$", "", parts.path or "")
    query_items = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query_items = [(k, v) for k, v in query_items if not k.lower().startswith("utm_")
                   and k.lower() not in {"from", "source", "ref", "share_source"}]
    query = urllib.parse.urlencode(sorted(query_items))
    return urllib.parse.urlunsplit((parts.scheme.lower() or "https", host, path, query, ""))


def is_profile_url(url):
    path = urllib.parse.urlsplit(canonical_post_url(url)).path.lower()
    return bool(re.search(r"/(media|u)/\d+$", path)) or path.endswith("/mygroups")


def duplicate_key(post):
    """Prefer a post-specific URL; fall back to normalized Weibo text when only a profile URL exists."""
    url = canonical_post_url(post.get("url", ""))
    if url and not is_profile_url(url):
        return f"url:{url}"
    if post.get("platform") == "Weibo":
        text = clean_text(f"{post.get('titleOriginal') or post.get('title', '')} {post.get('excerptOriginal') or post.get('excerpt', '')}").lower()
        text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
        if text:
            return "weibo-text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return "id:" + str(post.get("id") or make_id(post.get("platform", ""), post.get("source", ""), post.get("title", ""), url))


def deduplicate_posts(posts):
    """Merge duplicate posts while preserving translations already saved."""
    result = {}
    for raw in posts:
        post = normalize_existing_post(raw)
        key = duplicate_key(post)
        if key not in result:
            result[key] = post
            continue
        old = result[key]
        preferred = post if len(clean_text(post.get("excerpt", ""))) > len(clean_text(old.get("excerpt", ""))) else old
        for field in ["titleOriginal", "excerptOriginal", "titleEnglish", "excerptEnglish"]:
            if old.get(field) and not preferred.get(field):
                preferred[field] = old[field]
            if post.get(field) and not preferred.get(field):
                preferred[field] = post[field]
        if is_profile_url(preferred.get("url", "")) and not is_profile_url(old.get("url", "")):
            preferred["url"] = old["url"]
        result[key] = preferred
    return list(result.values())


def determine_category(text, platform):
    lowered = (text or "").lower()
    if platform == "X": return "X Update"
    if platform == "News": return "News"
    if any(w in text for w in ["演唱会", "活动", "见面会", "盛典"]): return "Event"
    if any(w in text for w in ["代言", "品牌", "大使"]): return "Endorsement"
    if any(w in text for w in ["歌曲", "新歌", "音乐", "上线"]): return "Music"
    if any(w in text for w in ["视频", "花絮", "直播"]): return "Video"
    if any(w in text for w in ["照片", "写真", "大片", "造型"]): return "Photos"
    if any(w in text for w in ["电视剧", "电影", "角色", "定档"]): return "Drama"
    if "interview" in lowered or "采访" in text: return "Interview"
    return "Official"


def load_existing_data():
    default = {"lastUpdated": None, "status": "Initial setup", "translationStatus": {}, "sourceStatus": [], "posts": []}
    if not POSTS_FILE.exists(): return default
    try:
        with POSTS_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict): raise ValueError("posts.json must contain a JSON object")
        data.setdefault("posts", []); data.setdefault("sourceStatus", [])
        return data
    except Exception as error:
        print("Could not read posts.json:", error)
        return default


def parse_possible_date(text):
    now = singapore_now()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text or "")
    if match:
        try:
            y, m, d = map(int, match.groups())
            return datetime(y, m, d, 12, tzinfo=SINGAPORE).isoformat()
        except ValueError: pass
    match = re.search(r"(\d{1,2})月(\d{1,2})日", text or "")
    if match:
        try:
            m, d = map(int, match.groups())
            candidate = datetime(now.year, m, d, 12, tzinfo=SINGAPORE)
            if candidate > now.replace(hour=23, minute=59): candidate = candidate.replace(year=now.year - 1)
            return candidate.isoformat()
        except ValueError: pass
    return now.isoformat()


def parse_feed_date(value):
    value = clean_text(value)
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(SINGAPORE).isoformat()
        except Exception: pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(SINGAPORE).isoformat()
        except Exception: pass
    return singapore_now().isoformat()


def extract_weibo_display_name(soup, account_id):
    candidates = []
    for selector, attr in [
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
    ]:
        node = soup.select_one(selector)
        if node and node.get(attr):
            candidates.append(clean_text(node.get(attr)))
    if soup.title and soup.title.string:
        candidates.append(clean_text(soup.title.string))

    for candidate in candidates:
        candidate = re.sub(r"\s*[-_|｜].*$", "", candidate).strip()
        candidate = re.sub(r"^(微博|新浪微博)\s*[-_|｜:]?\s*", "", candidate).strip()
        candidate = re.sub(r"的微博(?:主页|)?$", "", candidate).strip()
        candidate = re.sub(r"\s*的个人主页$", "", candidate).strip()
        if (candidate and candidate not in {account_id, "新浪微博", "微博", "Sina Visitor System"}
                and len(candidate) <= 60 and not re.fullmatch(r"\d+", candidate)):
            return candidate
    return f"Weibo @{account_id}"


def collect_sina_account(source_config):
    response = requests.get(source_config["url"], headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    account_id = source_config["url"].rstrip("/").split("/")[-1]
    display_name = extract_weibo_display_name(soup, account_id)
    source_config["source"] = display_name
    source_config["name"] = display_name
    candidates = []
    for selector in ["article", ".card", ".feed-card", ".weibo-item", ".m-panel", ".content", "section"]:
        candidates.extend(soup.select(selector))
    if not candidates: candidates = soup.find_all(["div", "li"])
    collected, seen_text = [], set()
    for element in candidates:
        text = clean_text(element.get_text(" ", strip=True))
        if len(text) < 25 or len(text) > 1800 or text in seen_text: continue
        if "侯明昊" not in text: continue
        seen_text.add(text)
        link_element = element.find("a", href=True)
        post_url = urllib.parse.urljoin(source_config["url"], link_element.get("href")) if link_element else source_config["url"]
        title = shorten(re.sub(r"^\s*(侯明昊工作室|侯明昊)\s*", "", text), 120)
        collected.append({
            "id": make_id(source_config["platform"], source_config["source"], title, post_url),
            "platform": source_config["platform"], "source": source_config["source"],
            "title": title or "New official Hou Minghao update", "excerpt": shorten(text, 320),
            "category": determine_category(text, source_config["platform"]), "date": parse_possible_date(text),
            "url": post_url, "official": source_config["official"], "verified": True,
            "collectionMethod": "Automatic",
        })
    return collected


def collect_google_news(source_config):
    encoded = urllib.parse.quote(source_config["query"])
    url = f"https://news.google.com/rss/search?q={encoded}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    response = requests.get(url, headers=HEADERS, timeout=30); response.raise_for_status()
    root = ET.fromstring(response.content); collected = []
    for item in root.findall(".//item")[:25]:
        title, link = clean_text(item.findtext("title")), clean_text(item.findtext("link"))
        desc, pub = clean_text(item.findtext("description")), clean_text(item.findtext("pubDate"))
        source_el = item.find("source")
        publisher = clean_text(source_el.text) if source_el is not None and source_el.text else "News source"
        if not title or not link: continue
        collected.append({"id": make_id("News", publisher, title, link), "platform": "News", "source": publisher,
                          "title": shorten(title, 150), "excerpt": shorten(desc, 300), "category": "News",
                          "date": parse_feed_date(pub), "url": link, "official": False, "verified": True,
                          "collectionMethod": "Automatic"})
    return collected


def find_atom_text(item, field):
    atom = "{http://www.w3.org/2005/Atom}"
    return item.findtext(atom + field) or item.findtext(field)


def collect_rss_feed(source_config):
    response = requests.get(source_config["url"], headers=HEADERS, timeout=30); response.raise_for_status()
    root = ET.fromstring(response.content); items = root.findall(".//item"); is_atom = False
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry"); is_atom = True
    collected = []
    for item in items[:25]:
        if is_atom:
            title = clean_text(find_atom_text(item, "title")); desc = clean_text(find_atom_text(item, "content") or find_atom_text(item, "summary"))
            pub = clean_text(find_atom_text(item, "published") or find_atom_text(item, "updated")); author = clean_text(find_atom_text(item, "author"))
            link = ""
            for le in item.findall("{http://www.w3.org/2005/Atom}link"):
                if le.get("href"): link = clean_text(le.get("href")); break
        else:
            title = clean_text(item.findtext("title")); desc = clean_text(item.findtext("description") or item.findtext("content"))
            pub = clean_text(item.findtext("pubDate") or item.findtext("published") or item.findtext("updated"))
            author = clean_text(item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator")); link = clean_text(item.findtext("link"))
        if not title and desc: title = shorten(desc, 150)
        if not title or not link: continue
        
        match = re.search(r"@([A-Za-z0-9_]+)", f"{author} {title} {desc}")
        author_name = "@" + match.group(1) if match else author or source_config["source"]

        # Keep source matching source_config["source"] so UI source filtering works correctly
        collected.append({
            "id": make_id("X", source_config["source"], title, link),
            "platform": "X",
            "source": source_config["source"],
            "author": author_name,
            "title": shorten(title, 180),
            "excerpt": shorten(desc, 350),
            "category": "X Update",
            "date": parse_feed_date(pub),
            "url": link,
            "official": False,
            "verified": True,
            "collectionMethod": "Automatic"
        })
    return collected


def collect_source(source_config):
    kind = source_config["type"]
    if kind == "sina_account": return collect_sina_account(source_config)
    if kind == "google_news": return collect_google_news(source_config)
    if kind == "rss_feed": return collect_rss_feed(source_config)
    return []


def normalize_existing_post(post):
    post.setdefault("id", make_id(post.get("platform", ""), post.get("source", ""), post.get("title", ""), post.get("url", "")))
    post.setdefault("excerpt", ""); post.setdefault("category", "Other"); post.setdefault("official", False)
    post.setdefault("verified", False); post.setdefault("collectionMethod", "Manual")
    return post


def post_timestamp(post):
    try: return datetime.fromisoformat(post.get("date", "").replace("Z", "+00:00")).timestamp()
    except Exception: return 0


def translate_with_mymemory(text):
    response = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": "zh-CN|en"},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    translated = clean_text((payload.get("responseData") or {}).get("translatedText", ""))
    status = payload.get("responseStatus")
    if status not in (None, 200, "200"):
        raise RuntimeError(f"MyMemory response status {status}")
    if not translated:
        return ""
    normalized_input = re.sub(r"\s+", "", text).lower()
    normalized_output = re.sub(r"\s+", "", translated).lower()
    if normalized_output == normalized_input:
        return ""
    if contains_chinese(translated) and not re.search(r"[A-Za-z]", translated):
        return ""
    return translated


def translate_to_english(text):
    text = clean_text(text)
    if not text:
        return {"translation": "", "rate_limited": False, "provider": "none"}
    if not contains_chinese(text):
        return {"translation": text, "rate_limited": False, "provider": "not_needed"}

    google_limited = False
    try:
        translated = clean_text(GoogleTranslator(source="auto", target="en").translate(text))
        if translated:
            return {"translation": translated, "rate_limited": False, "provider": "Google"}
    except Exception as error:
        message = str(error).lower()
        google_limited = type(error).__name__ == "TooManyRequests" or any(
            marker in message for marker in ["too many requests", "429", "rate limit"]
        )
        print("Google translation failed:", f"{type(error).__name__}: {error}"[:200])

    try:
        translated = translate_with_mymemory(text)
        if translated:
            print("Translation backup used: MyMemory")
            return {"translation": translated, "rate_limited": False, "provider": "MyMemory"}
    except Exception as error:
        print("MyMemory backup failed:", f"{type(error).__name__}: {error}"[:200])

    return {"translation": "", "rate_limited": google_limited, "provider": "unavailable"}


def add_translations(posts):
    translations_added = 0
    posts_translated = 0
    posts_attempted = 0
    rate_limited = False
    translation_providers = {"Google": 0, "MyMemory": 0}
    print("\nChecking English translations (Weibo only)...")

    for post in posts:
        if post.get("platform") != "Weibo":
            post.pop("titleEnglish", None)
            post.pop("excerptEnglish", None)

    weibo_posts = [post for post in posts if post.get("platform"] == "Weibo"]

    def translation_priority(post):
        text = clean_text(
            f"{post.get('titleOriginal') or post.get('title', '')} "
            f"{post.get('excerptOriginal') or post.get('excerpt', '')}"
        )
        needs_translation = 0 if contains_chinese(text) else 1
        return (needs_translation, -post_timestamp(post))

    for index, post in enumerate(sorted(weibo_posts, key=translation_priority), 1):
        original_title = clean_text(post.get("titleOriginal") or post.get("title", ""))
        original_excerpt = clean_text(post.get("excerptOriginal") or post.get("excerpt", ""))
        post["titleOriginal"], post["excerptOriginal"] = original_title, original_excerpt
        title_en = clean_text(post.get("titleEnglish", ""))
        excerpt_en = clean_text(post.get("excerptEnglish", ""))
        title_need = contains_chinese(original_title) and not title_en
        excerpt_need = contains_chinese(original_excerpt) and not excerpt_en

        if not contains_chinese(original_title) and not title_en:
            post["titleEnglish"] = original_title
        if not contains_chinese(original_excerpt) and not excerpt_en:
            post["excerptEnglish"] = original_excerpt

        if not title_need and not excerpt_need:
            continue
        if posts_attempted >= MAX_TRANSLATED_POSTS_PER_RUN:
            print("Translation attempt limit reached; remaining Weibo posts will be handled in a future run.")
            break

        posts_attempted += 1
        print(f"Translating Weibo post {index}/{len(weibo_posts)}")
        had_translation = False
        for field, original, needed in [
            ("titleEnglish", original_title, title_need),
            ("excerptEnglish", original_excerpt, excerpt_need),
        ]:
            if not needed:
                continue
            result = translate_to_english(original)
            if result["rate_limited"]:
                rate_limited = True
            if result["translation"]:
                post[field] = result["translation"]
                translations_added += 1
                had_translation = True
                provider = result.get("provider")
                if provider in translation_providers:
                    translation_providers[provider] += 1
                time.sleep(TRANSLATION_DELAY_SECONDS)
            else:
                print(f"Weibo translation unavailable for {field}; will retry on a later run.")
        if had_translation:
            posts_translated += 1

    return translations_added, posts_translated, rate_limited, translation_providers


def main():
    data = load_existing_data()
    existing = deduplicate_posts([p for p in data.get("posts", [])
                                  if isinstance(p, dict) and p.get("platform"] != "News"])
    collected, statuses = [], []
    for config in SOURCES:
        checked = singapore_now(); print(f"Checking {config['name']}...")
        try:
            posts = collect_source(config); collected.extend(posts)
            statuses.append({"source": config["name"], "status": "Success", "itemsFound": len(posts), "checkedAt": checked.isoformat()})
            print(f"{config['name']}: {len(posts)} items found")
        except Exception as error:
            print(f"{config['name']} failed: {type(error).__name__}: {error}")
            statuses.append({"source": config["name"], "status": "Failed", "itemsFound": 0, "checkedAt": checked.isoformat(), "message": str(error)[:200]})
        time.sleep(REQUEST_DELAY_SECONDS)

    today = singapore_now().date()
    def is_weibo_today(post):
        if post.get("platform") != "Weibo":
            return False
        try:
            stamp = datetime.fromisoformat(post.get("date", "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return stamp.astimezone(SINGAPORE).date() == today
        except Exception:
            return False

    existing_today_keys = {duplicate_key(p) for p in existing if is_weibo_today(p)}
    slots = max(0, WEIBO_POST_LIMIT - len(existing_today_keys))
    new_weibo = [p for p in collected if p.get("platform") == "Weibo"]
    other_new = [p for p in collected if p.get("platform"] not in {"Weibo", "News"}]
    unique_weibo = deduplicate_posts(new_weibo)
    ordered_weibo = sorted(unique_weibo, key=post_timestamp, reverse=True)
    accepted_weibo = [p for p in ordered_weibo if duplicate_key(p) not in existing_today_keys][:slots]
    collected = accepted_weibo + other_new
    print(f"Weibo daily limit: {len(existing_today_keys)} already saved today; "
          f"accepted {len(accepted_weibo)} new matching posts (limit {WEIBO_POST_LIMIT}).")

    account_names = {}
    for config in SOURCES:
        if config.get("type") == "sina_account":
            account_id = config.get("url", "").rstrip("/").split("/")[-1]
            name = config.get("source", "")
            if account_id and name and not re.fullmatch(r"Weibo @?\d+", name):
                account_names[account_id] = name
    for post in existing:
        if post.get("platform") == "Weibo":
            match = re.search(r"Weibo @?(\d+)", str(post.get("source", "")))
            if match and match.group(1) in account_names:
                post["source"] = account_names[match.group(1)]

    merged = deduplicate_posts(existing + collected)
    final_posts = sorted(merged, key=post_timestamp, reverse=True)[:MAX_POSTS]
    added, translated, limited, translation_providers = add_translations(final_posts)
    success = sum(s["status"] == "Success" for s in statuses); failed = len(statuses) - success
    output = {"lastUpdated": singapore_now().isoformat(timespec="seconds"),
              "status": f"Completed: {success} sources successful, {failed} failed",
              "translationStatus": {"translationsAdded": added, "postsTranslatedThisRun": translated,
                                    "maximumPostsPerRun": MAX_TRANSLATED_POSTS_PER_RUN, "rateLimited": limited,
                                    "providersUsed": translation_providers,
                                    "priority": "Weibo only",
                                    "platformsTranslated": ["Weibo"],
                                    "newsPostsRemoved": True,
                                    "backupProvider": "MyMemory"},
              "sourceStatus": statuses, "posts": final_posts}
    with POSTS_FILE.open("w", encoding="utf-8") as f: json.dump(output, f, ensure_ascii=False, indent=2)
    print("\nCollection completed")
    print("Newly collected:", len(collected)); print("Total saved:", len(final_posts))
    print("Successful sources:", success); print("Failed sources:", failed)
    print("Translations added:", added); print("Posts translated this run:", translated)
    print("Translation rate limited:", limited)


if __name__ == "__main__":
    main()
