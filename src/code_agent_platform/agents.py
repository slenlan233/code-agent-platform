"""Shared LangChain agent construction for the original CLI and platform nodes."""
from langchain.agents import create_agent


def build_agent(tools, memory=None, *, model, system_prompt, response_format=None, middleware=(), name=None):
    options = dict(model=model, tools=tools, checkpointer=memory, system_prompt=system_prompt, debug=False,
                   middleware=list(middleware))
    if response_format is not None:
        options["response_format"] = response_format
    if name is not None:
        options["name"] = name
    return create_agent(**options)
