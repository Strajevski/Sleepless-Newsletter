import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from circus_newsletter import imap


class FakeMailbox:
    capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def __init__(self):
        self.calls = []

    def login(self, account, password):
        self.calls.append(("LOGIN", account, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"11 12"]
        uid, fields = args
        gmail_id = int("1a0e6277086e662e", 16) if uid == b"11" else int("1a0e6277086e662f", 16)
        if fields == "(UID X-GM-MSGID INTERNALDATE)":
            return "OK", [f'1 (UID {uid.decode()} X-GM-MSGID {gmail_id} INTERNALDATE "28-Sep-2026 12:34:56 +0100")'.encode()]
        if fields == "(UID BODY.PEEK[])":
            raw = b"From: Rowan <rowan@example.com>\r\nSubject: Test\r\n\r\nA submission\r\n"
            return "OK", [(f"1 (UID {uid.decode()} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(fields)

    def logout(self):
        self.calls.append(("LOGOUT",))


class ImapTests(unittest.TestCase):
    def test_existing_api_id_is_skipped_and_new_message_is_raw(self):
        mailbox = FakeMailbox()
        known = {"gmail:1a0e6277086e662e"}
        found = list(imap.messages(mailbox, known))
        self.assertEqual(len(found), 1)
        source_id, raw, received = found[0]
        self.assertEqual(source_id, "gmail:1a0e6277086e662f")
        self.assertTrue(raw.startswith(b"From: Rowan"))
        self.assertEqual(received.isoformat(), "2026-09-28T12:34:56+01:00")
        raw_fetches = [call for call in mailbox.calls if call[0] == "FETCH" and call[2] == "(UID BODY.PEEK[])"]
        self.assertEqual(raw_fetches, [("FETCH", b"12", "(UID BODY.PEEK[])")])

    def test_connection_selects_readonly_with_one_stored_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({
                "account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"
            }), encoding="utf-8")
            mailbox = FakeMailbox()
            with patch.object(imap, "stored_password", return_value="app-secret"), \
                 patch.object(imap.imaplib, "IMAP4_SSL", return_value=mailbox):
                self.assertIs(imap.connect(root), mailbox)
            self.assertIn(("LOGIN", "newsletter@example.com", "app-secret"), mailbox.calls)
            self.assertIn(("SELECT", "INBOX", True), mailbox.calls)


if __name__ == "__main__":
    unittest.main()
