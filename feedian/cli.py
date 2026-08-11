from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from .locking import vault_write_lock
from .env import load_env_file
from .ingest import ingest_source_notes, render_source_notes
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
from .vault import find_vault_root, initialize_vault, load_vault_config, save_default_vault, vault_paths


COMMANDS = frozenset({"init", "config", "status", "migrate", "sync", "reextract", "enrich-stars", "render", "run", "snapshot", "restore", "schedule", "auth", "ingest"})


def is_modern_command(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
        ("migrate", "Create or migrate the Vault database, preserving existing raw Markdown."),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
        if command == "migrate":
            subparser.add_argument(
                "--skip-legacy-import",
                action="store_true",
                help="Do not import existing raw/**/*.md into the immutable legacy archive.",
            )

    sync = subparsers.add_parser("sync", help="Collect providers into SQLite without calling an LLM.")
    sync.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    sync.add_argument("--source", choices=("all", "raindrop", "hatena", "rss"), default="all")
    sync.add_argument("--limit", type=int, help="Maximum items per provider.")
    sync.add_argument("--skip-page-fetch", action="store_true", help="Store provider data only.")
    sync.add_argument("--skip-comments", action="store_true", help="Do not request public Hatena comments.")
    sync.add_argument("--force-fetch", action="store_true", help="Refetch page content even when it is younger than refresh_days.")
    sync.add_argument("--progress", choices=PROGRESS_MODES, default="auto")
    sync.add_argument("--verbose", action="store_true", help="Show each processed source item title.")

    reextract = subparsers.add_parser("reextract", help="Re-run extraction from response bytes already stored in SQLite.")
    reextract.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    reextract.add_argument("--media-type", help="Optional MIME prefix such as application/pdf.")
    reextract.add_argument("--limit", type=int)

    stars = subparsers.add_parser("enrich-stars", help="Fetch public Hatena star counts for stored comments without using an LLM.")
    stars.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    stars.add_argument("--limit", type=int, help="Maximum comments to enrich.")
    stars.add_argument("--progress", choices=PROGRESS_MODES, default="auto")

    render = subparsers.add_parser("render", help="Render SQLite records as Obsidian Markdown.")
    render.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    render.add_argument("--apply", action="store_true", help="Write to raw/. The default writes to .feedian/staging/raw/.")
    render.add_argument(
        "--replace-legacy",
        action="store_true",
        help="With --apply, replace only files that still exactly match their immutable legacy archive copy.",
    )

    run = subparsers.add_parser("run", help="Run due non-LLM source syncs, render raw, and snapshot weekly.")
    run.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    run.add_argument("--if-due", action="store_true", help="Exit without work unless the configured provider interval is due.")
    run.add_argument("--skip-snapshot", action="store_true", help="Run sync and render without creating a GitHub Release snapshot.")

    snapshot = subparsers.add_parser("snapshot", help="Publish a verified SQLite archive to a private GitHub Release.")
    snapshot.add_argument("--vault", help="Vault root. Defaults to the current or configured Vault.")
    snapshot.add_argument("--dry-run", action="store_true", help="Check prerequisites without creating a commit, tag, or Release.")

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
    ingest.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"))
    ingest.add_argument("--language", default="Japanese")
    ingest.add_argument("--limit", type=int)
    ingest.add_argument("--force", action="store_true", help="Ignore matching successful LLM results and run again.")
    return parser


def main(argv: list[str]) -> int:
    load_env_file(Path(".env"))
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
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
            return _migrate(args.vault, skip_legacy_import=args.skip_legacy_import)
        if args.command == "sync":
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
        raise ValueError(f"Command not implemented yet: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _status(explicit_vault: str | None) -> int:
    root = find_vault_root(explicit=explicit_vault)
    config = load_vault_config(root)
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
        for name, count in store.status_counts().items():
            print(f"{name}: {count}")
        latest = store.latest_sync_run()
        if latest is not None:
            print(f"last_sync: {latest['status']} {latest['finished_at'] or latest['started_at']}")
    finally:
        store.close()
    return 0


def _migrate(explicit_vault: str | None, *, skip_legacy_import: bool = False) -> int:
    root = find_vault_root(explicit=explicit_vault)
    paths = vault_paths(root)
    store = VaultStore.open(paths.database_path)
    try:
        with vault_write_lock(paths.state_dir):
            imported = skipped = 0
            if not skip_legacy_import:
                imported, skipped = store.import_legacy_artifacts(paths.raw_dir)
            if store.integrity_check() != "ok":
                raise RuntimeError("Database integrity check failed after migration.")
            print(
                f"migrated: schema_version={store.schema_version()} database={paths.database_path} "
                f"legacy_imported={imported} legacy_skipped={skipped}"
            )
    finally:
        store.close()
    return 0


def _sync(args: argparse.Namespace) -> int:
    root = find_vault_root(explicit=args.vault)
    config = load_vault_config(root)
    paths = vault_paths(root)
    store = VaultStore.open(paths.database_path)
    try:
        reporter = ProgressReporter(args.progress, verbose=args.verbose)
        with reporter:
            reporter.start_task(
                f"process: syncing {args.source}", total=args.limit if args.limit is not None else None
            )

            def update_progress(_processed: int, item) -> None:
                reporter.advance()
                reporter.verbose_log(f"  {item.title or item.url or item.source_id}")

            with vault_write_lock(paths.state_dir):
                report = sync_vault(
                    store,
                    config,
                    source=args.source,
                    limit=args.limit,
                    fetch_pages=not args.skip_page_fetch,
                    fetch_comments=not args.skip_comments,
                    force_fetch=args.force_fetch,
                    progress=update_progress,
                )
        print(
            f"sync: run={report.run_id} processed={report.processed} changed={report.changed} "
            f"fetched={report.fetched} failed={report.failed}"
        )
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
        with vault_write_lock(paths.state_dir):
            report = render_raw_views(
                store, root, config, apply=args.apply, replace_legacy=args.replace_legacy
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
    paths = vault_paths(root)
    if not paths.database_path.exists():
        raise FileNotFoundError(f"Database not found: {paths.database_path}; run feedian sync first.")
    store = VaultStore.open(paths.database_path)
    try:
        pending = int(
            store.connection.execute(
                """
                SELECT COUNT(1) FROM comment AS c
                JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                WHERE c.provider = 'hatena' AND c.removed_at IS NULL AND cr.star_count IS NULL
                """
            ).fetchone()[0]
        )
        total = min(pending, args.limit) if args.limit is not None else pending
        reporter = ProgressReporter(args.progress)
        with reporter:
            reporter.start_task("process: enriching Hatena stars", total=total)
            with vault_write_lock(paths.state_dir):
                report = enrich_hatena_stars(store, limit=args.limit, progress=lambda _count: reporter.advance())
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
        with vault_write_lock(paths.state_dir):
            report = create_snapshot(store, root, config, dry_run=args.dry_run)
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
                star_report = enrich_hatena_stars(store)
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
        with vault_write_lock(paths.state_dir):
            report = ingest_source_notes(
                store, root, config, model=args.model, language=args.language, limit=args.limit, force=args.force,
            )
            written, skipped = render_source_notes(store, root, config)
        print(
            f"ingest: processed={report.processed} created={report.created} reused={report.reused} failed={report.failed} "
            f"source_written={written} source_skipped={skipped}"
        )
        return 1 if report.failed else 0
    finally:
        store.close()
