# Spring Boot 分层与 REST 接口

文档编号：JAVA-KB-02｜版本：1.0｜日期：2026-10-27
适用范围：Spring Boot 3.5.x、Spring MVC、Java 17。性质：小型 REST 应用设计建议。

## 1. 各层职责

Controller 处理路由、HTTP 输入输出和边界校验；Service 表达业务规则及事务边界；Repository 或 Mapper 负责持久化。Controller 不拼 SQL，Repository 不决定用户界面文案。

一次“更新待办完成状态”的调用可以是：Controller 接收 ID 和 completed → Service 校验记录与规则 → Repository 执行更新 → Service 返回业务结果 → Controller 返回响应 DTO。

简单项目可以少建几层文件，但 HTTP、业务与 SQL 的职责仍应区分，避免为了分层增加只有一行转发而没有意义的抽象。

## 2. REST 资源与响应约定

下面是 Todo 的接口示例，不是所有项目都必须采用的业务接口。

| 方法与路径 | 用途 | 成功状态 | 常见错误 |
| --- | --- | --- | --- |
| `POST /api/todos` | 创建待办 | 201，返回记录，可附 Location | 400 输入无效 |
| `GET /api/todos` | 查询列表 | 200，返回数组或分页对象 | 400 分页参数无效 |
| `GET /api/todos/{id}` | 查询单条 | 200 | 404 不存在 |
| `PATCH /api/todos/{id}` | 更新指定字段 | 200，返回更新结果 | 400 / 404 / 409 |
| `DELETE /api/todos/{id}` | 删除 | 204，无响应体 | 示例选择不存在时返回 404 |

同一个项目保持稳定的成功与错误格式。允许采用直接 DTO 或统一包装，但不能前端假设数组而后端返回 `{data: [...]}`。HTTP 状态应保留含义，不能所有错误都用 200 掩盖。

列表需要明确排序，数据量可能增长时设计分页；不要依赖数据库默认返回顺序。

## 3. 输入校验

后端必须执行输入校验，前端校验只改善体验。Spring Boot 3 使用 `jakarta.validation`；配置所需的 validation 依赖，在请求对象上声明约束，在请求体参数上使用 `@Valid`。

```java
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

public record CreateTodoRequest(@NotBlank String title) {}

// Controller 方法片段；todoService 与返回类型由项目实现。
@PostMapping("/api/todos")
public TodoResponse create(@Valid @RequestBody CreateTodoRequest request) {
    return todoService.create(request.title());
}
```

上面片段只示意请求体校验，201 状态及 Location 仍需通过 `ResponseEntity` 或其他项目约定实现。`@Valid @RequestBody` 的校验错误通常映射为 400。[Spring MVC 请求体校验](https://docs.spring.io/spring-framework/reference/6.2/web/webmvc/mvc-controller/ann-methods/requestbody.html)

标题长度必须先定义“字符”的计量口径。若选 Unicode code point，Java 用 `codePointCount`、前端用 `Array.from(text).length` 并写边界测试；不能把 `String.length()`、Bean Validation 字符串长度和 HTML `maxlength` 当成任意 Unicode 场景下相同的计数方式。空白规范化也需前后端保持一致。

## 4. 业务错误与统一处理

建议通过 `@RestControllerAdvice` 将明确的输入异常、资源不存在、冲突等映射为 400、404、409，未知内部错误返回 500。数据库临时不可用可按项目策略返回 503。响应不包含 SQL、账号、堆栈或密码。

一个可选错误格式：

```json
{
  "code": "TODO_NOT_FOUND",
  "message": "待办不存在",
  "requestId": "example-request-id"
}
```

多个项目已有不同错误规范时保持各自规范，不为了套用示例破坏兼容性。非法 JSON、字段校验、路径参数错误与未知异常都应考虑。

## 5. 事务与并发

涉及多个必须同时成功的数据写入时，在 Service 的业务方法上设置事务。默认代理模式下，同一个对象内部调用另一个带 `@Transactional` 的方法不会通过事务代理。未定制回滚规则时，RuntimeException 和 Error 默认触发回滚，受检异常默认不触发；需要时按业务配置并测试。[Spring Framework 事务说明](https://docs.spring.io/spring-framework/reference/6.2/data-access/transaction/declarative/annotations.html)

事务中避免长时间网络请求。并发更新不能依赖“先查到就一定还在”，应检查更新影响行数、唯一约束或版本号。PATCH 应表达希望达到的状态，例如 `completed=true`；“翻转一次”在重试时可能产生相反结果。

## 6. 身份和部署边界

无登录的演示系统应只作为受限本地测试使用。真实共享系统还需要用户身份、资源归属检查和访问控制；知道记录 ID 不等于有权访问。

同源静态页面通常不需要 CORS。跨域时只开放明确的来源、方法与凭证策略，不能为解决联调问题盲目允许所有来源。Cookie 登录还应评估 CSRF 防护。

## 7. 完成判定

完成接口不等于“生成了 Controller”。至少需要真实调用验证成功、非法输入、资源不存在与数据库异常路径；再检查前端的路径、字段、HTTP 状态和后端约定一致。
