import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def scenarios_dir(repo_root: Path) -> Path:
    return repo_root / "scenarios"


@pytest.fixture(scope="session")
def fixture_home(repo_root: Path) -> Path:
    return repo_root / "examples" / "hermes-home-fixture"