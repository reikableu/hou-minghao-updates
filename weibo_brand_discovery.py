"""Page-one, rolling 24-hour discovery of relevant brand and event organizations.
Search availability depends on Weibo; failures do not remove previously saved accounts.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUT = Path('weibo_discovered_accounts.json')
TZ = ZoneInfo('Asia/Singapore')
HOURS = 24
TERMS = ['侯明昊', '侯明昊代言', '侯明昊合作', '侯明昊盛典', '侯明昊红毯',
         '侯明昊澳门', '侯明昊湾区升明月', '侯明昊W中文版', '侯明昊微博之夜',
         '侯明昊尖叫之夜', '侯明昊Prada', '侯明昊活动', '侯明昊颁奖']
FAN = ('粉丝','后援','超话','资源站','应援','个人','演员','博主','追星','小友','唯粉',
       '站姐','饭圈','打投','个人认证','自媒体','达人','网红','博主')
# Deliberately exclude generic words such as 活动、盛典、娱乐、文化、时尚:
# those can appear in an individual's verification description.
ORG = ('官方','品牌','旗舰店','集团','有限公司','有限责任公司','股份有限公司',
       '公司','企业','机构','协会','基金会','商会','政府','委员会','电视台',
       '报社','杂志社','出版社','传媒公司','文化传媒','主办方','承办方',
       'official','company','inc.','ltd','brand','corporation','organization')
EVENT = ('主办方','承办方','组委会','电影节','音乐节','颁奖典礼','微博之夜',
         '尖叫之夜','w gala','年度盛典','湾区升明月','庆典','event','gala','award')
ORG_VERIFIED_TYPES = {'2','3','5','7'}
OFFICIAL = {'1831550987','5763552342'}

def classify_account(user, name, verification):
    """Reject personal/unproven accounts; accept only verified organizations."""
    profile = (name + ' ' + verification).casefold()

    # Explicit personal/fan indicators always win.
    if any(token.casefold() in profile for token in FAN):
        return 'Rejected', False

    verified = bool(user.get('verified'))
    verified_type = str(user.get('verified_type', ''))

    # A generic/personal verification badge is not organization proof.
    if not verified or verified_type not in ORG_VERIFIED_TYPES:
        return 'Rejected', False

    # Require explicit organization wording in the account name or
    # verification description. Event keywords alone are insufficient.
    if not any(token.casefold() in profile for token in ORG):
        return 'Rejected', False

    if any(token.casefold() in profile for token in EVENT):
        return 'Verified Event', True
    return 'Verified Brand', True

def text(s):
    return re.sub(r'\s+', ' ', BeautifulSoup(unescape(str(s or '')), 'html.parser').get_text(' ', strip=True)).strip()

def date_of(raw, now):
    raw=text(raw)
    try: return datetime.strptime(raw,'%a %b %d %H:%M:%S %z %Y').astimezone(TZ)
    except ValueError: pass
    m=re.fullmatch(r'(\d+)\s*分钟前',raw)
    if m:return now-timedelta(minutes=int(m.group(1)))
    m=re.fullmatch(r'(\d+)\s*小时前',raw)
    if m:return now-timedelta(hours=int(m.group(1)))
    m=re.fullmatch(r'(今天|昨天)\s*(\d{1,2}):(\d{2})',raw)
    if m:
        day=(now-timedelta(days=int(m.group(1)=='昨天'))).date()
        return datetime(day.year,day.month,day.day,int(m.group(2)),int(m.group(3)),tzinfo=TZ)
    m=re.fullmatch(r'(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?',raw)
    if m:
        d=datetime(now.year,int(m.group(1)),int(m.group(2)),int(m.group(3) or 12),int(m.group(4) or 0),tzinfo=TZ)
        return d.replace(year=d.year-1) if d>now+timedelta(days=1) else d
    return None

def mblogs(obj):
    if isinstance(obj,list):
        for value in obj: yield from mblogs(value)
    elif isinstance(obj,dict):
        if isinstance(obj.get('mblog'),dict): yield obj['mblog']
        for key in ('cards','card_group','items'): yield from mblogs(obj.get(key))

def main():
    now=datetime.now(TZ); cutoff=now-timedelta(hours=HOURS)
    try:
        saved=json.loads(OUT.read_text(encoding='utf-8'))
        accounts={str(x['uid']):x for x in saved.get('accounts',[]) if isinstance(x,dict) and x.get('uid')}
    except (FileNotFoundError,ValueError,KeyError): accounts={}
    good=0; failed=0; added=0
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        context=browser.new_context(locale='zh-CN',user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1')
        cookie=os.getenv('WEIBO_COOKIE','')
        cookies=[]
        for part in cookie.split(';'):
            if '=' in part:
                key,value=part.strip().split('=',1)
                if key: cookies.append({'name':key,'value':value,'domain':'.weibo.cn','path':'/'})
        if cookies: context.add_cookies(cookies)
        page=context.new_page()
        try: page.goto('https://m.weibo.cn/',wait_until='domcontentloaded',timeout=20000)
        except Exception as e: print('Warmup notice:',str(e)[:120])
        for term in TERMS:
            print('Searching page 1:',term)
            query=urlencode({'containerid':'100103type=1&q='+term,'page_type':'searchall','page':1})
            try:
                response=page.goto('https://m.weibo.cn/api/container/getIndex?'+query,wait_until='domcontentloaded',timeout=30000)
                if response is None or response.status!=200: raise RuntimeError('HTTP '+str(response.status if response else 'none'))
                body=response.text().strip()
                if body.startswith('<'): raise RuntimeError('HTML response, possible authentication or challenge')
                data=json.loads(body)
                cards=(data.get('data') or {}).get('cards') or []
                good+=1
            except Exception as e:
                failed+=1;print('  Search unavailable:',str(e)[:150]);continue
            for post in mblogs(cards):
                posted=date_of(post.get('created_at'),now)
                if posted is None or not cutoff<=posted<=now+timedelta(minutes=10):continue
                body=text(post.get('text') or post.get('raw_text'))
                if '侯明昊' not in body and 'hou minghao' not in body.lower():continue
                user=post.get('user') or {};uid=str(user.get('id') or user.get('idstr') or '')
                if not uid.isdigit() or uid in OFFICIAL:continue
                name=text(user.get('screen_name'));reason=text(user.get('verified_reason') or user.get('description'))
                profile=(name+' '+reason).lower()
                category, auto_enable = classify_account(user, name, reason)
                if category == 'Rejected':
                    accounts.pop(uid, None)
                    print('  Rejected personal or non-organization account:', name)
                    continue
                old=accounts.get(uid,{})
                # Preserve an existing manual disable; never silently re-enable it.
                enabled = bool(old.get('enabled', auto_enable)) and auto_enable
                accounts[uid]={'uid':uid,'name':name,'classification':category,'category':category,
                               'verified':bool(user.get('verified')),
                               'verifiedType':str(user.get('verified_type', '')),
                               'enabled':enabled,'autoDiscovered':True,
                               'verificationDescription':reason,
                               'firstSeen':old.get('firstSeen',now.isoformat()),
                               'lastSeen':posted.isoformat(),
                               'lastMentionPostId':str(post.get('id') or post.get('mid') or ''),
                               'discoveredBy':term}
                added+=1
                print('  Candidate:',name,category,'enabled='+str(enabled))
            time.sleep(0.5)
        browser.close()
    # Sanitize the entire persisted discovery list on every run, not just
    # accounts encountered in the current search. This purges older personal
    # accounts that may have slipped through a previous version of the filter.
    cleaned_accounts = {}
    for uid, item in accounts.items():
        profile = (str(item.get('name', '')) + ' ' +
                   str(item.get('verificationDescription', ''))).casefold()
        is_verified_org = (
            bool(item.get('verified'))
            and str(item.get('verifiedType', '')) in ORG_VERIFIED_TYPES
            and any(token.casefold() in profile for token in ORG)
            and not any(token.casefold() in profile for token in FAN)
        )
        if is_verified_org:
            cleaned_accounts[uid] = item
        else:
            print('Purging previously saved personal/non-organization account:',
                  item.get('name', uid))
    accounts = cleaned_accounts

    OUT.write_text(json.dumps({'updatedAt':now.isoformat(),'windowHours':HOURS,
                               'accounts':sorted(accounts.values(),key=lambda x:x.get('name',''))},
                              ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Discovery: successful keyword searches={good}; failed={failed}; eligible mentions={added}; stored accounts={len(accounts)}')
    if not good: print('SEARCH FAILED: existing account list retained; zero results do not mean zero mentions')

if __name__=='__main__':main()
