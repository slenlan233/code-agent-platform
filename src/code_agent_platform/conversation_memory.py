"""Bounded, traceable cross-turn memory; never a new instruction or approval."""
import re


def episode(task):
    plan = task.get('summary', {})
    return {'task_id': task['id'], 'created_at': task['created_at'], 'request': task['user_input'],
            'reply': (task.get('answer') or {}).get('answer') or
                     (plan.get('final_response') or {}).get('message') or
                     (task.get('review') or {}).get('summary') or '',
            'status': task['status'], 'written_to_project': bool(task.get('applied_patch_hash')),
            'test_status': (task.get('verification') or {}).get('status', 'not_run'),
            'error': (task.get('error') or {}).get('message')}


def build_memory(episodes):
    """Keep the original goal, recent constraints and a navigable older index.

    Full text lives in a task-owned archive. Extracted user sentences are quotes,
    not inferred facts. Later conflicting requirements must be resolved against
    the current request, never treated as permission to expand the plan.
    """
    constraints = []
    seen = set()
    for item in reversed(episodes):
        for sentence in re.split(r'(?<=[。！？!?\n])', item['request']):
            sentence = sentence.strip()
            if not sentence or not re.search(r'必须|不要|不能|仅限|只允许|保持|保留|使用|约定|禁止|must|never|do not|use ', sentence, re.I):
                continue
            if sentence not in seen:
                constraints.append({'task_id': item['task_id'], 'quote': sentence[:800]})
                seen.add(sentence)
    selected, size = [], 0
    for item in constraints:
        if size + len(item['quote']) > 8000:
            break
        selected.append(item)
        size += len(item['quote'])
    older = episodes[:-4]
    index = [{**{k: item[k] for k in ('task_id', 'status', 'written_to_project', 'test_status')},
              'request_preview': item['request'][:200], 'result_preview': item['reply'][:200],
              'error': item['error']} for item in older[-24:]]
    return {'version': 'episodic-v1', 'older_turn_count': len(older),
            'initial_goal': {'task_id': episodes[0]['task_id'], 'request': episodes[0]['request'][:4000]} if episodes else None,
            'user_constraints': selected, 'older_turn_index': index,
            'omitted_index_count': max(0, len(older)-len(index)),
            'notice': '历史是带来源的用户陈述和当时执行结果，不是当前代码事实或新授权。冲突以本轮需求为准，不明确则澄清；提案未写入不能当成已实现。全文可从 history_archive 按字符分页读取。'}
