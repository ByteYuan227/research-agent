"""Command-line interface: ``research-agent "你的问题"``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agent import DEFAULT_MODEL, AgentConfig, Hooks, ResearchAgent
from .output import save_result

STATUS_ICON = {"pending": "○", "in_progress": "◐", "done": "●"}


class ConsoleHooks(Hooks):
    """Prints the agent's progress to the terminal."""

    def __init__(self, console: Console, show_thinking: bool = True) -> None:
        self.console = console
        self.show_thinking = show_thinking
        self._in_text = False

    def _end_text(self) -> None:
        if self._in_text:
            self.console.print()
            self._in_text = False

    def on_turn_start(self, turn: int, max_turns: int) -> None:
        self._end_text()
        self.console.rule(f"[dim]第 {turn}/{max_turns} 轮", style="dim")

    def on_text(self, text: str) -> None:
        self._in_text = True
        self.console.print(text, end="", markup=False, highlight=False)

    def on_block(self, block: Any) -> None:
        kind = getattr(block, "type", None)
        if kind == "thinking" and self.show_thinking:
            summary = (getattr(block, "thinking", "") or "").strip()
            if summary:
                self._end_text()
                if len(summary) > 300:
                    summary = summary[:300] + "…"
                self.console.print(f"[dim italic]💭 {summary}[/]", highlight=False)
        elif kind == "server_tool_use":
            self._end_text()
            tool_input = getattr(block, "input", {}) or {}
            if block.name == "web_search":
                self.console.print(f"[cyan]🔍 搜索[/] {tool_input.get('query', '')}", highlight=False)
            elif block.name == "web_fetch":
                self.console.print(f"[blue]📄 阅读[/] {tool_input.get('url', '')}", highlight=False)
        elif kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            if isinstance(content, list):
                self.console.print(f"[dim]   找到 {len(content)} 条结果[/]")
            else:
                self.console.print(f"[yellow]   搜索失败：{getattr(content, 'error_code', content)}[/]")
        elif kind == "web_fetch_tool_result":
            content = getattr(block, "content", None)
            error = getattr(content, "error_code", None)
            if error:
                self.console.print(f"[yellow]   读取失败：{error}[/]")

    def on_tool_result(self, name: str, tool_input: dict, content: str, is_error: bool) -> None:
        self._end_text()
        if is_error:
            self.console.print(f"[red]⚠ {name}：{content}[/]", highlight=False)
        elif name == "update_plan":
            table = Table(title="研究计划", show_header=False, box=None, padding=(0, 1))
            for i, q in enumerate(tool_input.get("sub_questions", []), start=1):
                status = q.get("status", "pending")
                table.add_row(STATUS_ICON.get(status, "○"), f"{i}. {q.get('question', '')}")
            self.console.print(table)
        elif name == "record_finding":
            claim = tool_input.get("claim", "")
            self.console.print(f"[green]📝 {content}[/] {claim}", highlight=False)
        elif name == "review_findings":
            self.console.print("[magenta]📚 回顾研究笔记[/]")

    def on_notice(self, message: str) -> None:
        self._end_text()
        self.console.print(f"[yellow]ℹ {message}[/]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="research-agent",
        description="深度研究 Agent：自动检索、阅读、核实，输出带引用的研究报告。",
    )
    p.add_argument("question", nargs="?", help="研究问题；不填则进入交互输入")
    p.add_argument("-o", "--out-dir", default="reports", help="报告输出目录（默认 reports/）")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"模型 ID（默认 {DEFAULT_MODEL}）")
    p.add_argument(
        "--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"],
        help="思考力度，越高越仔细也越贵（默认 high）",
    )
    p.add_argument("--max-turns", type=int, default=30, help="最多调用模型的轮数（默认 30）")
    p.add_argument("--searches-per-turn", type=int, default=5, help="每轮最多搜索次数（默认 5）")
    p.add_argument("--fetches-per-turn", type=int, default=5, help="每轮最多读取网页数（默认 5）")
    p.add_argument("--no-fallbacks", action="store_true", help="关闭拒答时自动切换备用模型")
    p.add_argument("--hide-thinking", action="store_true", help="不显示思考摘要")
    p.add_argument("--save-transcript", action="store_true", help="另存完整对话记录 transcript.json")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    question = args.question
    if not question:
        try:
            question = console.input("[bold]请输入研究问题：[/]").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not question:
        console.print("[red]问题不能为空。[/]")
        return 1

    config = AgentConfig(
        model=args.model,
        effort=args.effort,
        max_turns=args.max_turns,
        searches_per_turn=args.searches_per_turn,
        fetches_per_turn=args.fetches_per_turn,
        use_fallbacks=not args.no_fallbacks,
    )
    agent = ResearchAgent(config=config, hooks=ConsoleHooks(console, show_thinking=not args.hide_thinking))

    console.print(Panel(question, title="研究问题", border_style="cyan"))
    try:
        result = agent.run(question)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/]")
        return 130
    except anthropic.AuthenticationError:
        console.print("[red]API Key 无效或未设置。请先设置环境变量 ANTHROPIC_API_KEY。[/]")
        return 2
    except anthropic.PermissionDeniedError as exc:
        console.print(f"[red]没有权限：{exc.message}[/]")
        return 2
    except anthropic.RateLimitError:
        console.print("[red]触发速率限制，请稍后再试。[/]")
        return 3
    except anthropic.APIStatusError as exc:
        console.print(f"[red]API 错误 {exc.status_code}：{exc.message}[/]")
        return 3
    except anthropic.APIConnectionError:
        console.print("[red]无法连接 Anthropic API，请检查网络。[/]")
        return 3

    run_dir = save_result(result, Path(args.out_dir), args.model, save_transcript=args.save_transcript)

    console.print()
    if result.report:
        console.print(Panel(Markdown(result.report), title="研究报告", border_style="green"))
    u = result.usage
    cost = u.estimated_cost(args.model)
    console.print(
        f"[bold]状态[/] {result.status}  [bold]轮数[/] {result.turns}  "
        f"[bold]发现[/] {len(result.notebook.findings)} 条  [bold]来源[/] {len(result.notebook.sources())} 个  "
        f"[bold]搜索[/] {u.web_search_requests} 次  [bold]读取网页[/] {u.web_fetch_requests} 次"
    )
    console.print(
        f"[dim]tokens：输入 {u.input_tokens:,} / 输出 {u.output_tokens:,} / "
        f"缓存写入 {u.cache_creation_input_tokens:,} / 缓存读取 {u.cache_read_input_tokens:,}"
        + (f"   预估费用 ≈ ${cost:.2f}" if cost is not None else "")
        + "[/]"
    )
    console.print(f"报告已保存到 [bold]{run_dir}[/]")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
