import unittest
from unittest.mock import patch

from raindian.extract import PageFetchResult
from raindian.llm import extract_output_text, summarize_bookmark


class LlmTests(unittest.TestCase):
    def test_extract_output_text_prefers_direct_field(self) -> None:
        self.assertEqual(extract_output_text({"output_text": "{}"}), "{}")

    def test_extract_output_text_from_response_items(self) -> None:
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"summary":"ok"}'},
                    ],
                }
            ]
        }
        self.assertEqual(extract_output_text(data), '{"summary":"ok"}')

    @patch("raindian.llm.urlopen")
    def test_summary_request_limits_output_and_reasoning(self, mock_urlopen) -> None:
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.return_value = (
            b'{"output_text":"{\\"note_title\\":\\"Title\\",\\"summary\\":\\"Summary\\",'
            b'\\"key_points\\":[],\\"tags\\":[\\"tag\\"],\\"content_type\\":\\"link\\"}"}'
        )

        summarize_bookmark(
            api_key="key",
            model="gpt-5.6-luna",
            item={"title": "Title", "link": "https://example.com"},
            page=PageFetchResult(url="https://example.com", text="Body", title="Title", error=None),
            language="ja",
            timeout_seconds=30,
            max_output_tokens=800,
            reasoning_effort="none",
        )

        payload = __import__("json").loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["max_output_tokens"], 800)
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["text"]["verbosity"], "low")


if __name__ == "__main__":
    unittest.main()
