"""scripts/daily_run.sh with a stand-in `claude` that has run out of quota or lost its token."""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _setup(tmp_path, monkeypatch, claude_output: str):
    home = tmp_path / "home"
    profile = yaml.safe_load((ROOT / "examples" / "profile.example.yaml").read_text())
    profile["preferences"]["sources"] = {"simplify_internships": False, "simplify_new_grad": False, "companies": []}
    profile["preferences"]["categories"] = []
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile))
    jobs = [dict(id=f"greenhouse:acme:{i}", source="test", company="Acme", title=f"ML Intern {i}",
                 url=f"http://127.0.0.1:9/{i}", apply_url=f"http://127.0.0.1:9/{i}", ats="greenhouse",
                 ats_company="acme", ats_job_id=str(i), posted_at=time.time(), terms=["Summer 2027"],
                 job_type="internship", locations=["Remote (US)"]) for i in (1, 2)]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs))
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"#!/usr/bin/env bash\necho {json.dumps(claude_output)}\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, JOBAGENT_HOME=str(home), JOBAGENT_PROFILE=str(tmp_path / "profile.yaml"),
               JOBAGENT_EXTRA_JOBS=str(tmp_path / "jobs.json"), PATH=f"{fake.parent}:{os.environ['PATH']}")
    env.pop("JOBAGENT_PROFILE_YAML", None)
    return env, home


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Claude AI usage limit reached|1793900000", "quota_exhausted"),
        ("5-hour limit reached ∙ resets 11pm", "quota_exhausted"),
        ("API Error: 400 Your credit balance is too low to access the Anthropic API.", "quota_exhausted"),
        ("API Error: 401 authentication_error: OAuth token has expired", "auth_error"),
    ],
)
def test_quota_and_auth_stop_cleanly(tmp_path, monkeypatch, message, kind):
    env, home = _setup(tmp_path, monkeypatch, message)
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    state = json.loads((home / "state.json").read_text())
    assert state["last_run"].get(kind), state["last_run"]
    # Scoring never happened, so both jobs are still waiting; nothing was lost or marked failed.
    assert {j["status"] for j in state["jobs"].values()} == {"candidate"}
    report = subprocess.run(["jobagent", "report"], env=env, capture_output=True, text=True).stdout
    assert ("额度用完" if kind == "quota_exhausted" else "令牌无效") in report


def test_quota_during_applications_requeues_the_job(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    fake = tmp_path / "bin" / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == *"Mode SCORE"* ]]; then\n'
        "  jobagent score greenhouse:acme:1 90 --reason r --decision apply > /dev/null\n"
        "  jobagent score greenhouse:acme:2 85 --reason r --decision apply > /dev/null\n"
        "  echo 'scored 2'; exit 0\n"
        "fi\n"
        "echo 'Claude AI usage limit reached|1793900000'; exit 1\n"
    )
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    state = json.loads((home / "state.json").read_text())
    assert state["last_run"]["quota_exhausted"]
    # The interrupted job and the one never started are both kept for the next run.
    assert [j["status"] for j in state["jobs"].values()] == ["scored", "scored"]
    queue = subprocess.run(["jobagent", "queue"], env=env, capture_output=True, text=True).stdout.split()
    assert queue == ["greenhouse:acme:1", "greenhouse:acme:2"]
