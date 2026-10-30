# Code Agent Platform

从工具调用原型逐步演进的 AI 编程工作台，探索可审批、可验证、可恢复的 Agent 工程实现。

第一阶段的学习代码见 [LangChain Coding Agent Lab](https://github.com/slenlan233/langchain-coding-agent-lab)。本仓库保留平台本体及其配置模板、文档、测试和示例，展示在原有工具和 Agent 基础上持续改进的过程。

## 主要能力

- LangChain Agent 执行模型与工具循环，LangGraph 编排任务阶段、暂停与恢复。
- 项目文件夹授权、连续多轮会话、任务快照与有界跨轮记忆。
- 计划确认、补丁生成、隔离验证、AI 代码检查与人工接受写入。
- 工具权限、MCP 接入、百炼检索、来源校验与操作审计。
- Docker 测试与临时 MySQL 集成环境；业务数据库与测试数据库分离。
- Token 用量与预算、失败重试、输出拆批、归档与上下文压缩。
- 本地 Web 工作台、SQLite 持久化、可选 PostgreSQL 与 Java 关系索引。

## 快速开始

需要 Python 3.12+；Docker 用于隔离验证，Java/MySQL 场景还需要准备对应镜像。

```powershell
git clone https://github.com/slenlan233/code-agent-platform.git
cd code-agent-platform
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[api,graph]"
Copy-Item .env.example .env
Copy-Item agent-config.example.json agent-config.local.json
.\.venv\Scripts\python.exe -m code_agent_platform --config agent-config.local.json init
.\.venv\Scripts\python.exe -m code_agent_platform --config agent-config.local.json token
.\.venv\Scripts\python.exe -m code_agent_platform --config agent-config.local.json serve --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765/，使用本地生成的访问口令。模型 URL、密钥和远程开关需在自己的 `.env` 与本地配置中设置。第一次启动使用上面的 CLI；`scripts/start-workbench.ps1` 是原工作区的便捷脚本，固定使用 `.temp/platform-preview`，不是全新克隆的初始化入口。

先在允许的目录内添加实验项目；如需本机文件夹选择，按自己的环境开启 `allow_local_project_selection`。MySQL 实例、百炼等可选服务必须单独配置，不会继承开发者电脑的连接或密钥。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m code_agent_platform --config agent-config.local.json eval
.\.venv\Scripts\python.exe -m code_agent_platform --config agent-config.local.json --data-dir .temp/model-evaluation eval --engine model --repeats 1 --output .temp/model-evaluation.json
```

离线验收验证流程与权限，不代表模型准确率。真实模型验收产生实际 API 用量，使用独立 Python/Java 实验仓库，不自动批准文件或数据库修改。仅用于检查旧 `app/` 的五个测试模块未放入这个平台仓库。

## 设计与演进记录

- [改造设计](docs/AI编程平台_合并改造方案.md)
- [原代码复用评估](docs/原代码复用评估.md)
- [平台实现与验收](docs/平台升级实现与验收.md)
- [会话与运行稳定性](docs/会话与运行稳定性.md)
- [上下文管理](docs/上下文管理.md)
- [数据库集成验证](docs/后端与数据库集成验证.md)

部分文档保留了开发时的本机路径、当时的验收记录和旧版本状态，属于演进记录；实际初始化步骤以本 README 为准。被忽略的 `.temp/` 证明文件不随仓库上传，历史本机测试结果不能当作每次克隆都已完成的验收。

当前定位为学习和工程实践项目。长对话记忆、知识检索路由和代码检查都有边界；AI 检查通过不保证没有缺陷，重要变更仍需人工验收。

## 仓库结构

```text
src/code_agent_platform/  平台实现、工具封装与 Web 界面
tests/                   平台回归测试
docs/                    设计、迁移和验收记录
examples/                示例项目与 Docker 构建材料
evals/                   固定验收数据
scripts/                 本机与 CI 辅助脚本
```

不包含开发者的 `.env`、访问口令、本地数据库、项目连接配置、运行记录、压缩包或依赖目录。
