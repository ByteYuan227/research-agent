"""The daily summary: what was found, what was applied to, what is waiting for you."""

from __future__ import annotations

import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

from .profile import Profile
from .store import Job, State

MANUAL_LIMIT = 80


def _md(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def _reason(state: State, job: Job) -> str:
    return next((e.detail for e in reversed(state.events) if e.job_id == job.id and e.type == "manual"), "")


def build_report(state: State, profile: Profile | None, day: dt.date | None = None, detail: str | None = None) -> str:
    """Markdown report for one day (in the profile's timezone).

    detail="public" leaves out score reasons, for public repositories.
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
    skipped = [j for j in jobs if j.status == "skipped"]
    closed = [j for j in jobs if j.status in ("closed", "duplicate")]
    retry = [j for j in jobs if j.status == "scored" and any(
        e.job_id == j.id and e.type == "scored" and "will retry next run" in e.detail for e in events)]
    # Claude's verdicts only (`jobagent score` records "<score>: <reason>"), not re-queues or manual marks.
    scored_today = [e for e in events if e.type in ("scored", "skipped") and re.match(r"\d+: ", e.detail)]
    new_manual = {j.id for j in jobs if j.status == "manual"}
    pending = sorted((j for j in state.jobs.values() if j.status == "manual"),
                     key=lambda j: (j.id not in new_manual, -(j.score or 0)))
    rehearsed = {e.job_id: e.detail for e in events if e.type == "dry_run"}
    new_seen = sum(1 for t in state.seen.values() if start <= t < end)
    run = state.last_run or {}

    lines = [f"# 求职日报 {day.isoformat()}", ""]
    if profile and profile.notify.github_username:
        lines += [f"@{profile.notify.github_username}", ""]
    if run.get("quota_exhausted"):
        lines += [f"> ⛔ **Claude 额度用完了**：{_md(str(run['quota_exhausted']))[:200]}",
                  "> 没处理完的岗位已经保留，下次运行会接着做。订阅用户等额度重置即可；用 API Key 的请到 Console 充值。", ""]
    if run.get("auth_error"):
        lines += ["> 🔑 **Claude 令牌无效或已过期**：请在本地重新运行 `claude setup-token`，把新令牌更新到仓库 Secret "
                  "`CLAUDE_CODE_OAUTH_TOKEN`。", ""]
    if run.get("setup_needed"):
        lines += ["> ⚠️ **还没有配置个人资料**，今天只做了岗位发现，没有打分和投递。配置方法见 README。", ""]

    lines += [
        "## 今日概览",
        "",
        f"- 新出现的岗位：**{new_seen}** 个（筛选后进入精读：{run.get('candidates', 0)} 个）",
        f"- Claude 打分：{len(scored_today)} 个",
        f"- ✅ 自动投递成功：**{len(applied)}**",
        f"- ✋ 待你手动投递：**{len(pending)}**（今天新增 {len(new_manual)}）",
        f"- ⏭️ 匹配度不够、已跳过：{len(skipped)}",
    ]
    if closed:
        n_dup = sum(1 for j in closed if j.status == "duplicate")
        lines.append(f"- 🚫 岗位已关闭或之前投过：{len(closed)}（之前投过 {n_dup} 个），不用管")
    if retry:
        lines.append(f"- 🔁 中途中断、下次自动重试：{len(retry)}")
    if rehearsed:
        ok = sum(1 for d in rehearsed.values() if d == "dry_run_ok")
        lines.append(f"- 🧪 演练模式：{len(rehearsed)} 个申请已准备好但没有提交（成功 {ok} 个）")
    lines.append("")

    if applied:
        lines += ["## ✅ 今天投递的岗位", "", "| 公司 | 岗位 | 匹配度 | 地点 | |", "|---|---|---|---|---|"]
        for j in applied:
            app = j.application or {}
            flag = " ⚠️ 未看到确认页，留意确认邮件" if app.get("mode") == "browser" and app.get("confirmed_by_page") is False else ""
            lines.append(f"| {_md(j.company)} | {_md(j.title)}{flag} | {j.score or '-'} | {_md(', '.join(j.locations[:2]))} "
                         f"| [链接]({j.url}) |")
        lines.append("")
        if full:
            lines += [f"- **{_md(j.company)}**：{_md(j.reason)}" for j in applied if j.reason] + [""]

    if pending:
        lines += [
            "## ✋ 待你手动投递",
            "",
            "这些岗位匹配度够，但没能自动提交（原因写在后面）。定制好的简历和准备好的回答都已保存，见下方「材料在哪」。"
            "**投完一个就在这里勾选**，下次运行会记为已投递。",
            "",
        ]
        for j in pending[:MANUAL_LIMIT]:
            new = "🆕 " if j.id in new_manual else ""
            score = f" · {j.score} 分" if j.score is not None else ""
            reason = _reason(state, j)
            maybe = "⚠️ **可能已经提交，先查邮件再决定要不要重投** · " if "submit button was pressed" in reason else ""
            lines.append(f"- [ ] {new}**{_md(j.company)}** · {_md(j.title)}{score} · {maybe}{_md(reason)[:120]} · "
                         f"[申请链接]({j.url}) <!-- job:{j.id} -->")
        if len(pending) > MANUAL_LIMIT:
            lines.append(f"- …另有 {len(pending) - MANUAL_LIMIT} 个，见 `jobagent materials open`")
        lines += [
            "",
            "<details><summary>材料在哪</summary>",
            "",
            "- 配置了邮箱的话，今天新增岗位的定制简历已作为附件发到你的邮箱。",
            "- 私有仓库：本次运行记录页面底部的 Artifacts 里有全部材料（保留 14 天）。",
            "- 任何仓库：材料加密保存在 `jobagent-data` 分支的 `materials.enc`，在你电脑上运行 "
            "`JOBAGENT_STATE_KEY=<密钥> jobagent materials open materials.enc` 解开，每个岗位一个文件夹：简历、求职信、"
            "准备好的回答（answers.md）和截图。",
            "",
            "</details>",
            "",
        ]

    if full and skipped:
        lines += ["<details><summary>跳过的岗位</summary>", ""]
        for j in sorted(skipped, key=lambda j: -(j.score or 0))[:40]:
            why = j.reason or next((e.detail for e in reversed(state.events) if e.job_id == j.id and e.type == "skipped"), "")
            lines.append(f"- {j.score or '-'} 分 · {_md(j.company)} - {_md(j.title)}：{_md(why)[:160]}")
        lines += ["", "</details>", ""]

    learned = run.get("learned") or []
    if learned:
        lines += [f"<details><summary>🧠 新记住的回答（{len(learned)} 个，以后遇到同样的问题会自动使用）</summary>", ""]
        for q in learned[:40]:
            entry = next((v for v in state.memory.values() if v.get("question", "")[:160] == q), None)
            answer = f" → {_md(str(entry['answer']))[:80]}" if (full and entry) else ""
            lines.append(f"- {_md(q)[:140]}{answer}")
        lines += ["", "答得不对的话，在资料的 `custom_answers` 里写上正确答案即可覆盖。", "", "</details>", ""]

    notes = run.get("notes") or []
    if notes:
        lines += ["## 📝 Agent 备注", ""] + [f"- {_md(n)}" for n in notes] + [""]

    auto_total = sum(1 for j in state.jobs.values() if j.status == "applied")
    manual_total = sum(1 for j in state.jobs.values() if j.status == "applied_manual")
    week_start = start - 6 * 86400
    week = sum(1 for j in state.jobs.values() if j.applied_at and week_start <= j.applied_at < end)
    lines += ["## 📈 累计", "", f"- 近 7 天投递：{week}", f"- 总投递：{auto_total + manual_total}（自动 {auto_total}，你手动 {manual_total}）", ""]
    if run.get("errors"):
        lines += ["<details><summary>运行中的问题</summary>", ""] + [f"- {_md(e)[:200]}" for e in run["errors"][:20]] + ["", "</details>", ""]
    if run.get("run_url"):
        lines.append(f"运行记录：{run['run_url']}")
    return "\n".join(lines).strip() + "\n"
