import json
import tempfile
import unittest
from pathlib import Path

from ilink_receive_probe.probe.identity_migration import (
    IdentityPromotionError,
    StagedIdentityPromotion,
)
from ilink_receive_probe.probe.storage import ProbeStorage


class FakeCredential:
    def __init__(self, directory, secret):
        self.state_dir = Path(directory)
        self.path = self.state_dir / "credential.dat"
        self.meta = self.state_dir / "credential.meta.json"
        self.secret = secret
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load(self):
        return self.secret


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.production = FakeCredential(root / "production", "old-secret")
        self.staging = FakeCredential(root / "staging", "new-secret")
        self.production.path.write_bytes(b"ENCRYPTED-OLD")
        self.production.meta.write_text(json.dumps({
            "schema_version": 1, "credential_type": "DPAPI_CURRENT_USER",
            "account_fingerprint": "a" * 12, "bot_fingerprint": "b" * 12,
            "state": "PRODUCTION",
        }), encoding="utf-8")
        self.staging.path.write_bytes(b"ENCRYPTED-NEW")
        self.staging.meta.write_text(json.dumps({
            "schema_version": 1, "credential_type": "DPAPI_CURRENT_USER",
            "account_fingerprint": "c" * 12, "bot_fingerprint": "d" * 12,
            "credential_fingerprint": "c" * 12, "state": "STAGED_NEW_BOT",
        }), encoding="utf-8")
        self.prod_db = root / "production.db"
        self.stage_db = root / "staging.db"
        for path, cursor in ((self.prod_db, "old-cursor"), (self.stage_db, "probe-4-cursor")):
            store = ProbeStorage(path)
            store.advance(cursor)
            store.close()

    def test_validation_and_promotion_keep_identity_set_consistent(self):
        promotion = StagedIdentityPromotion(
            self.production, self.staging, self.prod_db, self.stage_db,
            snapshot_dir=Path(self.temp.name) / "rollback",
            expected_account_fingerprint="c" * 12,
            process_guard=lambda: True,
        )
        validation, snapshot = promotion.promote()
        self.assertEqual(validation.bot_fingerprint, "d" * 12)
        self.assertTrue(snapshot.directory.exists())
        self.assertEqual(self.production.path.read_bytes(), b"ENCRYPTED-NEW")
        self.assertEqual(json.loads(self.production.meta.read_text())["state"], "PRODUCTION")
        store = ProbeStorage(self.prod_db)
        self.addCleanup(store.close)
        self.assertEqual(store.cursor(), "probe-4-cursor")
        self.assertEqual((snapshot.directory / "cursor.bin").read_text(), "old-cursor")

    def test_account_mismatch_fails_before_any_mutation(self):
        promotion = StagedIdentityPromotion(
            self.production, self.staging, self.prod_db, self.stage_db,
            snapshot_dir=Path(self.temp.name) / "rollback",
            expected_account_fingerprint="different-account",
            process_guard=lambda: True,
        )
        with self.assertRaises(IdentityPromotionError):
            promotion.promote()
        self.assertEqual(self.production.path.read_bytes(), b"ENCRYPTED-OLD")
        self.assertIn("b" * 12, self.production.meta.read_text())

    def test_active_process_blocks_promotion(self):
        promotion = StagedIdentityPromotion(
            self.production, self.staging, self.prod_db, self.stage_db,
            snapshot_dir=Path(self.temp.name) / "rollback",
            expected_account_fingerprint="c" * 12,
            process_guard=lambda: False,
        )
        with self.assertRaises(IdentityPromotionError):
            promotion.promote()
        self.assertEqual(self.production.path.read_bytes(), b"ENCRYPTED-OLD")


if __name__ == "__main__":
    unittest.main()
