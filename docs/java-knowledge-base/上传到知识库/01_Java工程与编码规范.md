# Java 工程与编码规范

文档编号：JAVA-KB-01｜版本：1.0｜日期：2026-10-27
适用范围：Java 17、Spring Boot 3.5.x 的小型 Web 应用。性质：工程建议；已有项目优先保持其经过确认的约定。

## 1. 工程与依赖

项目采用 UTF-8，构建文件声明 Java 17。提交源码、构建描述、迁移脚本、测试和启动说明；`target/`、IDE 缓存及真实连接配置不进入版本管理。

建议目录：

```text
pom.xml
src/main/java/com/example/app/
  AppApplication.java
  todo/                  # 一个业务功能的接口、服务、数据访问和 DTO
  common/error/          # 公共错误处理
src/main/resources/
  application.yml
  db/migration/
  static/
src/test/java/com/example/app/
scripts/start.ps1
.env.example
README.md
```

对于小应用，按业务功能组织代码即可；无需仅为一个 CRUD 功能引入微服务、消息队列或多层抽象。JPA、MyBatis、JdbcTemplate 选择一种主要数据访问方式，遵循项目已有选型。

Spring Boot 项目优先通过其 parent 或 BOM 统一依赖版本；BOM 没有管理的依赖和插件显式固定兼容版本。不要使用 `LATEST`、动态版本或在生产构建里依赖 SNAPSHOT。升级前记录触发原因、兼容影响和验证结果，不以“版本号大”作为唯一依据。

## 2. 命名与职责

包名使用小写，类型使用 `UpperCamelCase`，方法和变量使用 `lowerCamelCase`，常量使用 `UPPER_SNAKE_CASE`。数据库表和列可使用小写下划线，并明确与 Java 字段的映射。

类名应表达职责，例如 `TodoController`、`TodoService`、`TodoRepository`。避免把所有方法堆进 `Utils` 或让 Controller 直接处理事务与 SQL。

方法以清晰、可测试为目标。复杂条件先用有意义的变量或小函数解释，不为追求短代码牺牲异常分支。注释优先解释业务原因、边界和兼容性，不逐句重复代码。

## 3. 依赖注入与数据对象

建议使用构造器注入，并将依赖字段设为 `final`，便于明确依赖和单元测试。

```java
@Service
public class TodoService {
    private final TodoRepository repository;

    public TodoService(TodoRepository repository) {
        this.repository = repository;
    }
}
```

请求 DTO、响应 DTO 与持久化实体分别表达接口输入、对外输出和数据库结构。简单 DTO 可以使用 Java 17 的 `record`；有生命周期、代理或可变性要求的实体按所选框架规则定义，不能因为 DTO 能用 record 就照搬到所有实体。

## 4. 空值、集合与边界

- 接口边界明确哪些字段可空；数据库 `NOT NULL` 与 Java 参数校验保持一致。
- 查询列表没有结果时返回空集合；单条不存在按接口约定返回 404 或业务结果，不伪造空实体。
- 对外部输入先做格式与业务校验，再进入数据操作；校验异常不能吞掉。
- 集合、分页大小和上传大小设置合理上限，避免一次读取无限量记录。
- 不在共享单例 Bean 中用可变字段保存当前请求的数据。

## 5. 金额与时间

金额通常使用 `BigDecimal`，明确币种、小数位和舍入规则。不要用二进制浮点数完成需要精确十进制的业务计算。构造十进制常量时采用字符串，数值比较用 `compareTo`，是否允许忽略 scale 由业务定义。

```java
BigDecimal price = new BigDecimal("19.90");
BigDecimal total = price.multiply(BigDecimal.valueOf(3));
boolean sameValue = total.compareTo(new BigDecimal("59.70")) == 0;
```

`equals` 同时受数值和 scale 影响，除法还需要考虑不能整除时的舍入行为。[Java 17 BigDecimal 文档](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/math/BigDecimal.html)

事件时刻建议使用 `Instant` 或显式带偏移的时间类型；生日、账期等本地业务日期可用 `LocalDate`。接口、Java、JDBC 和数据库必须约定同一时间含义。数据库使用不带时区的类型时，应明确保存 UTC 还是某地本地时间，并测试序列化往返。测试依赖当前时间时可注入 `Clock`，避免测试随机失败。

## 6. 异常与日志

可预期的业务错误定义具体类型或错误码；未知异常应保留堆栈供本地排查，对外返回一般错误信息。不要捕获 `Exception` 后返回“成功”，也不要把所有 `IllegalArgumentException` 一律当成用户输入错误。

日志使用参数占位符，记录请求标识和必要上下文。禁止输出密码、完整凭证、Authorization 头和带密码的连接串。异常排查需要查看配置时只展示脱敏的主机、端口、库名与“是否已配置”。

## 7. 最小代码审查

检查是否存在未使用依赖、硬编码密码、SQL 拼接、空异常处理、无边界集合、同一业务规则多份实现，以及源码和 README 不一致。格式问题应与行为错误分开描述；报告应说明实际运行了哪些检查。
