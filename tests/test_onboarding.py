import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from circus_newsletter import bootstrap, readiness
from circus_newsletter.settings import DEFAULTS

PROJECT = Path(__file__).resolve().parents[1]
REAL_EXAMPLE_CHECK = readiness._example_address


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for folder in ("config", "templates", "prompts"):
            (self.root / folder).mkdir()
        for filename in ("schedule.json", "delivery.json", "editor.json", "captioner.json",
                         "editor-output.schema.json", "captioner-output.schema.json"):
            shutil.copy2(PROJECT / "config" / filename, self.root / "config" / filename)
        for filename in ("issue-email.html", "preview-data.json"):
            shutil.copy2(PROJECT / "templates" / filename, self.root / "templates" / filename)
        for filename in ("edited-v1.5.1.md", "captions-v1.md"):
            shutil.copy2(PROJECT / "prompts" / filename, self.root / "prompts" / filename)
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings["email"]["login_address"] = "editor@newsletter.example.org"
        self.settings["newsletter"]["sender_address"] = "editor@newsletter.example.org"
        self.save()
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "reader": {"name": "Fictional Reader", "emails": ["reader@newsletter.example.org"]}}}))
        (self.root / "config/secrets").mkdir()
        (self.root / "config/secrets/imap-password.dpapi").write_bytes(b"fictional-encrypted-blob")
        self.patches = {}
        for key, target, kwargs in (
            # Positive readiness scenarios simulate an already configured account.
            # Keep its addresses reserved; production still rejects examples.
            ("example_fixture", "circus_newsletter.readiness._example_address", {"side_effect": lambda address:
                False if address in {"editor@newsletter.example.org", "reader@newsletter.example.org"}
                else REAL_EXAMPLE_CHECK(address)}),
            ("dependencies", "circus_newsletter.readiness._dependencies", {"return_value": None}),
            ("executable", "circus_newsletter.readiness.codex_executable", {"return_value": "codex-fixture.exe"}),
            ("process", "circus_newsletter.readiness.subprocess.run", {"return_value": SimpleNamespace(returncode=0)}),
            ("acl", "circus_newsletter.readiness.check_secret_acl", {"return_value": True}),
            ("password", "circus_newsletter.secret_store.read_password", {"return_value": "fictional-password"}),
            ("imap", "circus_newsletter.imap.connect", {}),
            ("smtp", "circus_newsletter.readiness.smtplib.SMTP_SSL", {}),
            ("quota", "circus_newsletter.editor_gate.read_codex_budget", {"return_value": SimpleNamespace(allowed=True)}),
            ("platform", "circus_newsletter.readiness.sys.platform", {"new": "win32"}),
        ):
            item = patch(target, **kwargs)
            self.patches[key] = item.start()
            self.addCleanup(item.stop)

    def save(self):
        (self.root / "config/settings.json").write_text(json.dumps(self.settings))

    def result(self, online=False):
        checks = readiness.inspect(self.root, online=online)
        return readiness.report(checks, online=online)

    def test_offline_is_non_sending_and_leaves_files_unchanged(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.result()
        self.assertTrue(result["passed"], result)
        for name in ("imap", "smtp", "quota"):
            self.patches[name].assert_not_called()
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "issues").exists())
        self.assertEqual(self.patches["process"].call_args.args[0][1:], ["login", "status"])

    def test_online_only_authenticates_and_queries_read_only_usage(self):
        result = self.result(online=True)
        self.assertTrue(result["passed"], result)
        self.patches["imap"].assert_called_once_with(self.root, "fictional-password")
        self.patches["imap"].return_value.logout.assert_called_once()
        self.patches["imap"].return_value.uid.assert_not_called()
        connection = self.patches["smtp"].return_value.__enter__.return_value
        connection.login.assert_called_once_with("editor@newsletter.example.org", "fictional-password")
        connection.sendmail.assert_not_called()
        connection.send_message.assert_not_called()
        self.patches["quota"].assert_called_once_with(root=self.root)

    def test_missing_credential_stops_email_authentication(self):
        (self.root / "config/secrets/imap-password.dpapi").unlink()
        self.assertFalse(self.result(online=True)["passed"])
        self.patches["imap"].assert_not_called()
        self.patches["smtp"].assert_not_called()

    def test_broad_secret_permissions_prevent_decryption(self):
        self.patches["acl"].return_value = False
        self.assertFalse(self.result()["passed"])
        self.patches["password"].assert_not_called()

    def test_service_errors_do_not_enter_report(self):
        self.patches["imap"].side_effect = RuntimeError("PRIVATE-ERROR-CONTENT")
        self.patches["smtp"].side_effect = RuntimeError("PRIVATE-ERROR-CONTENT")
        result = self.result(online=True)
        self.assertFalse(result["passed"])
        serialized = json.dumps(result)
        self.assertNotIn("PRIVATE-ERROR-CONTENT", serialized)
        self.assertNotIn("fictional-password", serialized)

    def test_placeholder_account_and_roster_are_not_ready(self):
        self.settings["email"]["login_address"] = "newsletter@example.com"
        self.settings["newsletter"]["sender_address"] = "newsletter@example.com"
        self.save()
        self.assertFalse(self.result(online=True)["passed"])
        self.patches["imap"].assert_not_called()
        self.settings["email"]["login_address"] = "editor@newsletter.example.org"
        self.settings["newsletter"]["sender_address"] = "editor@newsletter.example.org"
        self.save()
        (self.root / "config/roster.json").write_text((PROJECT / "config/roster.example.json").read_text())
        result = self.result()
        self.assertTrue(any(c["name"] == "Roster and schedule" and c["status"] == "fail" for c in result["checks"]))

    def test_unavailable_codex_has_action_and_no_online_query(self):
        self.patches["executable"].return_value = None
        result = self.result(online=True)
        self.assertFalse(result["passed"])
        self.patches["quota"].assert_not_called()
        check = next(c for c in result["checks"] if c["name"] == "Codex login")
        self.assertIn("codex login", check["action"])

    def test_blocked_ai_usage_warns_without_generation(self):
        self.patches["quota"].return_value = SimpleNamespace(allowed=False)
        result = self.result(online=True)
        self.assertTrue(result["passed"])
        self.assertTrue(any(c["name"] == "AI usage policy" and c["status"] == "warn" for c in result["checks"]))

    def test_bad_json_is_reported_without_traceback_or_credentials(self):
        (self.root / "config/settings.json").write_text('{"PRIVATE-CONTENT"')
        result = self.result(online=True)
        self.assertFalse(result["passed"])
        self.assertNotIn("PRIVATE-CONTENT", json.dumps(result))

    def test_reserved_fixture_domain_is_rejected_outside_positive_scenario_mock(self):
        self.assertTrue(REAL_EXAMPLE_CHECK("editor@newsletter.example.org"))
        self.assertTrue(REAL_EXAMPLE_CHECK("reader@example.com"))

    def test_existing_discord_webhook_is_checked_without_posting(self):
        (self.root / "config/secrets/discord-webhook.dpapi").write_bytes(b"fictional-blob")
        url = "https://discord.com/api/webhooks/123/NOT_A_REAL_WEBHOOK"
        with patch("circus_newsletter.secret_store.read_secret", return_value=url), \
             patch("circus_newsletter.discord.post_webhook") as post:
            result = self.result()
        self.assertTrue(result["passed"])
        post.assert_not_called()
        self.assertNotIn(url, json.dumps(result))


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Application with spaces"
        for folder in ("config", "scripts"):
            (self.root / folder).mkdir(parents=True)
        for name in bootstrap.REQUIREMENTS:
            shutil.copy2(PROJECT / name, self.root / name)
        for name in ("settings.example.json", "roster.example.json"):
            shutil.copy2(PROJECT / "config" / name, self.root / "config" / name)
        for name in ("doctor.py", "configure_imap.py"):
            (self.root / "scripts" / name).write_text("# Test fixture; never executed\n")
        self.platform = patch("circus_newsletter.bootstrap.sys.platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def fake_create(self, directory):
        (directory / "Scripts").mkdir(parents=True)
        (directory / "Scripts/python.exe").write_bytes(b"fixture")
        (directory / "pyvenv.cfg").write_text("fixture")

    def fake_run(self, command, **kwargs):
        return SimpleNamespace(returncode=0,
                               stdout=str((self.root / ".venv-host").resolve()) + "\nTrue\n")

    def test_first_setup_uses_only_local_environment_and_never_registers_tasks(self):
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run", side_effect=self.fake_run) as run, \
             patch("builtins.input", side_effect=AssertionError("Noninteractive setup prompted")):
            builder.return_value.create.side_effect = self.fake_create
            self.assertEqual(bootstrap.main(["--root", str(self.root), "--non-interactive"]), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[1:4] == ["-m", "pip", "install"] for command in commands))
        self.assertTrue(all(command[0] == str(self.root / ".venv-host/Scripts/python.exe") for command in commands))
        self.assertFalse(any("schtasks" in str(command).lower() or "send_due" in str(command) for command in commands))
        self.assertEqual(json.loads((self.root / "config/roster.json").read_text()), {"members": {}})
        self.assertTrue((self.root / "state").is_dir())

    def test_repeat_setup_preserves_settings_roster_archive_and_environment(self):
        bootstrap.configure(self.root, interactive=False)
        roster = self.root / "config/roster.json"
        roster.write_text('{"members": {"unchanged": {}}}')
        archive = self.root / "issues/old/outbound/delivery.json"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"existing-journal")
        settings = (self.root / "config/settings.json").read_bytes()
        self.fake_create(self.root / ".venv-host")
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run", side_effect=self.fake_run):
            self.assertEqual(bootstrap.main(["--root", str(self.root), "--non-interactive"]), 0)
            builder.assert_not_called()
        self.assertEqual((self.root / "config/settings.json").read_bytes(), settings)
        self.assertEqual(archive.read_bytes(), b"existing-journal")
        self.assertEqual(json.loads(roster.read_text()), {"members": {"unchanged": {}}})

    def test_incomplete_environment_is_not_deleted_or_recreated(self):
        directory = self.root / ".venv-host"
        directory.mkdir()
        marker = directory / "keep.txt"
        marker.write_text("preserve")
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                bootstrap.prepare_environment(self.root)
            builder.assert_not_called()
            run.assert_not_called()
        self.assertEqual(marker.read_text(), "preserve")

    def test_legacy_account_migrates_without_switching_to_example_values(self):
        (self.root / "config/mail.json").write_text(json.dumps({"account": "original@newsletter.example.org",
            "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}))
        bootstrap.configure(self.root, interactive=False)
        settings = json.loads((self.root / "config/settings.json").read_text())
        self.assertEqual(settings["email"]["login_address"], "original@newsletter.example.org")
        self.assertEqual(settings["newsletter"]["sender_address"], "original@newsletter.example.org")
        self.assertTrue(settings["notifications"]["discord_enabled"])

    def test_invalid_new_settings_do_not_replace_existing_settings(self):
        bootstrap.configure(self.root, interactive=False)
        path = self.root / "config/settings.json"
        before = path.read_bytes()
        with patch("builtins.input", side_effect=["Bad\nHeader", "", "", "", ""]):
            with self.assertRaises(ValueError):
                bootstrap.configure(self.root, interactive=True, reconfigure=True)
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_configuration_preserves_other_options(self):
        bootstrap.configure(self.root, interactive=False)
        path = self.root / "config/settings.json"
        settings = json.loads(path.read_text())
        settings["notifications"]["discord_enabled"] = False
        settings["newsletter"]["headings"]["editorial"] = "Final Thought"
        path.write_text(json.dumps(settings))
        with patch("builtins.input", side_effect=["New name", "", "New editor", "editor@newsletter.example.org", ""]):
            bootstrap.configure(self.root, interactive=True, reconfigure=True)
        after = json.loads(path.read_text())
        self.assertFalse(after["notifications"]["discord_enabled"])
        self.assertEqual(after["newsletter"]["headings"]["editorial"], "Final Thought")
        self.assertEqual(after["newsletter"]["editor_name"], "New editor")

    def test_dependency_failure_does_not_run_doctor_or_credentials(self):
        def process(command, **kwargs):
            if "pip" in command:
                raise subprocess.CalledProcessError(1, command)
            return self.fake_run(command, **kwargs)
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run", side_effect=process) as run:
            builder.return_value.create.side_effect = self.fake_create
            self.assertEqual(bootstrap.main(["--root", str(self.root), "--non-interactive"]), 1)
        self.assertFalse(any("doctor.py" in str(call.args[0]) or "configure_imap.py" in str(call.args[0])
                             for call in run.call_args_list))

    def test_noninteractive_credentials_flag_rejected_before_mutation(self):
        with self.assertRaises(SystemExit) as error:
            bootstrap.main(["--root", str(self.root), "--non-interactive", "--configure-credentials"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / "config/settings.json").exists())

    def test_readiness_failure_remains_incomplete(self):
        def process(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if "doctor.py" in str(command):
                result.returncode = 1
            return result
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run", side_effect=process):
            builder.return_value.create.side_effect = self.fake_create
            self.assertEqual(bootstrap.main(["--root", str(self.root), "--non-interactive"]), 1)

    def test_explicit_credential_setup_uses_hidden_input_script_not_arguments(self):
        bootstrap.configure(self.root, interactive=False)
        with patch("venv.EnvBuilder") as builder, patch("subprocess.run", side_effect=self.fake_run) as run, \
             patch("builtins.input", side_effect=AssertionError("Unexpected configuration prompt")):
            builder.return_value.create.side_effect = self.fake_create
            self.assertEqual(bootstrap.main(["--root", str(self.root), "--configure-credentials"]), 0)
        calls = [call.args[0] for call in run.call_args_list if "configure_imap.py" in str(call.args[0])]
        self.assertEqual(calls, [[str(self.root / ".venv-host/Scripts/python.exe"),
                                  str(self.root / "scripts/configure_imap.py"), "--root", str(self.root)]])

    def test_environment_with_wrong_prefix_is_not_used_for_installation(self):
        self.fake_create(self.root / ".venv-host")
        with patch("subprocess.run", return_value=SimpleNamespace(stdout="C:/another-environment\nTrue\n")) as run:
            with self.assertRaisesRegex(bootstrap.SetupError, "another installation"):
                bootstrap.prepare_environment(self.root)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
