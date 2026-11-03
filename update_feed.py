import hashlib
import json
import os
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
IMAGE_DIR = Path("media")
RAW_MEDIA_BASE = "https://raw.githubusercontent.com/reikableu/hou-minghao-updates/main/media"
MAX_IMAGE_DOWNLOADS_PER_RUN = 40
SINGAPORE = ZoneInfo("Asia/Singapore")
UTC = ZoneInfo("UTC")
MAX_POSTS = 200
MAX_TRANSLATED_POSTS_PER_RUN = 10  # Successful posts; Weibo is prioritized.
TRANSLATION_DELAY_SECONDS = 5
REQUEST_DELAY_SECONDS = 2
WEIBO_POST_LIMIT = 15  # Maximum matching Weibo posts across all accounts per Singapore calendar day.

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Your exact 2 XHS profiles with current tokens
XHS_PROFILES = [
    {
        "name": "XHS Profile 1", 
        "source": "XHS Profile 1",
        "profile_id": "680f416000000000080147b4",
        "url": "https://www.rednote.com/user/profile/680f416000000000080147b4?xsec_token=ABYtI7zH48KeUB5ohiJLScbnzYrm4p2gCMujsuApcYBi4%3D&xsec_source=pc_search"
    },
    {
        "name": "XHS Profile 2", 
        "source": "XHS Profile 2",
        "profile_id": "5bc495dfec27fc0001bd4010",
        "url": "https://www.rednote.com/user/profile/5bc495dfec27fc0001bd4010?xsec_token=ABVa0xZWa2vz4oYG34o70kY-jqrCRmlOmHAMqbDzX5Igk%3D&xsec_source=pc_search"
    },
]

XHS_POST_LIMIT_PER_PROFILE = 12
XHS_MAX_PROFILE_CANDIDATES = 40
XHS_PAGE_TIMEOUT_MS = 60000

# Updated Weibo Account IDs matching the high-efficiency approach in your gallery script
WEIBO_ACCOUNT_IDS = [
    "5763552342", "1831550987", "2356308454", "8450716290", "8004004308",
    "7954477778", "7431934486", "6941933193", "5919565877", "9072975151", 
    "6609562381", "5894691419", "1882778030", "7728002071", "7854683575", "2682624851",
]

SOURCES = [
    {
        "name": f"Weibo account {account_id}",
        "source": f"Weibo @{account_id}",
        "platform": "Weibo",
        "uid": account_id,
        "official": account_id in {"1831550987", "5763552342"},
        "type": "sina_api_account",
    }
    for account_id in WEIBO_ACCOUNT_IDS
] + [
    {"name": "Hou Minghao X List", "source": "Hou Minghao X Updates", "platform": "X",
     "url": "https://rss.app/feeds/k375DGhHRMrZ3Ju2.xml", "official": False, "type": "rss_feed"},
] + [{**profile, "platform": "XHS", "official": True, "type": "xhs_profile"}
     for profile in XHS_PROFILES]


def singapore_now():
    return datetime.now(SINGAPORE)


def clean_text(value):
    if not value:
        return ""
    value = unescape(str(value))
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def normalize_image_url(value, base_url=""):
    if not value:
        return ""
    raw = unescape(str(value)).strip().replace("\\/", "/")
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "html.parser")
        anchor = soup.find("a", href=True)
        image = soup.find("img")
        if anchor:
            raw = anchor.get("href", "")
        elif image:
            raw = image.get("src") or image.get("data-src") or image.get("data-original") or ""
        else:
            raw = soup.get_text(" ", strip=True)
    match = re.search(r"https?://[^\s\"'<>]+", raw)
    if match:
        raw = match.group(0)
    raw = raw.strip().rstrip(").,;]")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/") and base_url:
        raw = urllib.parse.urljoin(base_url, raw)
    return raw if raw.startswith(("http://", "https://")) else ""


