"""Layered, restrictive policies; no rule can grant capabilities absent from the platform."""
import fnmatch
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import PlatformError

CONTROLLED_ACTIONS = {"apply_patch", "run_tests", "export_patch", "run_powershell", "prepare_database", "drop_database"}


class PolicyRule(BaseModel):
    rule_id: str = Field(min_length=1, max_length=100)
    effect: Literal["allow", "ask", "deny"]
    tools: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=30)
    paths: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=30)
    modes: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def supported_approval(self):
        if self.effect == "ask" and not set(self.tools).issubset(CONTROLLED_ACTIONS):
            raise ValueError("ask 策略适用于已有人工审批流程的修改、验证和补丁交付操作；其他读取应显式 allow 或 deny。")
        return self


def evaluate(layers, actor, task, action, resources):
    matched = []
    for layer, definitions in layers:
        for definition in definitions:
            rule = definition if isinstance(definition, PolicyRule) else PolicyRule.model_validate(definition)
            if rule.modes and task["mode"] not in rule.modes or rule.users and actor.user_id not in rule.users or rule.roles and not actor.roles.intersection(rule.roles):
                continue
            actions = [action] if isinstance(action, str) else action
            if not any(fnmatch.fnmatchcase(candidate, pattern) for candidate in actions for pattern in rule.tools):
                continue
            # A denied path anywhere in a multi-file operation denies the whole operation.
            if not any(fnmatch.fnmatchcase(PurePosixPath(path.replace("\\", "/")).as_posix().lower(), pattern.lower()) for path in resources for pattern in rule.paths):
                continue
            matched.append({"layer": layer, "rule_id": rule.rule_id, "effect": rule.effect})
    effect = "deny" if any(item["effect"] == "deny" for item in matched) else "ask" if any(item["effect"] == "ask" for item in matched) else "allow"
    return {"effect": effect, "matched": matched}


def enforce(decision):
    if decision["effect"] == "deny":
        ids = ", ".join(item["rule_id"] for item in decision["matched"] if item["effect"] == "deny")
        raise PlatformError("policy_denied", "当前策略不允许此操作，匹配规则：" + ids, 403)
