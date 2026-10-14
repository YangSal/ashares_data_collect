"""官方新闻源伴生模块：礼貌 HTTP、解析器与来源契约（全离线）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from data_collect.jobs import news_regulator_official as no


class FakeResponse:
    def __init__(self, status_code, text="", *, headers=None, url="",
                 content=None, encoding=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = content
        self.encoding = encoding


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if not response.url:
            response.url = url
        return response


class FakeTextClient:
    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.calls = []

    def request_text(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.payloads[url]


def _client(session, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return no.PoliteClient(
        session=session,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: 100.0,
        jitter_fn=lambda _a, _b: 2.5,
    )


def test_polite_client_retries_503_once_and_respects_retry_after():
    """删掉短暂错误分支或忽略 Retry-After 时应失败。"""
    session = FakeSession([
        FakeResponse(503, headers={"Retry-After": "7"}),
        FakeResponse(200, text="ok"),
    ])
    sleeps = []
    assert _client(session, sleeps).request_text(
        "GET", "https://example.cn/list") == "ok"
    assert len(session.calls) == 2
    assert 7.0 in sleeps


def test_polite_client_paces_same_host_but_not_different_hosts():
    """去掉 host 级门控或错误做成全局门控时应失败。"""
    session = FakeSession([
        FakeResponse(200, text="one"),
        FakeResponse(200, text="two"),
        FakeResponse(200, text="three"),
    ])
    sleeps = []
    client = _client(session, sleeps)
    client.request_text("GET", "https://a.example/one")
    client.request_text("GET", "https://a.example/two")
    client.request_text("GET", "https://b.example/three")
    assert sleeps == [2.5]


def test_polite_client_replays_same_url_cookie_redirect_only_once():
    """缺少北交所一次性 cookie 握手兼容时应失败。"""
    url = "https://www.bse.cn/x"
    session = FakeSession([
        FakeResponse(302, headers={"Location": url}),
        FakeResponse(200, text="payload"),
    ])
    client = _client(session)
    assert client.request_text("POST", url, data={"page": 0}) == "payload"
    assert len(session.calls) == 2
    assert session.calls[0][2]["allow_redirects"] is False


def test_polite_client_rejects_redirect_loop():
    """无限重放 WAF 重定向会轰击源站，本测试锁住熔断。"""
    url = "https://www.bse.cn/x"
    session = FakeSession([
        FakeResponse(302, headers={"Location": url}),
        FakeResponse(302, headers={"Location": url}),
    ])
    with pytest.raises(no.OfficialHTTPError, match="redirect"):
        _client(session).request_text("GET", url)
    assert len(session.calls) == 2


def test_polite_client_does_not_retry_404():
    """把永久 404 当短暂故障会制造无意义重复请求。"""
    session = FakeSession([FakeResponse(404, text="missing")])
    with pytest.raises(no.OfficialHTTPError) as exc_info:
        _client(session).request_text("GET", "https://example.cn/missing?a=secret")
    assert exc_info.value.status_code == 404
    assert len(session.calls) == 1
    assert "a=secret" not in str(exc_info.value)


def test_polite_client_transient_retry_is_bounded_to_one():
    """重试循环上限从一次漂移时应失败。"""
    session = FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(200)])
    with pytest.raises(no.OfficialHTTPError):
        _client(session).request_text("GET", "https://example.cn/busy")
    assert len(session.calls) == 2


def test_polite_client_corrects_requests_latin1_default_for_utf8_html():
    """无 charset 的政府网页会被 requests 默认按 Latin-1 解码。"""
    raw = "部门文件标题".encode("utf-8")
    response = FakeResponse(
        200, raw.decode("latin-1"), content=raw, encoding="ISO-8859-1")
    assert _client(FakeSession([response])).request_text(
        "GET", "https://www.gov.cn/list") == "部门文件标题"


def test_validate_window_accepts_exactly_90_days():
    start, end = no.validate_window("20260601", "20260829")
    assert start.isoformat() == "2026-06-01"
    assert end.isoformat() == "2026-08-29"


def test_validate_window_rejects_over_90_days_and_reverse_range():
    with pytest.raises(ValueError, match="90 days"):
        no.validate_window("20260601", "20260830")
    with pytest.raises(ValueError, match="end_date"):
        no.validate_window("20260902", "20260901")


def test_decode_jsonp_accepts_exact_callback():
    payload = no.decode_jsonp(
        'probe([{"pageList":{"content":[]}}])', "probe")
    assert payload[0]["pageList"]["content"] == []


@pytest.mark.parametrize(
    "text", ["<html>302 Found</html>", "evil({})", "probe(not-json)"])
def test_decode_jsonp_rejects_challenge_wrong_callback_or_invalid_json(text):
    with pytest.raises(ValueError):
        no.decode_jsonp(text, "probe")


XINHUA_HTML = """
<ul>
  <li><a href="https://www.news.cn/fortune/20260901/abc123/c.html"
         title="政策标题">政策标题</a></li>
  <li><a href="https://www.news.cn/fortune/20260901/abc123/c.html">重复标题</a></li>
  <li><a href="https://www.news.cn/politics/20260901/not-fortune/c.html">排除</a></li>