def build_sina_image_variants(original_url):
    """Build alternate size and host paths for Sina images to ensure maximum download reliability."""
    original_url = normalize_image_url(original_url)
    if not original_url:
        return []
    variants, seen = [], set()

    def add_variant(val):
        val = normalize_image_url(val)
        if val and val not in seen:
            seen.add(val)
            variants.append(val)

    add_variant(original_url)
    for size_name in ["large", "original", "mw2000", "orj1080", "bmiddle"]:
        size_pattern = re.compile(r"/(?:mw2000|large|original|orj1080|orj360|bmiddle)/", re.IGNORECASE)
        if size_pattern.search(original_url):
            add_variant(size_pattern.sub(f"/{size_name}/", original_url, count=1))

    parsed = urllib.parse.urlsplit(original_url)
    if re.fullmatch(r"wx[1-4]\.sinaimg\.cn", parsed.netloc.lower()):
        for num in range(1, 5):
            alt_host = f"wx{num}.sinaimg.cn"
            add_variant(urllib.parse.urlunsplit((parsed.scheme or "https", alt_host, parsed.path, parsed.query, parsed.fragment)))
    return variants


def extract_general_images(html_content, base_url=""):
    if not html_content:
        return []
    raw = unescape(str(html_content)).replace("\\/", "/")
    soup = BeautifulSoup(raw, "html.parser")
    candidates = []
    for image in soup.find_all("img"):
        candidates.extend(image.get(name) for name in (
            "data-original", "data-lazy-src", "data-src", "data-url", "src"
        ))
        if image.get("srcset"):
            candidates.extend(part.strip().split(" ")[0] for part in image["srcset"].split(",") if part.strip())
    candidates.extend(re.findall(
        r"(?:https?:)?//[^\s\"'<>]+?\.(?:jpe?g|png|gif|webp)(?:\?[^\s\"'<>]*)?",
        raw, flags=re.IGNORECASE
    ))
    found = []
    for candidate in candidates:
        url = normalize_image_url(candidate, base_url)
        if url and url not in found:
            found.append(url)
    return found[:9]


def extract_feed_images(item, raw_description, base_url=""):
    images = extract_general_images(raw_description, base_url)
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = normalize_image_url(enclosure.get("url"), base_url)
        media_type = (enclosure.get("type") or "").lower()
        if url and (media_type.startswith("image/") or re.search(r"\.(?:jpe?g|png|gif|webp)(?:$|\?)", url, re.I)) and url not in images:
            images.append(url)
    for tag in ("{http://search.yahoo.com/mrss/}content", "{http://search.yahoo.com/mrss/}thumbnail"):
        for media in item.findall(".//" + tag):
            url = normalize_image_url(media.get("url"), base_url)
            if url and url not in images:
                images.append(url)
    return images[:9]


def image_extension(url, content_type=""):
    content_type = (content_type or "").lower()
    for marker, ext in (("png", ".png"), ("gif", ".gif"), ("webp", ".webp"), ("jpeg", ".jpg"), ("jpg", ".jpg")):
        if marker in content_type:
            return ext
    match = re.search(r"\.(jpe?g|png|gif|webp)$", urllib.parse.urlsplit(url).path.lower())
    return "." + match.group(1).replace("jpeg", "jpg") if match else ".jpg"


