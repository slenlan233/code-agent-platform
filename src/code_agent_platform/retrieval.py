from __future__ import annotations

import json
import math
import re
from datetime import date
from collections import Counter, defaultdict
from pathlib import Path

from .models import PlatformError
from .security import SECRET, files, safe_path


def terms(text):
    words = re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*|[\u4e00-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.search(r"[\u4e00-\u9fff]", word):
            result.extend(word[index:index + 2] for index in range(max(len(word) - 1, 1)))
        else:
            result.append(word)
    return result


def document_entries(project):
    root = Path(project["root"])
    try:
        manifest = safe_path(root, ".agent/knowledge.json")
    except PlatformError as error:
        if error.code != "not_found":
            raise
        manifest = None
    if manifest:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entries = data.get("documents", [])
        if len(entries) > 500:
            raise PlatformError("knowledge_limit", "知识文档清单过大。")
        return [item for item in entries if item.get("status", "active") == "active" and item.get("project_id", project["id"]) == project["id"] and item.get("effective_date", date.today().isoformat()) <= date.today().isoformat()]
    entries = []
    for folder in project["knowledge_paths"]:
        try:
            location = safe_path(root, folder)
        except PlatformError as error:
            if error.code == "not_found":
                continue
            raise
        candidates = files(location, limit=500) if location.is_dir() else [(folder, location)]
        for _, path in candidates:
            if path.suffix.lower() in {".md", ".txt", ".rst"}:
                entries.append({"path": path.relative_to(root).as_posix(), "status": "active"})
    return entries


def search_documents(gateway, task, project, query, limit, actor=None):
    root = Path(project["root"])
    candidates = []
    for metadata in document_entries(project):
        if actor is not None and not gateway.source_allowed(actor, task, project, metadata["path"], "rag_search"):
            continue
        path = safe_path(root, metadata["path"])
        if path.stat().st_size > gateway.settings.max_file_bytes:
            continue
        content = path.read_text(encoding="utf-8-sig")
        if SECRET.search(content):
            continue
        # 按标题和完整行切分，不在字符中间截断代码行。
        lines = content.splitlines()
        start = 0
        for index in range(len(lines) + 1):
            boundary = index == len(lines) or (index > start and (lines[index].startswith("#") or index - start >= 100))
            if boundary:
                chunk = "\n".join(lines[start:index])
                if chunk.strip():
                    candidates.append((metadata, start + 1, index - start, Counter(terms(chunk))))
                start = index
    query_terms = set(terms(query))
    frequencies = Counter()
    for _, _, _, counts in candidates:
        frequencies.update(counts.keys())
    average = sum(sum(item[3].values()) for item in candidates) / max(len(candidates), 1)
    ranked = []
    for metadata, start, length, counts in candidates:
        score = 0.0
        for term in query_terms:
            frequency = counts[term]
            if frequency:
                inverse = math.log(1 + (len(candidates) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * sum(counts.values()) / max(average, 1)))
        if score:
            ranked.append((score, metadata, start, length))
    ranked.sort(key=lambda item: (-item[0], -item[1].get("priority", 0), item[1]["path"], item[2]))
    evidence = [gateway.evidence(task, project, metadata["path"], start_line=start, limit=length, kind="document", metadata=metadata) for _, metadata, start, length in ranked[:limit]]
    decisions = defaultdict(lambda: defaultdict(list))
    for item in evidence:
        metadata = gateway.store.get("evidence", item["source_id"])["metadata"]
        if metadata.get("rule_id") and metadata.get("decision"):
            decisions[metadata["rule_id"]][metadata["decision"]].append(item["source_id"])
    conflicts = [{"rule_id": rule, "sources": [source for group in values.values() for source in group]} for rule, values in decisions.items() if len(values) > 1]
    return {"evidence": evidence, "conflicts": conflicts, "method": "bm25", "unknowns": ["语义矛盾检测和向量召回尚未启用。"]}


def fuse_rankings(*rankings, k=60):
    scores, objects = defaultdict(float), {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            key = (item["path"], item["start_line"], item["content_hash"])
            objects[key] = item
            scores[key] += 1 / (k + rank)
    return [objects[key] for key in sorted(scores, key=lambda item: -scores[item])]
