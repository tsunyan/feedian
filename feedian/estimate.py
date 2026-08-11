from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Callable, Sequence
from urllib.request import Request, urlopen

import tiktoken


@dataclass(frozen=True)
class ModelPrice:
    name: str
    model: str
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float


@dataclass(frozen=True)
class CostRow:
    name: str
    model: str
    input_cost: float
    output_cost: float
    total_cost: float


@dataclass(frozen=True)
class PriceRefresh:
    prices: tuple[ModelPrice, ...]
    source: str
    warning: str | None
    fallback_models: frozenset[str] = frozenset()


MODEL_DOCS_BASE_URL = "https://developers.openai.com"
MODEL_CATALOG_URL = f"{MODEL_DOCS_BASE_URL}/api/docs/models.md"
MODEL_PRICES = (
    ModelPrice("GPT-5.6 Sol", "gpt-5.6-sol", 5.00, 0.50, 30.00),
    ModelPrice("GPT-5.6 Terra", "gpt-5.6-terra", 2.00, 0.20, 12.00),
    ModelPrice("GPT-5.6 Luna", "gpt-5.6-luna", 0.20, 0.02, 1.20),
    ModelPrice("GPT-5.5", "gpt-5.5", 5.00, 0.50, 30.00),
)


def parse_sample_size(value: str, population: int) -> int:
    if population <= 0:
        return 0
    text = value.strip()
    if re.fullmatch(r"\d+", text):
        return min(int(text), population)
    if not text.endswith("%"):
        raise ValueError("sample size must be a non-negative integer or percentage.")
    try:
        percentage = Decimal(text[:-1])
    except InvalidOperation as exc:
        raise ValueError("sample percentage must be a number from 0 to 100.") from exc
    if not percentage.is_finite() or percentage < 0 or percentage > 100:
        raise ValueError("sample percentage must be from 0 to 100.")
    if percentage == 0:
        return 0
    count = int((Decimal(population) * percentage / 100).to_integral_value(rounding=ROUND_CEILING))
    return min(population, max(min(20, population), count))


