import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

OUTPUT_FILE = Path("weibo_discovered_accounts.json")
TZ = ZoneInfo("Asia/Singapore")
WINDOW_HOURS = int(os.environ.get("WEIBO_DISCOVERY_HOURS", "24"))
MAX_SEARCH_PAGES = int(os.environ.get("WEIBO_DISCOVERY_PAGES", "3"))
SEARCH_TERMS = [
    "侯明昊", "@侯明昊", "侯明昊代言", "侯明昊品牌大使", "侯明昊合作"
]
HOU_KEYWORDS = ["侯明昊", "Hou Minghao", "HouMinghao", "Hou Ming Hao", "侯明昊工作室"]
SKIP_UIDS = {"1831550987", "5763552342"}
BRAND_HINTS = (
    "官方", "品牌", "旗舰", "中国", "集团", "公司", "科技", "食品", "美妆", "护肤",
    "服饰", "珠宝", "汽车", "手机", "家电", "旅游", "文旅", "微博", "official"
)
PERSONAL_HINTS = ("粉丝", "后援", "超话", "资源站", "应援", "个人", "演员", "博主")
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def clean_text(value):
    if not value:
        return ""
    value = unescape(str(value))
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def mentions_hou(*values):
    blob = " ".join(clean_text(v) for v in values if v).lower()
    return any(keyword.lower() in blob for keyword in HOU_KEYWORDS)


def parse_created_at(value, now):
    value = clean_text(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(TZ)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d+)\s*分钟前", value)
    if match:
        return now - timedelta(minutes=int(match.group(1)))
    match = re.fullmatch(r"(\d+)\s*小时前", value)
    if match:
        return now - timedelta(hours=int(match.group(1)))
    match = re.fullmatch(r"(昨天|今天)\s*(\d{1,2}):(\d{2})", value)
    if match:
        day = now.date() - timedelta(days=1 if match.group(1) == "昨天" else 0)
        return datetime(day.year, day.month, day.day, int(match.group(2)), int(match.group(3)), tzinfo=TZ)
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", value)
    if match:
        hour = int(match.group(3) or 12)
        minute = int(match.group(4) or 0)
        candidate = datetime(now.year, int(match.group(1)), int(match.group(2)), hour, minute, tzinfo=TZ)
        if candidate > now + timedelta(days=1):
            candidate = candidate.replace(year=now.year - 1)
        return candidate
    return None


def load_existing():
    if not OUTPUT_FILE.exists():
        return {"updatedAt": None, "windowHours": WINDOW_HOURS, "accounts": []}
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
    except Exception as error:
        print("Could not read discovered account file:", error)
    return {"updatedAt": None, "windowHours": WINDOW_HOURS, "accounts": []}


def cookie_list():
    cookies = []
    for part in os.environ.get("WEIBO_COOKIE", "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip():
            for domain in (".weibo.cn", ".weibo.com"):
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": domain, "path": "/"})
    return cookies


def organization_verified(user):
    verified = bool(user.get("verified"))
    verified_type = user.get("verified_type")
    description = clean_text(user.get("verified_reason") or user.get("description") or "")
    name = clean_text(user.get("screen_name") or "")
    if not verified:
        return False, "not verified"
    # Weibo organization accounts commonly use verified_type 2, 3, 5 or 7. Because this
    # field is not perfectly consistent, require either an organization type or brand language.
    org_type = str(verified_type) in {"2", "3", "5", "7"}
    has_brand_hint = any(hint.lower() in f"{name} {description}".lower() for hint in BRAND_HINTS)
    has_personal_hint = any(hint.lower() in f"{name} {description}".lower() for hint in PERSONAL_HINTS)
    return (org_type or has_brand_hint) and not has_personal_hint, description


def get_json(page, url):
    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if response is None:
        raise RuntimeError("No response from Weibo")
    if response.status in (403, 418, 432):
        raise PermissionError(f"HTTP {response.status} from Weibo")
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}")
    body = (response.text() or "").strip()
    return json.loads(re.sub(r"<[^>]+>", "", body))


