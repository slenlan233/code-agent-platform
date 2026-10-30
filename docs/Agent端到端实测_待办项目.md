# Agent 端到端实测：Java / MySQL 待办项目

> 2026-10-26：已按用户要求清理此旧演示。目录移至回收站，专用数据库和账号在加密备份后移除，工作台项目列表已移除该项目。下文保留为历史验收记录。

实测日期：2026-10-26。用户要求在 D 盘新建简单 Java 前后端数据库项目，用来检验现有 Agent。

## 已交付的项目

- 目录：`D:\AgentTodoDemo`。
- 页面：<http://127.0.0.1:18081/>，服务仅监听本机。
- 技术：Java 17、Spring Boot 3.5.16、JdbcTemplate、Flyway、MySQL 8.4.13；前端为同一 Java 服务提供的 HTML/CSS/JavaScript。
- 功能：新增、列表、标记完成、取消完成、删除；统计总数与完成数；空标题和超长标题校验。
- 正常运行连接本机 `127.0.0.1:3306/agent_todo_demo`；账号 `agent_todo_app@localhost` 只对该库拥有 CRUD，`agent_todo_migrate@localhost` 负责该库迁移。密码在项目 `.env`，不进入代码、审计报告或 Git。
- 该库与先前的 `ai_project2` 分开。自动测试使用任务专用临时库，实际网页使用长期保留的本机库。

## Agent 实际完成的工作

初始目录只有人工给定的需求 `AGENTS.md`，没有复制旧示例的业务实现。真实 DeepSeek 模型通过现有 LangChain Agent 和 LangGraph 流程生成了 10 个文件：Maven 配置、Java 入口与接口、数据库配置、迁移 SQL、网页、集成测试、启动脚本、README、Git 忽略规则。

第一项任务 `task_85feca9796ae432e8299d9c1421ce710` 完成生成与写回。第二项任务 `task_f99c78c849a6417b928bdcbcdf3fb97e` 根据实际 Windows 启动失败，仅修复 `run-local.ps1`。两项任务均留下计划、具体审批、Diff、工具与用量记录，均为 completed / applied。

## 实际验证结果

1. Docker 中运行真实 MySQL 集成测试，6 项全部通过；启动脚本修复后再次运行同一套 6 项，全部通过。不是 12 个不同用例。
2. 在 Windows 运行生成的启动脚本，本机 MySQL 自动执行 V1 建表迁移，Java 服务启动成功。
3. 本机 HTTP / JDBC 检查 14 项通过，包括 CRUD、错误输入、404、实际数据库身份和迁移历史。
4. 通过浏览器实际新增、完成、取消完成、删除，统计数字同步更新。
5. 只重启该 Java 后端后，先前写入的记录仍然存在；浏览器刷新可见。
6. 平台修复后的完整回归 126 项通过，0.7.1 独立发行包安装与 MCP 子进程检查通过。

## 暴露并修复的问题

- 新文件的 Review 原先仍读取原项目目录，导致已通过测试的生成任务在审查处失败。平台现在为审查建立补丁副本；读取、搜索和文件列表使用副本；新来源绑定当前补丁内容，清理副本后仍可验证。补充了新文件、篡改、跨任务和失败恢复检查。
- 模型有一次返回 `structured_response: null`，原流程把空值传给审查节点。现在保留已有证据、最多补问一次；继续缺失则明确失败，不能当作通过。调用继续计入预算。
- 当前 Windows JDK 17.0.14 启动 Tomcat 时遇到本地临时目录相关的 loopback 错误。人工用独立 Java 检查定位后，Agent 为启动脚本加入项目 `.temp/java` 绝对目录及两个 JVM 参数；随后本机实际启动、重启均成功。网络临时目录参数依据 [Oracle Java 网络属性说明](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/net/doc-files/net-properties.html)。

这是一次经过人工审批、环境准备和问题修复后完成的实测，不应描述为从零到部署全程无人干预或一次成功。业务代码、网页、迁移、测试和启动修复由现有 Agent 生成；本机数据库用户、凭证、依赖缓存、平台修复和浏览器验收由本次测试协助完成。

模型审查仍有 warning/suggestion，例如配置引号解析、Unicode 长度语义及临时目录检查。已验证本次配置和操作可运行，不代表生产可用性或所有边界均已覆盖。

## 模型用量

两项任务合计，包括失败和恢复：输入 172,981 token，其中缓存命中 89,216、未缓存 83,765；输出 59,517；累计 232,498。模型调用 14 次，工具调用 36 次。未配置价格，不能据此声明实际费用。

## 本机操作与证据

重启前先停止当前后台服务，再运行：

```powershell
cd D:\AgentTodoDemo
pwsh -NoProfile -File .temp/stop-service.ps1
pwsh -NoProfile -File .\run-local.ps1
```

源码与使用说明位于项目 `README.md`；实测摘要为 `D:\AgentTodoDemo\.temp\agent-test-summary.json`，本机检查为 `.temp/local-validation.json`。平台原始任务报告位于本仓库 `.temp/todo-agent-report.json` 与 `.temp/todo-startup-report.json`。网页验收截图为可视化目录中的 `agent-todo-demo.png`。
