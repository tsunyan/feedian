import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from raindian.retry import run_with_retries


def http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://example.com", code, "error", headers, None)


class RetryTests(unittest.TestCase):
    @patch("raindian.retry.time.sleep")
    def test_retries_transient_http_error_with_retry_after(self, sleep) -> None:
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise http_error(429, "3")
            return "ok"

        self.assertEqual(run_with_retries(operation, max_retries=3, retry_base_seconds=1), "ok")
        sleep.assert_called_once_with(3.0)

    @patch("raindian.retry.time.sleep")
    def test_does_not_retry_permanent_http_error(self, sleep) -> None:
        with self.assertRaises(HTTPError) as raised:
            run_with_retries(lambda: (_ for _ in ()).throw(http_error(401)), max_retries=3, retry_base_seconds=1)
        raised.exception.close()
        sleep.assert_not_called()
