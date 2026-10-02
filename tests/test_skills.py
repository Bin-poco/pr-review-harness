from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from pr_review_harness.skills import SKILLS_ROOT, skill_files

_BOUNDARY_PATH = f"{SKILLS_ROOT}python-boundary-regressions/SKILL.md"
_BODY_ONLY_SENTENCE = "Check zero, negative, empty, and upper-bound inputs"


class SkillProbeModel(BaseChatModel):
    system_prompts: list[str] = Field(default_factory=list)
    read_results: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "skill-probe"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.system_prompts.append(
            "\n".join(str(m.content) for m in messages if m.type == "system")
        )
        tool_results = [m for m in messages if isinstance(m, ToolMessage) and m.name == "read_file"]
        if tool_results:
            self.read_results.extend(str(m.content) for m in tool_results)
            message = AIMessage(content="Skill read complete.")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": _BOUNDARY_PATH, "limit": 1000},
                        "id": "probe-read-skill",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_packaged_skills_are_discovered_and_loaded_only_on_read():
    files = skill_files()
    assert set(files) == {
        _BOUNDARY_PATH,
        f"{SKILLS_ROOT}python-api-compatibility/SKILL.md",
    }

    model = SkillProbeModel()
    agent = create_deep_agent(model=model, backend=StateBackend(), skills=[SKILLS_ROOT])
    agent.invoke(
        {"messages": [{"role": "user", "content": "Review a boundary condition"}], "files": files},
        config={"recursion_limit": 20},
    )

    assert "python-boundary-regressions" in model.system_prompts[0]
    assert _BOUNDARY_PATH in model.system_prompts[0]
    assert _BODY_ONLY_SENTENCE not in model.system_prompts[0]
    assert any(_BODY_ONLY_SENTENCE in result for result in model.read_results)
