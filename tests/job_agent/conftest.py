from pathlib import Path

import pytest
import yaml

from job_agent.profile import Profile

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def profile_data():
    return yaml.safe_load((ROOT / "examples" / "profile.example.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def profile(profile_data):
    return Profile.model_validate(profile_data)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOBAGENT_PROFILE", str(ROOT / "examples" / "profile.example.yaml"))
    monkeypatch.delenv("JOBAGENT_PROFILE_YAML", raising=False)
    monkeypatch.delenv("JOBAGENT_STATE", raising=False)
    return tmp_path / "home"
