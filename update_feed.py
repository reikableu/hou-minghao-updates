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
XHS_PROFILES = [
    {"name": "XHS Official 680f4160", "source": "XHS Official 680f4160",
     "profile_id": "680f416000000000080147b4",
     "url": "https://www.rednote.com/user/profile/680f416000000000080147b4?xsec_token=ABvIk97fz2vBAKgqNwvzALYPVlCS89d0xDhWg6AKhBmBc%3D&xsec_source=pc_search"},
    {"name": "XHS Official 5bc495df", "source": "XHS Official 5bc495df",
     "profile_id": "5bc495dfec27fc0001bd4010",
     "url": "https://www.rednote.com/user/profile/5bc495dfec27fc0001bd4010?xsec_token=ABVa0xZWa2vz4oYG34o70kY-if281iBw4p6RJwJ9_5K2I%3D&xsec_source=pc_search"},
]
XHS_POST_LIMIT_PER_PROFILE = 12
XHS_PAGE_TIMEOUT_MS = 60000

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
    """Return a clean absolute HTTP image URL, including malformed HTML values."""
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


def extract_general_images(html_content, base_url=""):
    """Extract images from RSS/HTML content without applying Weibo-only rules."""
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
    found=[]
    for candidate in candidates:
        url=normalize_image_url(candidate,base_url)
        if url and url not in found:
            found.append(url)
    return found[:9]


def extract_weibo_post_images(element, base_url=""):
    """Keep only confidently identified Weibo post media and reject profile photos."""
    if element is None:
        return []
    accepted=[]
    avatar_markers=(
        "avatar","profile","portrait","head","face","author","user",
        "icon","logo","badge","verified","vip","qrcode","emoji"
    )
    post_media_markers=(
        "large","mw690","mw1024","mw2000","bmiddle","original",
        "orj","wap720","photo","media","gallery","pic"
    )
    for image in element.find_all("img"):
        image_classes=" ".join(image.get("class",[]))
        parent_classes=" ".join(
            " ".join(parent.get("class",[])) for parent in image.parents
            if getattr(parent,"get",None)
        )
        context=f"{image_classes} {parent_classes} {image.get('alt','')}".lower()
        if any(marker in context for marker in avatar_markers):
            continue
        width_text=str(image.get("width","")).strip()
        height_text=str(image.get("height","")).strip()
        width=int(width_text) if width_text.isdigit() else 0
        height=int(height_text) if height_text.isdigit() else 0
        if width and height and width <= 160 and height <= 160:
            continue
        urls=[image.get(name) for name in (
            "data-original","data-lazy-src","data-src","data-url","src"
        )]
        if image.get("srcset"):
            urls.extend(part.strip().split(" ")[0] for part in image["srcset"].split(",") if part.strip())
        for raw_url in urls:
            url=normalize_image_url(raw_url,base_url)
            if not url:
                continue
            lowered=url.lower()
            parsed=urllib.parse.urlsplit(url)
            host=parsed.netloc.lower()
            path=parsed.path.lower()
            if any(marker in lowered for marker in avatar_markers):
                continue
            if "sinaimg" not in host and "weibocdn" not in host:
                continue
            if re.search(r"/(?:30|40|50|60|80|100|120|150|160)x(?:30|40|50|60|80|100|120|150|160)/",path):
                continue
            if not any(marker in path for marker in post_media_markers):
                continue
            if url not in accepted:
                accepted.append(url)
    return accepted[:9]


def extract_feed_images(item, raw_description, base_url=""):
    images=extract_general_images(raw_description,base_url)
    enclosure=item.find("enclosure")
    if enclosure is not None:
        url=normalize_image_url(enclosure.get("url"),base_url)
        media_type=(enclosure.get("type") or "").lower()
        if url and (media_type.startswith("image/") or re.search(r"\.(?:jpe?g|png|gif|webp)(?:$|\?)",url,re.I)) and url not in images:
            images.append(url)
    for tag in ("{http://search.yahoo.com/mrss/}content","{http://search.yahoo.com/mrss/}thumbnail"):
        for media in item.findall(".//"+tag):
            url=normalize_image_url(media.get("url"),base_url)
            if url and url not in images:
                images.append(url)
    return images[:9]


