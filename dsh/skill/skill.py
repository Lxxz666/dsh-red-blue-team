"""
dsh.skill.skill —— Skills 子系统（ctx.skills + tool-skill）。

对应 TS 版 skill + skill-filesystem + tool-skill：

- Skill = {id, name, description, instructions}（`~/.dsh/skills/<id>/SKILL.md`，
  带 YAML front matter: name/description）；
- ``ctx.skills`` 汇聚 provider（文件系统 provider 为默认）；
- 工具 ``skill_list``（目录快照）与 ``skill_load``（把指令经 defer_context
  注入下一请求）；注册变化广播 ``skills/change``。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.skill")


@dataclass
class Skill:
    """一个可加载技能。"""

    id: str
    name: str
    description: str = ""
    instructions: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "description": self.description}


class SkillService(Service):
    """技能 provider 注册表（ctx.skills）。"""

    provides = "skills"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._providers: List[Any] = []

    def apply(self, ctx) -> None:
        ctx.set("skills", self)

    def register_provider(self, provider) -> None:
        """注册技能 provider（list() -> [Skill]）。"""
        self._providers.append(provider)
        self.ctx.events.emit("skills/change")

    def list(self) -> List[Skill]:
        """全部技能（provider 顺序拼接，按 id 去重）。"""
        merged: Dict[str, Skill] = {}
        for provider in self._providers:
            for skill in provider.list():
                merged[skill.id] = skill
        return list(merged.values())

    def get(self, skill_id: str) -> Optional[Skill]:
        for skill in self.list():
            if skill.id == skill_id:
                return skill
        return None


class FilesystemSkillProvider:
    """从 `~/.dsh/skills/<id>/SKILL.md` 发现技能。"""

    def __init__(self, paths: Optional[List[str]] = None) -> None:
        self.paths = paths or [os.path.join(os.path.expanduser("~"), ".dsh",
                                            "skills")]

    def list(self) -> List[Skill]:
        skills: List[Skill] = []
        for root in self.paths:
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                skill_dir = os.path.join(root, entry)
                if not os.path.isdir(skill_dir):
                    continue
                markdown = os.path.join(skill_dir, "SKILL.md")
                if not os.path.exists(markdown):
                    continue
                try:
                    skills.append(self._parse(entry, markdown))
                except Exception:
                    log.exception("skill parse failed: %s", markdown)
        return skills

    @staticmethod
    def _parse(skill_id: str, path: str) -> Skill:
        text = open(path, "r", encoding="utf-8").read()
        name, description, body = skill_id, "", text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    name = str(meta.get("name") or skill_id)
                    description = str(meta.get("description") or "")
                    body = parts[2]
                except yaml.YAMLError:
                    pass
        return Skill(id=skill_id, name=name, description=description,
                     instructions=body.strip())


def build_skill_tools() -> List[Any]:
    """构造 skill_* 工具族。"""

    def _skills_of(run_ctx):
        agent = run_ctx.execution.agent
        ctx = agent.ctx if agent is not None else run_ctx.root_ctx
        if not ctx.has("skills"):
            from ..errors import ToolError
            raise ToolError("skills service not mounted", code="NO_SKILLS")
        return ctx.skills

    @define_tool(
        name="skill_list",
        description="列出可用技能目录（id/name/description）。",
        parameters={}, output={"type": "array", "items": {"type": "object"}})
    async def skill_list(args, run_ctx):
        return [s.to_json() for s in _skills_of(run_ctx).list()]

    @define_tool(
        name="skill_load",
        description="加载技能的完整指令到下一轮上下文。",
        parameters={"id": {"type": "string", "required": True}},
        output={"type": "string"})
    async def skill_load(args, run_ctx):
        skill = _skills_of(run_ctx).get(args["id"])
        if skill is None:
            from ..errors import ToolError
            raise ToolError(f"unknown skill: {args['id']}", code="UNKNOWN_SKILL")
        run_ctx.defer_context({
            "content": f"[skill:{skill.id}] {skill.instructions}",
            "source": {"kind": "plugin", "plugin": "skill",
                       "skill": skill.id}})
        return f"loaded skill {skill.id}: {skill.name}"

    return [skill_list, skill_load]


class SkillsPlugin(Service):
    """Skills 插件：文件系统 provider + 工具（base bundle 行）。"""

    inject = ("skills", "tools")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []
        self._provider = FilesystemSkillProvider(
            paths=(config or {}).get("paths"))

    def apply(self, ctx) -> None:
        ctx.skills.register_provider(self._provider)
        for tool in build_skill_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
