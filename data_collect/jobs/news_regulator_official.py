"""官方直连新闻源的礼貌 HTTP 与解析边界。

本模块只负责官方页面/API 的网络契约和结构化条目发现；归档、OpenSearch 写入、
来源隔离及日报健康账本仍由 ``news_regulator`` 统一处理。
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import hashlib
import html as html_lib
import json
import random
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from data_collect.utils import fulltext
from data_collect.utils import news_normalize as nn


_MIN_PACE_SECONDS = 2.0
_MAX_PACE_SECONDS = 5.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_TRANSIENT_STATUSES = {429, 502, 503, 504}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CALLBACK_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

XINHUA_URL = "https://www.news.cn/fortunepro/mobile/index.html"
GOV_BMWJ_URL = "https://www.gov.cn/zhengce/zhengceku/bmwj/"
MIIT_SEARCH_URL = "https://www.miit.gov.cn/search-front-server/api/search/info"
SSE_MEASURES_URL = "https://query.sse.com.cn/commonSoaQuery.do"
BSE_BASE_URL = "https://www.bse.cn/"
YICAI_HEADLINE_URL = "https://www.yicai.com/api/ajax/getAINews"
MAX_BACKFILL_PAGES = 12
MIN_ARTICLE_CHARS = 120
MAX_ARTICLE_CHARS = 50_000

_XINHUA_ARTICLE_RE = re.compile(
    r"^https://www\.news\.cn/fortune/(?P<date>20\d{6})/"
    r"(?P<native>[A-Za-z0-9]+)/c\.html$")
_GOV_ARTICLE_RE = re.compile(
    r"^https://www\.gov\.cn/zhengce/zhengceku/(?P<month>20\d{4})/"
    r"content_(?P<native>\d+)\.htm$")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")

SSE_SECTIONS = (
    ("上市公司监管措施",
     "https://www.sse.com.cn/regulation/supervision/measures/"),
    ("发行上市审核监管措施",
     "https://www.sse.com.cn/regulation/listing/measures/"),
    ("发行上市纪律处分",
     "https://www.sse.com.cn/regulation/listing/disposition/"),
    ("交易监管动态",
     "https://www.sse.com.cn/regulation/trading/dynamic/"),
    ("交易监管案例",
     "https://www.sse.com.cn/regulation/trading/case/"),
    ("会员监管措施",
     "https://www.sse.com.cn/regulation/members/measures/"),
    ("会员纪律处分",
     "https://www.sse.com.cn/regulation/members/disposition/"),
    ("自律管理复核",
     "https://www.sse.com.cn/regulation/review/"),
)

_BSE_REQUESTS = (
    ("dynamic", "监管动态",
     "/superviseDynamicController/superviseDynamicInfoResult.do",
     {"typejb": "T"}),
    ("self_measure", "自律监管措施",
     "/AnnouncementController/infoResult.do",
     {"announcementYn": "1", "newThreeArray[]": "3", "companyCode": ""}),
    ("discipline", "纪律处分",
     "/PunishmentController/infoResult.do",
     {"announcementYn": "2", "newThreeArray[]": "3", "companyCode": ""}),
    ("trade_limit", "限制交易",
     "/tradeSuperviseController/tradeSuperviseInfoResult.do",
     {"typejb": "T", "superviseType": "0"}),
    ("trade_case", "监管案例",
     "/tradeSuperviseController/tradeSuperviseInfoResult.do",
     {"typejb": "T", "superviseType": "1"}),
    ("review", "复核",
     "/reviewApplication/infoResultse.do",
     {"newThreeArray[]": "3", "companyCode": ""}),
)

BACKFILL_SOURCE_IDS = {
    "miit_policy", "xinhua_fortune", "gov_bmwj", "govcn_gwy",
    "sse_regulation", "bse_regulation",
}


class OfficialHTTPError(RuntimeError):
    """官方来源 HTTP 边界错误；只保留状态码和脱敏后的 URL。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OfficialStructureError(RuntimeError):
    """官方页面/API 响应成功但结构不再符合已知契约。"""


