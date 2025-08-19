# fastmcp-proxy-bridge

基于 [fastmcp](https://github.com/jlowin/fastmcp) 的 **SSE → STDIO 透明代理**。让只支持本地 STDIO MCP 的客户端（Claude Code / Cursor / VS Code MCP 插件等）可以配置 HTTP_PROXY 后，访问远程 SSE + /mcp 服务。

## 特性
- 0 自行协议实现：直接复用 fastmcp `SSETransport` + `FastMCP.as_proxy()`。
- Header 模板 + 环境变量占位 `${VAR}` 动态注入（`MCP_HEADER_TEMPLATE` / `MCP_HEADER_FILE`）。
- 多次 `--header k=v` 追加覆盖。
- 支持通过 `--proxy` / `--socks` 或标准 `HTTP_PROXY` 与 `HTTPS_PROXY` / `ALL_PROXY` 代理。
- 与 fastmcp 安装器兼容：导出模块级 `mcp` 变量。

## 安装（本地开发）
```bash
uv pip install -e fastmcp_proxy_bridge
```

## 运行示例

或通过 VS Code `.vscode/mcp.json`：
```jsonc
"jina-mcp-server-py": {
  "command": "uv",
  "args": ["run","python","-m","fastmcp_proxy_bridge.bridge"],
  "env": {
    "JINA_API_KEY": "${input:jina-key}",
    "MCP_REMOTE_SSE": "https://mcp.jina.ai/sse",
    "MCP_HEADER_TEMPLATE": "{\"Authorization\":\"Bearer ${JINA_API_KEY}\"}",
    "HTTP_PROXY": "http://127.0.0.1:8890",
    "HTTPS_PROXY": "http://127.0.0.1:8890"
  }
}
```

## 变量与解析流程
1. 读取 `--header-template` 或 `MCP_HEADER_TEMPLATE` JSON。
2. `apply_env()` 用正则替换 `${VAR}`。
3. 合并 `--header` 追加项。
4. 生成 `SSETransport(headers=...)` 供 MCP 会话使用。

## 代理行为
- 若传 `--proxy` 且未显式设置 `HTTP_PROXY` / `HTTPS_PROXY`，则写入二者。
- 若传 `--socks` 且未设置 `ALL_PROXY`，则写入 `ALL_PROXY`。
- httpx（fastmcp 底层）默认 `trust_env=True` 自动读取这些变量。

## License
MIT
