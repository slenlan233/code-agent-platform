package local.agent.demo;

import java.net.URI;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/notes")
public class NoteController {
    private final JdbcTemplate jdbc;
    public NoteController(JdbcTemplate jdbc) { this.jdbc = jdbc; }
    public record Note(long id, String title, String status) {}
    public record NoteInput(String title) {}

    private String title(NoteInput input) {
        if (input.title() == null || input.title().isBlank() || input.title().length() > 200)
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "title must contain 1-200 characters");
        return input.title().strip();
    }

    @GetMapping
    public List<Note> list() {
        return jdbc.query("SELECT id,title,status FROM agent_demo_notes ORDER BY id", (rs, index) -> new Note(rs.getLong(1), rs.getString(2), rs.getString(3)));
    }

    @GetMapping("/{id}")
    public Note get(@PathVariable long id) {
        return jdbc.query("SELECT id,title,status FROM agent_demo_notes WHERE id=?", (rs, index) -> new Note(rs.getLong(1),rs.getString(2),rs.getString(3)), id)
            .stream().findFirst().orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    @PostMapping
    public ResponseEntity<Note> create(@RequestBody NoteInput input) {
        String value=title(input);
        var key=new GeneratedKeyHolder();
        jdbc.update(connection -> {
            var statement=connection.prepareStatement("INSERT INTO agent_demo_notes(title) VALUES (?)", java.sql.Statement.RETURN_GENERATED_KEYS);
            statement.setString(1,value);
            return statement;
        },key);
        Note note=get(key.getKey().longValue());
        return ResponseEntity.created(URI.create("/api/notes/"+note.id())).body(note);
    }

    @PutMapping("/{id}")
    public Note update(@PathVariable long id,@RequestBody NoteInput input) {
        if(jdbc.update("UPDATE agent_demo_notes SET title=? WHERE id=?",title(input),id)==0)
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        return get(id);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable long id) {
        if(jdbc.update("DELETE FROM agent_demo_notes WHERE id=?",id)==0)
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        return ResponseEntity.noContent().build();
    }
}