@dataclasses.dataclass(frozen=True)
class OfficialItem:
    source: str
    native_id: str
    title: str
    pub_time: str
    url: str
    category: str = ""
    content: str = ""
    raw: object = None


def _safe_url(url: str) -> str:
    parsed = urlsplit(str(url))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _same_target(left: str, right: str) -> bool:
    a = urlsplit(left)
    b = urlsplit(right)
    return (a.scheme.lower(), a.netloc.lower(), a.path, a.query) == (
        b.scheme.lower(), b.netloc.lower(), b.path, b.query)


class PoliteClient:
    """按主机串行限速、有限重试，并兼容一次同 URL cookie 挑战。"""

    def __init__(self, *, session=None, timeout=30, sleep_fn=time.sleep,
                 monotonic_fn=time.monotonic, jitter_fn=random.uniform):
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._jitter = jitter_fn
        self._last_request: dict[str, float] = {}
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        })

    def _pace(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        now = self._monotonic()
        previous = self._last_request.get(host)
        if previous is not None:
            minimum = float(self._jitter(
                _MIN_PACE_SECONDS, _MAX_PACE_SECONDS))
            remaining = minimum - max(0.0, now - previous)
            if remaining > 0:
                self._sleep(remaining)
        return host

    def _mark_request(self, host: str) -> None:
        self._last_request[host] = self._monotonic()

    def _retry_delay(self, response=None) -> float:
        raw = ((getattr(response, "headers", {}) or {}).get("Retry-After")
               if response is not None else None)
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            seconds = float(self._jitter(
                _MIN_PACE_SECONDS, _MAX_PACE_SECONDS))
        return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))

    def request_text(self, method: str, url: str, *, data=None, params=None,
                     headers=None) -> str:
        """请求文本；瞬时失败最多重试一次，cookie 挑战最多重放一次。"""
        transient_retried = False
        redirect_replayed = False
        while True:
            host = self._pace(url)
            try:
                response = self.session.request(
                    method.upper(), url, data=data, params=params, headers=headers,
                    timeout=self.timeout, allow_redirects=False)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._mark_request(host)
                if transient_retried:
                    raise OfficialHTTPError(
                        f"official source request failed: {_safe_url(url)}") from exc
                transient_retried = True
                self._sleep(self._retry_delay())
                continue
            self._mark_request(host)

            status = int(response.status_code)
            if status in _REDIRECT_STATUSES:
                location = urljoin(url, str(response.headers.get("Location") or ""))
                if (redirect_replayed or not location
                        or not _same_target(url, location)):
                    raise OfficialHTTPError(
                        f"official source redirect rejected: {_safe_url(url)}",
                        status)
                redirect_replayed = True
                continue

            if 200 <= status < 300:
                content = getattr(response, "content", None)
                encoding = str(
                    getattr(response, "encoding", "") or "").lower()
                if (isinstance(content, (bytes, bytearray))
                        and encoding in {"iso-8859-1", "latin-1", "latin1"}):
                    try:
                        return bytes(content).decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                return str(response.text or "")

            if status in _TRANSIENT_STATUSES and not transient_retried:
                transient_retried = True
                self._sleep(self._retry_delay(response))
                continue

            raise OfficialHTTPError(
                f"official source HTTP {status}: {_safe_url(url)}", status)


def validate_window(start_date: str, end_date: str) -> tuple[dt.date, dt.date]:
    """解析 YYYYMMDD 回补窗口并硬限制为最多 90 个自然日。"""
    start = dt.datetime.strptime(start_date, "%Y%m%d").date()
    end = dt.datetime.strptime(end_date, "%Y%m%d").date()
    if end < start:
        raise ValueError("end_date must be >= start_date")
    if (end - start).days + 1 > 90:
        raise ValueError("official news backfill window must be <= 90 days")
    return start, end


def decode_jsonp(text: str, callback: str):
    """严格剥离指定 JSONP callback；拒绝 HTML 挑战页和任意代码。"""
    if not _CALLBACK_RE.fullmatch(str(callback or "")):
        raise ValueError("invalid JSONP callback")
    match = re.fullmatch(
        rf"\s*{re.escape(callback)}\s*\((.*)\)\s*;?\s*",
        str(text or ""), flags=re.S)
    if match is None:
        raise ValueError("invalid JSONP envelope")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSONP payload") from exc


