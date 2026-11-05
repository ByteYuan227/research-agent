# Repository guide

Two agents live here:

- `research_agent/`: a CLI deep-research agent on the Claude API (hand-written tool loop).
- `job_agent/`: a job-search agent. Python code does the deterministic work (discovery,
  filtering, resume rendering, browser form filling, encrypted state); Claude does the
  judgment, driving the `jobagent` CLI. The daily procedure is the `job-hunt` skill in
  `.claude/skills/job-hunt/SKILL.md`; it runs from `.github/workflows/job-hunt.yml`.

## Working on the code

- Install: `pip install -e '.[dev]'`; tests: `pytest -q` (browser tests need Chromium;
  set `JOBAGENT_CHROMIUM` to a Chromium binary if Playwright's bundled one is missing).
- Never commit personal data. Profiles live outside the repo (`~/.jobagent/profile.yaml`
  or the `JOBAGENT_PROFILE_YAML` secret); `examples/profile.example.yaml` is fictional.
- Form-filling changes should be checked against real forms with the `dev-probe` workflow
  (`dry_run_fill` or `pipeline_smoke`); those modes never submit applications.
- Keep the guarantees: resumes only select profile ids (`job_agent/resume.py`), file
  uploads are limited to the generated resume/cover letter, and anything the profile
  cannot answer goes to the manual queue.

## Running the job hunt yourself

Ask: "run today's job hunt using the job-hunt skill". It needs a profile and a browser.