def image_extension(url,content_type=""):
    content_type=(content_type or "").lower()
    for marker,ext in (("png",".png"),("gif",".gif"),("webp",".webp"),("jpeg",".jpg"),("jpg",".jpg")):
        if marker in content_type:
            return ext
    match=re.search(r"\.(jpe?g|png|gif|webp)$",urllib.parse.urlsplit(url).path.lower())
    return "."+match.group(1).replace("jpeg","jpg") if match else ".jpg"


def cache_remote_image(image_url,post_url=""):
    image_url=normalize_image_url(image_url)
    if not image_url:
        return ""
    if image_url.startswith(RAW_MEDIA_BASE+"/"):
        return image_url
    IMAGE_DIR.mkdir(exist_ok=True)
    digest=hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:24]
    for existing in IMAGE_DIR.glob(digest+".*"):
        return f"{RAW_MEDIA_BASE}/{existing.name}"
    headers=dict(HEADERS)
    headers["Accept"]="image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if post_url:
        headers["Referer"]=post_url
    response=requests.get(image_url,headers=headers,timeout=30,allow_redirects=True)
    response.raise_for_status()
    content_type=(response.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"Not an image response: {content_type or 'unknown'}")
    if not response.content or len(response.content)>12*1024*1024:
        raise ValueError("Image is empty or larger than 12 MB")
    filename=digest+image_extension(image_url,content_type)
    (IMAGE_DIR/filename).write_bytes(response.content)
    return f"{RAW_MEDIA_BASE}/{filename}"


def localize_post_images(posts):
    downloads=cached=failed=0
    for post in posts:
        localized=[]
        for raw_image in post.get("images",[])[:9]:
            image_url=normalize_image_url(raw_image)
            if not image_url:
                continue
            if image_url.startswith(RAW_MEDIA_BASE+"/"):
                final_url=image_url
            elif downloads>=MAX_IMAGE_DOWNLOADS_PER_RUN:
                final_url=image_url
            else:
                downloads+=1
                try:
                    final_url=cache_remote_image(image_url,post.get("url",""))
                    cached+=1
                except Exception as error:
                    failed+=1
                    final_url=image_url
                    print("Image cache failed:",type(error).__name__,str(error)[:160])
            if final_url and final_url not in localized:
                localized.append(final_url)
        post["images"]=localized[:9]
    print(f"Images cached: {cached}; image cache failures: {failed}")
    return cached,failed


def clean_weibo_content(text):
    """Strip out timestamps, view counts, short links, and engagement stats from Weibo text."""
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
        combined_images=[]
        for raw_image in old.get("images",[])+post.get("images",[]):
            image_url=normalize_image_url(raw_image)
            if image_url and image_url not in combined_images:
                combined_images.append(image_url)
        preferred["images"]=combined_images[:9]
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
        raw_text = clean_text(element.get_text(" ", strip=True))
        if len(raw_text) < 25 or len(raw_text) > 1800 or raw_text in seen_text: continue
        if "侯明昊" not in raw_text: continue
        seen_text.add(raw_text)
        link_element = element.find("a", href=True)
        post_url = urllib.parse.urljoin(source_config["url"], link_element.get("href")) if link_element else source_config["url"]
        
        image_urls = extract_weibo_post_images(element, source_config["url"])
        cleaned_content = clean_weibo_content(raw_text)
        title = shorten(re.sub(r"^\s*(侯明昊工作室|侯明昊)\s*", "", cleaned_content), 120)

        collected.append({
            "id": make_id(source_config["platform"], source_config["source"], title, post_url),
            "platform": source_config["platform"], 
            "source": source_config["source"],
            "title": title or "New official Hou Minghao update", 
            "excerpt": shorten(cleaned_content, 320),
            "images": image_urls,
            "category": determine_category(cleaned_content, source_config["platform"]), 
            "date": parse_possible_date(raw_text),
            "url": post_url, 
            "official": source_config["official"], 
            "verified": True,
            "collectionMethod": "Automatic",
        })
    return collected


def find_atom_text(item, field):
    atom = "{http://www.w3.org/2005/Atom}"
    return item.findtext(atom + field) or item.findtext(field)


