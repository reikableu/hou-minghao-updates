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
MAX_POSTS = 250
MAX_TRANSLATED_POSTS_PER_RUN = 10  # Successful posts; Weibo is prioritized.
TRANSLATION_DELAY_SECONDS = 5
REQUEST_DELAY_SECONDS = 2
# Shared Playwright page for Weibo (set in main). No login cookie needed.
WEIBO_PAGE = None
# Shared Playwright browser (set in main). Used for the X list, which needs login cookies.
BROWSER = None
# Your X list URL, e.g. https://x.com/i/lists/1234567890123456789 (can also be set via X_LIST_URL env var)
X_LIST_URL = os.environ.get("X_LIST_URL", "https://x.com/i/lists/2115680530159308929")
MOBILE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
WEIBO_POST_LIMIT = 15  # Maximum matching Weibo posts across all accounts per Singapore calendar day.

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Updated Weibo Account IDs
WEIBO_ACCOUNT_IDS = [
    # Official actor & studio accounts
    "5763552342",  # 侯明昊工作室 (Hou Minghao Studio)
    "1831550987",  # 侯明昊Neo (Hou Minghao personal account)
    # New endorsements (新增商务)
    "6453123896",  # 旭东食品 (Xudong Snacks)
    "1692125795",  # 乐事官方微博 (Lay's)
    # Brand endorsements (品牌代言)
    "1734997034",  # 海信电视 / Hisense海信
    "7513576766",  # olayks立时
    "1766627034",  # 美肤宝MEIFUBAO
    "7335269786",  # DearIrean独特艾琳
    "7868019741",  # 好流Official
    "1749127163",  # HM中国
    "6473618360",  # 珀芙研 (Biorrier)
    "7579169954",  # 棉致MIANZHI
    "7486821389",  # 千层雪Viennetta
    # Promotions & ambassador roles
    "5938174786",  # 西藏自治区文化和旅游厅
    "1767117565",  # MAC魅可
    "7639102431",  # fino芬浓
    "3219487770",  # 珍宝珠ChupaChups
    "7541604928",  # CASETiFY
    "7857186930",  # FADEAWAY_L
    "7867201948",  # PLUG-INPET
]

# Only keep posts from non-official Weibo accounts if they mention one of these.
# Matching is case-insensitive and also checks reposted text and hashtags.
HOU_KEYWORDS = ["侯明昊", "Hou Minghao", "HouMinghao", "Hou Ming Hao", "侯明昊工作室"]
FILTER_OFFICIAL_ACCOUNTS = False  # True = also filter the official accounts


def mentions_hou(*texts):
    blob = " ".join(t for t in texts if t).lower()
    return any(k.lower() in blob for k in HOU_KEYWORDS)


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
     "url": X_LIST_URL, "official": False, "type": "x_list_playwright"},
]


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
    uid = source_config["uid"]
    container_id = f"107603{uid}"
    target_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}"
    
    if WEIBO_PAGE is None:
        raise RuntimeError("Browser session not available")

    response = WEIBO_PAGE.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    if response is None:
        raise RuntimeError("No response from Weibo")
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status} (Weibo blocked this request)")
    body = (response.text() or "").strip()
    try:
        payload = json.loads(body)
    except ValueError:
        # Browsers sometimes wrap JSON in <pre>; try stripping tags once
        try:
            payload = json.loads(re.sub(r"<[^>]+>", "", body))
        except ValueError:
            raise RuntimeError(f"Non-JSON response (login/captcha page?): {body[:80]!r}")

    if payload.get("ok") not in (1, "1", True):
        msg = str(payload.get("msg") or "")
        if "还没有内容" in msg:
            return []
        raise RuntimeError(f"Weibo API ok=0: {msg or 'no message'}"[:200])
        
    cards = ((payload.get("data") or {}).get("cards")) or []
    collected = []
    skipped = 0
    
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
                
        # Filter: keep only posts that mention Hou Minghao (checks repost + hashtags too)
        if FILTER_OFFICIAL_ACCOUNTS or not source_config.get("official"):
            retweet = mblog.get("retweeted_status") or {}
            page_info = mblog.get("page_info") or {}
            topics = " ".join(
                str(t.get("topic_title") or "") for t in (mblog.get("topic_struct") or []) if isinstance(t, dict)
            )
            if not mentions_hou(
                raw_text,
                clean_text(mblog.get("text") or ""),
                clean_text(retweet.get("raw_text") or retweet.get("text") or ""),
                str(page_info.get("title") or ""),
                topics,
            ):
                skipped += 1
                continue

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
    if skipped:
        print(f"  {display_name}: skipped {skipped} posts not mentioning Hou Minghao, kept {len(collected)}")
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


