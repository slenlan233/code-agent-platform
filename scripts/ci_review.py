"""Explicit CI model review; emits an artifact without posting comments or changing files."""
from __future__ import annotations

import json
import os
from pathlib import Path

from code_agent_platform.config import Settings
from code_agent_platform.models import LOCAL_ACTOR, Mode, PlatformError, ProjectInput, TaskInput
from code_agent_platform.service import Platform


def main():
    settings = Settings.load(os.getenv("AI_AGENT_CONFIG"))
    output = Path(os.getenv("AI_AGENT_REVIEW_OUTPUT", ".temp/ci-review.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    platform = Platform(settings)
    try:
        root = Path.cwd().resolve()
        project = next((item for item in platform.projects(LOCAL_ACTOR) if Path(item["root"]) == root), None)
        if project is None:
            project = platform.register_project(LOCAL_ACTOR, ProjectInput(name="CI checkout", root=str(root)))
        task = platform.create_task(LOCAL_ACTOR, TaskInput(project_id=project["id"], mode=Mode.REVIEW, user_input="审查提交差异，输出可行动的问题与依据。", base_ref=os.getenv("AI_AGENT_REVIEW_BASE", "HEAD~1"), head_ref=os.getenv("AI_AGENT_REVIEW_HEAD", "HEAD")))
        platform.run(LOCAL_ACTOR, task["id"])
        result = platform.detail(LOCAL_ACTOR, task["id"])
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        issues = result["task"].get("review", {}).get("issues", [])
        return 2 if os.getenv("AI_AGENT_REVIEW_BLOCKING") == "true" and any(item["severity"] == "blocking" for item in issues) else 0
    except PlatformError as error:
        output.write_text(json.dumps({"status": "failed", "error": {"code": error.code, "message": error.message}}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    finally:
        platform.close()


if __name__ == "__main__":
    raise SystemExit(main())