def select_sample(items: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    if sample_size <= 0 or not items:
        return []
    sample_size = min(sample_size, len(items))
    groups: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(items):
        groups.setdefault(_collection_id(item), []).append((index, item))
    allocation = _allocate_samples({key: len(group) for key, group in groups.items()}, sample_size)
    selected: list[tuple[int, dict[str, Any]]] = []
    for collection_id, group in groups.items():
        count = allocation[collection_id]
        for position in _midpoint_positions(len(group), count):
            selected.append(group[position])
    return [item for _, item in sorted(selected, key=lambda entry: entry[0])]


def count_prompt_tokens(prompt: str, model: str) -> tuple[int, str | None]:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
        return len(encoding.encode(prompt)), "o200k_base"
    return len(encoding.encode(prompt)), None


def projected_costs(
    population: int,
    input_tokens_per_item: float,
    max_output_tokens: int,
    prices: Sequence[ModelPrice] = MODEL_PRICES,
) -> list[CostRow]:
    input_tokens = population * input_tokens_per_item
    output_tokens = population * max_output_tokens
    return [
        CostRow(
            name=price.name,
            model=price.model,
            input_cost=input_tokens * price.input_per_million / 1_000_000,
            output_cost=output_tokens * price.output_per_million / 1_000_000,
            total_cost=(input_tokens * price.input_per_million + output_tokens * price.output_per_million)
            / 1_000_000,
        )
        for price in prices
    ]


def refresh_model_prices(
    selected_model: str,
    timeout_seconds: int,
    fetch_text: Callable[[str, int], str] | None = None,
    include_recommended: bool = True,
) -> PriceRefresh:
    fetch = fetch_text or _fetch_official_text
    try:
        catalog = fetch(MODEL_CATALOG_URL, timeout_seconds)
        links = _model_links(catalog)
        candidates = _recommended_model_links(catalog) if include_recommended else []
        pricing_model = comparison_model(selected_model)
        if pricing_model not in {model for _, model, _ in candidates}:
            name, url = links.get(pricing_model, (pricing_model, _model_url(pricing_model)))
            if url:
                candidates.append((name, pricing_model, url))
        prices: list[ModelPrice] = []
        errors: list[str] = []
        fallback_models: set[str] = set()
        for name, _, url in candidates:
            try:
                prices.append(_parse_model_price(fetch(url, timeout_seconds), name))
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        if prices:
            if pricing_model not in {price.model for price in prices}:
                fallback_price = next(
                    (price for price in MODEL_PRICES if price.model == pricing_model), None
                )
                if fallback_price:
                    prices.append(fallback_price)
                    fallback_models.add(pricing_model)
            warning = "; ".join(errors) if errors else None
            return PriceRefresh(tuple(prices), "official", warning, frozenset(fallback_models))
        raise OSError("; ".join(errors) or "no model pricing was found")
    except (OSError, ValueError) as exc:
        return PriceRefresh(
            MODEL_PRICES,
            "fallback",
            str(exc),
            frozenset(price.model for price in MODEL_PRICES),
        )


def _fetch_official_text(url: str, timeout_seconds: int) -> str:
    request = Request(url, headers={"User-Agent": "Feedian/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8")


def _recommended_model_links(catalog: str) -> list[tuple[str, str, str]]:
    start = catalog.find("## Recommended models")
    if start < 0:
        raise ValueError("official model catalog has no Recommended models section")
    next_heading = catalog.find("\n## ", start + 1)
    section = catalog[start:] if next_heading < 0 else catalog[start:next_heading]
    links = _model_links(section)
    if not links:
        raise ValueError("official model catalog has no recommended model links")
    return [(name, model, url) for model, (name, url) in links.items()]


def _model_links(text: str) -> dict[str, tuple[str, str]]:
    links: dict[str, tuple[str, str]] = {}
    for name, path in re.findall(r"- \[([^\]]+)\]\((/api/docs/models/[A-Za-z0-9._-]+\.md)\)", text):
        model = path.removesuffix(".md").rsplit("/", 1)[-1]
        links.setdefault(model, (name, f"{MODEL_DOCS_BASE_URL}{path}"))
    return links


def _model_url(model: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
        return None
    return f"{MODEL_DOCS_BASE_URL}/api/docs/models/{model}.md"


def _parse_model_price(document: str, fallback_name: str) -> ModelPrice:
    model_match = re.search(r"^Model ID: `([^`]+)`$", document, flags=re.MULTILINE)
    if not model_match:
        raise ValueError("official model document has no model ID")
    heading_match = re.search(r"^# (.+)$", document, flags=re.MULTILINE)
    name = heading_match.group(1).strip() if heading_match else fallback_name
    pricing_start = document.find("### Text tokens")
    if pricing_start < 0:
        raise ValueError(f"official model document has no text token pricing for {model_match.group(1)}")
    pricing = document[pricing_start:]
    input_match = re.search(r"^\| Input \| \$([0-9]+(?:\.[0-9]+)?) \|", pricing, flags=re.MULTILINE)
    cached_input_match = re.search(
        r"^\| Cached input \| \$([0-9]+(?:\.[0-9]+)?) \|", pricing, flags=re.MULTILINE
    )
    output_match = re.search(r"^\| Output \| \$([0-9]+(?:\.[0-9]+)?) \|", pricing, flags=re.MULTILINE)
    if not input_match or not output_match:
        raise ValueError(f"official model document has incomplete text token pricing for {model_match.group(1)}")
    return ModelPrice(
        name=name,
        model=model_match.group(1),
        input_per_million=float(input_match.group(1)),
        cached_input_per_million=(
            float(cached_input_match.group(1)) if cached_input_match else float(input_match.group(1))
        ),
        output_per_million=float(output_match.group(1)),
    )


def usage_cost_usd(usage: dict[str, Any], price: ModelPrice) -> float:
    input_tokens = _usage_token_count(usage.get("input_tokens"))
    cached_input_tokens = min(input_tokens, _usage_token_count(usage.get("cached_input_tokens")))
    output_tokens = _usage_token_count(usage.get("output_tokens"))
    return (
        (input_tokens - cached_input_tokens) * price.input_per_million
        + cached_input_tokens * price.cached_input_per_million
        + output_tokens * price.output_per_million
    ) / 1_000_000


def _usage_token_count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def format_cost_rows(
    rows: Sequence[CostRow],
    selected_model: str,
    typical_rows: Sequence[CostRow] | None = None,
) -> list[str]:
    pricing_model = comparison_model(selected_model)
    lines: list[str] = []
    if not any(row.model == pricing_model for row in rows):
        lines.append(f"selected model: {selected_model} (not in comparison table)")
    typical_by_model = {row.model: row for row in typical_rows} if typical_rows else {}
    if typical_rows:
        lines.append(
            "model\tinput\toutput (input-matched)\ttotal (input-matched)\toutput (max)\ttotal (max)"
        )
    else:
        lines.append("model\tinput\toutput (max)\ttotal (max)")
    for row in rows:
        selected = " [selected]" if row.model == pricing_model else ""
        typical = typical_by_model.get(row.model)
        if typical:
            lines.append(
                f"{row.name}{selected}\t${row.input_cost:.2f}\t${typical.output_cost:.2f}\t"
                f"${typical.total_cost:.2f}\t${row.output_cost:.2f}\t${row.total_cost:.2f}"
            )
        else:
            lines.append(
                f"{row.name}{selected}\t${row.input_cost:.2f}\t${row.output_cost:.2f}\t${row.total_cost:.2f}"
            )
    return lines


def comparison_model(model: str) -> str:
    return "gpt-5.6-sol" if model == "gpt-5.6" else model


def _collection_id(item: dict[str, Any]) -> int:
    collection = item.get("collection")
    if not isinstance(collection, dict):
        return 0
    collection_id = collection.get("$id")
    return collection_id if isinstance(collection_id, int) else 0


def _allocate_samples(group_sizes: dict[int, int], sample_size: int) -> dict[int, int]:
    allocation = {collection_id: 0 for collection_id in group_sizes}
    if sample_size >= len(group_sizes):
        allocation = {collection_id: 1 for collection_id in group_sizes}
        remaining = sample_size - len(group_sizes)
        weights = {collection_id: size - 1 for collection_id, size in group_sizes.items()}
    else:
        remaining = sample_size
        weights = dict(group_sizes)
    weight_total = sum(weights.values())
    if remaining == 0 or weight_total == 0:
        return allocation
    quotas = {
        collection_id: remaining * size / weight_total
        for collection_id, size in weights.items()
    }
    for collection_id, quota in quotas.items():
        allocation[collection_id] += math.floor(quota)
    unassigned = remaining - sum(math.floor(quota) for quota in quotas.values())
    for collection_id in sorted(
        quotas,
        key=lambda key: (quotas[key] - math.floor(quotas[key]), group_sizes[key], -key),
        reverse=True,
    )[:unassigned]:
        allocation[collection_id] += 1
    return allocation


def _midpoint_positions(length: int, count: int) -> list[int]:
    return [math.floor((position + 0.5) * length / count) for position in range(count)]
