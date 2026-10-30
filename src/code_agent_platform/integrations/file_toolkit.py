"""Original file toolkit factory, without starting an MCP service at import time."""
from langchain_community.agent_toolkits.file_management import FileManagementToolkit


def file_tools(root):
    return FileManagementToolkit(root_dir=str(root)).get_tools()
