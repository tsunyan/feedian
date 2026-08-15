from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any, TextIO

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .locking import VaultWriteLockError


CYAN = "#43C6E8"
BLUE = "#6EA8FE"
VIOLET = "#A78BFA"
MUTED = "#8B95A7"
RED = "#FF6B7A"
YELLOW = "#F4C95D"

COMMAND_GROUPS = (
    ("Collect", ("sync", "reextract", "enrich-stars", "ingest")),
    ("Obsidian", ("render", "search", "status")),
    ("Vault", ("init", "config", "migrate", "restore")),
    ("Automation", ("run", "schedule", "snapshot")),
)

COMMAND_DESCRIPTIONS = {
    "init": "Initialize Feedian in an Obsidian Vault.",
    "config": "Manage local Feedian settings.",
    "status": "Show Vault database and sync status.",
    "migrate": "Create or migrate the Vault database.",
    "sync": "Collect providers into SQLite without calling an LLM.",
    "reextract": "Re-run extraction from retained source bytes.",
    "enrich-stars": "Refresh public Hatena star totals.",
    "render": "Render SQLite records as Obsidian Markdown.",
    "run": "Run due syncs, render raw notes, and snapshot.",
    "snapshot": "Publish a verified SQLite archive to a private Release.",
    "restore": "Restore a verified SQLite snapshot.",
    "schedule": "Manage periodic Windows scheduled jobs.",
    "ingest": "Create source notes with an LLM.",
    "search": "Inspect or rebuild the local full-text index.",
}


class RichArgumentParser(argparse.ArgumentParser):
    def print_help(self, file: TextIO | None = None) -> None:
        print_parser_help(self, file=file)

    def error(self, message: str) -> None:
        console = _console(sys.stderr)
        console.print(
            Panel(
                Group(
                    Text(message, style="bold"),
                    Text(f"Run  {self.prog} --help  to see valid options.", style=MUTED),
                ),
                title=Text("Invalid command", style=f"bold {RED}"),
                border_style=RED,
                padding=(1, 2),
            )
        )
        self.exit(2)


def print_parser_help(parser: argparse.ArgumentParser, *, file: TextIO | None = None) -> None:
    console = _console(file or sys.stdout)
    command = parser.prog.split()[1:]
    if not command:
        _print_root_help(console, parser)
    else:
        _print_command_help(console, parser, command)


def print_cli_error(error: Exception, *, file: TextIO | None = None) -> None:
    console = _console(file or sys.stderr)
    if isinstance(error, VaultWriteLockError):
        details = Table.grid(padding=(0, 2))
        details.add_column(style=MUTED, no_wrap=True)
        details.add_column(style="bold", overflow="fold")
        if error.pid is not None:
            details.add_row("Process", str(error.pid))
        if error.started_at:
            details.add_row("Started", _local_time(error.started_at))
        details.add_row("Lock", str(error.lock_path))
        body = Group(
            Text("Another Feedian command is writing to this Vault.", style="bold"),
            Text("Wait for it to finish before starting another write operation.", style="default"),
            Text(""),
            details,
            Text(""),
            Text("If the process is no longer running, remove only the lock file shown above and retry.", style=YELLOW),
        )
        title = "Vault is busy"
    else:
        body = Group(
            Text(str(error) or error.__class__.__name__, style="bold"),
            Text("The command stopped without changing any remaining steps.", style=MUTED),
        )
        title = "Command failed"
    console.print(
        Panel(
            body,
            title=Text(title, style=f"bold {RED}"),
            border_style=RED,
            padding=(1, 2),
        )
    )


