"""Cheap conservative routing for optional remote knowledge prefetch."""
import re

TRANSIENT_KNOWLEDGE_ERRORS = {'mcp_call_failed', 'mcp_execution_failed', 'mcp_unavailable',
    'mcp_closed', 'mcp_timeout', 'mcp_not_connected', 'mcp_output_type', 'mcp_output_limit'}


def retrieval_decision(text, *, required=False):
    if required:
        return True, 'required_project_source'
    value = re.sub(r'\s+', '', text).lower().strip('。！？!?')
    if re.search(r'不要.{0,6}(?:知识库|rag)|不查.{0,6}知识库|without\s*rag', value):
        return False, 'user_skipped_optional_knowledge'
    if re.search(r'知识库|规范|文档|安全要求|bailian|rag', value):
        return True, 'explicit_knowledge_request'
    if re.fullmatch(r'(?:你好|谢谢|好的|收到|继续|确认|同意|可以|hi|hello|thanks|ok)', value):
        return False, 'acknowledgement'
    if re.fullmatch(r'(?:这个项目|项目|它)?(?:怎么|如何)(?:运行|启动|打开)(?:这个项目|项目|它|网页)?|(?:怎么|如何)(?:运行|启动|打开)(?:这个项目|项目|它|网页)?', value):
        return False, 'local_startup_question'
    # Ambiguous development follow-ups still retrieve. Routing must not silently
    # remove potentially relevant requirements based on an uncertain guess.
    return True, 'development_or_ambiguous_request'
