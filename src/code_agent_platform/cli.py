from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings
from .models import ApprovalDecision, LOCAL_ACTOR, Mode, PlatformError, ProjectInput, TaskInput
from .service import Platform


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser():
    root = argparse.ArgumentParser(prog="ai-agent", description="可审批、可验证、可追踪的研发任务平台")
    root.add_argument("--config", help="管理员 JSON 配置文件")
    root.add_argument("--data-dir", help="本地平台数据目录")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化数据和访问口令")
    commands.add_parser("token", help="显示本地控制台访问口令")
    add = commands.add_parser("project-add", help="登记项目")
    add.add_argument("path")
    add.add_argument("--name", required=True)
    add.add_argument("--allow-write", action="store_true")
    add.add_argument("--mcp-server", action="append", help="为项目启用管理员登记的 MCP 配置，可重复传入")
    configure = commands.add_parser("project-config", help="更新项目的工具连接与隔离测试配置")
    configure.add_argument("--project", required=True)
    configure.add_argument("--file", required=True, help="JSON 对象：mcp_servers、test_command、sandbox_image")
    connections = commands.add_parser("tools", help="检查真实 MCP 连接，可执行一次已授权只读工具")
    connections.add_argument("--project", required=True)
    connections.add_argument("--call")
    connections.add_argument("--arguments", type=json.loads, default={})
    commands.add_parser("projects", help="列出项目")
    demo = commands.add_parser("demo", help="创建演示项目并运行一个任务")
    demo.add_argument("--write", action="store_true", help="演示需要人工审批的测试补全流程")
    run = commands.add_parser("run", help="提交研发任务")
    run.add_argument("prompt")
    run.add_argument("--project", required=True)
    run.add_argument("--mode", choices=[item.value for item in Mode], default=Mode.READONLY.value)
    run.add_argument("--engine", choices=["model", "demo"], default="model")
    run.add_argument("--session")
    show = commands.add_parser("show", help="查看任务和执行记录")
    show.add_argument("task_id")
    resume = commands.add_parser("resume", help="恢复已保存的任务")
    resume.add_argument("task_id")
    approve = commands.add_parser("approve", help="审批当前计划或补丁")
    approve.add_argument("approval_id")
    approve.add_argument("--reject", action="store_true")
    approve.add_argument("--comment", default="")
    approve.add_argument('--database-name', help='确认删库时必须指定确认卡展示的完整数据库名')
    export = commands.add_parser("export", help="导出已审批并验证的补丁")
    export.add_argument("task_id")
    export.add_argument("--output")
    apply = commands.add_parser("apply", help="将已审批并验证的补丁应用到登记项目")
    apply.add_argument("task_id")
    rollback = commands.add_parser("rollback", help="恢复该任务应用前的内容；不覆盖后续人工修改")
    rollback.add_argument("task_id")
    serve = commands.add_parser("serve", help="启动网页工作台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    evaluate = commands.add_parser("eval", help="运行固定离线验收集")
    evaluate.add_argument("--output")
    evaluate.add_argument("--dataset")
    evaluate.add_argument('--engine', choices=['demo', 'model'], default='demo', help='model 调用真实模型，产生实际用量')
    evaluate.add_argument('--repeats', type=int, default=1, help='真实模型验收重复次数 1—20')
    index = commands.add_parser("index", help="建立 Java 代码关系索引")
    index.add_argument("--project", required=True)
    graph = commands.add_parser("graph", help="查询 Java 调用关系")
    graph.add_argument("symbol")
    graph.add_argument("--project", required=True)
    graph.add_argument("--direction", choices=["callers", "callees"], default="callees")
    review = commands.add_parser("review", help="以 JSON 输出变更审查结果")
    review.add_argument("--project", required=True)
    review.add_argument("--fail-on-blocking", action="store_true")
    review.add_argument("--base", default="HEAD")
    review.add_argument("--head")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    settings = Settings.load(args.config)
    if args.data_dir:
        settings.data_dir = Path(args.data_dir).resolve()
    if args.command == "serve":
        try:
            import uvicorn
            from .api import create_app
        except ImportError:
            raise SystemExit("请先安装 api 可选依赖。") from None
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    platform = Platform(settings)
    try:
        actor = LOCAL_ACTOR
        if args.command == "init":
            platform.store.local_token()
            emit({"data_dir": str(settings.data_dir), "next": "ai-agent token / ai-agent serve"})
        elif args.command == "token":
            print(platform.store.local_token())
        elif args.command == "project-add":
            emit(platform.register_project(actor, ProjectInput(name=args.name, root=args.path, allow_write=args.allow_write, mcp_servers=args.mcp_server or ["project_files"])))
        elif args.command == "tools":
            emit(platform.inspect_tools(actor, args.project, call=args.call, arguments=args.arguments))
        elif args.command == "project-config":
            fields = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            if not isinstance(fields, dict) or set(fields) - {"mcp_servers", "test_command", "sandbox_image", "enabled", "allow_write", "policy_rules", "team","database_test_profile"}:
                raise PlatformError("project_config", "项目配置包含不支持的字段。")
            emit(platform.update_project(actor, args.project, **fields))
        elif args.command == "projects":
            emit(platform.projects(actor))
        elif args.command in {"run", "demo", "review"}:
            if args.command == "demo":
                project = platform.create_demo(actor)
                data = TaskInput(project_id=project["id"], user_input="解释加法函数和测试规范" if not args.write else "为加法函数补充正数、负数和零的单元测试", mode=Mode.WRITE if args.write else Mode.READONLY, engine="demo")
            elif args.command == "review":
                data = TaskInput(project_id=args.project, user_input="审查当前 Git Diff，输出可行动问题和证据。", mode=Mode.REVIEW, base_ref=args.base, head_ref=args.head)
            else:
                data = TaskInput(project_id=args.project, user_input=args.prompt, mode=args.mode, engine=args.engine, session_id=args.session)
            task = platform.create_task(actor, data)
            platform.run(actor, task["id"])
            detail = platform.detail(actor, task["id"])
            emit(detail if args.command == "review" else detail["task"])
            if args.command == "review" and args.fail_on_blocking and any(item["severity"] == "blocking" for item in detail["task"].get("review", {}).get("issues", [])):
                raise SystemExit(2)
        elif args.command == "show":
            emit(platform.detail(actor, args.task_id))
        elif args.command == "resume":
            emit(platform.run(actor, args.task_id, resume=True))
        elif args.command == "approve":
            approval = platform.decide(actor, args.approval_id, ApprovalDecision(decision="rejected" if args.reject else "approved", comment=args.comment, database_name=args.database_name))
            emit(platform.run(actor, approval["task_id"], resume=True))
        elif args.command == "export":
            result = platform.export_patch(actor, args.task_id)
            if args.output:
                with Path(args.output).open("x", encoding="utf-8") as handle:
                    handle.write(result["diff"])
                emit({"path": str(Path(args.output).resolve())})
            else:
                print(result["diff"])
        elif args.command == "apply":
            emit(platform.apply_patch(actor, args.task_id))
        elif args.command == "rollback":
            emit(platform.rollback_patch(actor, args.task_id))
        elif args.command == "eval":
            from .evaluation import evaluate, evaluate_model
            result = evaluate_model(platform, actor, args.dataset, repeats=args.repeats) if args.engine == 'model' else evaluate(platform, actor, args.dataset)
            if args.output:
                Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            emit(result)
            if result["failed"]:
                raise SystemExit(1)
        elif args.command in {"index", "graph"}:
            from .graph import CodeGraph
            graph = CodeGraph(platform)
            emit(graph.build(actor, args.project) if args.command == "index" else graph.query(actor, args.project, args.symbol, args.direction))
    except (PlatformError, FileExistsError) as error:
        emit({"error": {"code": getattr(error, "code", "file_exists"), "message": getattr(error, "message", "输出文件已存在，未覆盖。")}})
        raise SystemExit(1) from None
    finally:
        platform.close()


if __name__ == "__main__":
    main()
