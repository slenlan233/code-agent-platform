package local.agent.demo;

import java.sql.DriverManager;
import java.util.Map;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import static org.assertj.core.api.Assertions.*;

@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT)
class BackendDatabaseTest {
    @Autowired TestRestTemplate http;
    @Autowired JdbcTemplate jdbc;
    @Autowired Flyway flyway;

    @Test void migrationsCreateAndAlterSchemaExactlyOnce() {
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM agent_demo_schema_history WHERE success=1",Integer.class)).isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_demo_notes' AND COLUMN_NAME='status'",Integer.class)).isEqualTo(1);
        assertThat(flyway.migrate().migrationsExecuted).isZero();
        assertThat(flyway.info().pending()).isEmpty();
    }

    @Test void realHttpCreateReadUpdateDeleteUsesMySql() {
        var created=http.postForEntity("/api/notes",Map.of("title","first"),NoteController.Note.class);
        assertThat(created.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        long id=created.getBody().id();
        assertThat(created.getBody().status()).isEqualTo("open");
        assertThat(http.getForObject("/api/notes/"+id,NoteController.Note.class).title()).isEqualTo("first");
        var updated=http.exchange("/api/notes/"+id,HttpMethod.PUT,new HttpEntity<>(Map.of("title","updated")),NoteController.Note.class);
        assertThat(updated.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(updated.getBody().title()).isEqualTo("updated");
        assertThat(http.exchange("/api/notes/"+id,HttpMethod.DELETE,HttpEntity.EMPTY,Void.class).getStatusCode()).isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(http.getForEntity("/api/notes/"+id,String.class).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
    }

    @Test void invalidInputAndMissingRowsAreRejected() {
        assertThat(http.postForEntity("/api/notes",Map.of("title","  "),String.class).getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(http.getForEntity("/api/notes/9223372036854775807",String.class).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
    }

    @Test void runtimeUserCanWriteDataButCannotCreateTables() {
        assertThat(jdbc.queryForObject("SELECT CURRENT_USER()",String.class)).startsWith("app_writer@");
        assertThatThrownBy(() -> jdbc.execute("CREATE TABLE forbidden_runtime_ddl(id INT)" )).isInstanceOf(DataAccessException.class);
    }

    @Test void runtimeUserCannotReadAdministrativeDatabase() {
        assertThatThrownBy(() -> jdbc.queryForList("SELECT 1 FROM mysql.user LIMIT 0")).isInstanceOf(DataAccessException.class);
    }

    @Test void independentConnectionSeesDataWrittenThroughHttp() throws Exception {
        String title="persisted-"+UUID.randomUUID();
        var note=http.postForEntity("/api/notes",Map.of("title",title),NoteController.Note.class).getBody();
        try(var connection=DriverManager.getConnection(System.getenv("APP_JDBC_URL"),System.getenv("APP_DB_USER"),System.getenv("APP_DB_PASSWORD"));var statement=connection.prepareStatement("SELECT title FROM agent_demo_notes WHERE id=?")) {
            statement.setLong(1,note.id());
            try(var rows=statement.executeQuery()){assertThat(rows.next()).isTrue();assertThat(rows.getString(1)).isEqualTo(title);}
        } finally { http.delete("/api/notes/"+note.id()); }
    }
}
