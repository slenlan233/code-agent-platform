"""Locate review findings on either side of an actual unified diff."""
import re


def diff_lines(text):
    result = {}
    old_path = new_path = None
    old_line = new_line = old_left = new_left = 0
    for row in text.splitlines():
        if old_left or new_left:
            if row.startswith('\\ No newline'):
                continue
            if row.startswith('-') and old_left:
                result[old_path]['old'].add(old_line)
                old_line += 1
                old_left -= 1
            elif row.startswith('+') and new_left:
                result[new_path]['new'].add(new_line)
                new_line += 1
                new_left -= 1
            elif row.startswith(' '):
                old_line += 1
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if row.startswith('--- '):
            old_path = row[6:] if row.startswith('--- a/') else None
        elif row.startswith('+++ '):
            new_path = row[6:] if row.startswith('+++ b/') else None
            for path in (old_path, new_path):
                if path is not None:
                    result.setdefault(path, {'old': set(), 'new': set()})
        elif row.startswith('@@'):
            match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', row)
            if match and (old_path or new_path):
                old_line, old_count, new_line, new_count = match.groups()
                old_line, new_line = int(old_line), int(new_line)
                old_left = int(old_count) if old_count is not None else 1
                new_left = int(new_count) if new_count is not None else 1
    return result


def review_error(review, locations):
    for issue in review.issues:
        if issue.line not in locations.get(issue.path, {}).get(issue.side, set()):
            return f'{issue.path}:{issue.line} ({issue.side}) 不在当前 Diff 的变更行中。新增/修改行使用 new，删除行使用 old 和删除前行号。'
        if not issue.source_ids:
            return f'{issue.path}:{issue.line} 缺少实际来源 source_ids。'
    return None


def location_ranges(locations):
    """Compact consecutive lines before adding them to the model context."""
    def ranges(values):
        output = []
        for line in sorted(values):
            if output and output[-1][1] + 1 == line:
                output[-1][1] = line
            else:
                output.append([line, line])
        return output
    return {path: {side: ranges(lines) for side, lines in sides.items()} for path, sides in locations.items()}
