#!/usr/bin/env python
"""fastmcp-proxy-bridge: 使用 fastmcp 构建的 SSE → STDIO 透明代理 MCP Server

核心需求：
- 让只支持本地 STDIO MCP 的客户端透明访问远程 SSE + /mcp HTTP 服务器。
- 不自造协议轮子，完全复用 fastmcp 的 Client + FastMCP.as_proxy 能力。

实现思路：
1. 解析 CLI / 环境变量，得到远程 SSE URL；
2. 处理 header 模板、环境变量占位符、额外 header；
3. 可选设置代理（简单：写入环境变量，交由 httpx 自动处理）；
4. 构建 fastmcp.client.transports.SSETransport；
5. 创建 fastmcp.Client；
6. FastMCP.as_proxy(client, ...) → 得到 FastMCPProxy；
7. 以 transport="stdio" 运行，向上层暴露标准 MCP STDIO 服务。

导出变量：mcp
- 以便 fastmcp install / Claude / Cursor 等自动发现。

环境变量支持：
- MCP_REMOTE_SSE: 远程 SSE URL (若 CLI 未提供)
- MCP_HEADER_TEMPLATE / MCP_HEADER_FILE: header 模板（JSON）
- 额外 headers: --header k=v 可多次
- HTTP(S) 代理：--proxy 或自行 export HTTP_PROXY / HTTPS_PROXY
- SOCKS 代理：--socks 或自行 export ALL_PROXY

占位符替换：值中支持 ${ENV_NAME} -> os.environ['ENV_NAME']。

关键实现要点（回答“模板在哪里被解析 / 代理变量如何生效”）：
1. Header 模板解析链：`MCP_HEADER_TEMPLATE` 或 `--header-template` -> `load_header_template()` JSON 解析 -> `apply_env()` 用正则替换 `${VAR}` -> `merge_headers()` 合并 `--header k=v` -> 传入 `SSETransport(headers=...)`。
2. 代理变量生效：若提供 `--proxy`/`--socks` 并且对应标准变量未设置，则写入 `HTTP_PROXY`/`HTTPS_PROXY` 或 `ALL_PROXY`；`SSETransport` 内部使用 httpx，默认 `trust_env=True` 自动读取这些变量，无需显式传递。

License: MIT
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import anyio
import httpx
from fastmcp import FastMCP
from fastmcp import settings as fastmcp_settings
from fastmcp.client.client import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from mcp.shared._httpx_utils import create_mcp_http_client

VERSION = "0.2.0"


# ---------------- Data Model ----------------
@dataclass
class Options:
    sse: Optional[str] = None
    header_template: Optional[str] = None
    header_file: Optional[str] = None
    headers: list[str] = field(default_factory=list)
    proxy: Optional[str] = None
    socks: Optional[str] = None
    name: str = "Proxy Bridge"
    instructions: Optional[str] = None
    log_level: Optional[str] = None
    show_banner: bool = True
    # timeouts / retries
    connect_timeout: Optional[float] = None  # TCP/TLS 建连 + 首字节
    request_timeout: Optional[float] = None  # MCP 请求整体（Client(timeout=...))
    sse_read_timeout: Optional[float] = None  # SSETransport 内部 sse_read_timeout
    retries: int = 1
    retry_backoff: float = 2.0  # 指数退避基数（秒）
    retry_min: Optional[float] = None  # 最小退避秒数（可将指数退避夹在 [min,max] 内）
    retry_max: Optional[float] = None  # 最大退避秒数
    retry_jitter: float = 0.0  # 抖动比例（0-1），例如 0.2 表示 ±20%
    # request-level retries（运行期每个 MCP 调用）
    request_retries: int = 0
    request_retry_backoff: float = 1.0
    request_retry_min: Optional[float] = None
    request_retry_max: Optional[float] = None
    request_retry_jitter: float = 0.0
    # transport & http2
    transport: str = "sse"  # sse | streamable-http | auto
    disable_http2: bool = False


# ---------------- Header Helpers ----------------


def load_header_template(opts: Options) -> Dict[str, str]:
    raw = opts.header_template or os.getenv("MCP_HEADER_TEMPLATE")
    if not raw and (opts.header_file or os.getenv("MCP_HEADER_FILE")):
        file_path = opts.header_file or os.getenv("MCP_HEADER_FILE")
        if file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("header template must be JSON object")
        return {str(k): str(v) for k, v in obj.items()}
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] header-template parse error: {e}", file=sys.stderr)
        return {}


def apply_env(headers: Dict[str, str]) -> Dict[str, str]:
    import re

    out: Dict[str, str] = {}
    for k, v in headers.items():

        def repl(match: "re.Match[str]") -> str:  # type: ignore
            name = match.group(1)
            return os.getenv(name, "")

        out[k] = re.sub(r"\$\{([A-Z0-9_]+)\}", repl, v)
    return out


def merge_headers(tmpl: Dict[str, str], extra_pairs: list[str]) -> Dict[str, str]:
    merged = dict(tmpl)
    for pair in extra_pairs:
        if "=" in pair:
            k, v = pair.split("=", 1)
            merged[k] = v
    return merged


# ---------------- Core Build ----------------


async def _probe_client(client: Client) -> None:
    """进行一次轻量握手 + list_tools 以验证远程可达。

    失败抛出原始异常供上层重试。
    """
    async with client:
        # list_tools 触发初始化握手；若服务器空列表也算成功
        try:
            await client.list_tools()
        except Exception:
            # 仍算握手成功（工具列出失败并不一定阻塞调用），但保留继续向外抛出更严重错误
            raise


def build_proxy(opts: Options) -> FastMCP:
    sse_url = opts.sse or os.getenv("MCP_REMOTE_SSE")
    if not sse_url:
        raise SystemExit("必须通过 --sse 或 MCP_REMOTE_SSE 指定远程 SSE URL")

    # headers
    tmpl = load_header_template(opts)
    tmpl_env = apply_env(tmpl)
    headers = merge_headers(tmpl_env, opts.headers)

    # proxy env injection (让 httpx 自动识别)
    if opts.proxy and not os.getenv("HTTP_PROXY"):
        os.environ["HTTP_PROXY"] = opts.proxy
        os.environ.setdefault("HTTPS_PROXY", opts.proxy)
    if opts.socks and not os.getenv("ALL_PROXY"):
        os.environ["ALL_PROXY"] = opts.socks

    # 定制 httpx client factory 以支持 connect_timeout / request_timeout
    httpx_client_factory = None
    if opts.connect_timeout or opts.request_timeout:

        def factory(headers: dict[str, str] | None = None, timeout=None, auth=None):  # type: ignore[override]
            # 基于用户参数构造 Timeout；若仅设置 connect_timeout 则给一个合理 total
            total = opts.request_timeout or 30.0
            connect = opts.connect_timeout
            # read 阶段若用户提供 request_timeout 就同时设 read；否则交由 httpx 默认
            httpx_timeout = httpx.Timeout(
                total, connect=connect, read=opts.request_timeout
            )
            # 若需要禁用 HTTP/2，手动创建 AsyncClient；否则沿用 MCP 默认工厂
            if opts.disable_http2:
                return httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx_timeout,
                    auth=auth,
                    follow_redirects=True,
                    http2=False,
                )
            else:
                return create_mcp_http_client(
                    headers=headers, timeout=httpx_timeout, auth=auth
                )

        httpx_client_factory = factory

    def make_transport_sse() -> SSETransport:
        return SSETransport(
            url=sse_url,
            headers=headers,
            sse_read_timeout=opts.sse_read_timeout,
            httpx_client_factory=httpx_client_factory,
        )

    def make_transport_streamable() -> StreamableHttpTransport:
        return StreamableHttpTransport(
            url=sse_url,
            headers=headers,
            sse_read_timeout=opts.sse_read_timeout,
            httpx_client_factory=httpx_client_factory,
        )

    attempt = 0
    last_err: Exception | None = None
    chosen = opts.transport
    while attempt < max(1, opts.retries):
        attempt += 1
        if chosen == "sse":
            transport = make_transport_sse()
        elif chosen == "streamable-http":
            transport = make_transport_streamable()
        else:  # auto -> 首选 SSE
            transport = make_transport_sse()
        client = Client(transport, timeout=opts.request_timeout)
        try:
            # 进行一次探测（同步环境下用 anyio.run）
            anyio.run(_probe_client, client)
            if attempt > 1:
                print(
                    f"[bridge] probe success after {attempt} attempts", file=sys.stderr
                )
            break
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ) as e:  # noqa: PERF203
            last_err = e
            if attempt >= max(1, opts.retries):
                print(
                    f"[bridge] probe failed (attempt {attempt}): {e}", file=sys.stderr
                )
                # auto 模式下对 RemoteProtocolError 尝试回退到 streamable-http
                if opts.transport == "auto" and isinstance(
                    e, httpx.RemoteProtocolError
                ):
                    print(
                        "[bridge] auto: falling back to streamable-http",
                        file=sys.stderr,
                    )
                    chosen = "streamable-http"
                    attempt = 0
                    continue
                raise SystemExit(f"无法建立连接: {e}")
            # Annealing/exponential backoff with optional jitter and min/max clamp
            backoff = opts.retry_backoff * (2 ** (attempt - 1))
            if opts.retry_jitter and opts.retry_jitter > 0:
                # 在 [1-j, 1+j] 范围内随机抖动
                j = max(0.0, min(1.0, opts.retry_jitter))
                factor = random.uniform(1.0 - j, 1.0 + j)
                backoff *= factor
            if opts.retry_min is not None:
                backoff = max(backoff, float(opts.retry_min))
            if opts.retry_max is not None:
                backoff = min(backoff, float(opts.retry_max))
            print(
                f"[bridge] probe failed (attempt {attempt}/{opts.retries}): {e}; retry in {backoff:.1f}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
            continue
        except Exception as e:  # 其它异常直接退出
            print(f"[bridge] unexpected probe error: {e}", file=sys.stderr)
            raise

    else:
        # 理论上不会到这里
        if last_err:
            raise SystemExit(f"无法建立 SSE 连接: {last_err}")

    # 探测完成后重用最后构造的 client（探测时上下文已关闭，后续会重新建立 session）
    # 包装 request 级别重试
    proxy_client: Client | RetryingClient
    if opts.request_retries and opts.request_retries > 0:
        proxy_client = RetryingClient(client, opts)
    else:
        proxy_client = client

    proxy = FastMCP.as_proxy(
        proxy_client,  # type: ignore[arg-type]
        name=opts.name,
        instructions=opts.instructions,
        version=VERSION,
    )
    return proxy  # type: ignore[return-value]


# ---------------- Request-level Retrier ----------------


def _calc_backoff(
    base: float,
    attempt: int,
    min_s: Optional[float],
    max_s: Optional[float],
    jitter: float,
) -> float:
    backoff = base * (2 ** (attempt - 1))
    if jitter and jitter > 0:
        j = max(0.0, min(1.0, jitter))
        factor = random.uniform(1.0 - j, 1.0 + j)
        backoff *= factor
    if min_s is not None:
        backoff = max(backoff, float(min_s))
    if max_s is not None:
        backoff = min(backoff, float(max_s))
    return backoff


_TRANSIENT_HTTPX_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


class RetryingClient:
    """一个简单的 duck-typed Client 包装器，对所有异步方法做重试。

    仅对被认为是瞬态网络错误的 httpx 异常进行重试；其它异常直接透传。
    """

    def __init__(self, inner: Client, opts: Options):
        self._inner = inner
        self._opts = opts

    # ---- context manager ----
    async def __aenter__(self):  # noqa: D401
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: D401
        return await self._inner.__aexit__(exc_type, exc, tb)

    # ---- attribute proxy with async call retry ----
    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if inspect.iscoroutinefunction(attr) or (
            inspect.ismethod(attr) and inspect.iscoroutinefunction(attr.__func__)  # type: ignore[attr-defined]
        ):

            async def wrapper(*args, **kwargs):
                attempts = max(1, int(self._opts.request_retries))
                last_err: Optional[BaseException] = None
                for i in range(1, attempts + 1):
                    try:
                        return await attr(*args, **kwargs)
                    except _TRANSIENT_HTTPX_ERRORS as e:  # noqa: PERF203
                        last_err = e
                        if i >= attempts:
                            raise
                        backoff = _calc_backoff(
                            self._opts.request_retry_backoff,
                            i,
                            self._opts.request_retry_min,
                            self._opts.request_retry_max,
                            self._opts.request_retry_jitter,
                        )
                        print(
                            f"[bridge] request retry {i}/{attempts} in {backoff:.1f}s due to: {e}",
                            file=sys.stderr,
                        )
                        await anyio.sleep(backoff)
                        continue
                if last_err:
                    raise last_err

            return wrapper
        return attr


# ---------------- CLI ----------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("fastmcp-proxy-bridge", add_help=False)
    p.add_argument(
        "--sse", dest="sse", help="远程 SSE /mcp 事件流地址 (必填或用环境变量)"
    )
    p.add_argument(
        "--name", dest="name", help="本地暴露的 server 名称", default="Proxy Bridge"
    )
    p.add_argument(
        "--instructions", dest="instructions", help="本地 server instructions"
    )
    p.add_argument(
        "--header",
        dest="headers",
        action="append",
        default=[],
        help="额外 header k=v 可多次",
    )
    p.add_argument(
        "--header-template", dest="header_template", help="内联 JSON header 模板"
    )
    p.add_argument("--header-file", dest="header_file", help="JSON header 模板文件路径")
    p.add_argument(
        "--proxy", dest="proxy", help="HTTP/HTTPS 代理，如 http://127.0.0.1:7890"
    )
    p.add_argument(
        "--socks", dest="socks", help="SOCKS5 代理，如 socks5://127.0.0.1:1080"
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        help="日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)，或使用环境变量 FASTMCP_LOG_LEVEL",
    )
    p.add_argument(
        "--no-banner",
        dest="show_banner",
        action="store_false",
        help="禁用启动时的 banner 输出",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        dest="connect_timeout",
        help="SSE 连接阶段（TCP/TLS 首包）超时秒数；默认使用 httpx 默认值 (≈30s)",
    )
    p.add_argument(
        "--request-timeout",
        type=float,
        dest="request_timeout",
        help="单个 MCP 请求总超时秒数（Client timeout）",
    )
    p.add_argument(
        "--sse-read-timeout",
        type=float,
        dest="sse_read_timeout",
        help="SSE 空闲读取超时（无事件间隔），秒；默认使用库内默认值",
    )
    p.add_argument(
        "--retries",
        type=int,
        dest="retries",
        default=1,
        help="初始探测重试次数（含首次）。默认 1 不重试",
    )
    p.add_argument(
        "--retry-backoff",
        type=float,
        dest="retry_backoff",
        default=2.0,
        help="重试指数退避基数秒 (实际间隔 = base * 2^(attempt-1))",
    )
    p.add_argument(
        "--retry-min",
        type=float,
        dest="retry_min",
        help="退避下限（秒）；与指数退避组合，最终等待会被夹在 [min, max] 范围内",
    )
    p.add_argument(
        "--retry-max",
        type=float,
        dest="retry_max",
        help="退避上限（秒）；典型如 30 与 --retry-min 5 组合形成 5-30s 退火窗口",
    )
    p.add_argument(
        "--retry-jitter",
        type=float,
        dest="retry_jitter",
        default=0.0,
        help="退避抖动比例（0-1），例如 0.2 表示在 ±20% 范围内随机波动",
    )
    # request-level retry controls
    p.add_argument(
        "--request-retries",
        type=int,
        dest="request_retries",
        default=0,
        help="运行期每个 MCP 请求的重试次数（含首次）。默认 0 不重试",
    )
    p.add_argument(
        "--request-retry-backoff",
        type=float,
        dest="request_retry_backoff",
        default=1.0,
        help="请求级指数退避基数秒",
    )
    p.add_argument(
        "--request-retry-min",
        type=float,
        dest="request_retry_min",
        help="请求级退避下限（秒）",
    )
    p.add_argument(
        "--request-retry-max",
        type=float,
        dest="request_retry_max",
        help="请求级退避上限（秒）",
    )
    p.add_argument(
        "--request-retry-jitter",
        type=float,
        dest="request_retry_jitter",
        default=0.0,
        help="请求级退避抖动比例（0-1）",
    )
    p.add_argument(
        "--transport",
        choices=["sse", "streamable-http", "auto"],
        default="sse",
        dest="transport",
        help="远端传输协议选择：SSE 或 Streamable HTTP；auto 模式在 SSE 失败时回退",
    )
    p.add_argument(
        "--no-http2",
        dest="disable_http2",
        action="store_true",
        help="禁用 HTTP/2（部分代理/服务在 HTTP/2 下不稳定）",
    )
    p.add_argument("-h", "--help", action="help")
    return p


def main():  # pragma: no cover
    parser = build_parser()
    args = parser.parse_args()
    opts = Options(
        sse=args.sse,
        header_template=args.header_template,
        header_file=args.header_file,
        headers=args.headers,
        proxy=args.proxy,
        socks=args.socks,
        name=args.name or "Proxy Bridge",
        instructions=args.instructions,
        log_level=args.log_level,
        show_banner=args.show_banner,
        connect_timeout=args.connect_timeout,
        request_timeout=args.request_timeout,
        sse_read_timeout=args.sse_read_timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        retry_min=args.retry_min,
        retry_max=args.retry_max,
        retry_jitter=args.retry_jitter,
        request_retries=args.request_retries,
        request_retry_backoff=args.request_retry_backoff,
        request_retry_min=args.request_retry_min,
        request_retry_max=args.request_retry_max,
        request_retry_jitter=args.request_retry_jitter,
        transport=args.transport,
        disable_http2=args.disable_http2,
    )
    print(
        f"[bridge] start v{VERSION} name={opts.name} sse={opts.sse or os.getenv('MCP_REMOTE_SSE')}",
        file=sys.stderr,
    )
    # 设置日志级别：优先 CLI -> 环境变量 FASTMCP_LOG_LEVEL（由 pydantic Settings 处理）
    if opts.log_level:
        try:
            fastmcp_settings.log_level = opts.log_level.upper()
        except Exception as e:  # noqa: BLE001
            print(f"[bridge] 无效的 log_level '{opts.log_level}': {e}", file=sys.stderr)
    proxy = build_proxy(opts)
    proxy.run(transport="stdio", show_banner=opts.show_banner)


if __name__ == "__main__":  # pragma: no cover
    main()

# 导出 mcp 供 fastmcp install / 其它启动器发现；如果没有 URL，仅创建占位 server
try:
    _default_url = os.getenv("MCP_REMOTE_SSE")
    if _default_url:
        mcp = build_proxy(Options(sse=_default_url))  # type: ignore
    else:
        mcp = FastMCP(name="Bridge Placeholder")
except Exception:
    mcp = FastMCP(name="Bridge Placeholder")
