# LLM Execution Backends

## Status

Proposed specification. This document defines the target design for adding local Codex and Claude Code execution incrementally while preserving the existing OpenAI Responses and Manus ingest behavior.

## Purpose

Feedian's summarization flow must be independent of any one vendor API or CLI. The abstraction boundary is an LLM execution backend, not an LLM model. A direct API and a local agent CLI from the same vendor differ in authentication, billing, permissions, lifecycle, output, and failure behavior.

This specification enables Feedian to:

- move the existing OpenAI Responses and Manus behavior behind a common contract;
- add Codex CLI and Claude Code CLI as local-only, opt-in backends;
- add a future direct Anthropic API backend without conflating it with Claude Code;
- enforce Feedian-owned permissions and persistence rules when processing untrusted web content; and
- normalize usage, billing, and audit data without discarding backend-specific evidence.

## Terms and identifiers

`backend` identifies an execution route, `model` identifies the model selected through that route, and `auth_mode` identifies the authentication and billing route. They must not be collapsed into one `provider` value.

The initial canonical backend identifiers are:

| Backend ID | Execution route | Initial status |
| --- | --- | --- |
| `openai-responses` | OpenAI Responses API | default, stable |
| `manus-api` | Manus asynchronous task API | stable |
| `codex-local` | Local Codex CLI | experimental, opt-in |
| `claude-code-local` | Local Claude Code CLI | experimental, opt-in |

`anthropic-api` is reserved for a future direct API backend. New ambiguous identifiers such as `claude` or `openai` must not be introduced.

## Architecture

`ingest` uses a summarization contract and contains no backend-specific execution branches.

```text
IngestService
    |
    v
SummaryBackend
    +-- OpenAIResponsesBackend
    +-- ManusBackend
    +-- CodexCliBackend
    +-- ClaudeCodeCliBackend
    `-- AnthropicApiBackend       (future)