def cache_remote_image(image_url, post_url=""):
    image_url = normalize_image_url(image_url)
    if not image_url:
        return ""
    if image_url.startswith(RAW_MEDIA_BASE + "/"):
        return image_url
    IMAGE_DIR.mkdir(exist_ok=True)
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:24]
    for existing in IMAGE_DIR.glob(digest + ".*"):
        return f"{RAW_MEDIA_BASE}/{existing.name}"
    
    headers = dict(HEADERS)
    headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if post_url:
        headers["Referer"] = post_url

    # Try variants if initial URL fails
    variants = build_sina_image_variants(image_url) if "sinaimg.cn" in image_url else [image_url]
    response = None
    for variant_url in variants:
        try:
            resp = requests.get(variant_url, headers=headers, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                response = resp
                break
        except Exception:
            continue
            
    if not response or response.status_code != 200:
        raise ValueError(f"Failed to fetch image from variants for {image_url}")

    content_type = (response.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"Not an image response: {content_type or 'unknown'}")
    if not response.content or len(response.content) > 15 * 1024 * 1024:
        raise ValueError("Image is empty or larger than 15 MB")
        
    filename = digest + image_extension(image_url, content_type)
    (IMAGE_DIR / filename).write_bytes(response.content)
    return f"{RAW_MEDIA_BASE}/{filename}"


def localize_post_images(posts):
    downloads = cached = failed = 0
    for post in posts:
        localized = []
        for raw_image in post.get("images", [])[:9]:
            image_url = normalize_image_url(raw_image)
            if not image_url:
                continue
            if image_url.startswith(RAW_MEDIA_BASE + "/"):
                final_url = image_url
            elif downloads >= MAX_IMAGE_DOWNLOADS_PER_RUN:
                final_url = image_url
            else:
                downloads += 1
                try:
                    final_url = cache_remote_image(image_url, post.get("url", ""))
                    cached += 1
                except Exception as error:
                    failed += 1
                    final_url = image_url
                    print("Image cache failed:", type(error).__name__, str(error)[:160])
            if final_url and final_url not in localized:
                localized.append(final_url)
        post["images"] = localized[:9]
    print(f"Images cached: {cached}; image cache failures: {failed}")
    return cached, failed


def clean_weibo_content(text):
    if not text:
        return ""
    text = re.sub(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}[日]?\s*\d{1,2}:\d{2}\s*", "", text)
    text = re.sub(r"https?://t\.cn/[a-z0-9]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\d\s万w\.,]+$", "", text)
    return clean_text(text)


def shorten(value, maximum=220):
    value = clean_text(value)
    return value if len(value) <= maximum else value[:maximum - 1].rstrip() + "…"


def contains_chinese(text):
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def make_id(platform, source, title, url):
    value = "|".join([platform or "", source or "", title or "", url or ""])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def canonical_post_url(url):
    url = clean_text(url)
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    host = (parts.netloc or "").lower()
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


def normalize_weibo_text(text):
    text = clean_weibo_content(text).lower()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def duplicate_key(post):
    url = canonical_post_url(post.get("url", ""))
    if url and not is_profile_url(url):
        return f"url:{url}"
    if post.get("platform") == "Weibo":
        combined = f"{post.get('titleOriginal') or post.get('title', '')} {post.get('excerptOriginal') or post.get('excerpt', '')}"
        norm_text = normalize_weibo_text(combined)
        if norm_text:
            return "weibo-text:" + hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
    return "id:" + str(post.get("id") or make_id(post.get("platform", ""), post.get("source", ""), post.get("title", ""), url))


def deduplicate_posts(posts):
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
        combined_images = []
        for raw_image in old.get("images", []) + post.get("images", []):
            image_url = normalize_image_url(raw_image)
            if image_url and image_url not in combined_images:
                combined_images.append(image_url)
        preferred["images"] = combined_images[:9]
        result[key] = preferred
    return list(result.values())


def determine_category(text, platform):
    lowered = (text or "").lower()
    if platform == "X": return "X Update"
    if platform == "XHS": return "XHS Update"
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


def collect_sina_api_account(source_config):
    """Collect posts and image galleries via Weibo's m.weibo.cn container API (same as your gallery script)."""
    uid = source_config["uid"]
    container_id = f"107603{uid}"
    target_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}"
    
    response = requests.get(target_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    
    if payload.get("ok") not in (1, "1", True):
        return []
        
    cards = ((payload.get("data") or {}).get("cards")) or []
    collected = []
    
    display_name = f"Weibo @{uid}"
    for card in cards:
        if not isinstance(card, dict):
            continue
        mblog = card.get("mblog")
        if not isinstance(mblog, dict):
            continue
            
        user = mblog.get("user") or {}
        if user.get("screen_name"):
            display_name = f"Weibo @{user.get('screen_name')}"
            
        post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
        if not post_id:
            continue
            
        raw_text = clean_text(mblog.get("raw_text") or mblog.get("text") or "")
        if not raw_text:
            continue
            
        # Filter for relevant posts if needed, or ingest directly
        pics = mblog.get("pics") or []
        image_urls = []
        for pic in pics:
            if not isinstance(pic, dict):
                continue
            large = pic.get("large") or {}
            img_url = large.get("url") or pic.get("url") or ""
            img_url = normalize_image_url(img_url)
            if img_url and img_url not in image_urls:
                image_urls.append(img_url)
                
        cleaned_content = clean_weibo_content(raw_text)
        title = shorten(re.sub(r"^\s*(侯明昊工作室|侯明昊)\s*", "", cleaned_content), 120)
        post_url = f"https://m.weibo.cn/status/{post_id}"
        
        source_config["source"] = display_name
        source_config["name"] = display_name

        collected.append({
            "id": make_id("Weibo", display_name, title, post_url),
            "platform": "Weibo",
            "source": display_name,
            "title": title or "New Weibo update",
            "excerpt": shorten(cleaned_content, 320),
            "images": image_urls[:9],
            "category": determine_category(cleaned_content, "Weibo"),
            "date": parse_possible_date(raw_text),
            "url": post_url,
            "official": source_config["official"],
            "verified": True,
            "collectionMethod": "Weibo API",
        })
    return collected


def collect_rss_feed(source_config):
    response = requests.get(source_config["url"], headers=HEADERS, timeout=30)
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
            raw_description = find_atom_text(item, "content") or find_atom_text(item, "summary") or ""
            desc = clean_text(raw_description)
            pub = clean_text(find_atom_text(item, "published") or find_atom_text(item, "updated"))
            author_node = item.find("{http://www.w3.org/2005/Atom}author")
            author = clean_text(author_node.findtext("{http://www.w3.org/2005/Atom}name") or author_node.text) if author_node is not None else ""
            link = ""
            for link_element in item.findall("{http://www.w3.org/2005/Atom}link"):
                if link_element.get("href"):
                    link = clean_text(link_element.get("href")); break
        else:
            title = clean_text(item.findtext("title"))
            raw_description = item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or item.findtext("content") or ""
            desc = clean_text(raw_description)
            pub = clean_text(item.findtext("pubDate") or item.findtext("published") or item.findtext("updated"))
            author = clean_text(item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator"))
            link = clean_text(item.findtext("link"))
        if not title and desc:
            title = shorten(desc, 150)
        if not title or not link:
            continue
        images = extract_feed_images(item, raw_description, source_config["url"])
        match = re.search(r"@([A-Za-z0-9_]+)", f"{author} {title} {desc}")
        author_name = "@" + match.group(1) if match else author or source_config["source"]
        collected.append({
            "id": make_id("X", source_config["source"], title, link), "platform": "X",
            "source": source_config["source"], "author": author_name, "title": shorten(title, 180),
            "excerpt": shorten(desc, 350), "images": images, "category": "X Update",
            "date": parse_feed_date(pub), "url": link, "official": False, "verified": True,
            "collectionMethod": "Automatic"
        })
    return collected


def find_atom_text(item, field):
    atom = "{http://www.w3.org/2005/Atom}"
    return item.findtext(atom + field) or item.findtext(field)


def load_xhs_cookies():
    raw = os.environ.get("XHS_COOKIES_JSON", "").strip()
    if not raw:
        return []
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"XHS_COOKIES_JSON is invalid JSON: {error}") from error
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("XHS_COOKIES_JSON must be a non-empty JSON array")
    allowed = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}
    result = []
    for index, item in enumerate(cookies, 1):
        if not isinstance(item, dict):
            raise ValueError(f"XHS cookie {index} must be an object")
        cookie = {key: value for key, value in item.items() if key in allowed}
        if not cookie.get("name") or cookie.get("value") is None:
            raise ValueError(f"XHS cookie {index} requires name and value")
        if not cookie.get("url") and not (cookie.get("domain") and cookie.get("path")):
            cookie["domain"], cookie["path"] = ".rednote.com", "/"
        same_site = str(cookie.get("sameSite", "Lax")).capitalize()
        cookie["sameSite"] = same_site if same_site in {"Strict", "Lax", "None"} else "Lax"
        result.append(cookie)
    return result


