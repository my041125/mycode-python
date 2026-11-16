"""
联网搜索（对应 Java 版 WebSearchTool / SearchProvider / TavilySearchProvider /
BochaSearchProvider / MockSearchProvider / SearchResult 六个文件）。

把「搜索服务」包装成内核认识的 Tool。三件必须做的事，缺一件线上就会出问题：
    1. 条数写死上限（top_k），不能让模型一次要 50 条把上下文冲爆；
    2. 结果按固定模板格式化，并明确要求模型用 [编号] 引用，
       否则它会把搜索结果当成自己本来就懂的知识，用户没法核实；
    3. 加 TTL 缓存，同一个 query 短时间内重复搜索既烧钱又慢。

关键认知：搜索是「工具」，不是「模型能力」。
做成工具，条数、超时、缓存、来源全部可控；用模型内置联网，这些都管不了。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from mycode.kernel.tool import Tool
from mycode.clients import net

# 缓存有效期，单位秒。10 分钟内的相同 query 直接走缓存
TTL_SECONDS = 600

# 缓存最大条数，超出按 LRU 淘汰最久未使用的
MAX_CACHE = 64

# 单条摘要的字符上限。三条各 220 字，多搜几次上下文也不会满
SNIPPET_LIMIT = 220

# 搜索接口超时秒数
_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class SearchResult:
    """一条搜索结果。

    三个字段都不可省：title 让模型知道这条讲什么；url 让用户能核实来源；
    snippet 是喂给模型的正文，太长会撑爆上下文，由使用方截断。
    """

    title: str
    url: str
    snippet: str

    @classmethod
    def of(cls, title: str | None, url: str | None, snippet: str | None) -> "SearchResult":
        """宽松构造：各家服务商的字段名和小毛病都不一样
        （标题可能是 None、摘要里可能有一堆换行），统一在这里收敛掉。"""
        return cls(
            "(无标题)" if not title else title.strip(),
            "" if not url else url.strip(),
            # 把连续空白压成单个空格，避免摘要里一堆换行把上下文切碎
            "" if not snippet else re.sub(r"\s+", " ", snippet.strip()),
        )


class SearchProvider:
    """搜索能力的抽象。内核只认这个接口，换服务商改一行装配代码即可。

    注意 search 签名里的 top_k：结果条数必须由调用方（WebSearchTool）决定，
    绝不能让模型自己传 —— 模型要 50 条的时候，上下文基本就废了。
    """

    def name(self) -> str:
        """服务商名称，会出现在工具返回结果里，方便排查到底是谁在搜。"""
        raise NotImplementedError

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """执行搜索。没搜到返回空列表（不要返回 None，也别抛异常）。"""
        raise NotImplementedError


class MockSearchProvider(SearchProvider):
    """离线假搜索：不联网也能验证「搜索 → 引用」整条链路。

    两个作用：开发调试时省额度；做回归测试时返回固定数据集，输出可预期。
    真实项目里应该把它做成「录制回放」：把线上真实响应录下来，
    每次改完代码跑一遍，确认 agent 的最终回答没变差。
    """

    def name(self) -> str:
        return "离线模拟"

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """返回写死的三条零陵区旅游数据；关键词不匹配时返回空列表。"""
        if "柳子庙" in query or "零陵" in query:
            results = [
                SearchResult.of(
                    "柳子庙 - 零陵区人民政府官网",
                    "https://www.lingling.gov.cn/spot/liuzimiao",
                    "柳子庙位于永州市零陵区潇水西岸，为纪念唐代文学家柳宗元而建。"
                    "开放时间 08:00-17:30，成人票 60 元，学生凭学生证半价。"),
                SearchResult.of(
                    "零陵古城游玩攻略（2026 最新）",
                    "https://travel.example.com/lingling-guide",
                    "零陵古城免大门票，内部小景点单独收费。夜间灯光秀每周五、六 19:30 开始，"
                    "持续约 40 分钟。建议傍晚入园，白天逛柳子庙、晚上看灯光秀。"),
                SearchResult.of(
                    "周家大院修缮后重新开放",
                    "https://news.example.com/zhoujiadayuan",
                    "零陵区周家大院完成修缮，2026 年 3 月起恢复开放，门票 35 元，每日限流 800 人。"),
            ]
            # 按 top_k 截断，模拟真实服务商的行为，顺便验证上层传参是否生效
            return results[:max(0, min(top_k, len(results)))]
        return []


class HttpSearchProvider(SearchProvider):
    """走「POST JSON + Bearer 鉴权」的搜索服务商基类。

    Tavily 与博查的连接、鉴权、错误处理完全同构，只有端点和响应结构不同 ——
    公共的收发部分收在这里，子类只管组装请求体和解析结果。
    """

    # 子类覆盖：完整的搜索接口地址
    ENDPOINT = ""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key  # Bearer 鉴权的 API Key

    def _post(self, body: dict) -> dict:
        """发搜索请求并解析 JSON 响应；非 200 抛带状态码与响应体摘要的异常。"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api_key,
        }
        conn, path = net.open_connection(self.ENDPOINT, _TIMEOUT_SECONDS)
        try:
            conn.request("POST", path, body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                # 把响应体前 200 字带上，否则排查 401/429 时完全看不出原因
                raise RuntimeError(self.name() + " HTTP " + str(resp.status) + ": "
                                   + net.read_body(resp)[:200])
            return json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()


class TavilySearchProvider(HttpSearchProvider):
    """Tavily 搜索服务商实现。

    特点：专为 LLM 设计，返回的 content 字段已经是提炼过的片段，
    不需要自己抓网页再清洗，接入成本最低。
    代价：国内访问偶尔不稳，通常要配代理；中文网页覆盖不如博查。

    报文要点：
        请求  POST /search  {"query":..., "max_results":N, "search_depth":"basic"}
        鉴权  Authorization: Bearer tvly-xxx
        响应  results[] 里每个元素含 title / url / content
    """

    ENDPOINT = "https://api.tavily.com/search"

    def name(self) -> str:
        return "Tavily"

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """调用 Tavily 搜索接口。

        search_depth=basic —— advanced 更慢且更贵，agent 循环里等不起；
        include_answer=false —— 不需要它自带的问答摘要，要的是原始来源链接。
        """
        body = {
            "query": query,
            "max_results": min(top_k, 10),   # 二次上限保护，即使上层传了更大的值也不会失控
            "search_depth": "basic",
            "include_answer": False,
        }
        root = self._post(body)
        return [SearchResult.of(r.get("title"), r.get("url"), r.get("content"))
                for r in root.get("results") or []]


class BochaSearchProvider(HttpSearchProvider):
    """博查（Bocha）搜索服务商实现。

    为什么中文场景优先选它：国内可直连、中文网页覆盖好、返回的 summary
    已是提炼过的摘要，省掉自己清洗网页的活。申请地址：https://open.bochaai.com

    报文要点：
        请求  POST /v1/web-search  {"query":..., "summary":true, "count":N}
        响应  data.webPages.value[] 里每个元素含 name / url / snippet / summary
    """

    ENDPOINT = "https://api.bochaai.com/v1/web-search"

    def name(self) -> str:
        return "博查"

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """调用博查搜索接口。summary=true 让服务端返回提炼后的摘要。"""
        body = {
            "query": query,
            "summary": True,
            "count": min(top_k, 10),   # 二次上限保护
        }
        root = self._post(body)
        out: list[SearchResult] = []
        for r in ((root.get("data") or {}).get("webPages") or {}).get("value") or []:
            # summary 是模型提炼过的长摘要，snippet 是原始片段，优先用 summary
            text = r.get("summary") or ""
            if not text.strip():
                text = r.get("snippet") or ""
            out.append(SearchResult.of(r.get("name"), r.get("url"), text))
        return out


class _CacheEntry:
    """一条缓存记录：格式化好的文本 + 过期时间戳。"""

    def __init__(self, text: str, expire_at: float) -> None:
        self.text = text
        self.expire_at = expire_at

    def alive(self, now: float) -> bool:
        """是否还在有效期内。"""
        return now < self.expire_at


class WebSearchTool(Tool):
    """把搜索服务包装成工具：校验 → 查缓存 → 调服务商 → 格式化 → 回写缓存。"""

    def __init__(self, provider: SearchProvider, top_k: int) -> None:
        self._provider = provider
        self._top_k = top_k          # 每次搜索返回几条。代码里定死，不暴露给模型
        # OrderedDict 实现 LRU：命中时 move_to_end，超出容量淘汰最老一条
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()  # 并行工具执行时会并发进来

    def name(self) -> str:
        return "web_search"

    def description(self) -> str:
        """写得好的关键：既要说「什么时候用」，也要说「什么时候别用」。

        这里明确拦住了「查系统内部数据」的场景，
        否则模型会用搜索去查项目里本来就有的东西。
        """
        return ("联网搜索公开网页。用于查询实时信息：新闻、活动、开放时间、门票价格变动、交通等。"
                "项目内部数据不要用它，应该用 read_code / list_files 这类内部工具。")

    def parameters(self) -> dict:
        """参数只有一个 query，且必填。"""
        return Tool.schema({
            "query": Tool.str_param("搜索关键词，用自然语言短句，例如「零陵古城 夜间灯光秀 时间」"),
        }, "query")

    def run(self, args: dict) -> str:
        """执行搜索。

        格式化模板是刻意设计的，包含三段信息：
            [编号] 标题   —— 让模型能引用
            URL          —— 让用户能核实
            摘要         —— 让模型有内容可用
        末尾那句「引用要求」不是装饰：不写这句，模型十有八九会把搜索内容当自己的话讲。
        """
        # 1. 参数校验：模型偶尔会漏传或传空串
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")

        # 2. 查缓存
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(query)
            if hit is not None and hit.alive(now):
                self._cache.move_to_end(query)
                return hit.text + "\n（来自缓存，未再次请求搜索服务）"

        # 3. 调服务商
        results = self._provider.search(query, self._top_k)
        if not results:
            # 没搜到也要给出可执行的建议，别只回一句「无结果」
            return f"检索「{query}」没有返回结果。建议：换更短的关键词、去掉限定词，或直接告知用户未查到。"

        # 4. 按固定模板格式化
        lines = [f"检索「{query}」共 {len(results)} 条（来源: {self._provider.name()}）："]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title}")
            lines.append(f"    URL: {r.url}")
            lines.append(f"    摘要: {self._snippet(r.snippet)}")
        lines.append("引用要求：回答时用 [1][2] 标注来源，并附上 URL；摘要没有覆盖的细节不要臆测。")
        text = "\n".join(lines)

        # 5. 回写缓存再返回（OrderedDict 超容量时弹最老一条）
        with self._lock:
            self._cache[query] = _CacheEntry(text, time.monotonic() + TTL_SECONDS)
            while len(self._cache) > MAX_CACHE:
                self._cache.popitem(last=False)
        return text

    @staticmethod
    def _snippet(s: str | None) -> str:
        """截断单条摘要，避免某条结果特别长把其它结果挤掉。"""
        if not s:
            return ""
        return s if len(s) <= SNIPPET_LIMIT else s[:SNIPPET_LIMIT] + "…"
