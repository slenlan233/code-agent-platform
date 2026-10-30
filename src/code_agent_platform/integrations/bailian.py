import hashlib
import os
import json
from datetime import date
from pathlib import Path
from typing import Annotated

import alibabacloud_bailian20231229.client as bailian_20231229_client
import requests
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_bailian20231229 import models as bailian_20231229_models
from alibabacloud_tea_util import models as util_models

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from code_agent_platform.models import digest
from code_agent_platform.security import SECRET, safe_path

mcp = FastMCP("bailian-knowledge", log_level="WARNING")


ALIBABA_CLOUD_ACCESS_KEY_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
ALIBABA_CLOUD_ACCESS_KEY_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")


"""
lease_id	上传许可证	临时的，只负责这次上传
file_id	    文件身份证	表示百炼数据中心里的一个文件
index_id	知识库身份证	表示整个 RAG 知识库
job_id	    任务身份证	表示一次建索引/添加文档任务
"""
def create_client()-> bailian_20231229_client.Client:
    key = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not key or not secret:
        raise RuntimeError("请先配置百炼访问凭证环境变量。")
    config = open_api_models.Config(
        access_key_id=key,
        access_key_secret=secret,
    )
    config.endpoint = "bailian.cn-beijing.aliyuncs.com"
    return bailian_20231229_client.Client(config)

def retrieve_index(client, workspace_id, index_id, query):
    retrieve_request = bailian_20231229_models.RetrieveRequest(
        index_id=index_id,
        query=query,
    )
    runtime = util_models.RuntimeOptions(connect_timeout=5000, read_timeout=15000)
    return client.retrieve_with_options(
        workspace_id,
        retrieve_request,
        {},
        runtime,
    )

@mcp.tool(name="query_rag", description="检索当前项目已登记的百炼规范和知识，返回来源、版本和内容快照。")
def query_rag_from_bailian(query:Annotated[str, Field(description="查询当前项目的规范与知识", min_length=1, max_length=2000)]):
    bailian_client = create_client()
    workspace_id = required_setting("BAILIAN_WORKSPACE_ID")
    index_id = required_setting("BAILIAN_INDEX_ID")
    rag = retrieve_index(bailian_client, workspace_id, index_id, query)

    result = []
    for data in (rag.body.data.nodes or [])[:12]:
        metadata = data.metadata if isinstance(data.metadata, dict) else {}
        if metadata.get("project_id") and metadata["project_id"] != os.getenv("MCP_PROJECT_ID"):
            continue
        if metadata.get("status", "active") != "active" or metadata.get("effective_date", date.today().isoformat()) > date.today().isoformat():
            continue
        text = (data.text or "")[:16000]
        if SECRET.search(text):
            continue
        document_id = str(metadata.get("document_id") or metadata.get("file_id") or digest(text)[:24])
        result.append({"text": text, "path": f"bailian://{workspace_id}/{index_id}/{document_id}",
                       "document_id": document_id, "title": str(metadata.get("title") or metadata.get("file_name") or document_id),
                       "version": str(metadata.get("version") or digest(text)), "status": "active", "score": data.score})
    return {"status": "succeeded", "provider": "bailian", "evidence": result,
            "freshness": "retrieved_snapshot", "unknowns": ["远程版本未提供时使用返回文本摘要，不能代替源知识库的版本管理。"]}


def required_setting(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"请配置 {name}。")
    return value

