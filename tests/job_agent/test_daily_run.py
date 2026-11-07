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
    env["JOBAGENT_SCORE"] = "1"  # these tests exercise the separate scoring pass
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


def _models_fake(tmp_path, opus_out_of_quota=False):
    """A stand-in claude that records the model of each session and scores two jobs (90 and 80)."""
    calls = tmp_path / "calls.txt"
    quota = 'if [[ "$model" == "opus" ]]; then echo "Claude AI usage limit reached|1793900000"; exit 1; fi\n' \
        if opus_out_of_quota else ""
    (tmp_path / "bin" / "claude").write_text(
        "#!/usr/bin/env bash\n"
        'model=""; prev=""; for a in "$@"; do if [[ "$prev" == "--model" ]]; then model="$a"; fi; prev="$a"; done\n'
        'kind="other"; if [[ "$2" == *"Mode SCORE"* ]]; then kind=score; elif [[ "$2" == *"Mode APPLY"* ]]; then kind=apply; '
        'elif [[ "$2" == *"Mode INBOX"* ]]; then kind=inbox; fi\n'
        f'echo "$kind $model" >> {calls}\n'
        'if [[ "$kind" == score ]]; then\n'
        "  jobagent score greenhouse:acme:1 90 --reason r --decision apply > /dev/null\n"
        "  jobagent score greenhouse:acme:2 80 --reason r --decision apply > /dev/null\n"
        "fi\n"
        + quota +
        'if [[ "$kind" == apply ]]; then id=$(echo "$2" | grep -oE "greenhouse:acme:[0-9]+"); '
        'jobagent mark "$id" --status manual --reason "test" > /dev/null; fi\n'
        "echo ok; exit 0\n"
    )
    return calls


def test_models_sonnet_for_most_opus_for_best_matches(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    env["JOBAGENT_MODEL_TOP"] = "opus"
    calls = _models_fake(tmp_path)
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    assert calls.read_text().split("\n")[:4] == ["score sonnet", "apply opus", "apply sonnet", "inbox sonnet"]


def test_opus_out_of_quota_continues_on_sonnet(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    env["JOBAGENT_MODEL_TOP"] = "opus"
    calls = _models_fake(tmp_path, opus_out_of_quota=True)
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    # The 90-point job was retried on Sonnet right away; the rest of the day ran on Sonnet.
    assert calls.read_text().split("\n")[:5] == ["score sonnet", "apply opus", "apply sonnet", "apply sonnet", "inbox sonnet"]
    state = json.loads((home / "state.json").read_text())
    assert not state["last_run"].get("quota_exhausted")
    assert any("额度用完" in n for n in state["last_run"].get("notes", []))
    assert {j["status"] for j in state["jobs"].values()} == {"manual"}


def test_one_model_for_everything(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    env["JOBAGENT_CLAUDE_MODEL"] = "claude-opus-5-5"
    calls = _models_fake(tmp_path)
    subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert set(calls.read_text().split()) - {"score", "apply", "inbox"} == {"claude-opus-5-5"}


def test_one_job_only(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    calls = _models_fake(tmp_path)
    # Score both jobs first (as an earlier run would have), then apply to one of them only.
    subprocess.run(["jobagent", "discover"], env=env, capture_output=True, text=True)
    for jid, score in (("greenhouse:acme:1", "90"), ("greenhouse:acme:2", "80")):
        subprocess.run(["jobagent", "score", jid, score, "--reason", "r", "--decision", "apply"], env=env, capture_output=True)
    env["JOBAGENT_ONLY_JOB"] = "greenhouse:acme:2"
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    assert calls.read_text().split("\n")[:2] == ["apply sonnet", ""]  # no discovery, scoring or inbox session
    state = json.loads((home / "state.json").read_text())
    assert state["jobs"]["greenhouse:acme:2"]["status"] == "manual" and state["jobs"]["greenhouse:acme:1"]["status"] == "scored"


def test_default_applies_without_a_scoring_pass(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    env.pop("JOBAGENT_SCORE")
    calls = _models_fake(tmp_path)
    p = subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    kinds = calls.read_text().split("\n")
    assert "score sonnet" not in kinds and kinds.count("apply sonnet") == 2  # both jobs, straight to applying


def test_sonnet_everywhere_by_default(tmp_path, monkeypatch):
    env, home = _setup(tmp_path, monkeypatch, "unused")
    calls = _models_fake(tmp_path)
    subprocess.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert set(calls.read_text().split()) - {"score", "apply", "inbox"} == {"sonnet"}
