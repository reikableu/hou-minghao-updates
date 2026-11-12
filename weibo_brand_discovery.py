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
FAN = ('粉丝','后援','超话','资源站','应援','个人','演员','博主','追星','小友','唯粉','站姐','饭圈','打投')
ORG = ('官方','品牌','旗舰','集团','公司','科技','食品','美妆','护肤','服饰','珠宝','汽车',
       '家电','旅游','文旅','传媒','娱乐','文化','杂志','时尚','卫视','电视台','平台',
       '主办','盛典','活动','电影节','音乐节','颁奖','official','company','brand')
EVENT = ('盛典','晚会','红毯','颁奖','电影节','音乐节','尖叫之夜','微博之夜','w gala',
         '年度盛典','湾区升明月','庆典','event','gala','award')
OFFICIAL = {'1831550987','5763552342'}

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
                if any(h in profile for h in FAN):continue
                if not any(h in profile for h in ORG):continue
                verified=bool(user.get('verified'))
                org_type=str(user.get('verified_type')) in {'2','3','5','7'}
                event_source=any(h in profile for h in EVENT)
                category=('Verified Event' if verified and event_source else
                          'Verified Brand' if verified and (org_type or any(h in profile for h in ORG)) else
                          'Unverified')
                old=accounts.get(uid,{})
                accounts[uid]={'uid':uid,'name':name,'classification':category,'category':category,
                               'verified':verified,'enabled':True,'autoDiscovered':True,
                               'verificationDescription':reason,
                               'firstSeen':old.get('firstSeen',now.isoformat()),
                               'lastSeen':posted.isoformat(),
                               'lastMentionPostId':str(post.get('id') or post.get('mid') or ''),
                               'discoveredBy':term}
                added+=1
                print('  Eligible:',name,category)
            time.sleep(0.5)
        browser.close()
    OUT.write_text(json.dumps({'updatedAt':now.isoformat(),'windowHours':HOURS,
                               'accounts':sorted(accounts.values(),key=lambda x:x.get('name',''))},
                              ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Discovery: successful keyword searches={good}; failed={failed}; eligible mentions={added}; stored accounts={len(accounts)}')
    if not good: print('SEARCH FAILED: existing account list retained; zero results do not mean zero mentions')

if __name__=='__main__':main()
