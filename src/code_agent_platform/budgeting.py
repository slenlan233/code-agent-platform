"""Project budgets aggregate reported input/output across all chats and tasks."""
from .models import DEFAULT_PROJECT_TOKEN_BUDGET


def project_token_usage(store,project):
    tasks=store.list('task',project_id=project['id'])
    usages=[t.get('usage',{}) for t in tasks]
    incoming=sum(u.get('input_tokens',0) for u in usages)
    outgoing=sum(u.get('output_tokens',0) for u in usages)
    cache_unknown=any(u.get('input_tokens',0)>0 and u.get('cache_read_tokens') is None for u in usages)
    cached=None if cache_unknown else sum(u.get('cache_read_tokens') or 0 for u in usages)
    limit=project.get('token_budget',DEFAULT_PROJECT_TOKEN_BUDGET)
    return {'limit':limit,'unlimited':limit is None,'input_tokens':incoming,'output_tokens':outgoing,'cache_read_tokens':cached,
            'uncached_input_tokens':None if cached is None else incoming-cached,
            'total_tokens':incoming+outgoing,'remaining_tokens':None if limit is None else max(0,limit-incoming-outgoing),
            'exhausted':limit is not None and incoming+outgoing>=limit,'model_calls':sum(u.get('model_calls',0) for u in usages),
            'task_count':len(tasks), 'interrupted_calls':sum(u.get('interrupted_calls',0) for u in usages),
            'usage_incomplete':any(u.get('interrupted_calls') or u.get('unreported_calls') for u in usages)}
