import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from raindian.recovery import PendingTransaction, load_pending, save_pending


class RecoveryTests(unittest.TestCase):
    def test_pending_transaction_round_trips_and_is_scoped_to_destination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            destination = root / "vault-a" / "Raindrop"
            other_destination = root / "vault-b" / "Raindrop"
            transaction = PendingTransaction(
                transaction_id="txn-1",
                target=destination / "one.md",
                markdown="# One\n",
                usage_record={"transaction_id": "txn-1", "input_tokens": 10},
            )

            save_pending(destination, transaction, state_root=state_root)

            self.assertEqual(load_pending(destination, state_root=state_root), transaction)
            self.assertIsNone(load_pending(other_destination, state_root=state_root))


if __name__ == "__main__":
    unittest.main()