@mcp.tool(name="upload_rag_from_local_file_to_bailian_rag", description="将本地的知识文件上传到百炼平台")
def upload_rag_to_bailian(
        file_path: Annotated[
            str,
            Field(
                description="本地知识文件的路径，需要传入绝对路径",
                examples=[r"D:\学习Java\ai项目\3.xiangmu\app\code_agent\rag\rag_test.txt"],
            ),
        ],
):
    if os.getenv("AI_AGENT_ENABLE_KNOWLEDGE_ADMIN") != "true":
        raise PermissionError("知识库上传仅允许显式启用的管理入口。")
    root = Path(required_setting("MCP_PROJECT_ROOT")).resolve()
    candidate = Path(file_path).resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError("上传文件超出已登记项目。")
    candidate = safe_path(root, candidate.relative_to(root).as_posix())
    if candidate.stat().st_size > 2_000_000 or SECRET.search(candidate.read_text(encoding="utf-8")):
        raise PermissionError("上传文件超出限制或包含疑似凭证。")
    file_path = str(candidate)
    bailian_client = create_client()
    workspace_id = required_setting("BAILIAN_WORKSPACE_ID")
    category_id = required_setting("BAILIAN_CATEGORY_ID")
    index_id = required_setting("BAILIAN_INDEX_ID")
    file_id = upload_rag_file_to_bailian(bailian_client, workspace_id, category_id, file_path)
    return add_document_to_index(bailian_client, workspace_id, index_id, file_id)

@mcp.tool(name="query_bailian_rag_job_status", description="查询上传到百炼知识库中的知识文件处理状态")
def query_bailian_rag_job_status(job_id:str):
    bailian_client = create_client()
    workspace_id = required_setting("BAILIAN_WORKSPACE_ID")
    index_id = required_setting("BAILIAN_INDEX_ID")
    job_status = get_index_job_status(bailian_client, workspace_id, index_id, job_id)
    return job_status.body.data


def apply_lease(client, category_id, file_name, file_md5, file_size, workspace_id):
    headers = {}
    runtime = util_models.RuntimeOptions()
    request = bailian_20231229_models.ApplyFileUploadLeaseRequest(
        file_name = file_name,
        md_5 = file_md5,
        size_in_bytes=file_size,
    )
    return client.apply_file_upload_lease_with_options(
        category_id,
        workspace_id,
        request,
        headers,
        runtime
    )


def caculate_md5(file_path:str)->str:
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def get_file_info(file_path:str):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_md5 = caculate_md5(file_path)
    return file_name, file_size, file_md5

def apply_lease_by_file_path(client, category_id, workspace_id, file_path):
    file_name, file_size, file_md5 = get_file_info(file_path)
    return apply_lease(client, category_id, file_name, file_md5, file_size, workspace_id)


def upload_file_to_bailian(upload_url, headers, file_path):
    with open(file_path, "rb") as f:
        file_content = f.read()

    upload_headers = {
        "Content-Type": headers["Content-Type"],
        "X-bailian-extra":headers["X-bailian-extra"],
    }

    response = requests.put(upload_url, data = file_content, headers = upload_headers, timeout=(5, 30))
    response.raise_for_status()


def add_file_to_bailian_category(client, lease_id, parser, category_id, workspace_id):
    headers = {}
    runtime = util_models.RuntimeOptions()
    request = bailian_20231229_models.AddFileRequest(
        lease_id = lease_id,
        parser = parser,
        category_id = category_id,
    )
    return client.add_file_with_options(workspace_id, request, headers, runtime)


def describe_file(client, workspace_id, file_id):
    headers = {}
    runtime = util_models.RuntimeOptions()

    return client.describe_file_with_options(workspace_id, file_id, headers, runtime)


def create_index(client, workspace_id, name, file_id, structure_type="unstructured", source_type="DATA_CENTER_FILE", sink_type="BUILT_IN"):
    headers = {}
    runtime = util_models.RuntimeOptions()
    request = bailian_20231229_models.CreateIndexRequest(
        structure_type=structure_type,
        source_type=source_type,
        sink_type=sink_type,
        name=name,
        document_ids=[file_id],
    )
    return client.create_index_with_options(workspace_id, request, headers, runtime)

def submit_index(client, workspace_id, index_id):
    headers = {}
    runtime = util_models.RuntimeOptions()
    submit_index_job_request = bailian_20231229_models.SubmitIndexJobRequest(index_id=index_id)
    return client.submit_index_job_with_options(workspace_id, submit_index_job_request, headers, runtime)

def get_index_job_status(client, workspace_id, index_id, job_id):
    headers = {}
    runtime = util_models.RuntimeOptions()

    get_index_job_status_request = bailian_20231229_models.GetIndexJobStatusRequest(
        index_id = index_id,
        job_id = job_id,
    )

    return client.get_index_job_status_with_options(workspace_id, get_index_job_status_request, headers, runtime)

