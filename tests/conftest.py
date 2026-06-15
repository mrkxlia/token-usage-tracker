from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def claude_root() -> Path:
    """匿名化した Claude Code ログのルート（projects 相当）。"""
    return FIXTURES / "claude_projects"
