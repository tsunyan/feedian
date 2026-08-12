from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

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