</ul>
"""

GOV_HTML = """
<ul>
  <li><h4>
    <a href="../202608/content_7079000.htm">部门文件甲</a>
    <span class="date">2026-08-28</span>
  </h4></li>
</ul>
<a href="home_1.htm">下一页</a>
"""

GOV_PAGE_2 = """
<ul>
  <li><h4>
    <a href="../202607/content_7078000.htm">部门文件乙</a>
    <span class="date">2026-07-31</span>
  </h4></li>
</ul>
"""


def test_discover_xinhua_whitelists_fortune_links_and_deduplicates():
    """放宽 URL 白名单或去掉源内去重时会混入非财经/重复文章。"""
    client = FakeTextClient({no.XINHUA_URL: XINHUA_HTML})
    items = no.discover_xinhua(client)
    assert [(item.native_id, item.title, item.pub_time) for item in items] == [
        ("abc123", "政策标题", "2026-09-01 00:00:00")]
    assert items[0].source == "xinhua_fortune"


def test_discover_xinhua_filters_requested_date_window():
    client = FakeTextClient({no.XINHUA_URL: XINHUA_HTML})
    assert no.discover_xinhua(
        client, start=no.dt.date(2026, 8, 1), end=no.dt.date(2026, 8, 31)) == []


def test_discover_xinhua_skips_empty_image_duplicate_and_accepts_relative_url():
    """官网会先放空文本图片链接，正文链接还可能使用站内相对路径。"""
    html = """
    <a href="https://www.news.cn/fortune/20260902/a1/c.html"><img></a>
    <a href="https://www.news.cn/fortune/20260902/a1/c.html">绝对链接标题</a>
    <a href="/fortune/20260902/b2/c.html">相对链接标题</a>
    """
    client = FakeTextClient({no.XINHUA_URL: html})
    items = no.discover_xinhua(client)
    assert [(item.native_id, item.title) for item in items] == [
        ("a1", "绝对链接标题"), ("b2", "相对链接标题")]
    assert items[1].url == (
        "https://www.news.cn/fortune/20260902/b2/c.html")


def test_discover_xinhua_rejects_structure_drift():
    client = FakeTextClient({no.XINHUA_URL: "<html>新版空壳</html>"})
    with pytest.raises(no.OfficialStructureError, match="xinhua"):
        no.discover_xinhua(client)


def test_discover_gov_current_page_normalizes_url_and_date():
    """相对 URL 或日期关联解析错误会造成重复 ID/错年索引。"""
    client = FakeTextClient({no.GOV_BMWJ_URL: GOV_HTML})
    items = no.discover_gov_bmwj(client)
    assert len(items) == 1
    assert items[0].url == (
        "https://www.gov.cn/zhengce/zhengceku/202608/content_7079000.htm")
    assert items[0].native_id == "7079000"
    assert items[0].pub_time == "2026-08-28 00:00:00"


def test_discover_gov_backfill_follows_pages_and_stops_at_date_floor():
    page_2_url = "https://www.gov.cn/zhengce/zhengceku/bmwj/home_1.htm"
    client = FakeTextClient({no.GOV_BMWJ_URL: GOV_HTML, page_2_url: GOV_PAGE_2})
    items = no.discover_gov_bmwj(
        client, start=no.dt.date(2026, 8, 1), end=no.dt.date(2026, 8, 31),
        backfill=True)
    assert [item.native_id for item in items] == ["7079000"]
    assert [call[1] for call in client.calls] == [no.GOV_BMWJ_URL, page_2_url]


def test_discover_gov_rejects_structure_drift():
    client = FakeTextClient({no.GOV_BMWJ_URL: "<html>无文件链接</html>"})
    with pytest.raises(no.OfficialStructureError, match="gov_bmwj"):
        no.discover_gov_bmwj(client)


def test_hydrate_article_keeps_metadata_and_adds_clean_body(monkeypatch):
    item = no.OfficialItem(
        "gov_bmwj", "7079000", "标题", "2026-08-28 00:00:00",
        "https://www.gov.cn/zhengce/zhengceku/202608/content_7079000.htm")
    client = FakeTextClient({item.url: "<html>article</html>"})
    monkeypatch.setattr(no.fulltext, "_extract",
                        lambda _html, _url: "有效正文" * 80)
    hydrated = no.hydrate_article(client, item)
    assert hydrated.content.startswith("有效正文")
    assert hydrated.native_id == item.native_id
    assert hydrated.url == item.url


def test_hydrate_article_rejects_short_body(monkeypatch):
    item = no.OfficialItem(
        "xinhua_fortune", "abc123", "标题", "2026-09-01 00:00:00",
        "https://www.news.cn/fortune/20260901/abc123/c.html")
    client = FakeTextClient({item.url: "<html>stub</html>"})
    monkeypatch.setattr(no.fulltext, "_extract", lambda _html, _url: "过短")
    assert no.hydrate_article(client, item) is None


def test_supports_source_is_explicit():
    assert no.supports_source("xinhua_fortune") is True
    assert no.supports_source("gov_bmwj") is True
    assert no.supports_source("unknown") is False


SSE_HTML = """
<dl>
  <dd><span>2026-08-28</span>
    <a href="/regulation/trading/case/c/c_20260828_10830001.shtml"
       title="交易监管案例甲">交易监管案例甲</a>
  </dd>
  <dd><span>2026-08-27</span>
    <a href="/regulation/listing/disposition/c/10830002/files/action.pdf"
       title="纪律处分乙">纪律处分乙</a>
  </dd>
  <dd><span>2026-08-26</span>
    <a href="/disclosure/listedinfo/announcement/c/new-company.shtml"
       title="公司公告必须排除">公司公告必须排除</a>
  </dd>
  <dd><span>2026-08-28</span>
    <a href="/regulation/trading/case/c/c_20260828_10830001.shtml">重复</a>
  </dd>
