from __future__ import annotations

import unittest

from raindian.estimate import (
    count_prompt_tokens,
    parse_sample_size,
    projected_costs,
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


if __name__ == "__main__":
    unittest.main()
