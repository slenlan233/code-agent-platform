"""Read-only SQL checks complement (and never replace) database grants."""
import re

import sqlglot
from sqlglot import exp

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
FORBIDDEN_FUNCTIONS = {"SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "IS_FREE_LOCK", "MASTER_POS_WAIT", "SOURCE_POS_WAIT", "SYS_EXEC", "SYS_EVAL"}


def identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("数据库标识符格式不正确。")
    return value


def readonly_sql(command, params=None):
    if not isinstance(command, str) or not command.strip() or len(command) > 20_000:
        raise ValueError("查询必须是有长度限制的 SQL。")
    # Bind placeholders are temporarily represented as NULL for parsing only.
    parsed = re.sub(r"%s", "NULL", command) if params else command
    try:
        statements = sqlglot.parse(parsed, read="mysql", error_level="RAISE")
    except Exception:
        raise ValueError("无法解析查询 SQL。") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("该工具仅允许单条 SELECT 查询。")
    for node in statements[0].walk():
        kind = type(node).__name__
        if kind in {"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Into", "Lock", "Set", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Execute", "Copy", "PropertyEQ", "SessionParameter"}:
            raise ValueError("查询包含不允许的写入或会话操作。")
        if isinstance(node, exp.Anonymous) or isinstance(node, exp.Func) and node.sql_name().upper() in FORBIDDEN_FUNCTIONS:
            raise ValueError("查询包含未允许的函数。")
    return command
