"""Generate a small public regression example; no model-quality benchmark."""

import subprocess
from pathlib import Path

BASE_SOURCE = '''def apply_discount(price: float, percent: float) -> float:
    """Return the price after a percentage discount."""
    if price < 0 or not 0 <= percent <= 100:
        raise ValueError("invalid price or discount")
    return price * (1 - percent / 100)
'''
HEAD_SOURCE = BASE_SOURCE.replace("percent / 100", "percent // 100")
TEST_SOURCE = """import unittest
from pricing import apply_discount


class DiscountTests(unittest.TestCase):
    def test_partial_discount(self):
        self.assertEqual(apply_discount(100, 20), 80)

    def test_zero_discount(self):
        self.assertEqual(apply_discount(100, 0), 100)


if __name__ == "__main__":
    unittest.main()
"""


def create_demo(repo: Path) -> tuple[str, str]:
    """Create two commits with public tests and one deliberately introduced defect."""
    repo.mkdir(parents=True, exist_ok=False)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Harness Demo")
    _git(repo, "config", "user.email", "demo@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pricing.py").write_text(BASE_SOURCE)
    (repo / "tests").mkdir()
    (repo / "tests/test_pricing.py").write_text(TEST_SOURCE)
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "working discount")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pricing.py").write_text(HEAD_SOURCE)
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "regression example")
    return base, _git(repo, "rev-parse", "HEAD")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