def print_ingest_plan(
    plan: Any,
    *,
    model: str,
    provider: str = "openai",
    dry_run: bool,
    command: str,
    file: TextIO | None = None,
) -> None:
    console = _console(file or sys.stdout)
    mode = "Auto select" if plan.auto else "All resources"
    action = "Preview only  |  no API calls  |  no writes" if dry_run else "Ready to ingest"
    heading = Text.assemble(
        ("INGEST  ", f"bold {CYAN}"),
        ("/  ", MUTED),
        ("PREVIEW" if dry_run else "PLAN", f"bold {VIOLET}"),
    )
    flow = Text(justify="center")
    flow.append(f"{plan.total_resources:,}", style="bold")
    flow.append("  STORED  >  ", style=MUTED)
    flow.append(f"{len(plan.candidates):,}", style=f"bold {CYAN}")
    flow.append("  SELECTED  >  ", style=MUTED)
    flow.append(f"{plan.new_requests:,}", style=f"bold {VIOLET}")
    flow.append("  API CALLS", style=MUTED)
    identity = Table.grid(expand=True, padding=(0, 2))
    identity.add_column(ratio=1)
    identity.add_column(ratio=1)
    identity.add_column(ratio=1)
    identity.add_column(ratio=1)
    identity.add_row(
        Text.assemble(("Mode  ", MUTED), (mode, "bold")),
        Text.assemble(("Provider  ", MUTED), (provider, f"bold {CYAN}")),
        Text.assemble(("Model  ", MUTED), (model, f"bold {BLUE}")),
        Text.assemble(("Cached  ", MUTED), (f"{plan.reusable:,}", "bold")),
    )
    command_line = Text.assemble(
        ("Command  ", MUTED),
        (command, f"bold {BLUE}"),
    )
    console.print(
        Panel(
            Group(
                heading,
                Text(action, style=MUTED),
                Text(""),
                command_line,
                Text(""),
                flow,
                Text(""),
                identity,
            ),
            border_style=VIOLET,
            padding=(1, 2),
        )
    )

    budget = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {VIOLET}")
    budget.add_column("Estimate", style=MUTED)
    budget.add_column("Input", justify="right")
    budget.add_column("Output", justify="right")
    budget.add_column("Cost", justify="right")
    if plan.estimated_output_tokens is None:
        budget.add_row(
            "Expected from history",
            f"{plan.input_tokens:,}",
            "-",
            "-",
        )
    else:
        budget.add_row(
            f"Expected | {plan.usage_records:,} prior runs",
            f"{plan.input_tokens:,}",
            f"{plan.estimated_output_tokens:,}",
            _money(plan.estimated_cost_usd),
        )
    budget.add_row(
        "Maximum",
        f"{plan.input_tokens:,}",
        f"{plan.max_output_tokens:,}",
        _money(plan.max_cost_usd),
        style="bold",
    )
    console.print(budget)
    if plan.max_cost_usd is None:
        console.print(
            Text.assemble(
                ("  ! ", YELLOW),
                (f"Feedian has no price snapshot for {model}. ", "bold"),
                ("Cost cannot be estimated now, and stays n/a during the run.", MUTED),
            )
        )
    elif plan.estimated_output_tokens is None:
        console.print(
            Text.assemble(
                ("  ! ", YELLOW),
                ("Expected cost needs one completed ingest with this model. ", "bold"),
                ("The maximum above is available now.", MUTED),
            )
        )

    if not (dry_run or plan.auto):
        return
    console.print()
    console.print(Text("SELECTED RAW MATERIAL", style=f"bold {VIOLET}"))
    targets = Table(
        box=box.SIMPLE_HEAD,
        expand=True,
        header_style=f"bold {CYAN}",
        pad_edge=False,
    )
    targets.add_column("#", justify="right", style=MUTED, no_wrap=True)
    targets.add_column("Action", no_wrap=True)
    if plan.auto:
        targets.add_column("Field", style=BLUE, no_wrap=True)
        targets.add_column("Raw", justify="right", no_wrap=True)
        targets.add_column("Why", style=MUTED, no_wrap=True)
    targets.add_column("Tokens", justify="right", no_wrap=True)
    targets.add_column("Title", ratio=3, overflow="fold")

    displayed = plan.candidates[:100]
    for index, candidate in enumerate(displayed, start=1):
        action_text = Text("CACHE", style="bold green") if candidate.cached_result is not None else Text("LLM", style=f"bold {VIOLET}")
        row: list[Any] = [str(index), action_text]
        if plan.auto:
            reason = "New field" if candidate.reason == "uncovered-field" else "Large field"
            row.extend([candidate.topic, f"{candidate.topic_count:,}", reason])
        row.extend([f"{candidate.input_tokens:,}", candidate.title])
        targets.add_row(*row)
    omitted = len(plan.candidates) - len(displayed)
    if omitted:
        targets.caption = f"{omitted:,} more targets omitted | use --limit to inspect a smaller batch"
        targets.caption_style = MUTED
    console.print(targets)


def ingest_cost_text(report: Any) -> str:
    """Render an ingest cost so that "not reported" stays distinct from a real zero."""
    if report.unpriced_requests and not report.cost_usd:
        return "n/a"
    if report.unpriced_requests:
        return f"${report.cost_usd:.6f} (+{report.unpriced_requests} unpriced)"
    return f"${report.cost_usd:.6f}"


def ingest_cost_value(report: Any) -> str:
    """The same cost as a bare value, for the key=value summary line."""
    if report.unpriced_requests and not report.cost_usd:
        return "n/a"
    return f"{report.cost_usd:.6f}"


def ingest_tokens_text(report: Any) -> str:
    """Render token totals; a provider that reports no usage shows n/a, not zero."""
    if report.unmetered_requests and not (report.input_tokens or report.output_tokens):
        return "in n/a | out n/a"
    text = f"in {report.input_tokens:,} | out {report.output_tokens:,}"
    if report.unmetered_requests:
        text += f" (+{report.unmetered_requests} unmetered)"
    return text


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:.6f}"


