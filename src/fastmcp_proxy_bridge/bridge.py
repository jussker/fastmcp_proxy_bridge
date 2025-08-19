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
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastmcp import FastMCP
from fastmcp import settings as fastmcp_settings
from fastmcp.client.client import Client
from fastmcp.client.transports import SSETransport

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

    transport = SSETransport(url=sse_url, headers=headers)
    client = Client(transport)
    proxy = FastMCP.as_proxy(
        client,
        name=opts.name,
        instructions=opts.instructions,
        version=VERSION,
    )
    return proxy  # type: ignore[return-value]


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