def walk_mblogs(node):
    if isinstance(node, dict):
        mblog = node.get("mblog")
        if isinstance(mblog, dict):
            yield mblog
        for key in ("cards", "card_group", "items"):
            yield from walk_mblogs(node.get(key))
    elif isinstance(node, list):
        for item in node:
            yield from walk_mblogs(item)


def main():
    from playwright.sync_api import sync_playwright

    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    current = load_existing()
    accounts = {str(item.get("uid")): item for item in current.get("accounts", []) if item.get("uid")}
    seen_posts = set()
    discovered_this_run = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=MOBILE_UA, locale="zh-CN", viewport={"width": 390, "height": 844},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        cookies = cookie_list()
        if cookies:
            context.add_cookies(cookies)
            print("Brand discovery is using WEIBO_COOKIE")
        page = context.new_page()
        page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=20000)

        for term in SEARCH_TERMS:
            print(f"Searching Weibo for: {term}")
            for page_number in range(1, MAX_SEARCH_PAGES + 1):
                containerid = "100103type=1&q=" + urllib.parse.quote(term)
                query = urllib.parse.urlencode({
                    "containerid": containerid,
                    "page_type": "searchall",
                    "page": page_number,
                    "_": int(time.time() * 1000),
                })
                url = f"https://m.weibo.cn/api/container/getIndex?{query}"
                try:
                    payload = get_json(page, url)
                except Exception as error:
                    print(f"  Search page {page_number} failed: {error}")
                    break
                cards = ((payload.get("data") or {}).get("cards")) or []
                if not cards:
                    break
                found_recent = False
                for mblog in walk_mblogs(cards):
                    post_id = str(mblog.get("id") or mblog.get("mid") or "")
                    if not post_id or post_id in seen_posts:
                        continue
                    seen_posts.add(post_id)
                    created = parse_created_at(mblog.get("created_at"), now)
                    if not created or created < cutoff or created > now + timedelta(minutes=10):
                        continue
                    found_recent = True
                    retweet = mblog.get("retweeted_status") or {}
                    if not mentions_hou(mblog.get("text"), mblog.get("raw_text"), retweet.get("text"), retweet.get("raw_text")):
                        continue
                    user = mblog.get("user") or {}
                    uid = str(user.get("id") or user.get("idstr") or "").strip()
                    name = clean_text(user.get("screen_name") or "")
                    if not uid or not name or uid in SKIP_UIDS:
                        continue
                    qualifies, verification_reason = organization_verified(user)
                    if not qualifies:
                        print(f"  Review skipped: @{name} ({uid}) is not confidently an official brand")
                        continue
                    old = accounts.get(uid, {})
                    first_seen = old.get("firstSeen") or now.isoformat(timespec="seconds")
                    accounts[uid] = {
                        "uid": uid,
                        "name": name,
                        "category": "brand",
                        "verified": True,
                        "enabled": True,
                        "autoDiscovered": True,
                        "firstSeen": first_seen,
                        "lastSeen": created.isoformat(timespec="seconds"),
                        "lastMentionPostId": post_id,
                        "lastMentionUrl": f"https://m.weibo.cn/status/{post_id}",
                        "discoveredBy": term,
                        "verificationReason": verification_reason,
                    }
                    discovered_this_run.add(uid)
                    print(f"  Added/updated verified brand: @{name} ({uid})")
                if not found_recent:
                    break
                time.sleep(0.8)
        context.close()
        browser.close()

    output = {
        "updatedAt": now.isoformat(timespec="seconds"),
        "windowHours": WINDOW_HOURS,
        "accounts": sorted(accounts.values(), key=lambda item: (item.get("name") or "").lower()),
    }
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Discovery complete: {len(discovered_this_run)} verified brand account(s) found in the last {WINDOW_HOURS} hours; {len(accounts)} stored total.")


if __name__ == "__main__":
    main()
