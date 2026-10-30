import os
import pymysql
from code_agent_platform.integrations.sql_policy import identifier, readonly_sql
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, Annotated, Any


# =========================
# MySQL 配置
# =========================
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_READ_USER", "agent_reader"),
    "password": os.getenv("MYSQL_READ_PASSWORD", ""),
    "charset": "utf8mb4",
    "connect_timeout": 5,
    "read_timeout": 8,
    "write_timeout": 8,
}


# =========================
# MCP 工具统一返回格式
# =========================
class Response(BaseModel):
    success: bool
    database: str | None = None
    table: str | None = None
    data: dict | list | None = None
    rowcount: int | None = None


# 创建 MCP Server
mcp = FastMCP("mysql-readonly", log_level="WARNING")


# =========================
# 1. 获取 MySQL 连接
# =========================
def get_connection(db=None):
    allowed = [item.strip() for item in os.getenv("MYSQL_DATABASES", "").split(",") if item.strip()]
    if not allowed or db is not None and db not in allowed:
        raise ValueError("数据库不在管理员登记的查询范围内。")
    if MYSQL_CONFIG["user"].lower() == "root":
        raise ValueError("平台只读服务不允许使用 root 账号。")
    # 复制一份配置，避免修改原来的 MYSQL_CONFIG
    config = MYSQL_CONFIG.copy()

    # 如果指定了数据库，就加入 database 配置
    if db:
        config["database"] = db

    try:
        connection = pymysql.connect(**config)
        return connection

    except Exception as e:
        raise RuntimeError(f"mysql connection error: {str(e)}")


# =========================
# 2. 通用 SQL 执行函数
# =========================
def execute_sql(command, database=None, params=None, commit=False):
    if commit and os.getenv("AI_AGENT_ENABLE_LEGACY_DB_WRITES") != "true":
        raise PermissionError("数据库管理操作未启用。")
    connection = None

    try:
        # 连接 MySQL
        connection = get_connection(database)

        # 创建游标
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            if not commit:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")

            # 执行 SQL
            cursor.execute(command, params)

            # SELECT / SHOW 等查询语句有返回结果
            if cursor.description:
                result = list(cursor.fetchmany(201))
                if len(result) > 200:
                    raise ValueError("查询超过 200 行，请缩小范围或显式 LIMIT。")
            else:
                result = []

            # 获取影响/返回的行数
            rowcount = cursor.rowcount

            # INSERT / UPDATE / DELETE 等需要提交
            if commit:
                connection.commit()

            return result, rowcount

    except Exception:
        # 出错时回滚
        if connection:
            connection.rollback()

        raise

    finally:
        # 无论成功失败都关闭连接
        if connection:
            connection.close()