def canonical_xhs_post_url(value):
    raw = clean_text(value)
    if not raw:
        return ""
    url = urllib.parse.urljoin("https://www.rednote.com", raw)
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.lower() not in {"www.rednote.com", "rednote.com", "www.xiaohongshu.com", "xiaohongshu.com"}:
        return ""
    for pattern in (r"/explore/([A-Za-z0-9_-]+)", r"/discovery/item/([A-Za-z0-9_-]+)", r"/note/([A-Za-z0-9_-]+)", r"/item/([A-Za-z0-9_-]+)"):
        match = re.search(pattern, parts.path, re.I)
        if match:
            return f"https://www.rednote.com/discovery/item/{match.group(1)}"
    return ""


def parse_xhs_display_date(value):
    value = clean_text(value)
    if not value:
        return ""
    now = singapore_now()
    match = re.search(r"(?:Yesterday|昨天)[,,\s]*(\d{1,2}):(\d{2})", value, re.I)
    if match:
        yesterday = now.date().fromordinal(now.date().toordinal() - 1)
        return datetime(yesterday.year, yesterday.month, yesterday.day, int(match.group(1)), int(match.group(2)), tzinfo=SINGAPORE).isoformat()
    match = re.search(r"(?:Today|今天)[,,\s]*(\d{1,2}):(\d{2})", value, re.I)
    if match:
        return datetime(now.year, now.month, now.day, int(match.group(1)), int(match.group(2)), tzinfo=SINGAPORE).isoformat()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?", value)
    if match:
        year, month, day, hour, minute = match.groups()
        return datetime(int(year), int(month), int(day), int(hour or 12), int(minute or 0), tzinfo=SINGAPORE).isoformat()
    match = re.search(r"(\d{1,2})[-/.月](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?", value)
    if match:
        month, day, hour, minute = match.groups()
        return datetime(now.year, int(month), int(day), int(hour or 12), int(minute or 0), tzinfo=SINGAPORE).isoformat()
    return ""