def collect_rss_feed(source_config):
    response=requests.get(source_config["url"],headers=HEADERS,timeout=30)
    response.raise_for_status()
    root=ET.fromstring(response.content)
    items=root.findall(".//item")
    is_atom=False
    if not items:
        items=root.findall(".//{http://www.w3.org/2005/Atom}entry")
        is_atom=True
    collected=[]
    for item in items[:25]:
        if is_atom:
            title=clean_text(find_atom_text(item,"title"))
            raw_description=find_atom_text(item,"content") or find_atom_text(item,"summary") or ""
            desc=clean_text(raw_description)
            pub=clean_text(find_atom_text(item,"published") or find_atom_text(item,"updated"))
            author_node=item.find("{http://www.w3.org/2005/Atom}author")
            author=clean_text(author_node.findtext("{http://www.w3.org/2005/Atom}name") or author_node.text) if author_node is not None else ""
            link=""
            for link_element in item.findall("{http://www.w3.org/2005/Atom}link"):
                if link_element.get("href"):
                    link=clean_text(link_element.get("href"));break
        else:
            title=clean_text(item.findtext("title"))
            raw_description=item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or item.findtext("content") or ""
            desc=clean_text(raw_description)
            pub=clean_text(item.findtext("pubDate") or item.findtext("published") or item.findtext("updated"))
            author=clean_text(item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator"))
            link=clean_text(item.findtext("link"))
        if not title and desc:
            title=shorten(desc,150)
        if not title or not link:
            continue
        images=extract_feed_images(item,raw_description,source_config["url"])
        match=re.search(r"@([A-Za-z0-9_]+)",f"{author} {title} {desc}")
        author_name="@"+match.group(1) if match else author or source_config["source"]
        collected.append({
            "id":make_id("X",source_config["source"],title,link),"platform":"X",
            "source":source_config["source"],"author":author_name,"title":shorten(title,180),
            "excerpt":shorten(desc,350),"images":images,"category":"X Update",
            "date":parse_feed_date(pub),"url":link,"official":False,"verified":True,
            "collectionMethod":"Automatic"
        })
    return collected

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
    allowed = {"name", "value", "url", "domain", "path", "expires",
               "httpOnly", "secure", "sameSite", "partitionKey"}
    result = []
    for index, raw_cookie in enumerate(cookies, 1):
        if not isinstance(raw_cookie, dict):
            raise ValueError(f"XHS cookie {index} must be an object")
        cookie = {k: v for k, v in raw_cookie.items() if k in allowed}
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
    if parts.netloc.lower() not in {"www.rednote.com", "rednote.com",
                                    "www.xiaohongshu.com", "xiaohongshu.com"}:
        return ""
    for pattern in (r"/explore/([A-Za-z0-9_-]+)",
                    r"/discovery/item/([A-Za-z0-9_-]+)",
                    r"/note/([A-Za-z0-9_-]+)", r"/item/([A-Za-z0-9_-]+)"):
        match = re.search(pattern, parts.path, re.I)
        if match:
            return f"https://www.rednote.com/explore/{match.group(1)}"
    query = urllib.parse.parse_qs(parts.query)
    for key in ("note_id", "noteId", "item_id", "itemId"):
        if query.get(key):
            note_id = clean_text(query[key][0])
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", note_id):
                return f"https://www.rednote.com/explore/{note_id}"
    return ""


def parse_xhs_timestamp(value):
    if value in (None, ""):
        return singapore_now().isoformat()
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC).astimezone(SINGAPORE).isoformat()
    except (TypeError, ValueError, OSError):
        return parse_feed_date(str(value))


def xhs_images(value):
    found = []
    keys = {"url", "urlDefault", "urlPre", "url_default", "url_pre", "imageList",
            "images", "cover", "thumbnail", "originUrl", "originalUrl", "infoList"}
    def walk(node, parent=""):
        if len(found) >= 9: return
        if isinstance(node, dict):
            for key, item in node.items():
                if key in keys and isinstance(item, str):
                    url = normalize_image_url(item)
                    if url and not any(x in url.lower() for x in ("avatar", "profile", "portrait")) and url not in found:
                        found.append(url)
                elif isinstance(item, (dict, list)): walk(item, key)
        elif isinstance(node, list):
            for item in node: walk(item, parent)
    walk(value)
    return found[:9]