def _clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        values = dict(attrs)
        self._current = {
            "href": str(values.get("href") or ""),
            "title": str(values.get("title") or ""),
            "text": [],
        }

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self._current is None:
            return
        self.links.append((
            self._current["href"],
            self._current["title"],
            "".join(self._current["text"]),
        ))
        self._current = None


class _GovListParser(HTMLParser):
    """保留每个 `<li>` 内的链接与日期文本，避免日期串到相邻条目。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[tuple[str, str, str]], str]] = []
        self._depth = 0
        self._links: list[tuple[str, str, str]] = []
        self._text: list[str] = []
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        lower = tag.lower()
        if lower == "li":
            if self._depth == 0:
                self._links = []
                self._text = []
            self._depth += 1
        if lower == "a" and self._depth:
            values = dict(attrs)
            self._anchor = {
                "href": str(values.get("href") or ""),
                "title": str(values.get("title") or ""),
                "text": [],
            }

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        lower = tag.lower()
        if lower == "a" and self._anchor is not None:
            self._links.append((
                self._anchor["href"],
                self._anchor["title"],
                "".join(self._anchor["text"]),
            ))
            self._anchor = None
        if lower == "li" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.rows.append((list(self._links), "".join(self._text)))


class _SseListParser(HTMLParser):
    """提取上交所静态列表 `<dd>` 中彼此绑定的日期和链接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[tuple[str, str, str]], str]] = []
        self._depth = 0
        self._links: list[tuple[str, str, str]] = []
        self._text: list[str] = []
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        lower = tag.lower()
        if lower == "dd":
            if self._depth == 0:
                self._links = []
                self._text = []
            self._depth += 1
        if lower == "a" and self._depth:
            values = dict(attrs)
            self._anchor = {
                "href": str(values.get("href") or ""),
                "title": str(values.get("title") or ""),
                "text": [],
            }

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        lower = tag.lower()
        if lower == "a" and self._anchor is not None:
            self._links.append((
                self._anchor["href"],
                self._anchor["title"],
                "".join(self._anchor["text"]),
            ))
            self._anchor = None
        if lower == "dd" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.rows.append((list(self._links), "".join(self._text)))


def _date_in_window(value: dt.date, start: dt.date | None,
                    end: dt.date | None) -> bool:
    return (start is None or value >= start) and (end is None or value <= end)


def discover_xinhua(client: PoliteClient, start: dt.date | None = None,
                     end: dt.date | None = None) -> list[OfficialItem]:
    """发现新华网财经移动页当前可见文章；该入口没有历史分页。"""
    document = client.request_text("GET", XINHUA_URL)
    parser = _LinkParser()
    parser.feed(document)
    discovered: list[OfficialItem] = []
    seen: set[str] = set()
    for href, attr_title, link_text in parser.links:
        url = urljoin(XINHUA_URL, href.strip())
        match = _XINHUA_ARTICLE_RE.fullmatch(url)
        if match is None or url in seen:
            continue
        title = _clean_inline(attr_title or link_text)
        if not title:
            continue
        seen.add(url)
        published = dt.datetime.strptime(match.group("date"), "%Y%m%d").date()
        discovered.append(OfficialItem(
            source="xinhua_fortune",
            native_id=match.group("native"),
            title=title,
            pub_time=f"{published.isoformat()} 00:00:00",
            url=url,
            category="新华财经",
            raw={"href": href, "title": title},
        ))
    if not discovered:
        raise OfficialStructureError("xinhua list contains no accepted articles")
    return [item for item in discovered if _date_in_window(
        dt.date.fromisoformat(item.pub_time[:10]), start, end)]


