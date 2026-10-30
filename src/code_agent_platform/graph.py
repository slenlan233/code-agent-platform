"""Java / Spring / MyBatis graph PoC with explicit unresolved relationships."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path

from pydantic import BaseModel, Field

from .models import PlatformError, digest, timestamp
from .security import SECRET, files


class GraphArguments(BaseModel):
    symbol: str = Field(min_length=1, max_length=200)
    direction: str = "callees"
    depth: int = Field(default=3, ge=1, le=5)


def descend(node):
    yield node
    for child in node.named_children:
        yield from descend(child)


def tables(sql):
    return sorted(set(re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+[`\"]?([a-zA-Z_][a-zA-Z_0-9.]*)", sql, re.I)))


class CodeGraph:
    def __init__(self, platform):
        self.platform = platform

    def _inputs(self, project, actor, task=None):
        result = {}
        for name, path in files(Path(project["root"]), limit=self.platform.settings.max_scan_files):
            if not self.platform.gateway.source_allowed(actor, task, project, name, "graph_query"):
                continue
            if path.suffix in {".java", ".xml"} and path.stat().st_size <= self.platform.settings.max_file_bytes:
                raw = path.read_bytes()
                if not SECRET.search(raw.decode("utf-8", errors="replace")):
                    result[name] = raw
        return result

    def build(self, actor, project_id):
        actor.require("developer")
        project = self.platform.project(actor, project_id)
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_java
        except ImportError:
            raise PlatformError("graph_dependency", "请安装 graph 可选依赖。") from None
        parser = Parser(Language(tree_sitter_java.language()))
        inputs = self._inputs(project, actor)
        fingerprint = {name: digest(raw) for name, raw in inputs.items()}
        record_id = "graph_" + project_id
        try:
            previous = self.platform.store.get("graph", record_id)
        except PlatformError:
            previous = {}
        cache, nodes, pending, unknowns, fields = {}, [], [], [], {}
        parsed = 0
        for name, raw in inputs.items():
            if name.endswith(".xml"):
                continue
            if previous.get("fingerprint", {}).get(name) == fingerprint[name]:
                item = previous.get("cache", {}).get(name)
            else:
                item = None
            if item is None:
                item = self._parse_java(parser, name, raw)
                parsed += 1
            cache[name] = item
            nodes.extend(item["nodes"])
            pending.extend(item["calls"])
            unknowns.extend(item["unknowns"])
            fields.update(item["fields"])
        methods = defaultdict(list)
        classes = defaultdict(set)
        for node in nodes:
            if node["type"] == "Function":
                methods[(node["class"], node["name"], node["arity"])].append(node)
                classes[node["class"].split(".")[-1]].add(node["class"])
        edges = []
        for call in pending:
            receiver = call["receiver"]
            target_class = call["class"] if receiver in {"", "this"} else fields.get(call["class"], {}).get(receiver, receiver)
            if "." not in target_class:
                candidates = classes.get(target_class, set())
                same_package = [value for value in candidates if value.rsplit(".", 1)[0] == call["package"]]
                target_class = same_package[0] if len(same_package) == 1 else next(iter(candidates)) if len(candidates) == 1 else target_class
            targets = methods.get((target_class, call["name"], call["arity"]), [])
            if len(targets) == 1:
                edges.append({"from": call["from"], "to": targets[0]["id"], "type": "CALLS", "path": call["path"], "line": call["line"], "source": "ast+receiver_type", "certainty": "resolved"})
            else:
                unknowns.append({"path": call["path"], "line": call["line"], "reason": "目标类型、重载或动态调用未唯一解析", "symbol": call["name"]})
        for node in list(nodes):
            if node["type"] != "Function":
                continue
            for table in node.get("tables", []):
                self._table_edge(nodes, edges, node, table, "annotation")
            if node.get("api"):
                api = {"id": "api:" + node["id"], "name": node["api"], "type": "API", "path": node["path"], "line": node["line"]}
                nodes.append(api)
                edges.append({"from": api["id"], "to": node["id"], "type": "HANDLED_BY", "path": node["path"], "line": node["line"], "source": "spring_annotation", "certainty": "resolved"})
        for name, raw in inputs.items():
            if not name.endswith(".xml"):
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                unknowns.append({"path": name, "reason": "XML 无法解析"})
                continue
            if root.tag != "mapper":
                continue
            namespace = root.attrib.get("namespace", "")
            for statement in root:
                matches = [node for node in nodes if node["type"] == "Function" and node["class"] == namespace and node["name"] == statement.attrib.get("id")]
                if len(matches) != 1:
                    continue
                for table in tables(" ".join(statement.itertext())):
                    self._table_edge(nodes, edges, matches[0], table, "mybatis_xml", path=name)
        result = {"id": record_id, "project_id": project_id, "fingerprint": fingerprint, "cache": cache, "nodes": list({node["id"]: node for node in nodes}.values()), "edges": edges, "unknowns": unknowns, "build_id": digest(fingerprint), "indexed_at": timestamp(), "parsed_files": parsed}
        self.platform.store.put("graph", result)
        return {key: result[key] for key in ("build_id", "indexed_at", "parsed_files")} | {"nodes": len(result["nodes"]), "edges": len(edges), "unresolved": len(unknowns), "backend": "local_poc"}

    @staticmethod
    def _table_edge(nodes, edges, method, table, source, path=None):
        node = {"id": "table:" + table, "name": table, "type": "Table", "path": path or method["path"], "line": method["line"]}
        nodes.append(node)
        edges.append({"from": method["id"], "to": node["id"], "type": "ACCESSES_TABLE", "path": path or method["path"], "line": 1 if path else method["line"], "source": source, "certainty": "resolved"})

    @staticmethod
    def _parse_java(parser, path, raw):
        tree = parser.parse(raw)
        text = lambda node: raw[node.start_byte:node.end_byte].decode("utf-8") if node else ""
        package = ""
        for child in tree.root_node.named_children:
            if child.type == "package_declaration":
                package = text(child).replace("package", "", 1).strip(" ;\n\r")
        nodes, calls, unknowns, field_types = [], [], [], {}
        if tree.root_node.has_error:
            unknowns.append({"path": path, "reason": "AST 含语法错误，关系可能不完整"})
        for class_node in descend(tree.root_node):
            if class_node.type not in {"class_declaration", "interface_declaration"}:
                continue
            class_name = text(class_node.child_by_field_name("name"))
            qualified = package + "." + class_name if package else class_name
            body = class_node.child_by_field_name("body")
            if body is None:
                continue
            fields = {}
            for child in body.named_children:
                if child.type == "field_declaration":
                    type_name = text(child.child_by_field_name("type")).split("<")[0]
                    for declaration in child.named_children:
                        if declaration.type == "variable_declarator":
                            fields[text(declaration.child_by_field_name("name"))] = type_name
            field_types[qualified] = fields
            class_prefix = raw[class_node.start_byte:body.start_byte].decode("utf-8")
            base_route = re.search(r'@RequestMapping\s*\(\s*"([^"\n]*)"', class_prefix)
            for method in body.named_children:
                if method.type != "method_declaration":
                    continue
                name = text(method.child_by_field_name("name"))
                parameters = method.child_by_field_name("parameters")
                parameter_nodes = [item for item in parameters.named_children if item.type in {"formal_parameter", "spread_parameter"}] if parameters else []
                signature = ",".join(text(item.child_by_field_name("type")) for item in parameter_nodes)
                method_id = f"{path}:{qualified}.{name}({signature})"
                content = text(method)
                route = re.search(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?"([^"\n]*)"', content)
                api = None
                if route:
                    api = route.group(1).upper() + " " + ((base_route.group(1) if base_route else "") + route.group(2))
                node = {"id": method_id, "name": name, "qualified_name": qualified + "." + name + "(" + signature + ")", "class": qualified, "type": "Function", "arity": len(parameter_nodes), "path": path, "line": method.start_point.row + 1, "end_line": method.end_point.row + 1, "api": api, "tables": tables(content) if re.search(r"@(?:Select|Insert|Update|Delete)", content) else []}
                nodes.append(node)
                for call in descend(method):
                    if call.type == "method_invocation":
                        arguments = call.child_by_field_name("arguments")
                        calls.append({"from": method_id, "class": qualified, "package": package, "receiver": text(call.child_by_field_name("object")), "name": text(call.child_by_field_name("name")), "arity": len(arguments.named_children) if arguments else 0, "path": path, "line": call.start_point.row + 1})
        return {"nodes": nodes, "calls": calls, "unknowns": unknowns, "fields": field_types}

    def query(self, actor, project_id, symbol, direction="callees", depth=3, *, task=None):
        project = self.platform.project(actor, project_id)
        if direction not in {"callers", "callees"} or not 1 <= depth <= 5:
            raise PlatformError("graph_query", "图查询方向或深度不合法。")
        try:
            graph = self.platform.store.get("graph", "graph_" + project_id)
        except PlatformError:
            return {"status": "not_indexed", "nodes": [], "edges": [], "unknowns": ["尚未建立代码索引。"]}
        allowed = lambda path: self.platform.gateway.source_allowed(actor, task, project, path, "graph_query")
        visible_fingerprint = {name: value for name, value in graph["fingerprint"].items() if allowed(name)}
        if {name: digest(raw) for name, raw in self._inputs(project, actor, task).items()} != visible_fingerprint:
            return {"status": "stale", "nodes": [], "edges": [], "unknowns": ["代码已变化，需要更新索引。"]}
        visible_nodes = [node for node in graph["nodes"] if allowed(node["path"])]
        node_ids = {node["id"] for node in visible_nodes}
        visible_edges = [edge for edge in graph["edges"] if edge["from"] in node_ids and edge["to"] in node_ids and allowed(edge["path"])]
        visible_unknowns = [item for item in graph["unknowns"] if not isinstance(item, dict) or allowed(item.get("path", "."))]
        graph = {**graph, "nodes": visible_nodes, "edges": visible_edges, "unknowns": visible_unknowns}
        candidates = [node for node in graph["nodes"] if symbol in {node["id"], node["name"], node.get("qualified_name"), node.get("class", "") + "." + node["name"]}]
        if len(candidates) != 1:
            return {"status": "ambiguous" if candidates else "not_found", "candidates": candidates, "nodes": [], "edges": [], "unknowns": ["请使用包含类名和参数签名的唯一符号。"]}
        nodes = {node["id"]: node for node in graph["nodes"]}
        seen, selected, queue = {candidates[0]["id"]}, [], deque([(candidates[0]["id"], 0)])
        while queue and len(selected) < 50:
            current, distance = queue.popleft()
            if distance >= depth:
                continue
            for edge in graph["edges"]:
                origin, target = (edge["from"], edge["to"]) if direction == "callees" else (edge["to"], edge["from"])
                if origin == current:
                    selected.append(edge)
                    if target not in seen:
                        seen.add(target)
                        queue.append((target, distance + 1))
        return {"status": "ready", "build_id": graph["build_id"], "nodes": [nodes[key] for key in sorted(seen)], "edges": selected, "unknowns": graph["unknowns"], "complete": False, "backend": "local_poc"}

    def register_tool(self):
        def query(actor, task, project, symbol, direction="callees", depth=3):
            result = self.query(actor, project["id"], symbol, direction, depth, task=task)
            result["evidence"] = []
            seen = set()
            for edge in result.get("edges", []):
                key = (edge["path"], edge["line"])
                if key not in seen:
                    seen.add(key)
                    result["evidence"].append(self.platform.gateway.evidence(task, project, edge["path"], start_line=edge["line"], limit=10, kind="graph"))
            return result
        self.platform.gateway.tools["graph_query"] = (GraphArguments, query)
