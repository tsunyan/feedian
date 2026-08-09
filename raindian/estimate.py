from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

import tiktoken


@dataclass(frozen=True)
class ModelPrice:
    name: str
    model: str
    input_per_million: float
    output_per_million: float


@dataclass(frozen=True)
class CostRow:
    name: str
    model: str
    input_cost: float
    output_cost: float
    total_cost: float


MODEL_PRICES = (
    ModelPrice("GPT-5.6 Sol", "gpt-5.6-sol", 5.00, 30.00),
    ModelPrice("GPT-5.6 Terra", "gpt-5.6-terra", 2.50, 15.00),
    ModelPrice("GPT-5.6 Luna", "gpt-5.6-luna", 1.00, 6.00),
    ModelPrice("GPT-5.5", "gpt-5.5", 5.00, 30.00),
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
    if percentage < 0 or percentage > 100:
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
        for price in MODEL_PRICES
    ]


def format_cost_rows(rows: list[CostRow], selected_model: str) -> list[str]:
    lines = ["model\tinput\toutput (max)\ttotal (max)"]
    for row in rows:
        selected = " [selected]" if row.model == selected_model else ""
        lines.append(
            f"{row.name}{selected}\t${row.input_cost:.2f}\t${row.output_cost:.2f}\t${row.total_cost:.2f}"
        )
    return lines


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
