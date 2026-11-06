# Job Agent

**[English](#english) · [中文说明](#中文)**

A Claude-powered agent that finds US internship / new-grad jobs every day, tailors your resume to each job description, applies on any application site (Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, company portals...), reads verification emails, and sends you an evening report. It runs on GitHub Actions with **your own Claude subscription** — no API key needed.

> This repository also contains a deep-research agent: see [docs/research-agent.md](docs/research-agent.md).

每天自动找美国实习 / 应届岗位，按 JD 定制简历，在任何招聘网站上自动投递（Greenhouse、Lever、Ashby、Workday、iCIMS、SmartRecruiters、公司官网……），自动读取邮箱验证码，每晚发日报。运行在 GitHub Actions 上，用的是**你自己的 Claude 订阅额度**，不需要 API Key。

> 本仓库还有一个深度研究 Agent，见 [docs/research-agent.md](docs/research-agent.md)。

---

## English

### What it does every day

| Time (US Eastern) | What happens |
|---|---|
| 7:17 pm | Pulls every open role from the SimplifyJobs US internship / new-grad lists and the career pages of companies you follow; filters by job type, term, location, visa restrictions, recency and duplicates |
| then | Claude reads each job description and scores it 0–100; hard blockers (citizenship, clearance, no sponsorship) are skipped |
| then | For every job above your threshold: tailors a one-page resume to the JD → opens the application → signs up if needed (generated password, stored encrypted) → reads the email verification code → fills every step, uploads the resume, answers the questions → submits |
| ~8 pm onward | Posts a daily report issue that @-mentions you (and optionally emails it): what was submitted, what needs you, interview invitations in your inbox |

There is **no cap**, per day or per company (optional: `max_per_company: 3` limits applications to one company within 30 days to the 3 best matches). Fresh postings go first. Anything that cannot be submitted automatically (CAPTCHAs, documents only you have…) keeps its tailored resume and prepared answers and goes on the report's **apply-by-hand checklist**. Postings that have closed, and ones the site says you already applied to, are recognised and kept off your list.

### Quick start (3 steps)

> You need: a Claude Pro / Max subscription, a GitHub account, and Python 3.10+, Node.js 18+ and [Claude Code](https://code.claude.com/docs) on your computer (already there if you use Claude Code in VS Code).

**1. Public or private repository?** Choose before you start:

| | Public | Private |
|---|---|---|
| GitHub Actions minutes | free and unlimited | GitHub Free: 2,000 a month ([billing docs](https://docs.github.com/billing/managing-billing-for-your-products/managing-billing-for-github-actions)), so set the repository variable `JOBAGENT_MAX_MINUTES` to `60` (about 10 applications a day) |
| Your profile, passwords, resumes | never visible: Secrets and encrypted state | same |
| Daily report issue | visible to everyone (companies and roles you applied to, without score reasons) | only you |

To go private: Repository → Settings → General → Change visibility → Make private.

**2. Let Claude build your profile.** Clone the repository, open Claude Code in it (VS Code or terminal) and say:

> Set up my job agent profile from my resume at ~/Downloads/resume.pdf

Claude reads your resume, asks what a resume does not say (visa status, sponsorship, background declarations, preferences), writes `~/.jobagent/profile.yaml` and validates it.

**3. Store the configuration in GitHub with one command.** It needs the [GitHub CLI](https://cli.github.com) (run `gh auth login` first). The script validates the profile, creates the encryption key and asks you to paste the subscription token from `claude setup-token`:

```bash
bash scripts/setup_secrets.sh ~/.jobagent/profile.yaml
```

Done. Go to GitHub → Actions → **job-hunt** → Run workflow with **dry_run** checked to rehearse (prepares everything, submits nothing), then read the report under Issues. From then on it runs daily; **the first scheduled run is always a rehearsal**, so real submissions start the day after.

### Detailed setup (manual)

1. **Install**
   ```bash
   git clone https://github.com/<your-username>/research-agent.git
   ```
   ```bash
   cd research-agent && python -m venv .venv && source .venv/bin/activate
   ```
   ```bash
   pip install -e . && python -m playwright install chromium
   ```
2. **Profile**: `jobagent init` creates `~/.jobagent/profile.yaml`. Fill it in (every field is commented), then run `jobagent check` until it reports `"profile": "ok"`.
3. **Subscription token**: run `claude setup-token`, approve in the browser, and copy the `sk-ant-oat…` token (valid for one year).
4. **Encryption key**: run `python -m job_agent keygen` and copy the output.
5. **(Recommended) Gmail app password**: Google Account (2-step verification on) → Security → App passwords. Used to read verification codes, track interview invitations and email you the report (with tailored resumes for the apply-by-hand list attached).
6. **Secrets**: Repository → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value | Required |
|---|---|---|
| `JOBAGENT_PROFILE_YAML` | the whole profile.yaml | ✅ |
| `CLAUDE_CODE_OAUTH_TOKEN` | token from step 3 | ✅ (or `ANTHROPIC_API_KEY` for pay-as-you-go) |
| `JOBAGENT_STATE_KEY` | key from step 4 (**keep a copy: without it the history cannot be read**) | ✅ |
| `JOBAGENT_IMAP_USER` | your Gmail address | recommended |
| `JOBAGENT_IMAP_PASSWORD` | the Gmail app password | recommended |

7. **Rehearse**: Actions → job-hunt → Run workflow → check dry_run.

### One switch for the run mode

Repository → Settings → Secrets and variables → Actions → **Variables** tab → New repository variable named `JOBAGENT_MODE`:

| Value | Effect |
|---|---|
| `apply` (or unset) | Normal: find, score, apply, report |
| `rehearse` | Rehearsal: every application is prepared but nothing is submitted; the report still comes |
| `off` | Paused: find jobs and report only |

To pick a model, add the variable `JOBAGENT_CLAUDE_MODEL` (e.g. `claude-opus-5-5`). To limit the daily run time (private repositories), add `JOBAGENT_MAX_MINUTES` (default `300`).

### Your profile: how answers are tailored to you

`profile.yaml` is the agent's only source of facts; it never invents experience, numbers or eligibility.

| Section | Used for |
|---|---|
| `personal` | Name, contact details, links, address. Workday and similar portals require a mailing address: fill in `location.street` |
| `education`, `experience`, `projects`, `skills` | Resume material. Every bullet has an id; the agent selects and orders them per job and may reword them in the JD's language — checked so that **no number or technology/company name absent from your profile can appear** |
| `work_authorization` | Citizenship / permanent residency, citizenships, visa type, sponsorship. Non-citizens are never answered as citizens; citizenship-only jobs are filtered out |
| `declarations` | Background questions answered in advance: criminal record, pending charges, background-check / drug-test consent, non-compete, relatives at the company, government official |
| `custom_answers` | Your own answers: `{match: "<regex>", answer: "..."}`; they override every built-in rule |
| `about_me` | Free-form facts Claude may use for other questions |
| `answers` | Common answers (start date, relocation, salary, EEO…), plus automatic acceptance of terms / arbitration / privacy notices (`accept_agreements`) and AI-written answers (`write_answers`) |
| `preferences` | Job types, terms, keywords, locations, excluded companies, minimum score (`min_score`), daily cap (`max_applications_per_day: null` = unlimited), optional per-company cap over 30 days (`max_per_company`, default `null` = unlimited) |

**Checked before submitting**: before pressing Submit, the program compares what the page shows with your profile (work authorization, sponsorship, citizenship, declarations such as criminal record, your `custom_answers`, name and email). On any contradiction it does not submit until the answer is fixed. Titles that limit a role to undergraduates / graduate students / another term, to one school or one career fair, and contract marketplaces such as Mercor are filtered out before scoring.

Answers Claude gives to non-standard questions are remembered and reused for the same question later (listed in the report as learned answers). Sensitive questions (EEO, health, criminal record, salary, identity details) and essays are never remembered. If a learned answer is wrong, add the correct answer to `custom_answers`.

### The report and the apply-by-hand checklist

- One issue per day; older ones are closed. Jobs waiting for you form a **cumulative checklist** with links and reasons. **Tick a box once you have applied**: the next run records it as applied by you.
- "⚠️ may already be submitted": the submit button was pressed but the run ended before the result was seen. The agent never applies twice on its own; check your email for a confirmation first.
- "⚠️ no confirmation page" in the applied table: submitted, but the page showed no clear confirmation; watch for the confirmation email.
- Applications cut short by a timeout or a browser error are retried once in the next run.
- Tailored resumes and prepared answers:
  - with email configured: today's resumes are attached to the email report;
  - private repositories: the run's Artifacts (kept 14 days);
  - any repository: encrypted on the `jobagent-data` branch. Unpack them on your computer:
    ```bash
    git fetch origin jobagent-data && git show FETCH_HEAD:materials.enc > materials.enc
    ```
    ```bash
    JOBAGENT_STATE_KEY=<your key> jobagent materials open materials.enc
    ```
- All applications as a spreadsheet: fetch `state.enc` the same way, run `jobagent state import state.enc`, then `jobagent export applications.csv`.
- Accounts the agent created: `jobagent account list`; a site's password: `jobagent account password --site <domain>` (your own computer only).

### When Claude's quota or token runs out

| Situation | What the agent does | What you do |
|---|---|---|
| Subscription limit reached (5-hour / weekly) | Stops at once, says so at the top of the report, keeps unfinished jobs for the next run | Usually nothing; upgrade the plan to apply to more per day |
| API credit exhausted | Same | Top up in the Claude Console |
| Token invalid or expired (valid one year) | Stops and says so in the report | Run `claude setup-token` again and update `CLAUDE_CODE_OAUTH_TOKEN` |
| Run longer than 5 hours | Stops starting new applications; the rest continue tomorrow | Nothing |
| CAPTCHA | Never solved; the job goes on your checklist | Apply by hand |
| Site wants camera / video / ID photo / a fee / a download | Never done; the job goes on your checklist | Decide yourself |

The subscription quota is shared with your own use of Claude, so on heavy days you may hit your limit sooner in the evening.

### Running on your own computer (no GitHub)

With Claude Code logged in and `~/.jobagent/profile.yaml` filled in:

```bash
JOBAGENT_DRY_RUN=1 bash scripts/daily_run.sh
```

Drop `JOBAGENT_DRY_RUN=1` to apply for real. See the results:

```bash
jobagent report
```

### What it will not do

- Invent facts, or make false statements for you (such as certifying that no AI was used). Jobs that require one are prepared and handed to you.
- Solve CAPTCHAs or evade bot detection.
- Grant camera, microphone or location access; do video interviews, selfies or ID checks; sign up on contract / crowd-work marketplaces; pay anything; download or install anything.
- Log in to LinkedIn / Indeed (account bans); they are not job sources either.
- Type personal data anywhere but the employer's own site or a known applicant system. Text on job pages ("ignore previous instructions"…) is treated as data, never as instructions.
- Show passwords to Claude: they are generated and typed by the program.

### Command cheat sheet

| Command | What it does |
|---|---|
| `jobagent init` / `jobagent check` | create the profile template / check the setup |
| `jobagent discover` | fetch and filter jobs |
| `jobagent status` / `jobagent report` | counts / today's report |
| `jobagent materials open materials.enc` | unpack materials for the apply-by-hand list |
| `jobagent export applications.csv` | all applications as CSV |
| `jobagent memory` | learned answers |
| `jobagent --help` | every command |

Design and implementation notes: [docs/job-agent.md](docs/job-agent.md).

---

## 中文

### 它每天做什么

| 时间（美东） | 做什么 |
|---|---|
| 19:17 | 从 SimplifyJobs 美国实习 / 应届岗位库、你关注的公司招聘页拉取所有在招岗位，按类型、实习季、地点、签证限制、发布时间、重复岗位初筛 |
| 随后 | Claude 逐个读 JD 打分（0–100），要求公民身份 / 安全许可等硬性不符的直接跳过 |
| 随后 | 对每个达到分数线的岗位：按 JD 定制一页简历 → 打开申请页 → 需要注册就注册（密码自动生成并加密保存）→ 读邮箱验证码 → 逐页填表、上传简历、回答问题 → 提交 |
| 约 20:00 起 | 在 GitHub 发日报 issue 并 @ 你（可同时发邮件）：投了哪些、哪些需要你手动投、邮箱里的面试邀请 |

**不设投递上限**，能投的都投，同一家公司也不限（想限制的话可以设 `max_per_company`，比如 3 = 同一家公司 30 天内最多投 3 个，挑匹配度最高的）。新发布的岗位优先投。投不成的（验证码、需要你本人提供的材料等）会把定制好的简历和准备好的回答保存下来，进入日报里的「待你手动投递」清单。已经关闭的岗位、网站提示你投过的岗位会自动识别，不会占用你的时间。

### 快速开始（3 步）

> 前提：Claude Pro / Max 订阅；GitHub 账号；电脑上装了 Python 3.10+、Node.js 18+ 和 [Claude Code](https://code.claude.com/docs)（在 VS Code 里用过 Claude Code 就已经有了）。

**第 1 步：仓库用公开还是私有？** 开始前先选：

| | 公开 | 私有 |
|---|---|---|
| GitHub Actions 运行时长 | 免费、不限 | 免费账号每月 2000 分钟（[官方说明](https://docs.github.com/billing/managing-billing-for-your-products/managing-billing-for-github-actions)），需要把仓库变量 `JOBAGENT_MAX_MINUTES` 设为 `60`（每天约 10 个申请） |
| 你的资料、密码、简历 | 都不会公开：存在 Secrets 和加密状态里 | 同左 |
| 每日日报 issue | 所有人可见（投了哪些公司和岗位，不含打分理由） | 只有你能看 |

改成私有：GitHub 仓库页 → Settings → General → 最下方 Change visibility → Make private。

**第 2 步：让 Claude 帮你生成个人资料。** 克隆仓库，在仓库目录里打开 Claude Code（VS Code 或终端都行），然后说：

> 帮我配置求职 Agent，我的简历在 ~/Downloads/resume.pdf

Claude 会读你的简历、问你几个简历上没有的问题（签证、是否需要担保、犯罪记录等事先声明、求职偏好），生成 `~/.jobagent/profile.yaml` 并检查格式。

**第 3 步：一条命令把配置存进 GitHub。** 需要 [GitHub CLI](https://cli.github.com)，先运行 `gh auth login`。然后运行下面的脚本，它会检查资料、生成加密密钥，并提示你粘贴 `claude setup-token` 生成的订阅令牌：

```bash
bash scripts/setup_secrets.sh ~/.jobagent/profile.yaml
```

完成。去 GitHub → Actions → **job-hunt** → Run workflow，勾选 **dry_run** 先演练一次（只准备、不提交），然后在 Issues 里看日报。之后每天自动运行；**第一次定时运行也会自动按演练处理**，第二天才开始正式投递。

### 详细配置步骤（手动方式）

不想用脚本的话，按下面做，效果一样。

1. **安装**
   ```bash
   git clone https://github.com/<你的用户名>/research-agent.git
   ```
   ```bash
   cd research-agent && python -m venv .venv && source .venv/bin/activate
   ```
   ```bash
   pip install -e . && python -m playwright install chromium
   ```
2. **创建资料**：运行 `jobagent init`，它会生成 `~/.jobagent/profile.yaml`。按里面的中文注释填写，然后运行 `jobagent check` 直到显示 `"profile": "ok"`。
3. **订阅令牌**：在终端运行 `claude setup-token`，浏览器里授权后会输出一个 `sk-ant-oat…` 开头的令牌，有效期一年。
4. **加密密钥**：运行 `python -m job_agent keygen`，复制输出。
5. **（推荐）Gmail 应用专用密码**：Google 账号开启两步验证 → 安全 → 应用专用密码，创建一个。用于读取验证码、跟踪面试邀请、给你发邮件版日报（附带待手动投递岗位的定制简历）。
6. **添加 Secrets**：仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 内容 | 必需 |
|---|---|---|
| `JOBAGENT_PROFILE_YAML` | 整个 profile.yaml 的内容 | ✅ |
| `CLAUDE_CODE_OAUTH_TOKEN` | 第 3 步的令牌 | ✅（或用 `ANTHROPIC_API_KEY` 按量付费） |
| `JOBAGENT_STATE_KEY` | 第 4 步的密钥（**丢了就读不回历史记录**，请另存一份） | ✅ |
| `JOBAGENT_IMAP_USER` | Gmail 地址 | 推荐 |
| `JOBAGENT_IMAP_PASSWORD` | Gmail 应用专用密码 | 推荐 |

7. **演练**：Actions → job-hunt → Run workflow → 勾选 dry_run。

### 一个开关控制运行模式

仓库 → Settings → Secrets and variables → Actions → **Variables** 标签 → New repository variable，名字填 `JOBAGENT_MODE`：

| 值 | 效果 |
|---|---|
| `apply`（或不设置） | 正常：找岗位、打分、投递、发日报 |
| `rehearse` | 演练：所有申请都准备好但不提交，日报照发 |
| `off` | 暂停投递：只找岗位、发日报 |

想换模型的话，再加一个变量 `JOBAGENT_CLAUDE_MODEL`（例如 `claude-opus-5-5`）。想限制每天运行时长（私有仓库），加变量 `JOBAGENT_MAX_MINUTES`（默认 `300`）。

### 个人资料：怎么让 Agent 按你的情况回答

`profile.yaml` 是 Agent 唯一的事实来源。它不会编造任何经历、数字或身份信息。

| 部分 | 用途 |
|---|---|
| `personal` | 姓名、联系方式、链接、地址。Workday 等网站要求邮寄地址，请在 `location.street` 填上 |
| `education`、`experience`、`projects`、`skills` | 简历素材。每条 bullet 有 id，Agent 按 JD 挑选、排序，可以换个说法贴近 JD 用词，但程序会检查：**不能出现原文没有的数字、资料里没有的技术或公司名** |
| `work_authorization` | 是否公民 / 绿卡、国籍、签证类型、是否需要担保。非公民不会被答成公民，要求公民身份的岗位自动过滤 |
| `declarations` | 事先声明：犯罪记录、待决指控、背调 / 药检同意、竞业协议、亲属在职、政府官员。遇到这类问题直接作答 |
| `custom_answers` | 自定义问答：`{match: "正则", answer: "答案"}`，匹配到的问题优先用你的答案，覆盖所有内置规则 |
| `about_me` | 几条自由文本事实，Claude 回答其他问题时可以引用 |
| `answers` | 常用回答（开始日期、是否愿意搬迁、薪资期望、EEO 等），以及：服务条款 / 仲裁 / 隐私协议自动同意（`accept_agreements`），AI 写回答（`write_answers`） |
| `preferences` | 岗位类型、实习季、关键词、地点、排除公司、最低分（`min_score`）、每天上限（`max_applications_per_day: null` 表示不限）、同一公司 30 天内上限（`max_per_company`，默认 `null` 不限） |

**提交前核对**：点「提交」之前，程序会把页面上显示的答案和你的资料对一遍（工作许可、是否需要 sponsor、公民身份、犯罪记录等声明、你的 `custom_answers`、姓名和邮箱）。有矛盾就不提交，先改正。标题里写明只招本科生 / 研究生 / 其他学期、只面向某个学校或某个招聘会、或者是 Mercor 这类外包平台的岗位，会在打分前直接过滤掉。

Claude 回答过的非标准问题会被记住（「学到的回答」），下次遇到同样的问题自动使用，日报里会列出来。敏感问题（EEO、健康、犯罪记录、薪资、身份信息）和长文回答不会被记住。答错了就在 `custom_answers` 里写正确答案覆盖。

### 日报和「待你手动投递」清单

- 每天一个 issue，旧的自动关闭。待手动投递的岗位是**累计清单**，每项带申请链接和原因；**投完在 issue 里勾选**，下次运行记为「你已手动投递」。
- 标着「⚠️ 可能已经提交」的岗位：提交按钮已经按下，但运行在确认之前中断了。程序不会再自动投第二次，请先查邮箱有没有确认信。
- 投递表格里标着「⚠️ 未看到确认页」的：提交了但页面没有出现明确的确认文字，留意确认邮件。
- 中途超时或浏览器出错的岗位，下次运行会自动重试一次。
- 定制简历和准备好的回答：
  - 配了邮箱：当天新增岗位的简历作为附件发到邮箱；
  - 私有仓库：当次运行页面底部的 Artifacts（保留 14 天）；
  - 任何仓库：加密保存在 `jobagent-data` 分支，在你电脑上解开：
    ```bash
    git fetch origin jobagent-data && git show FETCH_HEAD:materials.enc > materials.enc
    ```
    ```bash
    JOBAGENT_STATE_KEY=<你的密钥> jobagent materials open materials.enc
    ```
- 导出所有投递记录为表格：先用同样方式取出 `state.enc`，运行 `jobagent state import state.enc`，再运行 `jobagent export applications.csv`。
- Agent 注册的网站账号：`jobagent account list`；查看某个网站的密码：`jobagent account password --site <域名>`（只能在你自己电脑上运行）。

### 额度用完、令牌过期怎么办

| 情况 | Agent 的处理 | 你需要做 |
|---|---|---|
| Claude 订阅额度用完（5 小时 / 每周上限） | 立刻停止，日报顶部写明，没投完的岗位保留，下次运行接着做 | 一般不用管；想多投可以升级订阅 |
| API Key 余额不足 | 同上 | 到 Claude Console 充值 |
| 令牌失效或过期（一年有效） | 停止并在日报提示 | 重新运行 `claude setup-token`，更新 Secret `CLAUDE_CODE_OAUTH_TOKEN` |
| 运行时间超过 5 小时 | 停止开始新的投递，剩下的明天继续 | 不用管 |
| 遇到图形验证码 | 不破解，进入手动清单 | 手动投递 |
| 网站要求开摄像头 / 录视频 / 上传证件照 / 付费 / 下载软件 | 一律不做，进入手动清单 | 自己判断要不要继续 |

订阅额度和你平时用 Claude 是共用的。投递量大的日子，晚上那几个小时你自己用 Claude 可能会更快碰到上限。

### 在自己电脑上运行（不用 GitHub）

已登录 Claude Code、填好 `~/.jobagent/profile.yaml` 后：

```bash
JOBAGENT_DRY_RUN=1 bash scripts/daily_run.sh
```

去掉 `JOBAGENT_DRY_RUN=1` 就是正式投递。查看结果：

```bash
jobagent report
```

### 它不会做的事

- 不编造任何事实，也不替你做虚假声明（比如「我声明没有使用 AI」）。需要这类声明的岗位会准备好全部材料，转给你手动处理。
- 不破解验证码，不绕过网站的机器人检测。
- 不授权摄像头、麦克风、定位；不做视频面试、自拍或证件验证；不在外包 / 众包平台注册；不付费；不下载安装任何东西。
- 不登录你的 LinkedIn / Indeed（容易封号），岗位来源也不包括它们。
- 个人信息只会填进招聘方自己的网站或已知的招聘系统。岗位页面里的「忽略之前的指令」之类文字会被当作数据，不会执行。
- 密码由程序生成和填写，Claude 看不到。

### 常用命令

| 命令 | 作用 |
|---|---|
| `jobagent init` / `jobagent check` | 创建资料模板 / 检查配置 |
| `jobagent discover` | 拉取并筛选岗位 |
| `jobagent status` / `jobagent report` | 统计 / 当天日报 |
| `jobagent materials open materials.enc` | 解开待手动投递岗位的材料 |
| `jobagent export applications.csv` | 导出投递记录 |
| `jobagent memory` | 查看学到的回答 |
| `jobagent --help` | 全部命令 |

设计和实现细节见 [docs/job-agent.md](docs/job-agent.md)。

---

## License

MIT