def extract_xhs_posts_from_json(payload, source_config, profile_name):
    posts, seen = [], set()
    def note_id(obj):
        for key in ("noteId", "note_id", "id"):
            value = obj.get(key)
            if isinstance(value, (str, int)) and re.fullmatch(r"[A-Za-z0-9_-]{12,64}", str(value)):
                return str(value)
        return ""
    def text(obj, keys):
        for key in keys:
            if isinstance(obj.get(key), str) and clean_text(obj[key]): return clean_text(obj[key])
        return ""
    def add(raw):
        obj = raw.get("noteCard") if isinstance(raw.get("noteCard"), dict) else raw
        nid = note_id(obj) or note_id(raw)
        if not nid: return
        url = f"https://www.rednote.com/explore/{nid}"
        if url in seen: return
        title = text(obj, ("displayTitle", "title", "name"))
        excerpt = text(obj, ("desc", "description", "content", "title", "displayTitle"))
        if not title: title = shorten(excerpt, 150) or "New XHS update"
        if not excerpt: excerpt = title
        date_value = next((obj.get(k) for k in ("time", "publishTime", "publish_time", "timestamp", "createTime") if obj.get(k) not in (None, "")), None)
        seen.add(url)
        posts.append({"id": make_id("XHS", profile_name, title, url), "platform": "XHS",
                      "source": profile_name, "title": shorten(title, 180),
                      "excerpt": shorten(excerpt, 350), "images": xhs_images(obj),
                      "category": "XHS Update", "date": parse_xhs_timestamp(date_value),
                      "url": url, "official": True, "verified": True,
                      "collectionMethod": "Playwright captured JSON",
                      "profileId": source_config.get("profile_id", "")})
    def walk(node):
        if len(posts) >= XHS_POST_LIMIT_PER_PROFILE: return
        if isinstance(node, dict):
            keys = set(node)
            if note_id(node) and keys.intersection({"displayTitle", "title", "desc", "noteCard", "cover", "imageList", "images"}): add(node)
            for item in node.values():
                if isinstance(item, (dict, list)): walk(item)
        elif isinstance(node, list):
            for item in node: walk(item)
    walk(payload)
    return posts[:XHS_POST_LIMIT_PER_PROFILE]


