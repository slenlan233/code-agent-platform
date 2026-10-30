"""Select recoverable phase materials; never silently discard source text."""
from copy import deepcopy
import json
from pathlib import PurePosixPath
import re

from .models import PlatformError

COMMON_TYPES = {'String', 'Object', 'Integer', 'Long', 'Boolean', 'Double', 'Float', 'Byte', 'Character',
                'List', 'Map', 'Set', 'Collection', 'Optional', 'ArrayList', 'HashMap', 'HashSet',
                'Override', 'Exception', 'RuntimeException', 'ResponseEntity', 'HttpStatus', 'None', 'True', 'False'}


def _symbols(text):
    symbols = set(re.findall(r'\b[A-Z][A-Za-z0-9_$]{2,}\b', text))-COMMON_TYPES
    for match in re.finditer(r'(?:from\s+([\w.]+)\s+import|import\s+([\w.]+)|(?:from|require\()\s*[\'"]([^\'"]+))', text):
        value = next((value for value in match.groups() if value), '')
        symbols.add(value.replace('.', '/').rsplit('/', 1)[-1])
    return symbols


def context_size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _path(value):
    return str(value or '').replace('\\', '/').removeprefix('./')


def _excerpt(text, identifiers, *, dependency=False, start_line=1):
    """Line-addressed hints, not a semantic substitute for the original code."""
    lines = text.splitlines()
    budget = 3600 if dependency else 1000
    signatures = re.compile(r'^\s*(?:package |import |from |export |(?:public |private |protected |static |final |abstract |async )*(?:class |interface |record |enum |def |function )|(?:public|protected)\s+.*\()')
    selected = set(range(min(4, len(lines))))
    if lines:
        selected.add(len(lines)-1)
    scored = []
    for i, line in enumerate(lines):
        words = set(re.findall(r'[A-Za-z_$][\w$]{2,}', line))
        score = 3*len(words & identifiers) + int(bool(signatures.search(line)))
        if score:
            scored.append((-score, i))
    for _, index in sorted(scored)[:24 if dependency else 6]:
        addition = set(range(max(0, index-1), min(len(lines), index+2)))
        proposed = selected | addition
        if sum(min(len(lines[i]), 500)+20 for i in proposed) <= budget:
            selected = proposed
    output, used = [], 0
    for index in sorted(selected):
        line = f'{index+start_line}: {lines[index][:500]}'
        if used+len(line)+1 > budget:
            continue
        output.append(line)
        used += len(line)+1
    return '\n'.join(output)


def compact_context(context, limit=180000, *, archive=None):
    original = context_size(context)
    if original <= limit:
        return context, None
    view = deepcopy(context)
    batch = {_path(p) for p in view.get('patch_batch', {}).get('files', [])}
    active = batch or {_path(p) for p in view.get('approved_plan', {}).get('files', [])}
    rules = {_path(p) for p in view.get('project_rules', [])}
    candidates, active_text = [], []
    for item in view.get('evidence', []):
        if not isinstance(item.get('snippet'), str):
            continue
        path = _path(item.get('path'))
        protected = (path in active or path in rules or item.get('source_type') in {'request', 'rule'}
                     or PurePosixPath(path).name.upper() in {'AGENTS.MD', 'CLAUDE.MD'})
        if path in active:
            active_text.append(item['snippet'])
        if not protected:
            candidates.append((item, 'snippet', 'evidence', path))
    for kind, items in (('generated_file', view.get('generated_files', [])),
                        ('previous_patch', view.get('previous_patch', {}).get('changes', []))):
        for item in items:
            if not isinstance(item.get('content'), str):
                continue
            path = _path(item.get('path'))
            if path in active:
                active_text.append(item['content'])
            else:
                candidates.append((item, 'content', kind, path))
    previous = view.get('conversation', {}).get('previous_task_summary')
    if isinstance(previous, dict) and context_size(previous) > 6000:
        candidates.append((view['conversation'], 'previous_task_summary', 'historical_summary', ''))
    identifiers = _symbols('\n'.join(active_text))
    # Exact identifier/file matches are hints, not a complete dependency graph.
    def dependency(candidate):
        item, key, _, path = candidate
        if not path:
            return False
        return (PurePosixPath(path).stem in identifiers or
                bool(identifiers & _symbols(item[key])))
    candidates.sort(key=lambda candidate: (dependency(candidate), -context_size(candidate[0][candidate[1]])))
    changed = []
    for item, key, kind, path in candidates:
        if context_size(view) <= limit:
            break
        value = item[key]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) < 1600 or archive is None:
            continue
        related = dependency((item, key, kind, path))
        preview = _excerpt(text, identifiers, dependency=related, start_line=item.get('start_line', 1))
        descriptor = {'kind': kind, 'path': path, 'source_ids': [item['source_id']] if item.get('source_id') else []}
        if len(preview)+900 >= len(text):
            continue
        saved = archive(text, descriptor)
        reference = {'artifact_id': saved['id'], 'characters': len(text), 'snapshot': True,
                     'read_with': 'read_tool_result(artifact_id, offset, length)',
                     'note': '以下仅为带原始行号的定位片段，不是完整文件。需要接口、实现或遗漏细节时先读回原文；不要猜测。'}
        if kind == 'historical_summary':
            item[key] = {**{name: value[name] for name in ('status', 'test_status', 'applied_to_project', 'changed_files', 'unresolved') if name in value},
                         'archive': reference, 'preview': preview}
        else:
            item[key] = preview
            item['context_archive'] = reference
        changed.append({**descriptor, 'artifact_id': saved['id'], 'dependency_hint': related})
        view['context_compaction'] = {'method': 'recoverable_materials', 'archived_materials': len(changed),
            'note': '原文完整保留；定位片段不能代替完整实现。当前批次、规则、用户指令、审批与失败信息保持原样。'}
    retained = context_size(view)
    if retained > limit:
        raise PlatformError('context_budget', '必要材料仍超过阶段预算，未截断需求、规则或当前代码；请缩小本轮范围。原文和审批已保留。')
    return view, {'original_characters': original, 'retained_characters': retained,
                  'method': 'recoverable_materials', 'materials': changed}
