# Java / MySQL 验收项目

使用 Spring Boot 3.5、Java 17、Flyway 和真实 MySQL。
迁移文件位于 src/main/resources/db/migration，使用带版本号的 SQL。
业务账号只有增删改查权限，DDL 由独立迁移账号执行。
HTTP 和数据库集成测试位于 src/test/java/local/agent/demo/BackendDatabaseTest.java。
测试命令：mvn -o -B -Dmaven.repo.local=/opt/m2 verify。
不修改或访问开发者已有数据库，不使用 H2 替代 MySQL 验收。