def collect_xhs_profile(source_config):
    cookies = load_xhs_cookies()
    if not cookies:
        print(f"{source_config['name']}: XHS_COOKIES_JSON missing; skipping XHS")
        return []
    from playwright.sync_api import sync_playwright
    collected, seen = [], set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                      viewport={"width": 1440, "height": 1000},
                                      user_agent=HEADERS["User-Agent"],
                                      extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]})
        context.add_cookies(cookies)
        page = context.new_page()
        payloads = []
        def capture(response):
            try:
                if "json" not in (response.headers.get("content-type") or "").lower(): return
                url = response.url.lower()
                if not any(x in url for x in ("/api/sns/web/", "user_post", "note_list", "notelist", "profile")): return
                data = response.json()
                if isinstance(data, (dict, list)): payloads.append((response.url, data))
            except Exception: pass
        page.on("response", capture)
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        response = page.goto(source_config["url"], wait_until="domcontentloaded", timeout=XHS_PAGE_TIMEOUT_MS)
        page.wait_for_timeout(8000)
        status = response.status if response else 0
        print("XHS HTTP status:", status)
        print("XHS final URL:", page.url)
        print("XHS page title:", clean_text(page.title())[:150])
        if status >= 400: raise RuntimeError(f"XHS profile returned HTTP {status}")
        profile_name = source_config["source"]
        for selector in ("h1", "[class*='user-name']", "[class*='username']", "[class*='nickname']"):
            try:
                value = clean_text(page.locator(selector).first.inner_text(timeout=1200))
                if value and len(value) <= 80: profile_name = value; break
            except Exception: pass
        source_config["source"] = source_config["name"] = profile_name
        print("XHS captured JSON responses:", len(payloads))
        for api_url, payload in payloads:
            for post in extract_xhs_posts_from_json(payload, source_config, profile_name):
                if post["url"] not in seen:
                    seen.add(post["url"]); collected.append(post)
            if len(collected) >= XHS_POST_LIMIT_PER_PROFILE: break
        print("XHS posts from captured responses:", len(collected))
        # Robust DOM fallback: inspect all anchors and their nearest meaningful card.
        for _ in range(7):
            anchors = page.locator("a[href]")
            print("XHS candidate DOM links:", anchors.count())
            for i in range(anchors.count()):
                if len(collected) >= XHS_POST_LIMIT_PER_PROFILE: break
                link = anchors.nth(i)
                try:
                    url = canonical_xhs_post_url(link.get_attribute("href") or "")
                    if not url or url in seen: continue
                    card = link.locator("xpath=ancestor::*[self::section or self::article or self::li or self::div][1]")
                    body = clean_text(card.inner_text(timeout=1000)) or clean_text(link.inner_text(timeout=700))
                    if not body: continue
                    images=[]
                    for j in range(min(card.locator("img").count(),9)):
                        img=card.locator("img").nth(j)
                        for attr in ("src","data-src","data-original"):
                            image_url=normalize_image_url(img.get_attribute(attr),source_config["url"])
                            if image_url and image_url not in images: images.append(image_url); break
                    title=shorten(body,150)
                    seen.add(url)
                    collected.append({"id":make_id("XHS",profile_name,title,url),"platform":"XHS",
                        "source":profile_name,"title":title,"excerpt":shorten(body,350),"images":images,
                        "category":"XHS Update","date":singapore_now().isoformat(),"url":url,
                        "official":True,"verified":True,"collectionMethod":"Playwright DOM",
                        "profileId":source_config.get("profile_id","")})
                except Exception as error:
                    print("XHS card skipped:",type(error).__name__,str(error)[:120])
            if len(collected)>=XHS_POST_LIMIT_PER_PROFILE: break
            page.mouse.wheel(0,1600); page.wait_for_timeout(2000)
        if not collected:
            hrefs=[]
            for i in range(min(page.locator("a[href]").count(),250)):
                href=page.locator("a[href]").nth(i).get_attribute("href") or ""
                if href and href not in hrefs: hrefs.append(href)
                if len(hrefs)>=30: break
            print("XHS diagnostic sample hrefs:",hrefs)
            print("XHS diagnostic body preview:",clean_text(page.locator("body").inner_text())[:500])
        browser.close()
    return collected[:XHS_POST_LIMIT_PER_PROFILE]


def collect_source(source_config):
    kind = source_config["type"]
    if kind == "sina_account": return collect_sina_account(source_config)
    if kind == "rss_feed": return collect_rss_feed(source_config)
    if kind == "xhs_profile": return collect_xhs_profile(source_config)
    return []


def normalize_existing_post(post):
    post.setdefault("id", make_id(post.get("platform", ""), post.get("source", ""), post.get("title", ""), post.get("url", "")))
    post.setdefault("excerpt", ""); post.setdefault("category", "Other"); post.setdefault("official", False)
    post.setdefault("verified", False); post.setdefault("collectionMethod", "Manual")
    raw_images=post.get("images",[])
    if not isinstance(raw_images,list):
        raw_images=[raw_images] if raw_images else []
    post["images"]=[url for url in (normalize_image_url(value) for value in raw_images) if url][:9]
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

    weibo_posts = [post for post in posts if post.get("platform") == "Weibo"]

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
                                  if isinstance(p, dict)])
    legacy_weibo_images_removed=0
    for post in existing:
        if post.get("platform")=="Weibo" and post.get("images"):
            post["images"]=[]
            legacy_weibo_images_removed+=1
    print("Legacy Weibo image references removed:",legacy_weibo_images_removed)

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
    other_new = [p for p in collected if p.get("platform") != "Weibo"]
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
    images_cached,image_cache_failures=localize_post_images(final_posts)
    added, translated, limited, translation_providers = add_translations(final_posts)
    success = sum(s["status"] == "Success" for s in statuses); failed = len(statuses) - success
    output = {"lastUpdated": singapore_now().isoformat(timespec="seconds"),
              "status": f"Completed: {success} sources successful, {failed} failed",
              "translationStatus": {"translationsAdded": added, "postsTranslatedThisRun": translated,
                                    "maximumPostsPerRun": MAX_TRANSLATED_POSTS_PER_RUN, "rateLimited": limited,
                                    "providersUsed": translation_providers,
                                    "priority": "Weibo only",
                                    "platformsTranslated": ["Weibo"],
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



