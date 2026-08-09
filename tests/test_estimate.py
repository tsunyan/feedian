from __future__ import annotations

import unittest

from raindian.estimate import (
    MODEL_CATALOG_URL,
    MODEL_DOCS_BASE_URL,
    MODEL_PRICES,
    count_prompt_tokens,
    format_cost_rows,
    parse_sample_size,
    projected_costs,
    refresh_model_prices,
    select_sample,
)


def bookmark(item_id: int, collection_id: int) -> dict[str, object]:
    return {"_id": item_id, "collection": {"$id": collection_id}}


class EstimateTests(unittest.TestCase):
    def test_percentage_size_uses_minimum_twenty_and_ceiling(self) -> None:
        self.assertEqual(parse_sample_size("10%", 50), 20)
        self.assertEqual(parse_sample_size("10%", 3_112), 312)

    def test_integer_size_is_used_exactly(self) -> None:
        self.assertEqual(parse_sample_size("5", 3_112), 5)
        self.assertEqual(parse_sample_size("0", 3_112), 0)

    def test_size_is_clamped_and_invalid_values_fail(self) -> None:
        self.assertEqual(parse_sample_size("1000", 3), 3)
        with self.assertRaisesRegex(ValueError, "sample size"):
            parse_sample_size("abc", 10)
        with self.assertRaisesRegex(ValueError, "percentage"):
            parse_sample_size("101%", 10)
        with self.assertRaisesRegex(ValueError, "percentage"):
            parse_sample_size("NaN%", 10)

    def test_selection_is_proportional_and_evenly_spaced(self) -> None:
        items = [bookmark(index, 1) for index in range(1, 9)]
        items.extend(bookmark(index, 2) for index in range(9, 11))

        selected = select_sample(items, 5)

        self.assertEqual([item["_id"] for item in selected], [2, 4, 6, 8, 10])

    def test_unknown_model_uses_o200k_fallback(self) -> None:
        count, fallback = count_prompt_tokens("hello world", "unknown-model")

        self.assertGreater(count, 0)
        self.assertEqual(fallback, "o200k_base")

    def test_cost_projection_lists_each_supported_model(self) -> None:
        rows = projected_costs(
            population=100,
            input_tokens_per_item=2_000,
            max_output_tokens=800,
        )

        self.assertEqual(
            [row.name for row in rows],
            ["GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna", "GPT-5.5"],
        )
        self.assertEqual(rows[2].input_cost, 0.04)

    def test_sol_alias_is_marked_as_selected(self) -> None:
        lines = format_cost_rows(projected_costs(1, 1, 1), selected_model="gpt-5.6")

        self.assertIn("GPT-5.6 Sol [selected]", lines[1])

    def test_cost_rows_show_input_matched_and_maximum_output_estimates(self) -> None:
        typical_rows = projected_costs(
            population=3_112,
            input_tokens_per_item=365,
            max_output_tokens=365,
        )
        maximum_rows = projected_costs(
            population=3_112,
            input_tokens_per_item=365,
            max_output_tokens=800,
        )

        lines = format_cost_rows(
            maximum_rows,
            selected_model="gpt-5.6-luna",
            typical_rows=typical_rows,
        )

        self.assertEqual(
            lines[0],
            "model\tinput\toutput (input-matched)\ttotal (input-matched)\toutput (max)\ttotal (max)",
        )
        self.assertEqual(
            lines[3],
            "GPT-5.6 Luna [selected]\t$0.23\t$1.36\t$1.59\t$2.99\t$3.21",
        )

    def test_unknown_selected_model_is_explicit_in_the_table(self) -> None:
        lines = format_cost_rows(projected_costs(1, 1, 1), selected_model="custom-model")

        self.assertEqual(lines[0], "selected model: custom-model (not in comparison table)")

    def test_price_refresh_uses_recommended_models_and_configured_model(self) -> None:
        documents = {
            MODEL_CATALOG_URL: """
## Recommended models

- [GPT Test Alpha](/api/docs/models/test-alpha.md): First model.
- [GPT Test Beta](/api/docs/models/test-beta.md): Second model.

## Browse our full catalog of models
""",
            f"{MODEL_DOCS_BASE_URL}/api/docs/models/test-alpha.md": """
# GPT Test Alpha
Model ID: `test-alpha`
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $2.50 | 1M tokens |
| Output | $15.00 | 1M tokens |
""",
            f"{MODEL_DOCS_BASE_URL}/api/docs/models/test-beta.md": """
# GPT Test Beta
Model ID: `test-beta`
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $1.00 | 1M tokens |
| Output | $6.00 | 1M tokens |
""",
            f"{MODEL_DOCS_BASE_URL}/api/docs/models/custom-model.md": """
# Custom Model
Model ID: `custom-model`
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $0.50 | 1M tokens |
| Output | $3.00 | 1M tokens |
""",
        }

        refreshed = refresh_model_prices(
            "custom-model",
            timeout_seconds=1,
            fetch_text=lambda url, _: documents[url],
        )

        self.assertEqual(refreshed.source, "official")
        self.assertEqual(
            [(price.name, price.model, price.input_per_million, price.output_per_million) for price in refreshed.prices],
            [
                ("GPT Test Alpha", "test-alpha", 2.5, 15.0),
                ("GPT Test Beta", "test-beta", 1.0, 6.0),
                ("Custom Model", "custom-model", 0.5, 3.0),
            ],
        )

    def test_price_refresh_falls_back_when_the_official_catalog_is_unavailable(self) -> None:
        def unavailable(_: str, __: int) -> str:
            raise OSError("network unavailable")

        refreshed = refresh_model_prices(
            "gpt-5.5",
            timeout_seconds=1,
            fetch_text=unavailable,
        )

        self.assertEqual(refreshed.source, "fallback")
        self.assertEqual(refreshed.prices, MODEL_PRICES)
        self.assertIn("network unavailable", refreshed.warning or "")

    def test_price_refresh_keeps_the_selected_fallback_price_when_only_it_fails(self) -> None:
        documents = {
            MODEL_CATALOG_URL: """
## Recommended models

- [GPT Test Alpha](/api/docs/models/test-alpha.md): First model.

## Browse our full catalog of models
""",
            f"{MODEL_DOCS_BASE_URL}/api/docs/models/test-alpha.md": """
# GPT Test Alpha
Model ID: `test-alpha`
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $2.50 | 1M tokens |
| Output | $15.00 | 1M tokens |
""",
        }

        def fetch(url: str, _: int) -> str:
            if url in documents:
                return documents[url]
            raise OSError("selected model unavailable")

        refreshed = refresh_model_prices("gpt-5.5", timeout_seconds=1, fetch_text=fetch)

        self.assertEqual(refreshed.source, "official")
        self.assertEqual([price.model for price in refreshed.prices], ["test-alpha", "gpt-5.5"])
        self.assertIn("selected model unavailable", refreshed.warning or "")

    def test_price_refresh_can_fetch_only_the_selected_model(self) -> None:
        documents = {
            MODEL_CATALOG_URL: """
## Recommended models

- [GPT Test Alpha](/api/docs/models/test-alpha.md): First model.
- [GPT Test Beta](/api/docs/models/test-beta.md): Second model.

## Browse our full catalog of models
""",
            f"{MODEL_DOCS_BASE_URL}/api/docs/models/custom-model.md": """
# Custom Model
Model ID: `custom-model`
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $0.50 | 1M tokens |
| Cached input | $0.05 | 1M tokens |
| Output | $3.00 | 1M tokens |
""",
        }
        fetched_urls: list[str] = []

        def fetch(url: str, _: int) -> str:
            fetched_urls.append(url)
            return documents[url]

        refreshed = refresh_model_prices(
            "custom-model",
            timeout_seconds=1,
            fetch_text=fetch,
            include_recommended=False,
        )

        self.assertEqual([price.model for price in refreshed.prices], ["custom-model"])
        self.assertEqual(fetched_urls, [MODEL_CATALOG_URL, f"{MODEL_DOCS_BASE_URL}/api/docs/models/custom-model.md"])


if __name__ == "__main__":
    unittest.main()
