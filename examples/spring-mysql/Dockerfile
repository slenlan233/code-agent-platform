FROM maven:3.9-eclipse-temurin-17 AS dependencies
WORKDIR /opt/seed
COPY pom.xml .
RUN mvn -B -q -Dmaven.repo.local=/opt/m2 dependency:go-offline
COPY src ./src
RUN mvn -B -q -Dmaven.repo.local=/opt/m2 -DskipTests package
RUN chmod -R a+rX /opt/m2 /opt/seed

FROM dependencies AS test-runtime
ENV MAVEN_CONFIG=/tmp/maven
ENV MAVEN_OPTS="-Xmx256m -Djava.io.tmpdir=/tmp"
WORKDIR /workspace

FROM maven:3.9-eclipse-temurin-17 AS application
COPY --from=dependencies /opt/seed/target/spring-mysql-demo-1.0.0.jar /app/app.jar
ENV JAVA_TOOL_OPTIONS="-Xmx384m -Djava.io.tmpdir=/tmp"
USER 65534:65534
ENTRYPOINT ["java","-jar","/app/app.jar"]
