# Raindrop Sync Rate Limits

## Goal

Keep Raindrop tag and note synchronization within the API rate limit without
slowing ordinary bookmark generation.

## Behavior

`sync_request_interval_seconds` is a configuration value with a default of
`0.5`. It limits the start of every Raindrop HTTP request made by
`--sync-raindrop-tags` and `--sync-raindrop-summary`; the client performs no
such throttling for other commands.

If Raindrop responds with HTTP 429, retries wait until the later of
`Retry-After` and `X-RateLimit-Reset`, plus a small safety margin. The reset
header is a UTC epoch timestamp. Other transient retry behavior remains
unchanged.

## Observability

Both sync commands retain their existing final `elapsed=<seconds>s` output and
also print the configured per-request interval when they start. The final line
therefore reports planned, updated, skipped, failed, and elapsed counts.

## Verification

Tests cover configuration validation, request spacing only when enabled,
reset-header retry delays, sync startup output, and existing elapsed output.