</dl>
"""

SSE_MEASURES_JSONP = """officialProbe({
  "result": [{
    "docId": "10830003_28_10007",
    "docTitle": "上市公司监管措施丙",
    "createTime": "2026-08-25 18:00:00",
    "docURL": "www.sse.com.cn/disclosure/credibility/supervision/measures/c/10830003/files/action.pdf",
    "extWTFL": "监管警示",
    "extSECURITY_CODE": "600001"
  }],
  "pageHelp": {"pageCount": 1, "pageNo": 1, "total": 1}
})"""

MIIT_JSON = """{
  "code":"200","success":true,
  "data":{"searchResult":{"totalHits":1,"dataResults":[{
    "groupData":[{"data":{
      "metaid":"6fbc038bf15c445ab53b2a94a3f9d4e4",
      "title_text":"工业和信息化部办公厅关于开展专项行动的通知",
      "deploytime":"1788134400000",
      "url":"/zwgk/zcwj/wjfb/tz/art/2026/art_6fbc038bf15c445ab53b2a94a3f9d4e4.html",
      "columnname":"通知"
    }}]
  }]}}
}"""


def _sse_client(static_html=SSE_HTML, measures_jsonp=SSE_MEASURES_JSONP):
    payloads = {url: static_html for _category, url in no.SSE_SECTIONS[1:]}
    payloads[no.SSE_MEASURES_URL] = measures_jsonp
    return FakeTextClient(payloads)


def test_discover_sse_keeps_only_regulation_links_and_deduplicates():
    """误收公司公告或跨栏目重复时会污染监管口径。"""
    client = _sse_client()
    items = no.discover_sse_regulation(client)
    assert len(items) == 3
    assert {item.title for item in items} == {
        "上市公司监管措施丙", "交易监管案例甲", "纪律处分乙"}
    assert all(item.url.startswith("https://www.sse.com.cn/") for item in items)
    assert all("/disclosure/listedinfo/announcement/" not in item.url
               for item in items)
    assert all(item.category in item.content for item in items)

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("GET", no.SSE_MEASURES_URL)
    assert kwargs["params"]["sqlId"] == "BS_KCB_GGLL_NEW"
    assert kwargs["params"]["pageHelp.pageSize"] == 25


def test_discover_sse_filters_date_window():
    client = _sse_client()
    items = no.discover_sse_regulation(
        client, start=no.dt.date(2026, 8, 28), end=no.dt.date(2026, 8, 28))
    assert [item.title for item in items] == ["交易监管案例甲"]


def test_discover_sse_rejects_total_structure_drift():
    client = _sse_client(
        static_html="<html>新版空壳</html>",
        measures_jsonp='officialProbe({"result":[],"pageHelp":{"pageCount":1}})')
    with pytest.raises(no.OfficialStructureError, match="sse"):
        no.discover_sse_regulation(client)


def test_discover_sse_backfill_pages_dynamic_table_with_cap_contract():
    class PagingClient:
        def __init__(self):
            self.calls = []

        def request_text(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if url != no.SSE_MEASURES_URL:
                return "<html>静态栏目本页无数据</html>"
            page = kwargs["params"]["pageHelp.pageNo"]
            return f'''officialProbe({{
              "result":[{{"docId":"id-{page}","docTitle":"措施{page}",
              "createTime":"2026-08-2{page} 00:00:00",
              "docURL":"www.sse.com.cn/regulation/x/c/id-{page}.pdf"}}],
              "pageHelp":{{"pageCount":2,"pageNo":{page},"total":2}}
            }})'''

    client = PagingClient()
    items = no.discover_sse_regulation(client, backfill=True)
    assert [item.native_id for item in items] == ["id-1", "id-2"]
    pages = [call[2]["params"]["pageHelp.pageNo"]
             for call in client.calls if call[1] == no.SSE_MEASURES_URL]
    assert pages == [1, 2]


def test_discover_miit_policy_uses_current_file_release_search_api():
    client = FakeTextClient({no.MIIT_SEARCH_URL: MIIT_JSON})
    items = no.discover_miit_policy(client)
    assert [(item.native_id, item.pub_time, item.category) for item in items] == [
        ("6fbc038bf15c445ab53b2a94a3f9d4e4",
         "2026-08-31 00:00:00", "工信部文件发布/通知")]
    assert items[0].url.startswith(
        "https://www.miit.gov.cn/zwgk/zcwj/wjfb/")
    method, url, kwargs = client.calls[0]
    assert (method, url) == ("GET", no.MIIT_SEARCH_URL)
    assert kwargs["params"]["cateid"] == 58
    assert kwargs["params"]["pg"] == 10


def test_discover_miit_policy_backfill_pages_at_ten_rows_per_request():
    class PagingClient:
        def __init__(self):
            self.calls = []

        def request_text(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            page = kwargs["params"]["p"]
            payload = {
                "code": "200", "success": True,
                "data": {"searchResult": {
                    "totalHits": 11,
                    "dataResults": [{"groupData": [{"data": {
                        "metaid": f"id-{page}",
                        "title_text": f"文件{page}",
                        "deploytime": "1788134400000",
                        "url": ("/zwgk/zcwj/wjfb/tz/art/2026/"
                                f"art_id-{page}.html"),
                        "columnname": "通知",
                    }}]},
                ]}},
            }
            import json
            return json.dumps(payload, ensure_ascii=False)

    client = PagingClient()
    items = no.discover_miit_policy(client, backfill=True)
    assert [item.native_id for item in items] == ["id-1", "id-2"]
    assert [call[2]["params"]["p"] for call in client.calls] == [1, 2]


def test_miit_ids_use_article_urls_when_metaid_is_shared_or_changes():
    """线上 metaid 是共享分类值，不能作为文章主键。"""
    import json
    row = json.loads(MIIT_JSON)["data"]["searchResult"]["dataResults"][0]["groupData"][0]["data"]
    first = no._miit_item(dict(row, metaid="shared-category"))
    second = no._miit_item(dict(
        row, metaid="shared-category",
        url="/zwgk/zcwj/wjfb/tz/art/2026/art_another-article.html"))
    changed = no._miit_item(dict(row, metaid="changed-category"))
    assert first.native_id == "6fbc038bf15c445ab53b2a94a3f9d4e4"
    assert second.native_id == "another-article"
    assert changed.native_id == first.native_id


def test_miit_nonstandard_article_path_uses_url_hash_not_metaid():
    import hashlib
    rows = [dict(title="文件", deploytime="1788134400000", metaid="shared",
                 url=f"/zwgk/zcwj/wjfb/tz/{name}.html") for name in ("a", "b")]
    items = [no._miit_item(row) for row in rows]
    assert len({item.native_id for item in items}) == 2
    assert all(item.native_id == hashlib.sha1(item.url.encode()).hexdigest()
               for item in items)


class FakeBSEClient:
    def __init__(self, *, total_pages=1, message=None, bad_schema=False,
                 external_url=False):
        self.total_pages = total_pages
        self.message = message
        self.bad_schema = bad_schema
        self.external_url = external_url
        self.calls = []

    def request_text(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        data = kwargs["data"]
        callback = data["callback"]
        if self.message:
            return f'{callback}([{{"msg":"{self.message}"}}])'
        index = len(self.calls)
        page = int(data["page"])
        path = ("https://evil.example/report.pdf" if self.external_url else
                f"/uploads/6/file/public/202608/report-{index}-{page}.pdf")
        page_list = {
            "content": [{
                "id": 1000 + index * 10 + page,
                "title": f"北交所监管项目{index}-{page}",
                "publishDateStr": "2026-08-28",
                "filePath": path,
            }],
            "number": page,
            "totalPages": self.total_pages,
            "lastPage": page + 1 >= self.total_pages,
        }
        if self.bad_schema:
            page_list.pop("totalPages")
        import json
        return f"{callback}({json.dumps([{'pageList': page_list}])})"


def test_discover_bse_covers_categories_and_emits_exact_parameters():
    """漏掉任一类别或参数名漂移时官方接口会静默空/返回参数异常。"""
    client = FakeBSEClient()
    items = no.discover_bse_regulation(
        client, start=no.dt.date(2026, 8, 1), end=no.dt.date(2026, 9, 1))
    assert len(client.calls) == 6  # 五接口；交易监管含两个 tab
    assert len(items) == 6
    categories = {item.category for item in items}
    assert {"监管动态", "自律监管措施", "纪律处分", "限制交易", "监管案例", "复核"} == categories
    assert len({item.native_id for item in items}) == 6
    for method, url, kwargs in client.calls:
        assert method == "POST"
        assert url.startswith("https://www.bse.cn/")
        data = kwargs["data"]
        assert data["siteId"] == "6"
        assert data["page"] == 0
        assert data["startTime"] == "2026-08-01"
        assert data["endTime"] == "2026-09-01"
        assert data["callback"] == "officialProbe"
    trade_types = [call[2]["data"]["superviseType"] for call in client.calls
                   if "tradeSuperviseController" in call[1]]
    assert trade_types == ["0", "1"]
    announcement_values = [call[2]["data"].get("announcementYn")
                           for call in client.calls]
    assert "1" in announcement_values and "2" in announcement_values


def test_discover_bse_backfill_follows_zero_based_pages():
    client = FakeBSEClient(total_pages=2)
    items = no.discover_bse_regulation(client, backfill=True)
    assert len(items) == 12
    endpoint_pages = {}
    for _method, url, kwargs in client.calls:
        endpoint_pages.setdefault((url, kwargs["data"].get("superviseType")), []).append(
            kwargs["data"]["page"])
    assert all(pages == [0, 1] for pages in endpoint_pages.values())


def test_discover_bse_rejects_parameter_error_and_schema_drift():
    with pytest.raises(no.OfficialStructureError, match="请求参数异常"):
        no.discover_bse_regulation(FakeBSEClient(message="请求参数异常"))
    with pytest.raises(no.OfficialStructureError, match="pageList"):
        no.discover_bse_regulation(FakeBSEClient(bad_schema=True))


def test_discover_bse_rejects_nonofficial_file_urls():
    items = no.discover_bse_regulation(FakeBSEClient(external_url=True))
    assert items == []


def test_discover_source_dispatch_and_backfill_set():
    assert no.supports_source("sse_regulation") is True
    assert no.supports_source("bse_regulation") is True
    assert no.BACKFILL_SOURCE_IDS == {
        "miit_policy", "xinhua_fortune", "gov_bmwj", "govcn_gwy",
        "sse_regulation", "bse_regulation"}


def test_smoke_source_sse_fetches_only_one_section(monkeypatch):
    """单源冒烟是轻量探针，不应扫描上交所全部栏目。"""
    client = FakeTextClient({no.SSE_MEASURES_URL: SSE_MEASURES_JSONP})
    monkeypatch.setattr(no, "PoliteClient", lambda **_kwargs: client)

    result = no.smoke_source(SimpleNamespace(
        id="sse_regulation", timeout=30))

    assert "1 条" in result
    assert "上市公司监管措施" in result
    assert len(client.calls) == 1


def test_smoke_source_bse_fetches_only_one_request_group(monkeypatch):
    """北交所冒烟最多触发一个公开接口请求组。"""
    client = FakeBSEClient()
    monkeypatch.setattr(no, "PoliteClient", lambda **_kwargs: client)

    result = no.smoke_source(SimpleNamespace(
        id="bse_regulation", timeout=30))

    assert "1 条" in result
    assert "监管动态" in result
    assert len(client.calls) == 1


def test_smoke_source_miit_fetches_only_one_search_page(monkeypatch):
    client = FakeTextClient({no.MIIT_SEARCH_URL: MIIT_JSON})
    monkeypatch.setattr(no, "PoliteClient", lambda **_kwargs: client)
    result = no.smoke_source(SimpleNamespace(id="miit_policy", timeout=30))
    assert "1 条" in result
    assert "工信部文件发布" in result
    assert len(client.calls) == 1