```

Codex and Claude Code may share an internal `LocalAgentRunner` that owns process creation, standard I/O, deadlines, exit codes, process-tree termination, and temporary working directories. Command construction, security flags, event formats, and usage parsing remain backend-specific. API and CLI integrations must not be forced through one low-level transport interface.

## Common contract

The conceptual input is `SummaryRequest` and the output is `BackendResult`. Concrete Python names may change, but the implementation must preserve the following information.

`SummaryRequest` contains:

- source title, URL, extracted body, and existing metadata;
- output language;
- Feedian prompt version;
- JSON Schema and schema version;
- logical generation settings such as model, reasoning effort, and output limit; and
- execution deadline and security policy.

`BackendResult` contains:

- a schema-validated summary;
- normalized usage, with unreported values represented as unknown rather than zero;
- billing information that distinguishes provider-reported and Feedian-estimated amounts;
- the effective request or process arguments with secrets removed;
- the raw response or event log;
- backend ID, model, implementation revision, and relevant CLI or API version metadata; and
- warnings and recovery information such as remote task IDs.

Feedian validates every backend result against the same JSON Schema before persistence. It does not rely solely on a backend's structured-output feature. An invalid result must not produce a source note.

## Capabilities and security policy

Each backend declares at least the following capabilities:

- execution kind: `http` or `local-agent`;
- strict structured-output support;
- ability to disable all tool execution;
- ability to disable agent-accessible network features;
- ability to ignore user settings, project rules, and extensions;
- ability to run without session persistence;
- usage and monetary-cost reporting; and
- safe cancellation support.

Capabilities are enforced before execution, not merely displayed or used as informal hints. If a backend cannot satisfy Feedian's required policy, execution fails with `BackendPolicyError`; the policy must not be weakened implicitly.

Web pages, feed bodies, comments, and fetched metadata are all untrusted input. The initial local-agent policy is:

- start a fresh invocation for every article and never share conversation state between articles;
- use a dedicated Feedian-created temporary working directory;
- disable shell, filesystem, browser, MCP, and all other tools;
- disable agent-accessible network features other than communication required for model inference;
- ignore user settings, project rules, skills, plugins, hooks, and automatic memory;
- do not persist sessions, prompt history, or article bodies locally; and
- minimize the child-process environment and never record secrets in audit logs.

The initial Claude Code adapter combines non-interactive execution, bare mode, all-tools-disabled operation, no session persistence, JSON output, and JSON Schema output. Bare mode alone leaves built-in tools available, so disabling all tools is a separate requirement. The Codex adapter must provide equivalent sandboxing, ignored configuration and rules, and ephemeral execution. Version-specific CLI flags remain encapsulated inside each adapter.

## Configuration and selection

The target configuration separates `backend` and `model`:

```json
{
  "llm": {
    "backend": "openai-responses",
    "model": null,
    "fallback": {
      "enabled": false,
      "backend": null
    }
  }
}
```

Backend selection precedence is `--backend`, `LLM_BACKEND`, Vault configuration, then the built-in `openai-responses` default. Model selection precedence is `--model`, a backend-specific environment variable, Vault configuration, then the backend default. API keys, login tokens, and other secrets must not be stored in Vault configuration.

During migration, the existing `--provider openai|manus` and `LLM_PROVIDER` may remain as deprecated aliases for `openai-responses` and `manus-api`. Because `provider` also describes collection sources, new LLM configuration and output use `backend`.

Automatic fallback is disabled by default for every backend. In particular, Feedian must not switch silently to a metered API when a local subscription allowance, authentication state, or CLI fails. If fallback is enabled explicitly, the destination backend is named and shown in both the preflight preview and audit record.

## Execution, failure, and cancellation

The first local CLI implementations start one process per article. A persistent process or session reuse may not be introduced until a separate design preserves both security and article isolation.

The common error taxonomy includes at least:

- `BackendUnavailableError`: missing CLI or unreachable service;
- `BackendAuthError`: missing or expired authentication;
- `BackendRateLimitError`: allowance or rate limit reached;
- `BackendTimeoutError`: Feedian deadline exceeded;
- `BackendProtocolError`: invalid JSON, event stream, or schema;
- `BackendPolicyError`: required security policy cannot be satisfied.

On timeout or cancellation, a local adapter terminates the complete process tree, not only the parent, and records the outcome in the audit entry. For remote tasks that Feedian cannot cancel, such as Manus tasks, the task ID and inspection URL remain mandatory in both errors and audit records.

Before execution, each local adapter verifies that its CLI is installed and identifies its version. A version that lacks a required security or output feature is rejected before execution rather than being run with guessed substitute flags. The detected version is included in the audit record.

## Usage, billing, audit, and reuse

Normalized usage stores input, cached input, output, reasoning, and other categories separately. Values not reported by a backend remain unknown; missing data must not be interpreted as free usage or zero tokens.

Billing data separates provider-reported amount, Feedian's price-table estimate, and authentication or billing mode. An API-equivalent estimate for a local subscription-backed CLI must not be presented as the actual charge.

The existing LLM run audit stores the logical request, secret-free effective request, raw response, normalized result, usage, billing information, backend ID, model, and implementation revision. Article content must not be duplicated into new debug logs or CLI session histories.

A result-reuse key includes at least backend ID, model, prompt version, schema version, language, generation settings, and the input-content fingerprint. Results from different backends are not interchangeable merely because their model names match. Authentication secrets are never included in a key.

## Delivery order

1. Add common types, a backend registry, and a factory; move OpenAI and Manus without behavior changes.
2. Move backend-specific CLI, ingest, usage, and pricing branches into adapters or backend profiles.
3. Add `LocalAgentRunner` and experimental `codex-local`.
4. Add `claude-code-local` on the same runner.
5. Compare quality, structured-output success rate, latency, usage, and security on the same article set.
6. Treat any change to the default backend as a separate specification decision, even after successful evaluation.

## Out of scope

- Launching Codex or Claude Code directly inside Cloudflare Workers.
- Allowing the LLM to fetch pages, access files, or execute shell commands.
- Automatically rewriting prompts per backend in ways that change their meaning.
- Treating subscription allowances as unlimited or free compute.
- Silently falling back from an experimental backend to a metered API.

## Verification and acceptance criteria

- Existing OpenAI and Manus tests and persisted results remain equivalent after migration.
- Backend and model can be selected independently, and invalid combinations fail before execution.
- A result that fails common schema validation never produces a note.
- Local CLI tests cover disabled tools, ignored configuration, no persistence, dedicated working directory, and process-tree termination on timeout.
- Prompt-injection fixtures cannot access files, shell, browser, or MCP tools.
- Missing usage is not persisted or displayed as zero.
- Cached or completed-run reuse never crosses backend boundaries.
- With fallback disabled, allowance or authentication failures never invoke another backend.
- Audit records contain no API keys, login tokens, or authentication headers.
- An unsupported CLI version is rejected before article content is submitted.

## References

- [Codex non-interactive execution](https://developers.openai.com/codex/non-interactive-mode)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
