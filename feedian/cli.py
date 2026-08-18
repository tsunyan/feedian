from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .locking import vault_write_lock
from .cli_ui import (
    RichArgumentParser,
    ingest_cost_text,
    ingest_cost_value,
    ingest_tokens_text,
    print_cli_error,
    print_ingest_plan,
)
from .env import load_env_file
from .ingest import (
    ingest_source_notes,
    plan_source_notes,
    render_source_notes,
    fallback_maximum_cost,
    resolve_fallback,
)
from .llm_backends import BACKEND_IDS, canonical_backend_id, get_backend
from .notifications import notify_windows
from .progress import PROGRESS_MODES, ProgressReporter
from .restore import download_and_restore, restore_database
from .reextract import reextract_stored_resources
from .stars import enrich_hatena_stars
from .store import VaultStore
from .sync import sync_vault
from .renderer import render_raw_views
from .scheduler import install_schedule, remove_schedule, schedule_status
from .snapshots import create_snapshot
from .search import rebuild_search_index, search_index_generation
from .store import SCHEMA_VERSION
from .vault import (
    fetch_retry_settings,
    find_vault_root,
    initialize_vault,
    load_vault_config,
    migrate_vault_config,
    save_default_vault,
    user_env_path,
    vault_paths,
)


COMMANDS = frozenset({"init", "config", "status", "migrate", "sync", "reextract", "enrich-stars", "render", "run", "snapshot", "restore", "schedule", "auth", "ingest", "search"})