def collect_xhs_profile(source_config):
    cookies = load_xhs_cookies()
    if not cookies:
        print(f"{source_config['name']}: XHS_COOKIES_JSON missing; skipping XHS profile scrape safely.")
        return []
    
    from playwright.sync_api import sync_playwright
    posts = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 1100}, user_agent=HEADERS["User-Agent"])
            context.add_cookies(cookies)
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            
            response = page.goto(source_config["url"], wait_until="domcontentloaded", timeout=XHS_PAGE_TIMEOUT_MS)
            page.wait_for_timeout(6000)
            status = response.status if response else 0
            if status >= 400:
                browser.close()
                return []

            profile_name = source_config["source"]
            for selector in ("h1", "[class*='user-name']", "[class*='username']", "[class*='nickname']"):
                try:
                    value = clean_text(page.locator(selector).first.inner_text(timeout=1000))
                    if value and len(value) <= 80:
                        profile_name = value
                        break
                except Exception:
                    pass
            source_config["source"] = source_config["name"] = profile_name

            candidates, seen = [], set()
            stagnant, previous = 0, 0
            for _ in range(8):
                anchors = page.locator("a[href*='/discovery/item/'], a[href*='/explore/']")
                for index in range(anchors.count()):
                    try:
                        link = anchors.nth(index)
                        raw_href = link.get_attribute("href") or ""
                        original_url = urllib.parse.urljoin("https://www.rednote.com", raw_href)
                        canonical_url = canonical_xhs_post_url(original_url)
                        if not canonical_url or canonical_url in seen:
                            continue
                        seen.add(canonical_url)
                        card = link.locator("xpath=ancestor::*[self::section or self::article or self::li or self::div][1]")
                        card_text = clean_text(card.inner_text(timeout=1000)) or clean_text(link.inner_text(timeout=500))
                        images = []
                        for img_idx in range(min(card.locator("img").count(), 9)):
                            img = card.locator("img").nth(img_idx)
                            for attr in ("src", "data-src", "data-original"):
                                img_url = normalize_image_url(img.get_attribute(attr), source_config["url"])
                                if img_url and img_url not in images:
                                    images.append(img_url)
                                    break
                        candidates.append({"canonical_url": canonical_url, "original_url": original_url, "card_text": card_text, "images": images})
                    except Exception:
                        pass
                if len(candidates) >= XHS_MAX_PROFILE_CANDIDATES:
                    break
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(1500)
                stagnant = stagnant + 1 if len(candidates) == previous else 0
                previous = len(candidates)
                if stagnant >= 3:
                    break

            for candidate in candidates[:XHS_POST_LIMIT_PER_PROFILE]:
                note_page = context.new_page()
                try:
                    note_response = note_page.goto(candidate["original_url"], wait_until="domcontentloaded", timeout=XHS_PAGE_TIMEOUT_MS)
                    note_page.wait_for_timeout(2000)
                    if note_response and note_response.status >= 400:
                        continue
                    body = clean_text(note_page.locator("body").inner_text(timeout=4000))
                    title = candidate["card_text"]
                    for selector in ("h1", "[class*='note-title']", "[class*='title']"):
                        try:
                            value = clean_text(note_page.locator(selector).first.inner_text(timeout=600))
                            if value and len(value) <= 200:
                                title = value
                                break
                        except Exception:
                            pass
                    
                    post_date = ""
                    for selector in ("time", "[class*='date']", "[class*='publish']", "[class*='time']"):
                        try:
                            node = note_page.locator(selector).first
                            raw_date = clean_text(node.get_attribute("datetime") or node.inner_text(timeout=500))
                            post_date = parse_xhs_display_date(raw_date)
                            if post_date:
                                break
                        except Exception:
                            pass

                    images = list(candidate["images"])
                    for img_idx in range(min(note_page.locator("img").count(), 20)):
                        img = note_page.locator("img").nth(img_idx)
                        for attr in ("src", "data-src", "data-original"):
                            img_url = normalize_image_url(img.get_attribute(attr), candidate["original_url"])
                            if img_url and not any(marker in img_url.lower() for marker in ("avatar", "profile", "portrait")) and img_url not in images:
                                images.append(img_url)
                                break

                    clean_title = shorten(title or candidate["card_text"], 180) or "New XHS update"
                    posts.append({
                        "id": make_id("XHS", profile_name, clean_title, candidate["canonical_url"]), 
                        "platform": "XHS", 
                        "source": profile_name, 
                        "title": clean_title, 
                        "excerpt": shorten(body or candidate["card_text"], 350), 
                        "images": images[:9], 
                        "category": "XHS Update", 
                        "date": post_date, 
                        "canonicalUrl": candidate["canonical_url"], 
                        "url": candidate["original_url"], 
                        "official": True, 
                        "verified": True, 
                        "collectionMethod": "GitHub Playwright Scraper"
                    })
                except Exception:
                    pass
                finally:
                    note_page.close()
            browser.close()
    except Exception as error:
        print("XHS profile collection execution failed:", error)
    return posts


