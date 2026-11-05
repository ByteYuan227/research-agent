---
name: profile-setup
description: Create or update the job agent profile (~/.jobagent/profile.yaml) from the user's resume and a short interview, validate it, and help store it as GitHub secrets. Use when the user wants to set up the job agent, change their profile, or says something like "set up my job hunt" / "帮我配置求职 Agent".
---

# Set up the job agent profile

The profile is the only source of facts the job agent uses. Build it with the user;
never invent anything. Talk to the user in the language they use.

1. **Start from the template.** Run `jobagent init` (creates `~/.jobagent/profile.yaml`
   from `examples/profile.example.yaml` if it does not exist). Read the template so you
   know every field.

2. **Read their resume.** Ask for the path (PDF, Word export or text) if not given and
   read it. Fill `personal`, `education`, `experience`, `projects`, `skills`, `summaries`:
   - every entry and every bullet gets a short unique `id` (e.g. `exp_acme`, `acme_1`);
   - copy bullet facts exactly; you may split long bullets, never add numbers or tools;
   - if the resume has few bullets per role, ask whether they want to add more: the
     agent picks the most relevant 2-4 per job, so more material means better tailoring;
   - write 2-3 `summaries` variants (different emphasis), using only resume facts.

3. **Ask what a resume does not say** (one short message with all questions):
   - work authorization: U.S. citizen? green card? citizenship(s)? visa type (F-1, H-1B...)?
     needs sponsorship now / in the future? CPT/OPT eligible?
   - declarations: criminal record, pending charges, background-check and drug-test
     consent, non-compete, relatives at target companies, government official;
   - preferences: internship / new grad / full-time, terms (e.g. Summer 2027), roles and
     keywords, locations or remote, companies to avoid, minimum match score, how many
     applications per company within 30 days (`max_per_company`, default 3);
   - standard answers: earliest start date, graduation date, willing to relocate, salary
     expectation, how they usually hear about jobs, pronouns, languages;
   - voluntary EEO answers (default: decline to self-identify);
   - their GitHub username (for report @mentions) and whether they want email reports.
   Put recurring answers that have no dedicated field into `custom_answers`
   (`{match: "<regex>", answer: ...}`) and free-form facts into `about_me`.

4. **Validate.** Write the file, run `jobagent check` and fix every error it reports. Then
   run `jobagent init --sample-resume` and tell the user where the PDF is, so they can see
   how their resume will look.

5. **Store it for the daily cloud run.** Explain the remaining steps and offer to run the
   helper (it needs the GitHub CLI): `bash scripts/setup_secrets.sh ~/.jobagent/profile.yaml`.
   It also asks for the token from `claude setup-token`. Remind them to make the repository
   private, and that the first scheduled run is a rehearsal.

Never commit the profile to the repository: it contains personal data.