def is_modern_command(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = RichArgumentParser(
        prog="feedian",
        description="Collect external sources into a per-vault SQLite archive and Obsidian views.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Initialize .feedian/config.json in a Vault.")
    init.add_argument("--vault", required=True, help="Vault root to initialize.")
    init.add_argument("--set-default", action="store_true", help="Also make this the default Vault for this user.")

    config = subparsers.add_parser("config", help="Manage local Feedian settings.")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    set_default = config_subparsers.add_parser("set-default-vault", help="Set the default Vault for this user.")
    set_default.add_argument("vault", help="Initialized Vault root.")

    for command, help_text in (
        ("status", "Show Vault database and sync status."),
        ("migrate", "Create or migrate the Vault database."),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")

    sync = subparsers.add_parser(
        "sync",
        help="Collect providers into SQLite without calling an LLM.",
        description=(
            "Collect providers into SQLite without calling an LLM. Quick mode (the default) does not "
            "detect provider-side edits, comment changes, or refresh_days refreshes, and a Raindrop bulk "
            "import carrying past created dates is not detected by the early stop."
        ),
    )
    sync.set_defaults(_parser=sync)
    sync.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    sync.add_argument(
        "--source", choices=("all", "raindrop", "hatena", "rss"), default="all",
        help="Provider to sync (default: all).",
    )
    sync.add_argument("--limit", type=int, help="Maximum items per provider.")
    sync.add_argument("--full", action="store_true", help="Sync every item, including ones already stored.")
    sync.add_argument(
        "--quick", action="store_true",
        help=(
            "Only sync new items and bodies never fetched (default). Does not detect provider-side edits, "
            "comment changes, or refresh_days refreshes; a Raindrop bulk import carrying past created dates "
            "is not detected by the early stop. For --source rss there is almost no difference from --full, "
            "because a feed only ever exposes a bounded recent window."
        ),
    )
    sync.add_argument("--skip-page-fetch", action="store_true", help="Store provider data only.")
    sync.add_argument("--skip-comments", action="store_true", help="Do not request public Hatena comments.")
    sync.add_argument("--force-fetch", action="store_true", help="Check pages now, ignoring refresh_days; unchanged pages use conditional HTTP. Requires --full.")
    sync.add_argument("--force-comments", action="store_true", help="Refetch Hatena comments even when bookmark counts are unchanged. Requires --full.")
    sync.add_argument("--progress", choices=PROGRESS_MODES, default="auto", help="Progress display mode.")
    sync.add_argument("--verbose", action="store_true", help="Show each processed source item title.")

    reextract = subparsers.add_parser("reextract", help="Re-run extraction from retained non-HTML response bytes such as PDFs.")
    reextract.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    reextract.add_argument("--media-type", help="Optional MIME prefix such as application/pdf.")
    reextract.add_argument("--limit", type=int)

    stars = subparsers.add_parser("enrich-stars", help="Fetch public Hatena star counts for stored comments without using an LLM.")
    stars.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    stars.add_argument("--limit", type=int, help="Maximum comments to enrich.")
    stars.add_argument("--refresh-days", type=int, help="Refresh stars older than this many days (default: config or 30).")
    stars.add_argument("--force", action="store_true", help="Refresh every stored Hatena comment now.")
    stars.add_argument("--progress", choices=PROGRESS_MODES, default="auto", help="Progress display mode.")

    render = subparsers.add_parser("render", help="Render SQLite records as Obsidian Markdown.")
    render.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    render.add_argument("--apply", action="store_true", help="Write to raw/. The default writes to .feedian/staging/raw/.")
    render.add_argument("--progress", choices=PROGRESS_MODES, default="auto", help="Progress display mode.")

    run = subparsers.add_parser("run", help="Run due non-LLM source syncs, render raw, and snapshot weekly.")
    run.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    run.add_argument("--if-due", action="store_true", help="Exit without work unless the configured provider interval is due.")
    run.add_argument("--skip-snapshot", action="store_true", help="Run sync and render without creating a GitHub Release snapshot.")

    snapshot = subparsers.add_parser("snapshot", help="Publish a verified SQLite archive to a private GitHub Release.")
    snapshot.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    snapshot.add_argument("--dry-run", action="store_true", help="Check prerequisites without creating a commit, tag, or Release.")
    snapshot.add_argument("--progress", choices=PROGRESS_MODES, default="auto", help="Progress display mode.")

    restore = subparsers.add_parser("restore", help="Restore a verified SQLite snapshot only into a Vault without a database.")
    restore.add_argument("--vault", required=True, help="Vault root that has no .feedian/feedian.sqlite3 yet.")
    restore_group = restore.add_mutually_exclusive_group(required=True)
    restore_group.add_argument("--archive", help="Local .sqlite3.7z archive to restore.")
    restore_group.add_argument("--tag", help="GitHub Release tag to download and restore.")

    schedule = subparsers.add_parser("schedule", help="Manage periodic Windows Task Scheduler jobs for this Vault.")
    schedule_subparsers = schedule.add_subparsers(dest="schedule_command", required=True)
    install = schedule_subparsers.add_parser("install", help="Install six-hourly and logon catch-up jobs.")
    install.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    install.add_argument("--time", default="03:00", help="24-hour local time, default 03:00.")
    for action in ("status", "remove"):
        action_parser = schedule_subparsers.add_parser(action, help=f"{action.title()} the Feedian scheduled jobs.")
        action_parser.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")

    ingest = subparsers.add_parser("ingest", help="Use the LLM to create source notes from stored resources.")
    ingest.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    ingest.add_argument("--model", help="Model/profile for this run. Uses the backend setting when omitted.")
    backend_group = ingest.add_mutually_exclusive_group()
    backend_group.add_argument(
        "--backend", choices=BACKEND_IDS,
        help="LLM execution backend. Defaults to the Vault config.",
    )
    backend_group.add_argument("--provider", choices=("openai", "manus"), help=argparse.SUPPRESS)
    ingest.add_argument("--language", default="Japanese")
    ingest.add_argument("--limit", type=int)
    ingest.add_argument("--force", action="store_true", help="Ignore matching successful LLM results and run again.")
    ingest.add_argument("--dry-run", action="store_true", help="Show targets, token estimate, and maximum cost without writes or API calls.")
    ingest.add_argument(
        "--auto", action="store_true",
        help="Select representative resources from uncovered and largest fields (default limit: 20).",
    )
    ingest.add_argument("--progress", choices=PROGRESS_MODES, default="auto", help="Progress display mode.")

    search = subparsers.add_parser("search", help="Inspect or rebuild the disposable local full-text index.")
    search.add_argument("action", choices=("status", "rebuild"))
    search.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    return parser


def main(argv: list[str]) -> int:
    load_env_file(Path(".env"))
    load_env_file(user_env_path())
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    args._invocation = subprocess.list2cmdline(["feedian", *argv])
    try:
        if args.command == "init":
            paths = initialize_vault(args.vault)
            print(f"initialized: {paths.config_path}")
            if args.set_default:
                settings = save_default_vault(paths.root)
                print(f"default-vault: {paths.root} ({settings})")
            return 0
        if args.command == "config" and args.config_command == "set-default-vault":
            root = find_vault_root(explicit=args.vault)
            settings = save_default_vault(root)
            print(f"default-vault: {root} ({settings})")
            return 0
        if args.command == "status":
            return _status(args.vault)
        if args.command == "migrate":
            return _migrate(args.vault)
        if args.command == "sync":
            if args.full and args.quick:
                args._parser.error("--full and --quick are mutually exclusive.")
            if not args.full and (args.force_fetch or args.force_comments):
                args._parser.error("--force-fetch and --force-comments require --full.")
            return _sync(args)
        if args.command == "reextract":
            return _reextract(args)
        if args.command == "enrich-stars":
            return _enrich_stars(args)
        if args.command == "render":
            return _render(args)
        if args.command == "run":
            return _run_pipeline(args)
        if args.command == "snapshot":
            return _snapshot(args)
        if args.command == "restore":
            return _restore(args)
        if args.command == "schedule":
            return _schedule(args)
        if args.command == "ingest":
            return _ingest(args)
        if args.command == "search":
            return _search(args)
        raise ValueError(f"Command not implemented yet: {args.command}")
    except Exception as exc:
        print_cli_error(exc)
        return 1


def _status(explicit_vault: str | None) -> int:
    root = find_vault_root(explicit=explicit_vault)
    config = load_vault_config(root)
    try:
        _, _, terminal_http_statuses = fetch_retry_settings(config)
    except ValueError:
        terminal_http_statuses = None
    paths = vault_paths(root)
    print(f"vault: {root}")
    print(f"config: {paths.config_path}")
    print(f"database: {paths.database_path}")
    print(f"providers: {', '.join(name for name, value in config.providers.items() if value.enabled)}")
    if not paths.database_path.exists():
        print("database_status: not-created")
        return 0
    store = VaultStore.open(paths.database_path)
    try:
        print(f"schema_version: {store.schema_version()}")
        print(f"integrity: {store.quick_check()}")
        index_generation = search_index_generation(paths.search_database_path)
        print(
            f"search_index: {'current' if index_generation == store.search_generation() else 'stale-or-missing'} "
            f"generation={index_generation if index_generation is not None else '-'}"
        )
        for name, count in store.status_counts().items():
            print(f"{name}: {count}")
        if terminal_http_statuses is not None:
            print(f"unreachable: {store.terminal_failure_count(terminal_http_statuses)}")
        latest = store.latest_sync_run()
        if latest is not None:
            print(f"last_sync: {latest['status']} mode={latest['mode']} {latest['finished_at'] or latest['started_at']}")
    finally:
        store.close()
    return 0


def _migrate(explicit_vault: str | None) -> int:
    root = find_vault_root(explicit=explicit_vault)
    paths = vault_paths(root)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    with vault_write_lock(paths.state_dir):
        if migrate_vault_config(root):
            print(f"migration: vault config upgraded to format_version=2: {paths.config_path}")
        if paths.database_path.exists() and _database_schema_version(paths.database_path) < SCHEMA_VERSION:
            backup_path = paths.state_dir / "tmp" / f"migration-v{_database_schema_version(paths.database_path)}.sqlite3"
            print(f"migration: creating temporary safety backup: {backup_path}")
            _backup_database(paths.database_path, backup_path)
        store = VaultStore.open(paths.database_path, allow_migration=True)
        try:
            if store.integrity_check() != "ok":
                raise RuntimeError("Database integrity check failed after migration.")
            print("migration: compacting database")
            store.compact()
            if store.integrity_check() != "ok":
                raise RuntimeError("Database integrity check failed after compaction.")
            search = rebuild_search_index(store, paths.search_database_path, force=True)
            print(
                f"migrated: schema_version={store.schema_version()} database={paths.database_path} "
                f"search_sources={search.sources} search_resources={search.resources} search_comments={search.comments}"
            )
        finally:
            store.close()
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
    return 0


def _sync(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    store = VaultStore.open(paths.database_path)
    try:
        if args.source == "all":
            previous_source_total = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM source_item WHERE removed_at IS NULL"
                ).fetchone()[0]
            )
        else:
            previous_source_total = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM source_item WHERE provider = ? AND removed_at IS NULL",
                    (args.source,),
                ).fetchone()[0]
            )
        reporter = ProgressReporter(args.progress, verbose=args.verbose)
        with reporter:
            reporter.start_task(
                f"process: syncing {args.source}", total=args.limit if args.limit is not None else None
            )
            collection_reported = 0
            processing_started = False
            processing_total: int | None = None
            processing_reported = 0
            comments_reported = 0
            comments_started = False

            def update_collection(provider: str, collected: int, total: int) -> None:
                nonlocal collection_reported, processing_started
                if collected == 0:
                    collection_reported = 0
                    processing_started = False
                    reporter.start_task(f"process: collecting {provider} bookmarks")
                    return
                if total > 0:
                    # The API total is a snapshot and can grow while Feedian is
                    # paging through bookmarks added during the same run.
                    reporter.set_total(max(total, collected))
                reporter.advance(max(0, collected - collection_reported))
                collection_reported = collected

            def update_progress(_processed: int, item) -> None:
                nonlocal processing_started, processing_total, processing_reported
                if not processing_started:
                    if collection_reported:
                        reporter.finish_task(collection_reported)
                    processing_total = (
                        args.limit
                        if args.limit is not None
                        else (collection_reported or previous_source_total or None)
                    )
                    reporter.start_task(
                        f"process: syncing {args.source} items",
                        total=processing_total,
                        estimated_total=args.limit is None and processing_total is not None,
                        preserve_previous=bool(collection_reported),
                    )
                    processing_started = True
                elif args.limit is None and processing_total is not None and _processed > processing_total:
                    processing_total = _processed
                    reporter.set_total(processing_total, estimated_total=True)
                reporter.advance(max(0, _processed - processing_reported))
                processing_reported = _processed
                reporter.verbose_log(f"  {item.title or item.url or item.source_id}")

            def update_comments(processed: int, total: int) -> None:
                nonlocal comments_reported, comments_started
                if not comments_started:
                    comments_started = True
                    comments_reported = 0
                    reporter.finish_task(processing_reported)
                    reporter.start_task(
                        "process: checking and updating Hatena comments",
                        total=total,
                        preserve_previous=True,
                    )
                reporter.advance(max(0, processed - comments_reported))
                comments_reported = processed

            with vault_write_lock(paths.state_dir):
                report = sync_vault(
                    store,
                    config,
                    source=args.source,
                    limit=args.limit,
                    quick=not args.full,
                    fetch_pages=not args.skip_page_fetch,
                    fetch_comments=not args.skip_comments,
                    force_fetch=args.force_fetch,
                    force_comments=args.force_comments,
                    progress=update_progress,
                    collection_progress=update_collection,
                    comment_progress=update_comments,
                )
                reporter.finish_task(comments_reported if comments_started else report.processed)
                reporter.retain_final()
                rebuild_search_index(store, paths.search_database_path)
        mode = "quick" if report.quick else "full"
        line = (
            f"sync: run={report.run_id} mode={mode} processed={report.processed} changed={report.changed} "
            f"skipped={report.skipped} fetched={report.fetched} retried={report.retried} failed={report.failed}"
        )
        if report.stopped_early:
            line += f" stopped_early={','.join(report.stopped_early)}"
        print(line)
        return 1 if report.failed else 0
    finally:
        store.close()