def _parse_gov_page(document: str) -> list[OfficialItem]:
    parser = _GovListParser()
    parser.feed(document)
    items: list[OfficialItem] = []
    for links, row_text in parser.rows:
        date_match = _DATE_RE.search(row_text)
        if date_match is None:
            continue
        published = dt.date(*map(int, date_match.groups()))
        for href, attr_title, link_text in links:
            url = urljoin(GOV_BMWJ_URL, href.strip())
            match = _GOV_ARTICLE_RE.fullmatch(url)
            title = _clean_inline(attr_title or link_text)
            if match is None or not title:
                continue
            items.append(OfficialItem(
                source="gov_bmwj",
                native_id=match.group("native"),
                title=title,
                pub_time=f"{published.isoformat()} 00:00:00",
                url=url,
                category="部门文件",
                raw={"href": href, "title": title,
                     "date": published.isoformat()},
            ))
    return items


def _has_gov_next_page(document: str, page_number: int) -> bool:
    parser = _LinkParser()
    parser.feed(document)
    wanted = f"home_{page_number}.htm"
    return any(urlsplit(href).path.endswith(wanted)
               for href, _title, _text in parser.links)


def discover_gov_bmwj(client: PoliteClient, start: dt.date | None = None,
                      end: dt.date | None = None, *,
                      backfill: bool = False) -> list[OfficialItem]:
    """发现中国政府网部门文件；历史模式低速逐页并按日期下界停止。"""
    results: list[OfficialItem] = []
    seen_urls: set[str] = set()
    page_signatures: set[tuple[str, ...]] = set()
    for page_number in range(MAX_BACKFILL_PAGES):
        url = (GOV_BMWJ_URL if page_number == 0 else
               urljoin(GOV_BMWJ_URL, f"home_{page_number}.htm"))
        document = client.request_text("GET", url)
        page_items = _parse_gov_page(document)
        if page_number == 0 and not page_items:
            raise OfficialStructureError(
                "gov_bmwj list contains no accepted articles")
        if not page_items:
            break
        signature = tuple(item.url for item in page_items)
        if signature in page_signatures:
            break
        page_signatures.add(signature)
        page_dates = [dt.date.fromisoformat(item.pub_time[:10])
                      for item in page_items]
        for item, published in zip(page_items, page_dates):
            if item.url not in seen_urls and _date_in_window(
                    published, start, end):
                seen_urls.add(item.url)
                results.append(item)
        if not backfill:
            break
        if start is not None and min(page_dates) < start:
            break
        if not _has_gov_next_page(document, page_number + 1):
            break
    return results


def hydrate_article(client: PoliteClient,
                    item: OfficialItem) -> OfficialItem | None:
    """低速抓新华/Gov新文章正文；短文/挑战页拒收以便下轮重试。"""
    document = client.request_text("GET", item.url)
    try:
        extracted = fulltext._extract(document, item.url)
    except Exception:  # noqa: BLE001 -- 单篇解析失败由调用方跳过
        return None
    content = nn.clean_text(extracted or "")[:MAX_ARTICLE_CHARS]
    if len(content) < MIN_ARTICLE_CHARS:
        return None
    return dataclasses.replace(item, content=content)