def list_indices(client, workspace_id):
    headers = {}
    runtime = util_models.RuntimeOptions()
    list_indices_request = bailian_20231229_models.ListIndicesRequest()
    return client.list_indices_with_options(workspace_id, list_indices_request, headers, runtime)

def submit_index_add_documents_job(client, workspace_id, index_id, file_id, source_type="DATA_CENTER_FILE"):
    headers = {}
    runtime = util_models.RuntimeOptions()
    submit_index_add_documents_job_request = bailian_20231229_models.SubmitIndexAddDocumentsJobRequest(
        index_id = index_id,
        document_ids = [file_id],
        source_type=source_type,
    )
    return client.submit_index_add_documents_job_with_options(workspace_id, submit_index_add_documents_job_request, headers, runtime)

def upload_rag_file_to_bailian(client, workspace_id, category_id, file_path):
    """
    上传文件到百炼数据中心，并添加到指定分类

    参数:
        client: 百炼客户端
        workspace_id: 业务空间ID
        category_id: 分类ID
        file_path: 文件路径

    返回:
        file_id
    """

    pass  # Protocol stdout never carries diagnostic or credential data.

    # 1. 申请文件租约
    lease = apply_lease_by_file_path(
        client,
        category_id,
        workspace_id,
        file_path
    )

    headers = lease.body.data.param.headers
    lease_id = lease.body.data.file_upload_lease_id
    upload_url = lease.body.data.param.url

    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.

    # 2. 上传文件到 OSS
    upload_file_to_bailian(
        upload_url,
        headers,
        file_path
    )

    # 3. 将文件登记到百炼数据中心
    add_file_response = add_file_to_bailian_category(
        client,
        lease_id,
        "DASHSCOPE_DOCMIND",
        category_id,
        workspace_id
    )

    file_id = add_file_response.body.data.file_id

    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.

    # 4. 查询文件状态
    describe_file_response = describe_file(
        client,
        workspace_id,
        file_id
    )

    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.
    pass  # Protocol stdout never carries diagnostic or credential data.

    return file_id

def add_document_to_index(client, workspace_id, index_id, file_id):
    job_response = submit_index_add_documents_job(client, workspace_id, index_id, file_id)
    job_id = job_response.body.data.id
    job_status = get_index_job_status(client, workspace_id, index_id, job_id)
    return job_status.body.data

if os.getenv("AI_AGENT_ENABLE_KNOWLEDGE_ADMIN") != "true":
    mcp.remove_tool("upload_rag_from_local_file_to_bailian_rag")

if __name__ == "__main__":
    mcp.run(transport="stdio")
    # rag_file_path = "D:/学习Java/ai项目/3.xiangmu/app/code_agent/rag/rag_test.txt"
    # rag_workspace_id = "llm-ngs9ec0pm3nunoaa"
    # rag_category_id = "cate_fcfc0f5907cf4f06a5bb2956cfd39399_10575390"
    # bailian_client = create_client()

    # upload_rag_file_to_bailian(bailian_client, rag_workspace_id, rag_category_id, rag_file_path)
    # response = create_index(bailian_client, rag_workspace_id, "智能体控制知识库", "file_435931d254144dc58119678fc554f561_10575390")
    # print(response)

    # rag_index_id = "8smmnpc1rm"
    # job_response = submit_index(bailian_client, rag_workspace_id, rag_index_id)
    # job_id = job_response.body.data.id
    # print("job_id:", job_id)

    # rag_job_id = "62a3680d789a41ccb8b2c04943cf11bc"
    # job_status = get_index_job_status(bailian_client, rag_workspace_id, rag_index_id, rag_job_id)
    # print(job_status.body.data)

    # list_indices_response = list_indices(bailian_client, rag_workspace_id)
    # print(list_indices_response)

    # rag_file_id = "file_435931d254144dc58119678fc554f561_10575390"
    # job_status = submit_index_add_documents_job(bailian_client, rag_workspace_id, rag_index_id, rag_file_id)
    # print(job_status.body.data)