def collect_x_list(source_config):
    """Scrape an X list with Playwright using login cookies from env vars."""
    auth_token = os.environ.get("X_AUTH_TOKEN", "").strip()
    ct0 = os.environ.get("X_CT0", "").strip()
    if not auth_token or not ct0:
        raise RuntimeError("X_AUTH_TOKEN / X_CT0 secrets are missing")
    if BROWSER is None:
        raise RuntimeError("Playwright browser unavailable")

    context = BROWSER.new_context(
        user_agent=MOBILE_UA, locale="en-US", viewport={"width": 1280, "height": 1800},
    )
    try:
        context.add_cookies([
            {"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/", "secure": True},
        ])
        page = context.new_page()
        page.goto(source_config["url"], wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("article", timeout=30000)
        except Exception:
            if "login" in page.url or "flow" in page.url:
                raise RuntimeError("X login wall (cookies expired or IP blocked)")
            raise RuntimeError("No posts found on X list page (markup changed or list empty)")
        # X removes off-screen posts from the page as you scroll, so read the
        # visible posts after every scroll step instead of once at the end.
        raw_items, seen = [], set()

        def harvest():
            for article in page.query_selector_all("article"):
                try:
                    time_el = article.query_selector("a[href*='/status/'] time")
                    if not time_el:
                        continue
                    href = time_el.evaluate("e => e.parentElement.getAttribute('href')") or ""
                    match = re.match(r"^/([A-Za-z0-9_]+)/status/(\d+)", href)
                    if not match or match.group(2) in seen:
                        continue
                    text_el = article.query_selector("[data-testid='tweetText']")
                    text = text_el.inner_text() if text_el else ""
                    images = []
                    for img in article.query_selector_all("[data-testid='tweetPhoto'] img"):
                        src = img.get_attribute("src") or ""
                        if "pbs.twimg.com/media" in src:
                            images.append(re.sub(r"name=\w+", "name=large", src))
                    seen.add(match.group(2))
                    raw_items.append((match.group(1), match.group(2), text,
                                      time_el.get_attribute("datetime") or "", images))
                except Exception:
                    continue

        harvest()
        for _ in range(8):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1500)
            harvest()
    finally:
        try: context.close()
        except Exception: pass

    collected = []
    for handle, status_id, text, stamp, images in raw_items[:40]:
        text = clean_text(text)
        link = f"https://x.com/{handle}/status/{status_id}"
        title = shorten(text, 150) if text else f"Post by @{handle}"
        collected.append({
            "id": make_id("X", source_config["source"], title, link), "platform": "X",
            "source": source_config["source"], "author": "@" + handle, "title": shorten(title, 180),
            "excerpt": shorten(text, 350), "images": images[:9], "category": "X Update",
            "date": parse_feed_date(stamp), "url": link, "official": False, "verified": True,
            "collectionMethod": "Automatic",
        })
    return collected


def find_atom_text(item, field):
    atom = "{http://www.w3.org/2005/Atom}"
    return item.findtext(atom + field) or item.findtext(field)


def collect_source(source_config):
    kind = source_config["type"]
    if kind == "sina_api_account": return collect_sina_api_account(source_config)
    if kind == "rss_feed": return collect_rss_feed(source_config)
    if kind == "x_list_playwright": return collect_x_list(source_config)
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

    global WEIBO_PAGE, BROWSER
    collected, statuses = [], []
    pw = browser = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=MOBILE_UA, locale="en-US", viewport={"width": 1365, "height": 768},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
        )
        BROWSER = browser
        WEIBO_PAGE = context.new_page()
        try:
            r = WEIBO_PAGE.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=20000)
            print(f"Weibo warmup HTTP status: {r.status if r else 'none'}")
            time.sleep(2)
        except Exception as error:
            print(f"Weibo warmup notice: {type(error).__name__}: {error}")
    except Exception as error:
        print(f"Playwright unavailable, Weibo sources will fail: {error}")

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

    try:
        if browser: browser.close()
        if pw: pw.stop()
    except Exception:
        pass
    WEIBO_PAGE = None
    BROWSER = None

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
    
    new_weibo = [p for p in collected if p.get("platform") == "Weibo"]
    other_new = [p for p in collected if p.get("platform") != "Weibo"]
    
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
