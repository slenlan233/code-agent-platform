# PowerShell 与 DeepSeek 接入（0.4.0）

本轮接入实际 PowerShell 命令执行与真实 DeepSeek 模型。配置密钥保存在本机 `.env`，已经被版本控制忽略；示例文件和源码不含真实密钥。

## DeepSeek 配置

2026-10-26 核对 [DeepSeek 官方调用说明](https://api-docs.deepseek.com/) 与 [模型说明](https://api-docs.deepseek.com/quick_start/pricing/)：DeepSeek-V4.1-Flash-1017 对应的 API 模型名为 `deepseek-flash`，使用 `https://api.deepseek.com`。具体版本名称不直接作为请求的 model 参数。

```dotenv
AI_AGENT_MODEL=deepseek-flash
AI_AGENT_BASE_URL=https://api.deepseek.com
AI_AGENT_API_KEY=填入自己的密钥
AI_AGENT_ALLOW_REMOTE=true
AI_AGENT_MODEL_PROVIDER=deepseek
AI_AGENT_THINKING=enabled
```

平台使用 `langchain-deepseek==1.1.1` 与一个兼容适配类。它保留工具多轮调用的 reasoning_content，并在思考模式下使用 API 支持的 auto 工具选择。LangChain 的 ToolStrategy 仍负责解析和验证最终结构化结果；提示词指定最终提交的 schema 工具，缺少结构化结果则明确失败。非思考模式保留强制工具选择。

本地验证发现直接使用原 ChatOpenAI 序列化和 ToolStrategy 的强制选择会造成字段丢失或 API 400，不能仅替换 model 和 base_url。请求超时、最大输出、调用次数与 token 预算继续生效。思考字段用于协议往返，不把它作为用户报告内容。实际用量来自供应商，未配置计价时金额为空，不能解释为免费。

## PowerShell 命令流程

工具名为 `run_powershell`，参数为 script 和 timeout_seconds。它属于后端受控执行工具，由 LangChain 工具接口调用，不登记成“只读 MCP Server”。MCP 文件、数据库和知识库服务继续各自连接。

规划 Agent 在 PlanDraft.powershell_commands 中提出完整脚本，最多 5 个，每个最多 6000 字，超时 1–120 秒。网页在批准计划前显示脚本及超时；仅批准后才能执行。只读问答和计划模式不会执行，也不会向内部 Agent 暴露执行工具。模型发起的额外脚本、不同超时和过期审批均被拒绝，需重新规划。

```json
{
  "powershell_commands": [
    {"script": "Get-Content -LiteralPath calculator.py; Write-Output 'READY'", "timeout_seconds": 15}
  ]
}
```

批准后在单独的 PowerShell 节点执行计划命令，再把实际输出交给编码 Agent。相同任务、计划和参数重复调用复用已保存结果；崩溃期间丢失记录的命令可能重新运行，因此目前仅用于项目只读分析，不提供外部系统写入承诺。

执行器复用已有 DockerRunner 的文件输出采集、时限、资源限制与容器清理。默认镜像是微软官方 `mcr.microsoft.com/dotnet/sdk:8.0`，内含 PowerShell 7；固定实际镜像 digest 执行，项目副本只读、无网络、无主机盘符和桌面、无模型密钥，不挂载 Docker socket。复制前过滤敏感路径、疑似凭证和被禁止的来源；临时文件仅可写 `/tmp`。模型生成文件仍经补丁和隔离测试流程交付。

```powershell
docker pull mcr.microsoft.com/dotnet/sdk:8.0
uv sync --locked --inexact --extra api --extra graph
uv run --no-sync ai-agent serve
```

管理员可在配置中替换 powershell_image，并把该镜像加入 sandbox_images。工具的脚本、超时、退出码、stdout/stderr、镜像 digest 和输入快照摘要均保存到 command_run / artifact；BeforeCommand、AfterCommand 与工具审计共同约束执行。PowerShell 运行成功不等于测试通过，测试仍有独立报告。

项目测试入口也允许使用 pwsh 参数列表；需要测试框架或依赖时由管理员准备相应镜像，不在命令工具中临时开放网络安装。

原 `app/mcp/powershell_tools.py` 的可见窗口、键盘和 Transcript 功能保留在显式启用的旧本机扩展中，没有声称将这些桌面交互变成容器能力。新平台可以执行 PowerShell 脚本；需要操作 Windows 桌面、注册表或 Windows 专属模块的任务，需要另外登记隔离的 Windows 执行环境。

## 验证记录

真实 DeepSeek 已在自建示例上完成 MCP 工具调用和带有效来源的结构化回答，3 次模型调用，供应商报告 9007 输入、2025 输出 token。另一次完整写入任务经过两次审批、实际 PowerShell 执行、模型生成代码、3 项 Docker 测试、模型审查和补丁导出，8 次模型调用、35861 输入、6516 输出 token，原示例项目未被直接修改。运行器反测确认中文/错误输出和退出码、只读写入拦截、超时容器清理均有效。

最终回归结果见 [平台升级实现与验收](平台升级实现与验收.md)。真实付费联调仅使用平台自建示例，未把本项目其他私有代码发送给模型。API 对 Flash 使用可更新别名；当前对应版本依据官方模型说明，响应的别名本身不是固定版本证明。

官方依据：[思考模式与工具多轮调用](https://api-docs.deepseek.com/guides/thinking_mode/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[微软 PowerShell 容器说明](https://learn.microsoft.com/en-gb/powershell/scripting/install/powershell-in-docker?view=powershell-7.6)。
