"""Package the review playbooks for DeepAgents' on-demand skill loader."""

from importlib import resources

from deepagents.backends.protocol import FileData
from deepagents.backends.utils import create_file_data

SKILLS_ROOT = "/skills/review/"
_SKILL_NAMES = ("python-boundary-regressions", "python-api-compatibility")
_MAX_SKILL_CHARS = 2400


def skill_files() -> dict[str, FileData]:
    """Mount trusted, small package skills into the agent's ephemeral state."""
    package = resources.files("pr_review_harness.review_skills")
    files: dict[str, FileData] = {}
    for name in _SKILL_NAMES:
        content = package.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        if not content or len(content) > _MAX_SKILL_CHARS:
            raise ValueError(f"Bundled review skill {name} must contain 1-{_MAX_SKILL_CHARS} chars")
        files[f"{SKILLS_ROOT}{name}/SKILL.md"] = create_file_data(content)
    return files