def _miit_item(row: dict) -> OfficialItem | None:
    title = _clean_inline(str(
        row.get("title_text") or row.get("title") or ""))
    raw_time = str(row.get("deploytime") or "").strip()
    try:
        published = dt.datetime.fromtimestamp(
            int(raw_time) / 1000, tz=dt.timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        date_match = _DATE_RE.search(raw_time)
        if date_match is None:
            return None
        published = dt.date(*map(int, date_match.groups()))
    raw_url = str(row.get("url") or "").strip()
    url = urljoin("https://www.miit.gov.cn/", raw_url)
    parsed = urlsplit(url)
    if (not title or parsed.scheme != "https"
            or parsed.hostname != "www.miit.gov.cn"
            or not parsed.path.startswith("/zwgk/zcwj/wjfb/")
            or not parsed.path.endswith(".html")):
        return None
    # 搜索接口 metaid 在不同文件间复用（2026-10 实测），仅文章 URL 可作主键。
    match = re.search(r"/art_([^/]+)\.html$", parsed.path)
    native = match.group(1) if match else hashlib.sha1(url.encode()).hexdigest()
    subtype = _clean_inline(str(row.get("columnname") or ""))
    category = f"工信部文件发布/{subtype}" if subtype else "工信部文件发布"
    return OfficialItem(
        source="miit_policy",
        native_id=native,
        title=title,
        pub_time=f"{published.isoformat()} 00:00:00",
        url=url,
        category=category,
        content=f"{category}：{title}",
        raw=row,
    )


def discover_miit_policy(client: PoliteClient,
                         start: dt.date | None = None,
                         end: dt.date | None = None, *,
                         backfill: bool = False) -> list[OfficialItem]:
    """通过工信部当前“文件发布”官方搜索分类发现政策文件。"""
    page_size = 10
    page = 1
    results: list[OfficialItem] = []
    seen: set[str] = set()
    while True:
        params = {
            "websiteid": "110000000000000",
            "scope": "basic",
            "q": "",
            "pg": page_size,
            "p": page,
            "cateid": 58,
            "pos": "",
            "begin": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "dateField": "deploytime",
            "selectFields": (
                "title,title_text,deploytime,url,columnname,metaid"),
            "group": "distinct",
            "level": 6,
            "sortFields": json.dumps(
                [{"name": "deploytime", "type": "desc"}],
                separators=(",", ":")),
        }
        text = client.request_text(
            "GET", MIIT_SEARCH_URL, params=params,
            headers={"Referer": (
                "https://www.miit.gov.cn/search/wjfb.html?"
                "websiteid=110000000000000&category=51")})
        try:
            payload = json.loads(text)
            search = payload["data"]["searchResult"]
            groups = search["dataResults"]
            total_hits = int(search["totalHits"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise OfficialStructureError(
                "miit file-release search schema invalid") from exc
        if (payload.get("code") != "200" or payload.get("success") is not True
                or not isinstance(groups, list)):
            raise OfficialStructureError(
                "miit file-release search response rejected")
        for group in groups:
            if not isinstance(group, dict):
                raise OfficialStructureError("miit search group schema invalid")
            group_data = group.get("groupData")
            if (not isinstance(group_data, list) or not group_data
                    or not isinstance(group_data[0], dict)
                    or not isinstance(group_data[0].get("data"), dict)):
                raise OfficialStructureError(
                    "miit search groupData schema invalid")
            item = _miit_item(group_data[0]["data"])
            if item is None or item.url in seen:
                continue
            published = dt.date.fromisoformat(item.pub_time[:10])
            if _date_in_window(published, start, end):
                seen.add(item.url)
                results.append(item)
        page_count = (total_hits + page_size - 1) // page_size
        if not backfill or page >= page_count:
            break
        if page >= MAX_BACKFILL_PAGES:
            raise OfficialStructureError(
                f"miit search exceeds safety cap {MAX_BACKFILL_PAGES}")
        page += 1
    return results


def _allowed_sse_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.sse.com.cn"
            and parsed.path.startswith((
                "/regulation/",
                "/disclosure/credibility/supervision/")))


def _sse_native_id(url: str) -> str:
    match = re.search(r"c_(20\d{6}_\d+)", urlsplit(url).path)
    if match:
        return match.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _parse_sse_page(document: str, section_url: str,
                    category: str) -> list[OfficialItem]:
    parser = _SseListParser()
    parser.feed(document)
    items: list[OfficialItem] = []
    for links, row_text in parser.rows:
        date_match = _DATE_RE.search(row_text)
        if date_match is None:
            continue
        published = dt.date(*map(int, date_match.groups()))
        for href, attr_title, link_text in links:
            url = urljoin(section_url, href.strip())
            title = _clean_inline(attr_title or link_text)
            if not title or not _allowed_sse_url(url):
                continue
            items.append(OfficialItem(
                source="sse_regulation",
                native_id=_sse_native_id(url),
                title=title,
                pub_time=f"{published.isoformat()} 00:00:00",
                url=url,
                category=category,
                content=f"{category}：{title}",
                raw={"href": href, "title": title,
                     "date": published.isoformat(), "category": category},
            ))
    return items


def _sse_measure_item(row: dict) -> OfficialItem | None:
    title = _clean_inline(str(row.get("docTitle") or ""))
    date_match = _DATE_RE.search(str(row.get("createTime") or ""))
    native = str(row.get("docId") or "").strip()
    raw_url = str(row.get("docURL") or "").strip()
    if raw_url.startswith("//"):
        url = f"https:{raw_url}"
    elif raw_url.startswith("www.sse.com.cn/"):
        url = f"https://{raw_url}"
    else:
        url = urljoin("https://www.sse.com.cn/", raw_url)
    if not raw_url:
        suffix = native or hashlib.sha1(
            json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        url = f"{SSE_SECTIONS[0][1]}#doc-{suffix}"
    if not title or date_match is None or not _allowed_sse_url(url):
        return None
    published = dt.date(*map(int, date_match.groups()))
    subtype = _clean_inline(str(row.get("extWTFL") or ""))
    category = "上市公司监管措施"
    detail = f"{category}/{subtype}" if subtype else category
    return OfficialItem(
        source="sse_regulation",
        native_id=native or hashlib.sha1(url.encode()).hexdigest(),
        title=title,
        pub_time=f"{published.isoformat()} 00:00:00",
        url=url,
        category=category,
        content=f"{detail}：{title}",
        raw=row,
    )


def _discover_sse_measures(client: PoliteClient,
                           start: dt.date | None = None,
                           end: dt.date | None = None, *,
                           backfill: bool = False) -> list[OfficialItem]:
    callback = "officialProbe"
    results: list[OfficialItem] = []
    page = 1
    while True:
        params = {
            "jsonCallBack": callback,
            "isPagination": "true",
            "pageHelp.pageSize": 25,
            "pageHelp.pageNo": page,
            "pageHelp.beginPage": page,
            "pageHelp.cacheSize": 1,
            "pageHelp.endPage": page,
            "sqlId": "BS_KCB_GGLL_NEW",
            "siteId": 28,
            "channelId": "10007,10008,10009,10010",
            "type": "",
            "stockcode": "",
            "extTeacher": "",
            "extWTFL": "",
            "createTime": f"{start.isoformat()} 00:00:00" if start else "",
            "createTimeEnd": f"{end.isoformat()} 23:59:59" if end else "",
            "order": "createTime|desc,stockcode|asc",
        }
        text = client.request_text(
            "GET", SSE_MEASURES_URL, params=params,
            headers={"Referer": SSE_SECTIONS[0][1]})
        try:
            payload = decode_jsonp(text, callback)
        except ValueError as exc:
            raise OfficialStructureError(
                f"sse measures JSONP invalid: {exc}") from exc
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("result"), list)
                or not isinstance(payload.get("pageHelp"), dict)):
            raise OfficialStructureError("sse measures schema invalid")
        try:
            page_count = int(payload["pageHelp"]["pageCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OfficialStructureError(
                "sse measures pageHelp schema invalid") from exc
        for row in payload["result"]:
            if not isinstance(row, dict):
                raise OfficialStructureError("sse measures row schema invalid")
            item = _sse_measure_item(row)
            if item is None:
                continue
            published = dt.date.fromisoformat(item.pub_time[:10])
            if _date_in_window(published, start, end):
                results.append(item)
        if not backfill or page >= page_count:
            break
        if page >= MAX_BACKFILL_PAGES:
            raise OfficialStructureError(
                f"sse measures exceeds safety cap {MAX_BACKFILL_PAGES}")
        page += 1
    return results


def discover_sse_regulation(client: PoliteClient,
                            start: dt.date | None = None,
                            end: dt.date | None = None, *,
                            backfill: bool = False) -> list[OfficialItem]:
    """发现上交所监管栏目元数据，不进入文章/PDF正文。"""
    results: list[OfficialItem] = []
    seen: set[str] = set()
    accepted_before_filter = 0
    measure_items = _discover_sse_measures(
        client, start, end, backfill=backfill)
    accepted_before_filter += len(measure_items)
    for item in measure_items:
        if item.url not in seen:
            seen.add(item.url)
            results.append(item)
    for category, section_url in SSE_SECTIONS[1:]:
        page_items = _parse_sse_page(
            client.request_text("GET", section_url), section_url, category)
        accepted_before_filter += len(page_items)
        for item in page_items:
            published = dt.date.fromisoformat(item.pub_time[:10])
            if (item.url not in seen
                    and _date_in_window(published, start, end)):
                seen.add(item.url)
                results.append(item)
    if accepted_before_filter == 0:
        raise OfficialStructureError("sse regulation pages contain no accepted items")
    return results


def _allowed_bse_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.bse.cn"
            and bool(parsed.path))


def _bse_page_list(text: str, callback: str) -> dict:
    try:
        payload = decode_jsonp(text, callback)
    except ValueError as exc:
        raise OfficialStructureError(f"bse JSONP invalid: {exc}") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise OfficialStructureError("bse JSONP root schema invalid")
    root = payload[0]
    if root.get("msg"):
        raise OfficialStructureError(f"bse response: {root['msg']}")
    page_list = root.get("pageList")
    if (not isinstance(page_list, dict)
            or not isinstance(page_list.get("content"), list)
            or not isinstance(page_list.get("totalPages"), int)):
        raise OfficialStructureError("bse pageList schema invalid")
    return page_list


def _first_value(row: dict, names: tuple[str, ...]):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return ""


def _bse_item(row: dict, slug: str,
              category: str) -> OfficialItem | None:
    title = _clean_inline(str(_first_value(
        row, ("title", "disclosureTitle", "infoTitle", "xxfcbt", "fileName"))))
    date_text = str(_first_value(
        row, ("publishDateStr", "publishDate", "disclosureDate", "createDate")))
    date_match = _DATE_RE.search(date_text)
    path = str(_first_value(
        row, ("filePath", "fileUrl", "url", "path", "file_url"))).strip()
    url = urljoin(BSE_BASE_URL, path)
    native = str(_first_value(row, ("id", "infoId", "announcementId"))).strip()
    if not title or date_match is None or not path or not _allowed_bse_url(url):
        return None
    published = dt.date(*map(int, date_match.groups()))
    return OfficialItem(
        source="bse_regulation",
        native_id=f"{slug}-{native or hashlib.sha1(url.encode()).hexdigest()}",
        title=title,
        pub_time=f"{published.isoformat()} 00:00:00",
        url=url,
        category=category,
        content=f"{category}：{title}",
        raw=row,
    )


def _discover_bse_request(client: PoliteClient, request_spec,
                          start: dt.date | None = None,
                          end: dt.date | None = None, *,
                          backfill: bool = False) -> list[OfficialItem]:
    """执行一个北交所请求组；供完整采集和轻量冒烟共用。"""
    slug, category, endpoint, extra = request_spec
    callback = "officialProbe"
    results: list[OfficialItem] = []
    seen: set[str] = set()
    page = 0
    while True:
        data = {
            "page": page,
            "keyword": "",
            "startTime": start.isoformat() if start else "",
            "endTime": end.isoformat() if end else "",
            "siteId": "6",
            "callback": callback,
            **extra,
        }
        text = client.request_text(
            "POST", urljoin(BSE_BASE_URL, endpoint), data=data,
            headers={"Referer": urljoin(
                BSE_BASE_URL, "disclosure/supervise_dynamic.html")})
        page_list = _bse_page_list(text, callback)
        for row in page_list["content"]:
            if not isinstance(row, dict):
                raise OfficialStructureError("bse content row schema invalid")
            item = _bse_item(row, slug, category)
            if item is None:
                continue
            published = dt.date.fromisoformat(item.pub_time[:10])
            if (item.url not in seen
                    and _date_in_window(published, start, end)):
                seen.add(item.url)
                results.append(item)
        total_pages = int(page_list["totalPages"])
        if not backfill or page + 1 >= total_pages:
            break
        if page + 1 >= MAX_BACKFILL_PAGES:
            raise OfficialStructureError(
                f"bse pageList exceeds safety cap {MAX_BACKFILL_PAGES}")
        page += 1
    return results


def discover_bse_regulation(client: PoliteClient,
                            start: dt.date | None = None,
                            end: dt.date | None = None, *,
                            backfill: bool = False) -> list[OfficialItem]:
    """按北交所页面公开 JSONP 契约发现监管元数据。"""
    results: list[OfficialItem] = []
    seen: set[str] = set()
    for request_spec in _BSE_REQUESTS:
        for item in _discover_bse_request(
                client, request_spec, start, end, backfill=backfill):
            if item.url not in seen:
                seen.add(item.url)
                results.append(item)
    return results


def discover_yicai(client: PoliteClient) -> list[OfficialItem]:
    """官网头条首屏；旧 cid=48 已冻结。仅一次列表请求，不翻页或抓全文。"""
    text = client.request_text("GET", YICAI_HEADLINE_URL,
                               params={"page": 1, "pagesize": 30})
    try:
        rows = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise OfficialStructureError("yicai headline invalid JSON") from exc
    if not isinstance(rows, list):
        raise OfficialStructureError("yicai headline expected list")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            raise OfficialStructureError("yicai headline expected article object")
        url = urljoin("https://www.yicai.com/", str(row.get("url") or ""))
        # 仅新闻，排除视频、广告和站外链接；URL 同旧 RSSHub guid，保持幂等。
        if not re.fullmatch(r"https://www\.yicai\.com/news/\d+\.html", url):
            continue
        title = str(row.get("NewsTitle") or "").strip()
        try:
            published = dt.datetime.fromisoformat(str(row.get("CreateDate") or ""))
        except ValueError as exc:
            raise OfficialStructureError("yicai headline invalid publication date") from exc
        beijing = dt.timezone(dt.timedelta(hours=8))
        published = (published.replace(tzinfo=beijing) if published.tzinfo is None
                     else published.astimezone(beijing))
        if not title:
            raise OfficialStructureError("yicai headline missing title")
        items.append(OfficialItem(
            source="yicai", native_id=url, title=title,
            pub_time=published.strftime("%Y-%m-%d %H:%M:%S"), url=url,
            content=str(row.get("NewsNotes") or ""), raw=row))
    if not items:
        raise OfficialStructureError("yicai headline contains no accepted news")
    return items


def discover_source(source_id: str, client: PoliteClient,
                    start: dt.date | None = None,
                    end: dt.date | None = None, *,
                    backfill: bool = False) -> list[OfficialItem]:
    from data_collect.jobs import news_regulator_current as current
    if source_id in {"gov_bmwj", "govcn_gwy"}:
        return current.discover_gov_documents(
            client, source_id, start, end, backfill=backfill)
    if source_id == "thepaper":
        return current.discover_thepaper(client)
    if source_id == "csrc":
        return current.discover_csrc(client)
    if source_id == "yicai":
        if backfill or start is not None or end is not None:
            raise ValueError("yicai headline only supports current-page collection")
        return discover_yicai(client)
    if source_id == "miit_policy":
        return discover_miit_policy(
            client, start, end, backfill=backfill)
    if source_id == "xinhua_fortune":
        return discover_xinhua(client, start, end)
    if source_id == "sse_regulation":
        return discover_sse_regulation(
            client, start, end, backfill=backfill)
    if source_id == "bse_regulation":
        return discover_bse_regulation(
            client, start, end, backfill=backfill)
    raise KeyError(f"unsupported official source: {source_id}")


def supports_source(source_id: str) -> bool:
    return source_id in BACKFILL_SOURCE_IDS | {"thepaper", "csrc", "yicai"}


def smoke_source(source) -> str:
    """对官方直连源做一次轻量探测，不归档、不入库。"""
    client = PoliteClient(timeout=source.timeout)
    if source.id == "yicai":
        items = discover_yicai(client)
    elif source.id == "miit_policy":
        items = discover_miit_policy(client)
    elif source.id == "xinhua_fortune":
        items = discover_xinhua(client)
    elif source.id in {"gov_bmwj", "govcn_gwy", "thepaper", "csrc"}:
        items = discover_source(source.id, client)
    elif source.id == "sse_regulation":
        items = _discover_sse_measures(client)
        if not items:
            raise OfficialStructureError(
                "sse measures smoke page contains no accepted items")
    elif source.id == "bse_regulation":
        items = _discover_bse_request(client, _BSE_REQUESTS[0])
    else:
        raise KeyError(f"unsupported official source: {source.id}")
    first = items[0] if items else None
    detail = f"{first.category} | " if first and first.category else ""
    title = first.title if first else "-"
    return f"{source.id} {len(items)} 条 | {detail}首条: {title[:60]}"
