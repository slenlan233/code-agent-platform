"""Show all registered capabilities without claiming disabled services connect."""
import os


def mysql_target():
    return {"host": os.getenv("MYSQL_HOST", "127.0.0.1"), "port": os.getenv("MYSQL_PORT", "3306"),
            "user": os.getenv("MYSQL_READ_USER", "agent_reader"),
            "databases": [value.strip() for value in os.getenv("MYSQL_DATABASES", "").split(",") if value.strip()],
            "password_configured": bool(os.getenv("MYSQL_READ_PASSWORD"))}


def inventory(platform, actor, project, connections=()):
    actual = {item["server"]: item for item in connections}
    selected = project.get("mcp_servers", ["project_files"])
    servers = []
    # Preserve the connection API's original ordering for selected servers;
    # additional disabled/unselected profiles follow them in the inventory.
    ordered = [*actual, *(name for name in platform.settings.mcp_servers if name not in actual)]
    for name in ordered:
        config = platform.settings.mcp_servers[name]
        item = {"server": name, "selected": name in selected, "enabled": config.enabled,
                "transport": config.transport, "status": "not_checked" if name in selected and config.enabled else "not_selected" if config.enabled else "disabled",
                "tools": [], "available_tools": ["mcp__" + name + "__" + tool for tool in config.allow_tools]}
        item.update(actual.get(name, {}))
        if config.builtin == "mysql":
            item["connection"] = mysql_target()
            missing = []
            if not item["connection"]["databases"]:
                missing.append("数据库范围")
            if not item["connection"]["password_configured"]:
                missing.append("密码")
            item["database_status"] = "not_configured" if missing else "not_checked"
            item["missing"] = missing
        servers.append(item)
    labels = {"read_file":"读取文件", "list_files":"列出文件", "search_code":"搜索代码", "rag_search":"检索项目规范", "git_status":"查看 Git 状态", "git_diff":"查看 Git 差异", "graph_query":"查询代码关系", "run_powershell":"Shell / PowerShell 7"}
    labels['read_tool_result'] = '读取归档原文与执行日志'
    native = []
    for name in platform.gateway.tools:
        command = name == "run_powershell"
        native.append({"name": name, "label": labels.get(name, name), "kind": "command" if command else "readonly",
                       "status": "approval_required" if command and project.get("allow_write") else "disabled" if command else "available",
                       "backend": "docker" if command else "platform"})
    native.extend({"name": name, "label": label, "kind":"workflow", "status":"approval_required" if project.get("allow_write") else "disabled", "backend":"docker" if name == "run_tests" else "platform"}
                  for name, label in (("prepare_database","准备项目业务数据库"),("drop_database","删除本项目专属数据库（独立确认）"),("apply_patch","应用批准的修改"),("run_tests","执行隔离测试"),("export_patch","导出批准的补丁")))
    return {"servers": servers, "native_tools": native}