def _render(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        reporter = ProgressReporter(args.progress)
        reported = 0

        def update_progress(processed: int, total: int) -> None:
            nonlocal reported
            if processed == 0:
                reporter.start_task(
                    "process: applying Obsidian notes" if args.apply else "process: staging Obsidian notes",
                    total=total,
                )
                reported = 0
                return
            reporter.advance(max(0, processed - reported))
            reported = processed

        with reporter:
            with vault_write_lock(paths.state_dir):
                report = render_raw_views(
                    store,
                    root,
                    config,
                    apply=args.apply,
                    progress=update_progress,
                )
        print(
            f"render: output={report.output_root} written={report.written} comments={report.comments_written} "
            f"skipped={report.skipped} conflicts={report.conflicts}"
        )
        return 1 if report.conflicts else 0
    finally:
        store.close()


def _reextract(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        with vault_write_lock(paths.state_dir):
            report = reextract_stored_resources(store, media_type=args.media_type, limit=args.limit)
        print(f"reextract: processed={report.processed} changed={report.changed} failed={report.failed}")
        return 1 if report.failed else 0
    finally:
        store.close()


def _enrich_stars(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        refresh_days = args.refresh_days or int(config.fetch.get("star_refresh_days", 30))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, refresh_days))).isoformat()
        pending = int(
            store.connection.execute(
                """
                SELECT COUNT(1) FROM comment AS c
                JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                WHERE c.provider = 'hatena' AND c.removed_at IS NULL
                  AND (? OR cr.star_checked_at IS NULL OR cr.star_checked_at < ?)
                """,
                (int(args.force), cutoff),
            ).fetchone()[0]
        )
        total = min(pending, args.limit) if args.limit is not None else pending
        reporter = ProgressReporter(args.progress)
        with reporter:
            reporter.start_task("process: enriching Hatena stars", total=total)
            with vault_write_lock(paths.state_dir):
                report = enrich_hatena_stars(
                    store,
                    limit=args.limit,
                    refresh_days=refresh_days,
                    force=args.force,
                    progress=lambda _count: reporter.advance(),
                )
        print(
            f"enrich-stars: processed={report.processed} updated={report.updated} unavailable={report.unavailable}"
        )
        return 0
    finally:
        store.close()


