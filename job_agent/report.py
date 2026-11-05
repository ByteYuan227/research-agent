"""The daily summary: what was found, what was applied to, what needs you."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from .profile import Profile
from .store import Job, State


def _link(job: Job) -> str:
    return f"[链接]({job.url})" if job.url else ""


def _md(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def build_report(state: State, profile: Profile | None, day: dt.date | None = None, detail: str | None = None) -> str:
    """Markdown report for one day (in the profile's timezone).

    detail="public" leaves out score reasons and answers, for public repositories.
    """
    tz = ZoneInfo(profile.notify.timezone if profile else "America/New_York")
    day = day or dt.datetime.now(tz).date()
    start = dt.datetime.combine(day, dt.time(0), tz).timestamp()
    end = start + 86400
    detail = detail or os.environ.get("JOBAGENT_REPORT_DETAIL", "public")
    full = detail == "full"

    events = state.events_between(start, end)
    touched = {e.job_id for e in events}
    jobs = [state.jobs[j] for j in touched if j in state.jobs]
    applied = sorted((j for j in jobs if j.status == "applied" and j.applied_at and start <= j.applied_at < end),
                     key=lambda j: -(j.score or 0))
    manual = sorted((j for j in jobs if j.status == "manual"), key=lambda j: -(j.score or 0))
    skipped = [j for j in jobs if j.status == "skipped"]
    failed = [j for j in jobs if j.status == "failed"]
    scored_today = [e for e in events if e.type in ("scored", "skipped")]
    new_seen = sum(1 for t in state.seen.values() if start <= t < end)
    run = state.last_run or {}

    lines = [f"# 求职日报 {day.isoformat()}", ""]
    if profile and profile.notify.github_username:
        lines += [f"@{profile.notify.github_username}", ""]
    if run.get("setup_needed"):
        lines += ["> ⚠️ **还没有配置个人资料**，今天只做了岗位发现，没有打分和投递。配置方法见 `docs/job-agent.md`。", ""]
    lines += [
        "## 今日概览",
        "",
        f"- 新出现的岗位：**{new_seen}** 个（筛选后进入精读：{run.get('candidates', 0)} 个）",
        f"- Claude 打分：{len(scored_today)} 个",
        f"- ✅ 已投递：**{len(applied)}**",
        f"- ✋ 需要你手动处理：**{len(manual)}**",
        f"- ⏭️ 匹配度不够、已跳过：{len(skipped)}",
    ]
    if failed:
        lines.append(f"- ⚠️ 出错：{len(failed)}")
    rehearsed = {e.job_id: e.detail for e in events if e.type == "dry_run"}
    if rehearsed:
        ok = sum(1 for d in rehearsed.values() if d == "dry_run_ok")
        lines.append(f"- 🧪 演练模式：{len(rehearsed)} 个申请表已填好但没有提交（成功 {ok} 个）")
    lines.append("")

    if applied:
        lines += ["## ✅ 今天投递的岗位", "", "| 公司 | 岗位 | 匹配度 | 地点 | |", "|---|---|---|---|---|"]
        for j in applied:
            lines.append(f"| {_md(j.company)} | {_md(j.title)} | {j.score or '-'} | {_md(', '.join(j.locations[:2]))} | {_link(j)} |")
        lines.append("")
        if full:
            for j in applied:
                if j.reason:
                    lines.append(f"- **{_md(j.company)}**：{_md(j.reason)}")
            lines.append("")

    if manual:
        lines += ["## ✋ 需要你手动处理", "", "这些岗位匹配度够，但 Agent 不能替你完成，原因写在后面。定制好的简历在运行记录里。", "",
                  "| 公司 | 岗位 | 匹配度 | 原因 | |", "|---|---|---|---|---|"]
        for j in manual:
            why = next((e.detail for e in reversed(state.events) if e.job_id == j.id and e.type == "manual"), "")
            lines.append(f"| {_md(j.company)} | {_md(j.title)} | {j.score or '-'} | {_md(why)[:140]} | {_link(j)} |")
        lines.append("")

    if failed:
        lines += ["## ⚠️ 出错", ""]
        for j in failed:
            lines.append(f"- {_md(j.company)} - {_md(j.title)} {_link(j)}")
        lines.append("")

    if full and skipped:
        lines += ["<details><summary>跳过的岗位</summary>", ""]
        for j in sorted(skipped, key=lambda j: -(j.score or 0))[:40]:
            lines.append(f"- {j.score or '-'} 分 · {_md(j.company)} - {_md(j.title)}：{_md(j.reason)[:160]}")
        lines += ["", "</details>", ""]

    notes = run.get("notes") or []
    if notes:
        lines += ["## 📝 Agent 备注", ""]
        lines += [f"- {_md(n)}" for n in notes]
        lines.append("")

    total_applied = sum(1 for j in state.jobs.values() if j.status == "applied")
    week_start = start - 6 * 86400
    week = sum(1 for j in state.jobs.values() if j.applied_at and week_start <= j.applied_at < end)
    lines += ["## 📈 累计", "", f"- 近 7 天投递：{week}", f"- 总投递：{total_applied}", ""]
    if run.get("errors"):
        lines += ["<details><summary>运行中的问题</summary>", ""] + [f"- {_md(e)[:200]}" for e in run["errors"][:20]] + ["", "</details>", ""]
    if run.get("run_url"):
        lines.append(f"运行记录：{run['run_url']}")
    return "\n".join(lines).strip() + "\n"