def collect_source(source_config):
    kind = source_config["type"]
    if kind == "sina_api_account": return collect_sina_api_account(source_config)
    if kind == "rss_feed": return collect_rss_feed(source_config)
    if kind == "xhs_profile": return collect_xhs_profile(source_config)
    return []


def normalize_existing_post(post):
    post.setdefault("id", make_id(post.get("platform", ""), post.get("source", ""), post.get("title", ""), post.get("url", "")))
    post.setdefault("excerpt", ""); post.setdefault("category", "Other"); post.setdefault("official", False)
    post.setdefault("verified", False); post.setdefault("collectionMethod", "Manual")
    raw_images = post.get("images", [])
    if not isinstance(raw_images, list):
        raw_images = [raw_images] if raw_images else []
    post["images"] = [url for url in (normalize_image_url(value) for value in raw_images) if url][:9]
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

    try:
        translated = clean_text(GoogleTranslator(source="auto", target="en").translate(text))
        if translated:
            return {"translation": translated, "rate_limited": False, "provider": "Google"}
    except Exception:
        pass

    try:
        translated = translate_with_mymemory(text)
        if translated:
            return {"translation": translated, "rate_limited": False, "provider": "MyMemory"}
    except Exception:
        pass

    return {"translation": "", "rate_limited": False, "provider": "unavailable"}


