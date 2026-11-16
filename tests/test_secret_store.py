import os
import tempfile
import unittest
from pathlib import Path

from circus_newsletter.secret_store import (DISCORD_SECRET_NAME, SECRET_NAME, read_password,
                                            read_secret, store_password, store_secret)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class SecretStoreTests(unittest.TestCase):
    def test_roundtrip_uses_encrypted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_password(root, "dummy-app-password")
            encrypted = (root / "config/secrets" / SECRET_NAME).read_bytes()
            self.assertNotIn(b"dummy-app-password", encrypted)
            self.assertEqual(read_password(root), "dummy-app-password")

    def test_discord_webhook_uses_separate_encrypted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url = "https://discord.com/api/webhooks/123/secret-token"
            store_secret(root, DISCORD_SECRET_NAME, url)
            encrypted = (root / "config/secrets" / DISCORD_SECRET_NAME).read_bytes()
            self.assertNotIn(url.encode(), encrypted)
            self.assertEqual(read_secret(root, DISCORD_SECRET_NAME), url)


if __name__ == "__main__":
    unittest.main()
