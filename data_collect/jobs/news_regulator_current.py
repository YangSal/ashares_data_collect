"""2026 年官网当前入口；HTTP 限速、信封和持久化复用监管采集链。"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from data_collect.jobs import news_regulator_official as official
from data_collect.utils import news_normalize as nn

logger = logging.getLogger(__name__)
GOV_SEARCH_URL = "https://sousuo.www.gov.cn/search-gov/data"
THEPAPER_URL = "https://m.thepaper.cn/"
CSRC_SEARCH_URL = (
    "https://www.csrc.gov.cn/searchList/a1a078ee0bc54721ab6b148884c784a8")
_BEIJING = dt.timezone(dt.timedelta(hours=8))


def _json(text, label):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise official.OfficialStructureError(f"{label} invalid JSON") from exc


def _next_data(document):
    match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                      document, re.S | re.I)
    try:
        return _json(match.group(1), "thepaper")["props"]["pageProps"]
    except (AttributeError, KeyError, TypeError) as exc:
        raise official.OfficialStructureError("thepaper missing pageProps") from exc


def discover_thepaper(client):
    props = _next_data(client.request_text("GET", THEPAPER_URL))
    try:
        rows = props["data"]["list"] + (props.get("topData", {}).get("recommendImg") or [])
        if not isinstance(rows, list):
            raise TypeError()
    except (KeyError, TypeError, AttributeError) as exc:
        raise official.OfficialStructureError("thepaper invalid list") from exc
    result, seen = [], set()
    for row in rows:
        key = str(row.get("contId") or "")
        title = nn.clean_text(row.get("name") or "")
        external = str(row.get("link") or "").strip()
        if not key.isdigit() or not title or row.get("paywalled"):
            continue
        try:
            published = dt.datetime.fromtimestamp(int(row["pubTimeLong"]) / 1000, _BEIJING)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        # RSSHub 的历史 guid 使用 /detail/；更换抓取入口不能改变存量文章 ID。
        native = external or f"https://m.thepaper.cn/detail/{key}"
        url = external or f"https://m.thepaper.cn/newsDetail_forward_{key}"
        if urlsplit(url).scheme not in {"http", "https"} or native in seen:
            continue
        seen.add(native)
        result.append(official.OfficialItem(
            "thepaper", native, title, published.strftime("%Y-%m-%d %H:%M:%S"),
            url, category=str((row.get("nodeInfo") or {}).get("name") or ""),
            content=title if external else "", raw=row))
    if not result:
        raise official.OfficialStructureError("thepaper list contains no accepted articles")
    return result


def hydrate_thepaper(client, item):
    # 外链条目与旧 RSSHub 一致，仅保留首页提供的标题，不跨站追抓。
    if item.raw.get("link"):
        return item
    try:
        detail = _next_data(client.request_text("GET", item.url))["detailData"]
        content = (detail.get("contentDetail") or detail.get("liveDetail")
                   or (detail.get("specialDetail") or {}).get("specialInfo") or {})
        body = nn.clean_text(content.get("content") or content.get("summary")
                             or content.get("desc") or "")[:official.MAX_ARTICLE_CHARS]
        if not body:
            return None
        return dataclasses.replace(item, content=body,
                                   raw={"list": item.raw, "detail": content})
    except (official.OfficialHTTPError, official.OfficialStructureError,
            KeyError, TypeError, AttributeError) as exc:
        logger.warning("澎湃单篇暂不可用，留待下轮: %s (%s)", item.url, type(exc).__name__)
        return None


def discover_gov_documents(client, source, start=None, end=None, *, backfill=False):
    kind = {"gov_bmwj": "zhengcelibrary_bm", "govcn_gwy": "zhengcelibrary_gw"}[source]
    results, seen, signatures = [], set(), set()
    for page in range(1, official.MAX_BACKFILL_PAGES + 1):
        payload = _json(client.request_text("GET", GOV_SEARCH_URL, params={
            "t": kind, "q": "", "sort": "pubtime", "sortType": 1,
            "searchfield": "title", "p": page, "n": 20, "type": "gwyzcwjk",
        }), source)
        try:
            data = payload["searchVO"]
            rows, total = data["listVO"], int(data["totalCount"])
            if str(payload["code"]) != "200" or not isinstance(rows, list):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise official.OfficialStructureError(f"{source} search schema invalid") from exc
        page_items = []
        for row in rows:
            url = str(row.get("url") or "").strip().replace("http://www.gov.cn/", "https://www.gov.cn/")
            match = re.fullmatch(r'https://www\.gov\.cn/zhengce/(?:zhengceku/|content/)?20\d{4}/content_(\d+)\.htm', url)
            title = nn.clean_text(row.get("title") or "")
            try:
                published = dt.datetime.strptime(str(row["pubtimeStr"]), "%Y.%m.%d").date()
            except (KeyError, TypeError, ValueError):
                continue
            if not match or not title:
                continue
            page_items.append(official.OfficialItem(
                source, match.group(1), title, f"{published} 00:00:00", url,
                category="部门文件" if source == "gov_bmwj" else "国务院文件",
                content=nn.clean_text(row.get("summary") or ""), raw=row))
        if not page_items:
            raise official.OfficialStructureError(f"{source} list contains no accepted articles")
        signature = tuple(item.url for item in page_items)
        if signature in signatures:
            raise official.OfficialStructureError(f"{source} repeated page")
        signatures.add(signature)
        dates = [dt.date.fromisoformat(item.pub_time[:10]) for item in page_items]
        for item, date in zip(page_items, dates):
            if item.url not in seen and official._date_in_window(date, start, end):
                results.append(item)
                seen.add(item.url)
        if not backfill or page * 20 >= total or (start and min(dates) < start):
            return results
    raise official.OfficialStructureError(f"{source} exceeds safety cap; use a narrower window")


def hydrate_gov(client, item):
    """政府网当前正文置于布局表格，通用抽取会丢弃；校验稿件 ID 后取专属容器。"""
    from bs4 import BeautifulSoup
    try:
        document = client.request_text("GET", item.url)
    except official.OfficialHTTPError as exc:
        logger.warning("政府网单篇暂不可用: %s (%s)", item.url, exc.status_code)
        return None
    soup = BeautifulSoup(document, "html.parser")
    identity = soup.find("meta", attrs={"name": "manuscriptId"})
    article = soup.find(id="UCAP-CONTENT")
    if not identity or identity.get("content") != item.native_id or article is None:
        return None
    for unwanted in article.find_all(["script", "style"]):
        unwanted.decompose()
    content = nn.clean_text(str(article))[:official.MAX_ARTICLE_CHARS]
    if len(content) < official.MIN_ARTICLE_CHARS:
        return None
    return dataclasses.replace(item, content=content)


def discover_csrc(client):
    payload = _json(client.request_text("GET", CSRC_SEARCH_URL, params={
        "_isAgg": "true", "_isJson": "true", "_pageSize": 18,
        "_template": "index", "_rangeTimeGte": "", "_channelName": "", "page": 1,
    }), "csrc")
    try:
        rows = payload["data"]["results"]
        if not isinstance(rows, list):
            raise TypeError()
    except (KeyError, TypeError) as exc:
        raise official.OfficialStructureError("csrc search schema invalid") from exc
    results, seen = [], set()
    for row in rows:
        # 延续旧列表页 http URL 的 sha1 身份；正文来自 HTTPS 官方 API。
        url = urljoin("http://www.csrc.gov.cn/", str(row.get("url") or ""))
        url = url.replace("https://www.csrc.gov.cn/", "http://www.csrc.gov.cn/")
        if not re.fullmatch(r'http://www\.csrc\.gov\.cn/csrc/c\d+/c\d+/content\.shtml', url):
            continue
        title = nn.clean_text(row.get("title") or "")
        body = nn.clean_text(row.get("content") or row.get("contentHtml") or "")
        try:
            pub_time = dt.datetime.strptime(row["publishedTimeStr"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        if not title or not body or url in seen:
            continue
        seen.add(url)
        results.append(official.OfficialItem(
            "csrc", "", title, pub_time.strftime("%Y-%m-%d %H:%M:%S"), url,
            category="证监会要闻", content=body[:official.MAX_ARTICLE_CHARS], raw=row))
    if not results:
        raise official.OfficialStructureError("csrc list contains no accepted articles")
    return results