def add_translations(posts):
    translations_added = posts_translated = posts_attempted = 0
    rate_limited = False
    translation_providers = {"Google": 0, "MyMemory": 0}
    for post in posts:
        if post.get("platform") != "Weibo":
            post.pop("titleEnglish", None)
            post.pop("excerptEnglish", None)

    weibo_posts = [post for post in posts if post.get("platform") == "Weibo"]
    
    def translation_priority(post):
        text = clean_text(f"{post.get('titleOriginal') or post.get('title', '')} {post.get('excerptOriginal') or post.get('excerpt', '')}")
        return (0 if contains_chinese(text) else 1, -post_timestamp(post))

    for index, post in enumerate(sorted(weibo_posts, key=translation_priority), 1):
        original_title = clean_text(post.get("titleOriginal") or post.get("title", ""))
        original_excerpt = clean_text(post.get("excerptOriginal") or post.get("excerpt", ""))
        post["titleOriginal"], post["excerptOriginal"] = original_title, original_excerpt
        title_en = clean_text(post.get("titleEnglish", ""))
        excerpt_en = clean_text(post.get("excerptEnglish", ""))
        title_need = contains_chinese(original_title) and not title_en
        excerpt_need = contains_chinese(original_excerpt) and not excerpt_en

        if not contains_chinese(original_title) and not title_en: post["titleEnglish"] = original_title
        if not contains_chinese(original_excerpt) and not excerpt_en: post["excerptEnglish"] = original_excerpt

        if not title_need and not excerpt_need: continue
        if posts_attempted >= MAX_TRANSLATED_POSTS_PER_RUN: break

        posts_attempted += 1
        had_translation = False
        for field, original, needed in [("titleEnglish", original_title, title_need), ("excerptEnglish", original_excerpt, excerpt_need)]:
            if not needed: continue
            result = translate_to_english(original)
            if result["translation"]:
                post[field] = result["translation"]
                translations_added += 1
                had_translation = True
                provider = result.get("provider")
                if provider in translation_providers: translation_providers[provider] += 1
                time.sleep(TRANSLATION_DELAY_SECONDS)
        if had_translation: posts_translated += 1

    return translations_added, posts_translated, rate_limited, translation_providers


def main():
    data = load_existing_data()
    existing = deduplicate_posts([p for p in data.get("posts", []) if isinstance(p, dict)])

    collected, statuses = [], []
    for config in SOURCES:
        checked = singapore_now()
        print(f"Checking {config['name']}...")
        try:
            posts = collect_source(config)
            collected.extend(posts)
            statuses.append({"source": config["name"], "status": "Success", "itemsFound": len(posts), "checkedAt": checked.isoformat()})
            print(f"{config['name']}: {len(posts)} items found")
        except Exception as error:
            print(f"{config['name']} failed: {error}")
            statuses.append({"source": config["name"], "status": "Failed", "itemsFound": 0, "checkedAt": checked.isoformat(), "message": str(error)[:200]})
        time.sleep(REQUEST_DELAY_SECONDS)

    today = singapore_now().date()
    def is_weibo_today(post):
        if post.get("platform") != "Weibo": return False
        try:
            stamp = datetime.fromisoformat(post.get("date", "").replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=UTC)
            return stamp.astimezone(SINGAPORE).date() == today
        except Exception: return False

    existing_today_keys = {duplicate_key(p) for p in existing if is_weibo_today(p)}
    slots = max(0, WEIBO_POST_LIMIT - len(existing_today_keys))
    new_weibo = [p for p in collected if p.get("platform"] == "Weibo")
    other_new = [p for p in collected if p.get("platform"] != "Weibo")
    accepted_weibo = [p for p in sorted(deduplicate_posts(new_weibo), key=post_timestamp, reverse=True) if duplicate_key(p) not in existing_today_keys][:slots]
    collected = accepted_weibo + other_new

    merged = deduplicate_posts(existing + collected)
    final_posts = sorted(merged, key=post_timestamp, reverse=True)[:MAX_POSTS]
    images_cached, image_cache_failures = localize_post_images(final_posts)
    added, translated, limited, translation_providers = add_translations(final_posts)
    success = sum(s["status"] == "Success" for s in statuses)
    failed = len(statuses) - success

    output = {
        "lastUpdated": singapore_now().isoformat(timespec="seconds"),
        "status": f"Completed: {success} sources successful, {failed} failed",
        "translationStatus": {"translationsAdded": added, "postsTranslatedThisRun": translated, "rateLimited": limited, "providersUsed": translation_providers},
        "sourceStatus": statuses, 
        "posts": final_posts
    }
    with POSTS_FILE.open("w", encoding="utf-8") as f: json.dump(output, f, ensure_ascii=False, indent=2)
    print("\nCollection completed successfully.")


if __name__ == "__main__":
    main()