# =========================
# 3. 获取所有数据库
# =========================
@mcp.tool(name="mysql_list_databases",description="列举MySQL中包含哪些数据库")
def mysql_list_databases():
    try:
        databases = [identifier(item.strip()) for item in os.getenv("MYSQL_DATABASES", "").split(",") if item.strip()]
        rowcount = len(databases)

        return Response(
            success=True,
            database=None,
            table=None,
            data=databases,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=None,
            table=None,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 4. 获取指定数据库中的所有表
# =========================
@mcp.tool(name="mysql_list_tables",description="获取指定数据库中的所有表")
def mysql_list_tables(database: str):
    try:
        result, rowcount = execute_sql(
            "SHOW TABLES",
            database=database
        )

        # 每一行类似：
        # {"Tables_in_lima_test": "user"}
        # 我们只需要里面的值："user"
        tables = [
            list(row.values())[0]
            for row in result
        ]

        return Response(
            success=True,
            database=database,
            table=None,
            data=tables,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=None,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 5. 查看表结构
# =========================
@mcp.tool(name="mysql_describe_tables",description="获取表结构信息")
def mysql_describe_tables(database: str, table: str):
    try:
        command = f"DESCRIBE `{identifier(table)}`"

        result, rowcount = execute_sql(
            command,
            database=database
        )

        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=table,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 6. 执行 SELECT / SHOW 等查询
# =========================
@mcp.tool(name="mysql_execute_query",description="执行SELECT、SHOW等只读SQL查询")
def mysql_execute_query(command: str, database: str | None = None, params: list[Any] | None = None):
    try:
        readonly_sql(command, params)
        # MCP 传过来的 params 一般是 list
        # PyMySQL 这里转成 tuple
        params_tuple = tuple(params) if params else None

        result, rowcount = execute_sql(
            command,
            database=database,
            params=params_tuple
        )

        return Response(
            success=True,
            database=database,
            table=None,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=None,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 7. 插入数据
# =========================
@mcp.tool(name="mysql_insert_data",description="向表里插入数据")
def mysql_insert_data(database: str, table: str, data: dict[str, Any]):
    try:
        # 例如：
        # data = {
        #     "name": "张三",
        #     "age": 20
        # }

        # ["name", "age"]
        columns = list(data.keys())

        # ["张三", 20]
        values = list(data.values())

        # 有几个值就生成几个 %s
        # ["%s", "%s"]
        # ↓
        # "%s, %s"
        values_wrapper = ", ".join(
            ["%s"] * len(values)
        )

        # 得到：
        # INSERT INTO user (name,age)
        # VALUES (%s, %s)
        command = (
            f"INSERT INTO {table} "
            f"({','.join(columns)}) "
            f"VALUES ({values_wrapper})"
        )

        result, rowcount = execute_sql(
            command,
            database=database,
            params=tuple(values),
            commit=True
        )

        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=table,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 8. 更新数据
# =========================
@mcp.tool(name="mysql_update_data",description="更新表中的数据")
def mysql_update_data(database: str, table: str, data: dict[str, Any], where: dict[str, Any]):
    try:
        if not where:
            raise ValueError("UPDATE 必须提供 WHERE 条件")

        # 例如：
        # data = {"name": "李四", "age": 21}
        #
        # 得到：
        # name = %s, age = %s
        set_clause = ", ".join(
            [f"{key} = %s" for key in data.keys()]
        )

        # where = {"id": 1}
        #
        # 得到：
        # id = %s
        where_clause = " AND ".join(
            [f"{key} = %s" for key in where.keys()]
        )

        command = (
            f"UPDATE {table} "
            f"SET {set_clause} "
            f"WHERE {where_clause}"
        )

        # UPDATE 的参数顺序：
        # 先 SET 的值，再 WHERE 的值
        set_params = list(data.values())
        where_params = list(where.values())

        params = set_params + where_params

        result, rowcount = execute_sql(
            command,
            database=database,
            params=tuple(params),
            commit=True
        )

        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=table,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 9. 删除数据
# =========================
@mcp.tool(name="mysql_delete_data",description="删除表中的数据")
def mysql_delete_data(database: str, table: str, where: dict[str, Any]):
    try:
        if not where:
            raise ValueError("DELETE 必须提供 WHERE 条件")

        # where = {"id": 1}
        #
        # 得到：
        # id = %s
        where_clause = " AND ".join(
            [f"{key} = %s" for key in where.keys()]
        )

        command = (
            f"DELETE FROM {table} "
            f"WHERE {where_clause}"
        )

        params = list(where.values())

        result, rowcount = execute_sql(
            command,
            database=database,
            params=tuple(params),
            commit=True
        )

        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=table,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 10. 创建数据库
# =========================
@mcp.tool(name="mysql_create_database",description="创建新数据库")
def mysql_create_database(database_name: str, charset: str = "utf8mb4"):
    try:
        command = (
            f"CREATE DATABASE {database_name} "
            f"CHARACTER SET {charset}"
        )

        result, rowcount = execute_sql(
            command,
            commit=True
        )

        return Response(
            success=True,
            database=database_name,
            table=None,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database_name,
            table=None,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 11. 创建表
# =========================
@mcp.tool(name="mysql_create_table",description="创建新表")
def mysql_create_table(database: str, table_name: str,
    table_columns: Annotated[
        str,
        Field(
            description="建表语句中的字段部分",
            examples=[
                "`id` int NOT NULL AUTO_INCREMENT,"
                "`name` varchar(255) NOT NULL,"
                "PRIMARY KEY (`id`)"
            ]
        )
    ],

    table_schema: Annotated[
        str,
        Field(
            description="建表语句中的补充部分",
            examples=[
                "ENGINE=InnoDB "
                "DEFAULT CHARSET=utf8mb4"
            ]
        )
    ]
):
    try:
        command = (
            f"CREATE TABLE {table_name} "
            f"({table_columns}) "
            f"{table_schema}"
        )

        result, rowcount = execute_sql(
            command,
            database=database,
            commit=True
        )

        return Response(
            success=True,
            database=database,
            table=table_name,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=table_name,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 12. 执行其他 SQL 命令
# =========================
@mcp.tool(name="mysql_execute_command", description="执行特定SQL语句，如变更表结构、增加字段、删除字段")
def mysql_execute_command(database: str,command: str):
    try:
        result, rowcount = execute_sql(
            command,
            database=database,
            commit=True
        )

        return Response(
            success=True,
            database=database,
            table=None,
            data=result,
            rowcount=rowcount,
        )

    except Exception as e:
        return Response(
            success=False,
            database=database,
            table=None,
            data={"error": str(e)},
            rowcount=0,
        )


# =========================
# 启动 MCP Server
# =========================
if os.getenv("AI_AGENT_ENABLE_LEGACY_DB_WRITES") != "true":
    for _name in ("mysql_insert_data", "mysql_update_data", "mysql_delete_data", "mysql_create_database", "mysql_create_table", "mysql_execute_command"):
        mcp.remove_tool(_name)

if __name__ == "__main__":
    mcp.run(transport="stdio")
