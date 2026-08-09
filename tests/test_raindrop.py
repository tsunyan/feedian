import json
import unittest
from unittest.mock import MagicMock, patch

from raindian.raindrop import RaindropClient


class RaindropClientTests(unittest.TestCase):
    def test_update_raindrop_note_uses_a_put_request_with_only_the_note_field(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"result":true}'
        context_manager = MagicMock()
        context_manager.__enter__.return_value = response

        with patch("raindian.raindrop.urlopen", return_value=context_manager) as urlopen_mock:
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

        with patch("raindian.raindrop.urlopen", return_value=context_manager) as urlopen_mock:
            client = RaindropClient(token="token", max_retries=0)
            client.append_raindrop_tags(5, 123, ["ai", "new-tag"])

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "https://api.raindrop.io/rest/v1/raindrops/5")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"ids": [123], "tags": ["ai", "new-tag"]},
        )

    def test_request_interval_spaces_each_http_request(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"result":true}'
        context_manager = MagicMock()
        context_manager.__enter__.return_value = response

        with (
            patch("raindian.raindrop.urlopen", return_value=context_manager),
            patch("raindian.raindrop.time.monotonic", side_effect=[0.0, 0.0]),
            patch("raindian.raindrop.time.sleep") as sleep,
        ):
            client = RaindropClient(token="token", max_retries=0, request_interval_seconds=0.5)
            client.update_raindrop_note(123, "one")
            client.update_raindrop_note(124, "two")

        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
