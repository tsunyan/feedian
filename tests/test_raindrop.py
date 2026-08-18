import json
import unittest
from unittest.mock import MagicMock, patch

from feedian.raindrop import RaindropClient


class RaindropClientTests(unittest.TestCase):
    def test_update_raindrop_note_uses_a_put_request_with_only_the_note_field(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"result":true}'
        context_manager = MagicMock()
        context_manager.__enter__.return_value = response

        with patch("feedian.raindrop.urlopen", return_value=context_manager) as urlopen_mock:
            client = RaindropClient(token="token", max_retries=0)
            client.update_raindrop_note(123, "Japanese summary")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "https://api.raindrop.io/rest/v1/raindrop/123")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"note": "Japanese summary"})

    def test_append_raindrop_tags_uses_the_collection_batch_endpoint(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"result":true}'
        context_manager = MagicMock()
        context_manager.__enter__.return_value = response

        with patch("feedian.raindrop.urlopen", return_value=context_manager) as urlopen_mock:
            client = RaindropClient(token="token", max_retries=0)
            client.append_raindrop_tags(5, 123, ["ai", "new-tag"])

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "https://api.raindrop.io/rest/v1/raindrops/5")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"ids": [123], "tags": ["ai", "new-tag"]},
        )

    def test_iter_raindrop_pages_yields_pages_as_returned_including_a_short_final_page(
        self,
    ) -> None:
        first_page = MagicMock()
        first_page.read.return_value = json.dumps(
            {"items": [{"_id": 1}, {"_id": 2}]}
        ).encode("utf-8")
        second_page = MagicMock()
        second_page.read.return_value = json.dumps({"items": [{"_id": 3}]}).encode("utf-8")
        context_manager = MagicMock()
        context_manager.__enter__.side_effect = [first_page, second_page]

        with patch("feedian.raindrop.urlopen", return_value=context_manager):
            client = RaindropClient(token="token", max_retries=0)
            pages = list(client.iter_raindrop_pages(5, per_page=2, nested=False))

        self.assertEqual([len(page) for page in pages], [2, 1])
        self.assertEqual(pages[0], [{"_id": 1}, {"_id": 2}])
        self.assertEqual(pages[1], [{"_id": 3}])

    def test_iter_raindrops_with_limit_smaller_than_a_page_stops_mid_page(self) -> None:
        page = MagicMock()
        page.read.return_value = json.dumps(
            {"items": [{"_id": 1}, {"_id": 2}, {"_id": 3}]}
        ).encode("utf-8")
        context_manager = MagicMock()
        context_manager.__enter__.return_value = page

        with patch("feedian.raindrop.urlopen", return_value=context_manager) as urlopen_mock:
            client = RaindropClient(token="token", max_retries=0)
            items = list(client.iter_raindrops(5, per_page=3, nested=False, limit=2))

        self.assertEqual(items, [{"_id": 1}, {"_id": 2}])
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_iter_raindrops_without_limit_returns_every_item_across_pages_in_order(
        self,
    ) -> None:
        first_page = MagicMock()
        first_page.read.return_value = json.dumps(
            {"items": [{"_id": 1}, {"_id": 2}]}
        ).encode("utf-8")
        second_page = MagicMock()
        second_page.read.return_value = json.dumps({"items": [{"_id": 3}]}).encode("utf-8")
        context_manager = MagicMock()
        context_manager.__enter__.side_effect = [first_page, second_page]

        with patch("feedian.raindrop.urlopen", return_value=context_manager):
            client = RaindropClient(token="token", max_retries=0)
            items = list(client.iter_raindrops(5, per_page=2, nested=False))

        self.assertEqual(items, [{"_id": 1}, {"_id": 2}, {"_id": 3}])

    def test_request_interval_spaces_each_http_request(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"result":true}'
        context_manager = MagicMock()
        context_manager.__enter__.return_value = response

        with (
            patch("feedian.raindrop.urlopen", return_value=context_manager),
            patch("feedian.raindrop.time.monotonic", side_effect=[0.0, 0.0]),
            patch("feedian.raindrop.time.sleep") as sleep,
        ):
            client = RaindropClient(token="token", max_retries=0, request_interval_seconds=0.5)
            client.update_raindrop_note(123, "one")
            client.update_raindrop_note(124, "two")

        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