def _print_root_help(console: Console, parser: argparse.ArgumentParser) -> None:
    header = Group(
        Text("FEEDIAN", style=f"bold {CYAN}", justify="center"),
        Align.center(
            Text.assemble(
                ("sources", BLUE),
                ("  >  ", MUTED),
                ("SQLite", VIOLET),
                ("  >  ", MUTED),
                ("Obsidian", CYAN),
            )
        ),
    )
    console.print(Panel(header, border_style=VIOLET, padding=(1, 3)))
    console.print(Text("Collect external sources into a per-vault SQLite archive and Obsidian views.", style="bold"))
    console.print()
    _heading(console, "Usage")
    console.print(Text("  feedian COMMAND [OPTIONS]", style=f"bold {CYAN}"))
    console.print()
    _heading(console, "Commands")
    descriptions = _subcommand_descriptions(parser)
    for group, names in COMMAND_GROUPS:
        console.print(Text(group.upper(), style=f"bold {MUTED}"))
        table = Table.grid(padding=(0, 3))
        table.add_column(width=16, style=f"bold {CYAN}", no_wrap=True)
        table.add_column(style="default")
        for name in names:
            if name in descriptions:
                table.add_row(name, descriptions[name])
        console.print(table)
        console.print()
    _heading(console, "Common workflow")
    workflow = Table.grid(padding=(0, 2))
    workflow.add_column(style=BLUE, no_wrap=True)
    workflow.add_column(style=MUTED, no_wrap=True)
    workflow.add_column(style=VIOLET, no_wrap=True)
    workflow.add_column(style=MUTED, no_wrap=True)
    workflow.add_column(style=CYAN, no_wrap=True)
    workflow.add_row("sync", ">", "render --apply", ">", "snapshot")
    console.print(workflow)
    console.print()
    _heading(console, "Examples")
    for example in (
        "feedian sync --source hatena",
        "feedian sync --source raindrop",
        "feedian render --apply",
        "feedian status",
    ):
        console.print(Text(f"  {example}", style=BLUE))
    console.print()
    console.print(Text("Run  feedian COMMAND --help  for command-specific options.", style=MUTED))


def _print_command_help(console: Console, parser: argparse.ArgumentParser, command: list[str]) -> None:
    name = " / ".join(command)
    description = parser.description or COMMAND_DESCRIPTIONS.get(command[-1], "Feedian command options.")
    header = Group(
        Text(f"FEEDIAN  /  {name}", style=f"bold {CYAN}"),
        Text(description, style="default"),
    )
    console.print(Panel(header, border_style=VIOLET, padding=(1, 2)))
    console.print()
    _heading(console, "Usage")
    usage = parser.format_usage().strip()
    if usage.lower().startswith("usage:"):
        usage = usage.split(":", 1)[1].strip()
    console.print(Text(f"  {usage}", style=f"bold {CYAN}"))

    subcommands = _subcommand_descriptions(parser)
    if subcommands:
        console.print()
        _heading(console, "Commands")
        table = Table.grid(padding=(0, 3))
        table.add_column(width=18, style=f"bold {CYAN}", no_wrap=True)
        table.add_column()
        for subcommand, help_text in subcommands.items():
            table.add_row(subcommand, help_text)
        console.print(table)

    options = [
        action
        for action in parser._actions
        if not isinstance(action, argparse._SubParsersAction)
        and action.help is not argparse.SUPPRESS
    ]
    if options:
        console.print()
        _heading(console, "Options")
        table = Table.grid(padding=(0, 3))
        table.add_column(width=31, style=BLUE, no_wrap=True)
        table.add_column()
        for action in options:
            table.add_row(_action_label(action), _action_help(action))
        console.print(table)
    console.print()
    console.print(Text("Tip: the default Vault is used when --vault is omitted.", style=MUTED))


def _subcommand_descriptions(parser: argparse.ArgumentParser) -> dict[str, str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {
                choice.dest: choice.help or COMMAND_DESCRIPTIONS.get(choice.dest, "")
                for choice in action._choices_actions
            }
    return {}


def _action_label(action: argparse.Action) -> str:
    if action.option_strings:
        label = ", ".join(action.option_strings)
    else:
        label = action.metavar or action.dest
    if action.nargs != 0:
        metavar = action.metavar or action.dest.upper()
        if action.option_strings:
            label += f" {metavar}"
    return label


def _action_help(action: argparse.Action) -> str:
    help_text = str(action.help or "")
    if action.choices and not isinstance(action, argparse._SubParsersAction):
        help_text = f"{help_text}  Choices: {', '.join(str(choice) for choice in action.choices)}"
    return help_text


def _heading(console: Console, value: str) -> None:
    console.print(Text(value, style=f"bold {VIOLET}"))


def _console(file: TextIO) -> Console:
    return Console(file=file, color_system="auto", highlight=False, soft_wrap=False)


def _local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return value
