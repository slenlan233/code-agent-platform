# MySQL 设计与 Flyway 迁移

文档编号：JAVA-KB-03｜版本：1.0｜日期：2026-10-27
适用范围：MySQL 8.4、Spring Boot 3.5.x、Flyway。性质：数据库工程建议；表结构示例仅用于 Todo。

## 1. 持久化与账号

用户要求“刷新和重启后保留”时，后端写入真实 MySQL；浏览器内存、数组或进程内 Map 不能代替数据库。项目可配置到本机数据库；测试应连接独立的临时数据库，而不是清空正常业务库。

建议分离两类账号：应用账号只拥有该项目库中的必要增删改查权限；迁移账号只拥有该项目库所需的建表、改表等权限。运行普通业务接口不使用 root。账号、库的创建由有权限的管理入口负责，不靠业务接口临时创建任意数据库。

## 2. 字符集与表设计

文本使用 `utf8mb4`，支持包括补充字符在内的 Unicode；不要把 MySQL 的 `utf8` 别名当成完整的四字节 UTF-8。[MySQL 8.4 utf8mb4 说明](https://dev.mysql.com/doc/refman/8.4/en/charset-unicode-utf8mb4.html)

明确列的可空性、默认值、长度、唯一性和查询所需索引。必要的业务约束既要有接口校验，也应尽可能由数据库约束保障。不要为每一列都建索引；根据实际查询和执行计划调整。

Todo 示例，适用于第一次创建空表：

```sql
CREATE TABLE todo (
    id BIGINT NOT NULL AUTO_INCREMENT,
    title VARCHAR(200) NOT NULL,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT chk_todo_completed CHECK (completed IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

示例时间字段约定保存 UTC，应用写入；使用其他约定时要统一修改 API、Java 和 JDBC 的时间处理。默认排序可以是 `created_at DESC, id DESC`，需要时再评估索引。

`VARCHAR(200)` 是数据库上限，不自动等价于所有前端和 Java 的“200 个字符”。应对 emoji、组合字符和空白处理约定做端到端测试。

## 3. SQL 与事务

所有用户输入采用参数绑定。动态排序字段通过白名单映射，不能将外部输入直接拼成表名、列名或 SQL 片段。明确查询列，数据较多时限制分页。

```java
// 示例：JdbcTemplate 自动处理参数绑定。
jdbcTemplate.update("UPDATE todo SET completed = ?, updated_at = ? WHERE id = ?",
    completed, updatedAt, id);
```

批量操作、余额变更等涉及一致性时设计事务、唯一约束和并发策略。是否允许重复删除、重复提交要在业务接口中定义，而不是依赖偶然执行顺序。

## 4. Flyway 迁移约定

迁移文件放在 `src/main/resources/db/migration/`，例如 `V1__create_todo.sql`、`V2__add_priority.sql`。版本唯一；双下划线分隔版本和描述。

已在需要保留数据的环境执行过的版本迁移不要直接改内容；需要改变结构时新增版本。Flyway 通过历史记录与 checksum 识别已执行脚本，修改旧脚本可能触发校验失败。[Flyway 版本迁移说明](https://documentation.red-gate.com/fd/versioned-migrations-273973333.html)

对于已执行环境，不要通过删除历史表、自动 repair 或清库掩盖迁移问题。先查明源码版本、数据库历史和实际结构，再决定经过审查的修复；临时测试库可以按其隔离策略重建。

不建议在首次建表脚本里随意添加 `IF NOT EXISTS` 来吞掉结构不一致；已有同名表时应调查来源。不要同时让 Flyway 和 Hibernate 自动改表。采用 JPA 时可选择 `ddl-auto=validate` 或按项目需要关闭其 DDL；不采用 JPA 的项目不必添加 JPA 配置。

## 5. Spring Boot 与迁移数据源

Flyway 负责表结构演进，应用 DataSource 负责正常业务访问。可以为 Flyway 单独设置 URL、用户和密码；设置其 URL 或用户会使用独立数据源。具体环境变量映射见配套的《配置管理与数据库连接》。[Spring Boot 3.5 数据库初始化](https://docs.spring.io/spring-boot/3.5/how-to/data-initialization.html)

依赖应包含项目所需的 Flyway 核心及 MySQL 支持模块，并与 Boot 管理的版本兼容。不要通过关闭迁移或换成内存数据库来让构建表面通过。

## 6. 变更前后验证

新库验证：从空的专用库启动迁移，检查表与索引，再运行 CRUD 测试。升级验证：先准备旧版本结构和样本数据，再迁移到新版本，检查数据保留与应用兼容。回滚应用前先判断新 schema 是否向后兼容，SQL DDL 不应被假定为都可随事务回滚。

数据库备份、删除库、清理账号均是独立管理操作。仅删除源码或归档项目不等于删除数据库，检索到本规范也不授予删库权限。