def _snapshot(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian migrate or sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        reporter = ProgressReporter(args.progress)
        active_phase = 0

        def snapshot_progress(description: str, phase: int, total_phases: int, completed: bool) -> None:
            nonlocal active_phase
            if completed:
                reporter.finish_task(1)
                return
            reporter.start_task(
                f"snapshot: [{phase}/{total_phases}] {description}",
                total=1,
                preserve_previous=active_phase > 0,
            )
            active_phase = phase

        with reporter:
            with vault_write_lock(paths.state_dir):
                report = create_snapshot(
                    store,
                    root,
                    config,
                    dry_run=args.dry_run,
                    progress=snapshot_progress,
                )
        if report.dry_run:
            print(f"snapshot dry-run: id={report.snapshot_id} tag={report.tag}")
        else:
            print(f"snapshot: id={report.snapshot_id} tag={report.tag} sha256={report.archive_sha256}")
        return 0
    finally:
        store.close()


def _restore(args: argparse.Namespace) -> int:
    root = Path(args.vault).expanduser().resolve()
    paths = vault_paths(root)
    with vault_write_lock(paths.state_dir):
        database = download_and_restore(root, args.tag) if args.tag else restore_database(root, args.archive)
    print(f"restored: database={database}")
    return 0


def _run_pipeline(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    store = VaultStore.open(paths.database_path)
    try:
        try:
            with vault_write_lock(paths.state_dir):
                due_providers = _due_providers(store, config)
                snapshot_due = _snapshot_is_due(store)
                if args.if_due and not due_providers and not snapshot_due:
                    print("run: not due")
                    return 0
                processed = failed = 0
                for provider in due_providers:
                    sync_report = sync_vault(store, config, source=provider)
                    processed += sync_report.processed
                    failed += sync_report.failed
                star_report = enrich_hatena_stars(
                    store, refresh_days=int(config.fetch.get("star_refresh_days", 30))
                )
                rebuild_search_index(store, paths.search_database_path)
                render_report = render_raw_views(store, root, config, apply=True)
                if render_report.conflicts:
                    raise RuntimeError(f"Raw render has {render_report.conflicts} protected file conflict(s).")
                if failed:
                    raise RuntimeError(f"Sync completed with {failed} failed item(s).")
                if not args.skip_snapshot and snapshot_due:
                    snapshot_report = create_snapshot(store, root, config)
                    print(f"run: snapshot={snapshot_report.tag}")
                print(
                    f"run: providers={','.join(due_providers) or 'none'} processed={processed} "
                    f"stars_updated={star_report.updated} raw_written={render_report.written}"
                )
            notify_windows("Feedian", "Weekly sync and snapshot completed.")
            return 0
        except Exception as exc:
            notify_windows("Feedian", f"Weekly run failed: {str(exc)[:180]}")
            raise
    finally:
        store.close()


def _due_providers(store: VaultStore, config) -> list[str]:
    due: list[str] = []
    for provider, settings in config.providers.items():
        if not settings.enabled:
            continue
        latest = store.latest_provider_sync_run(provider)
        if latest is None or not latest["finished_at"]:
            due.append(provider)
            continue
        finished = datetime.fromisoformat(str(latest["finished_at"]).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - finished).total_seconds() >= (settings.poll_hours or 168) * 3600:
            due.append(provider)
    return due


def _snapshot_is_due(store: VaultStore) -> bool:
    latest = store.latest_snapshot()
    if latest is None:
        return True
    created = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).total_seconds() >= 168 * 3600


def _schedule(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    if args.schedule_command == "install":
        status = install_schedule(root, time=args.time)
    elif args.schedule_command == "remove":
        status = remove_schedule(root)
    else:
        status = schedule_status(root)
    print(
        f"schedule: periodic={'installed' if status.periodic_exists else 'absent'} ({status.periodic_task}) "
        f"catch_up={'installed' if status.catch_up_exists else 'absent'} ({status.catch_up_task})"
    )
    return 0


def _ingest(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        legacy_environment = os.environ.get("LLM_PROVIDER", "").strip()
        backend_id = canonical_backend_id(
            args.backend or args.provider or os.environ.get("LLM_BACKEND", "").strip()
            or legacy_environment or config.llm.backend
        )
        backend = get_backend(backend_id)
        fallback = resolve_fallback(config, backend_id)
        fallback_label = (
            f"{fallback.backend_id} / {fallback.model}" if fallback is not None else ""
        )
        configured_backend = canonical_backend_id(config.llm.backend)
        backend_model_environment = {
            "openai-responses": "OPENAI_MODEL",
            "manus-api": "MANUS_MODEL",
            "codex-local": "CODEX_MODEL",
            "claude-code-local": "CLAUDE_CODE_MODEL",
        }[backend_id]
        environment_model = os.environ.get(backend_model_environment, "").strip()
        model = args.model or (
            environment_model
            or (config.llm.model if backend_id == configured_backend else "")
            or backend.default_model()
        )
        if not model:
            raise ValueError(f"A model must be configured for backend {backend_id}.")
        if args.limit is not None and args.limit < 0:
            raise ValueError("--limit must be zero or greater.")
        if args.dry_run:
            plan = plan_source_notes(
                store, model=model, language=args.language, limit=args.limit,
                force=args.force, auto=args.auto, backend=backend_id, backend_instance=backend,
            )
            print_ingest_plan(
                plan, backend=backend_id, fallback=fallback_label, fallback_max_cost_usd=fallback_maximum_cost(plan, fallback), model=model, dry_run=True, command=args._invocation,
            )
            return 0

        reporter = ProgressReporter(args.progress)
        with vault_write_lock(paths.state_dir):
            plan = plan_source_notes(
                store, model=model, language=args.language, limit=args.limit,
                force=args.force, auto=args.auto, backend=backend_id, backend_instance=backend,
            )
            print_ingest_plan(
                plan, backend=backend_id, fallback=fallback_label, fallback_max_cost_usd=fallback_maximum_cost(plan, fallback), model=model, dry_run=False, command=args._invocation,
            )

            with reporter:
                reporter.start_task("ingest: creating source notes", total=len(plan.candidates))

                def ingest_progress(processed, total, candidate, current) -> None:
                    reporter.set_description(
                        f"ingest: {ingest_tokens_text(current)} | {ingest_cost_text(current)}"
                    )
                    reporter.advance()
                    identity = (
                        f"source={candidate.source_ref} resource={candidate.resource_id} "
                        f"run={current.last_run_id or '-'}"
                    )
                    reporter.log(
                        f"  {processed}/{total}  {current.last_status.upper()}  {identity}  "
                        f"{ingest_cost_text(current)}  {candidate.title}"
                    )
                    if current.last_error:
                        error = " ".join(current.last_error.split())
                        reporter.log(f"    error={error[:500]}")

                report = ingest_source_notes(
                    store, root, config, model=model, language=args.language,
                    limit=args.limit, force=args.force, auto=args.auto,
                    progress=ingest_progress, plan=plan, backend=backend_id,
                    backend_instance=backend,
                )
                written, skipped = render_source_notes(store, root, config)
        print(
            f"ingest: processed={report.processed} created={report.created} reused={report.reused} "
            f"failed={report.failed} input_tokens={report.input_tokens} output_tokens={report.output_tokens} "
            f"unmetered_requests={report.unmetered_requests} cost_usd={ingest_cost_value(report)} "
            f"unpriced_requests={report.unpriced_requests} "
            f"source_written={written} source_skipped={skipped}"
        )
        return 1 if report.failed else 0
    finally:
        store.close()


def _search(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        if args.action == "status":
            cached = search_index_generation(paths.search_database_path)
            current = store.search_generation()
            print(
                f"search: path={paths.search_database_path} status="
                f"{'current' if cached == current else 'stale-or-missing'} source_generation={current} "
                f"index_generation={cached if cached is not None else '-'}"
            )
            return 0
        with vault_write_lock(paths.state_dir):
            report = rebuild_search_index(store, paths.search_database_path, force=True)
        print(
            f"search: rebuilt={report.rebuilt} sources={report.sources} resources={report.resources} "
            f"comments={report.comments} path={report.path}"
        )
        return 0
    finally:
        store.close()


def _database_schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        connection.close()


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        if str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("Temporary migration backup integrity check failed.")
    finally:
        destination_connection.close()
        source_connection.close()
