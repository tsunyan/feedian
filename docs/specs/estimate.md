# `--estimate` Design

## Goal

Add a read-only CLI estimate mode that projects OpenAI API costs from representative
bookmark content. It must not create or update Obsidian notes and must not call the
OpenAI API.

## CLI

Add these arguments:

```text
--estimate
--estimate-sample-size SIZE
```

`--estimate` is mutually exclusive with `--list-collections` and `--dry-run`.
It honors the existing collection, nesting, and limit filters, so the estimate covers
the same target population that a subsequent processing command would cover.

`--estimate-sample-size` defaults to `10%`, with a minimum of 20 bookmarks when the
target population contains at least 20 bookmarks. It accepts:

- a positive integer such as `20`, meaning an absolute sample size;
- a percentage such as `10%` or `20%`;
- `0`, meaning count-only mode, without page fetches or content sampling.

The resolved sample size never exceeds the target population. Invalid values are
reported as command errors.

## Sampling

The command obtains the complete filtered bookmark metadata set first, then groups
items by Raindrop collection ID. It allocates the resolved sample size in proportion
to each collection's population using the largest-remainder method. When the sample
is at least the number of non-empty collections, each collection receives at least
one sample. When it is smaller, allocations go to the largest remainders.

Within each collection, items retain the Raindrop API ordering. The sampler selects
evenly spaced midpoint positions, so it covers each collection's full ordering rather
than taking only its earliest returned bookmarks. The algorithm is deterministic for
unchanged API results.

## Token and Cost Estimate

For every sampled bookmark with a fetchable page, Raindian:

1. fetches page content using the normal URL-safety, extraction, length, timeout, and
   retry settings;
2. builds the same LLM prompt used by normal processing;
3. counts its prompt tokens locally with `tiktoken`.

No `OPENAI_API_KEY` is required. The `tiktoken` package is a normal, version-pinned
dependency. The preferred encoding is selected by `encoding_for_model`; when a model
is unknown to the installed tokenizer, Raindian uses `o200k_base` and states that
fallback in its output.

The mean measured input-token count is multiplied by the total target bookmark count.
Each supported model's total is calculated using that projected input plus the
configured `max_output_tokens` for every target item. The output always includes GPT-5.6
Sol, GPT-5.6 Terra, GPT-5.6 Luna, and GPT-5.5. The model configured by `openai_model`
is visibly marked `selected`.

Count-only mode uses the documented generic input-token range from the README instead
of fetching pages. It is explicitly labeled as less precise.

## Output and Failures

The command reports target count, resolved sample size, sampled/failed page counts,
mean input tokens, projected input tokens, configured output-token cap, and elapsed
time. It then prints a table with all four model prices and estimates.

Page fetch failures do not stop the estimate. They are counted and summarized by
reason, and successful sampled prompts form the estimate. If every sampled fetch
fails, the command prints no sampled-content cost table and exits with an error.
Target populations of zero are reported without an estimate.

## Non-goals

- No OpenAI API request, generated note, or vault write occurs in estimate mode.
- The command does not guarantee the eventual billed amount: server-side request
  framing, model updates, and reasoning tokens can differ from local token counts.
- It does not add a cache or persist fetched page content.

## Tests and Documentation

Tests cover size parsing, minimum and population clamps, proportional allocation,
evenly spaced deterministic selection, prompt token counting and fallback behavior,
read-only estimate execution, failed-page handling, model-table selection marking,
and argument conflicts. README and the configuration example document invocation,
sample-size formats, dependencies, scope, and limitations.
