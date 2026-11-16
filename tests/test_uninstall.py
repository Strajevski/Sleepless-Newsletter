import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
HARNESS = r'''
param([string]$Fixture, [string]$Root, [string]$Script, [string]$Result,
      [string]$Mode = 'uninstall', [switch]$Apply, [switch]$RemoveCredentials)
$ErrorActionPreference = 'Stop'
$global:FixtureData = Get-Content -LiteralPath $Fixture -Raw | ConvertFrom-Json
$global:FixtureTasks = @($global:FixtureData.tasks)
$global:FixtureCalls = [System.Collections.Generic.List[string]]::new()
function global:Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($global:FixtureData.queryFailure) { throw 'Synthetic inventory failure' }
    if ($TaskName) {
        $found = @($global:FixtureTasks | Where-Object { $_.TaskName -eq $TaskName -and $_.TaskPath -eq $TaskPath })
        if (-not $found.Count) { throw 'Fixture task not found' }
        if ($global:FixtureData.changeOnRead) { $found[0].Actions[0].WorkingDirectory = 'C:\Another installation' }
        return $found
    }
    return $global:FixtureTasks
}
function global:Get-Item {
    param($LiteralPath, [switch]$Force, $ErrorAction)
    if ($global:FixtureData.simulateLink -and $LiteralPath -eq (Join-Path $Root '.venv-host')) {
        return [pscustomobject]@{ Attributes = [System.IO.FileAttributes]::ReparsePoint }
    }
    Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force -ErrorAction Stop
}
function global:Disable-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    $global:FixtureCalls.Add('disable:' + $TaskName)
}
function global:Stop-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    $global:FixtureCalls.Add('stop:' + $TaskName)
    if ($global:FixtureData.stopFailure) { throw 'Synthetic stop failure' }
    foreach ($t in $global:FixtureTasks) { if ($t.TaskName -eq $TaskName) { $t.State = 'Ready' } }
}
function global:Unregister-ScheduledTask {
    param($TaskName, $TaskPath, $Confirm, $ErrorAction)
    $global:FixtureCalls.Add('unregister:' + $TaskName)
    $global:FixtureTasks = @($global:FixtureTasks | Where-Object { $_.TaskName -ne $TaskName -or $_.TaskPath -ne $TaskPath })
}
$code = 0
try {
    if ($Mode -eq 'ownership') {
        . $Script
        $answers = @($global:FixtureTasks | ForEach-Object { Get-CircusTaskOwnership -Task $_ -Root $Root })
        Write-Output ('OWNERSHIP:' + (ConvertTo-Json -InputObject $answers -Compress))
    } elseif ($Mode -eq 'guard') {
        . $Script
        Assert-CircusTaskCanBeRegistered -TaskName 'Circus Newsletter Delivery' -Root $Root
    } elseif ($Mode -eq 'install') {
        function global:schtasks.exe {
            $global:FixtureCalls.Add('create')
            $index = [Array]::IndexOf($args, '/XML')
            if ($index -lt 0) { throw 'Fixture expected an XML argument' }
            $global:CapturedXmlPath = [string]$args[$index + 1]
            # A plain .NET string avoids WinPS 5.1 serialising Get-Content's
            # extended provider metadata during the fixture's JSON report.
            $global:CapturedXml = [System.IO.File]::ReadAllText($global:CapturedXmlPath)
            $global:LASTEXITCODE = 0
        }
        if ((Get-Command 'schtasks.exe').CommandType -ne 'Function' -or
            (Get-Command 'Get-ScheduledTask').CommandType -ne 'Function') { throw 'Task mocks are not active' }
        & $Script -Root $Root -Python $env:FIXTURE_PYTHON -Disabled:([bool]$global:FixtureData.installDisabled)
    } else {
        & $Script -Root $Root -Apply:$Apply -Yes:$Apply -RemoveCredentials:$RemoveCredentials
    }
} catch {
    Write-Output ('ERROR:' + $_.Exception.Message)
    $code = 1
}
@{ tasks = @($global:FixtureTasks); calls = @($global:FixtureCalls); xml = $global:CapturedXml;
   xmlTemporaryExists = ($global:CapturedXmlPath -and (Test-Path -LiteralPath $global:CapturedXmlPath)) } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Result -Encoding utf8
exit $code
'''


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell safety tests")
class UninstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "Installation with spaces"
        for folder in ("scripts", "circus_newsletter", "config/secrets", ".venv-host/Scripts", "issues", "state"):
            (self.root / folder).mkdir(parents=True)
        (self.root / ".venv-host/pyvenv.cfg").write_text("synthetic fixture")
        (self.root / ".venv-host/Scripts/python.exe").write_bytes(b"not-an-executable")
        for name in ("imap-password.dpapi", "discord-webhook.dpapi"):
            (self.root / "config/secrets" / name).write_bytes(b"fictional credential")
        (self.root / "config/settings.json").write_text('{"storage":{"data_directory":"Z:/OtherNewsletter"}}')
        (self.root / "issues/keep.txt").write_text("private archive")
        (self.root / "state/keep.txt").write_text("delivery state")
        self.harness = self.base / "harness.ps1"
        self.harness.write_text(HARNESS, encoding="utf-8")

    def task(self, name="Circus Newsletter Delivery", root=None, script="send_due.py", tail="", folder="\\"):
        root = Path(root or self.root)
        return {"TaskName": name, "TaskPath": folder, "State": "Running", "Actions": [{
            "Execute": str(root / ".venv-host/Scripts/python.exe"), "WorkingDirectory": str(root),
            "Arguments": '"' + str(root / "scripts" / script) + '"' + (" " + tail if tail else "")}]}

    def run_script(self, tasks=(), *, apply=False, credentials=False, mode="uninstall", root=None, script_name=None, **flags):
        fixture, result = self.base / "fixture.json", self.base / "result.json"
        fixture.write_text(json.dumps({"tasks": list(tasks), **flags}))
        script = script_name or ("uninstall.ps1" if mode == "uninstall" else "task_ownership.ps1")
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(self.harness),
                   "-Fixture", str(fixture), "-Root", str(root or self.root),
                   "-Script", str(PROJECT / "scripts" / script), "-Result", str(result), "-Mode", mode]
        if apply:
            command.append("-Apply")
        if credentials:
            command.append("-RemoveCredentials")
        environment = os.environ.copy()
        environment["FIXTURE_PYTHON"] = sys.executable
        process = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=25)
        self.assertTrue(result.exists(), process.stdout + process.stderr)
        return process, json.loads(result.read_text(encoding="utf-8-sig"))

    def assert_private_preserved(self):
        self.assertEqual((self.root / "issues/keep.txt").read_text(), "private archive")
        self.assertEqual((self.root / "state/keep.txt").read_text(), "delivery state")
        self.assertTrue((self.root / "config/settings.json").exists())
        self.assertTrue((self.root / "scripts").exists())

    def test_preview_changes_nothing_even_with_credential_option(self):
        process, result = self.run_script([self.task()], credentials=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(result["calls"], [])
        self.assertTrue((self.root / ".venv-host").exists())
        self.assertTrue((self.root / "config/secrets/imap-password.dpapi").exists())
        self.assert_private_preserved()

    def test_apply_removes_only_owned_tasks_and_environment_and_is_repeatable(self):
        other = self.task(root=Path("Z:/OtherNewsletter"))
        other["TaskName"] = "Unrelated task"
        process, result = self.run_script([self.task(), other], apply=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual([t["TaskName"] for t in result["tasks"]], ["Unrelated task"])
        self.assertEqual(result["calls"], ["disable:Circus Newsletter Delivery", "stop:Circus Newsletter Delivery",
                                           "unregister:Circus Newsletter Delivery"])
        self.assertFalse((self.root / ".venv-host").exists())
        self.assertTrue((self.root / "config/secrets/imap-password.dpapi").exists())
        self.assert_private_preserved()
        process, result = self.run_script(result["tasks"], apply=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(result["calls"], [])

    def test_foreign_named_task_is_preserved_and_installer_refuses_collision(self):
        foreign = self.task(root=Path("Z:/OtherNewsletter"))
        process, result = self.run_script([foreign], apply=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(result["calls"], [])
        self.assertEqual(len(result["tasks"]), 1)
        process, result = self.run_script([foreign], mode="guard")
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("not replaced", process.stdout)

    def test_ownership_requires_exact_folder_script_arguments_and_single_action(self):
        good = self.task()
        wrong_script = self.task(script="other.py")
        wrong_args = self.task(tail='--root "Z:\\OtherNewsletter"')
        wrong_folder = self.task(folder="\\Other\\")
        wrong_working = self.task()
        wrong_working["Actions"][0]["WorkingDirectory"] = "Z:/OtherNewsletter"
        multiple = self.task()
        multiple["Actions"].append(dict(multiple["Actions"][0]))
        legacy = self.task(name="Circus Newsletter Intake", script="intake.py", tail="--imap")
        process, result = self.run_script([good, wrong_script, wrong_args, wrong_folder, wrong_working, multiple, legacy], mode="ownership")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        line = next(line for line in process.stdout.splitlines() if line.startswith("OWNERSHIP:"))
        self.assertEqual(json.loads(line.split(":", 1)[1]), [True, False, False, False, False, False, True])

    def test_unrecognised_environment_user_blocks_all_mutation(self):
        process, result = self.run_script([self.task(name="Another app")], apply=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result["calls"], [])
        self.assertTrue((self.root / ".venv-host").exists())

    def test_inventory_failure_preserves_environment(self):
        process, result = self.run_script(apply=True, queryFailure=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue((self.root / ".venv-host").exists())
        self.assert_private_preserved()

    def test_stop_failure_or_changed_ownership_preserves_environment_and_credentials(self):
        for flag in ("stopFailure", "changeOnRead"):
            process, result = self.run_script([self.task()], apply=True, credentials=True, **{flag: True})
            self.assertNotEqual(process.returncode, 0)
            self.assertTrue((self.root / ".venv-host").exists())
            self.assertTrue((self.root / "config/secrets/imap-password.dpapi").exists())
            self.assert_private_preserved()

    def test_credential_deletion_requires_explicit_option(self):
        process, result = self.run_script(apply=True, credentials=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertFalse((self.root / "config/secrets/imap-password.dpapi").exists())
        self.assertFalse((self.root / "config/secrets/discord-webhook.dpapi").exists())
        self.assert_private_preserved()

    def test_already_disabled_task_is_removed_without_a_stop_request(self):
        task = self.task()
        task["State"] = "Disabled"
        process, result = self.run_script([task], apply=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(result["calls"], ["disable:Circus Newsletter Delivery", "unregister:Circus Newsletter Delivery"])
        self.assertFalse((self.root / ".venv-host").exists())
        self.assert_private_preserved()

    def test_network_roots_and_unrecognised_environment_are_refused(self):
        process, result = self.run_script(apply=True, root="\\\\NAS\\Share\\Circus")
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("network share", process.stdout)
        (self.root / ".venv-host/pyvenv.cfg").unlink()
        process, result = self.run_script(apply=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue((self.root / ".venv-host/Scripts/python.exe").exists())

    def test_junctions_cannot_redirect_environment_or_root_removal(self):
        outside = self.base / "Outside protected folder"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("preserve")
        for link, root in ((self.root / ".venv-host/redirect", self.root),
                           (self.base / "Installation alias", self.base / "Installation alias")):
            target = outside if root == self.root else self.root
            command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:TEST_LINK -Target $env:TEST_TARGET | Out-Null"
            environment = os.environ.copy()
            environment.update(TEST_LINK=str(link), TEST_TARGET=str(target))
            created = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                                     env=environment, capture_output=True, text=True, timeout=15)
            if created.returncode:
                self.skipTest("Host does not allow temporary junction fixtures")
            try:
                process, result = self.run_script(apply=True, root=root)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(result["calls"], [])
                self.assertEqual(sentinel.read_text(), "preserve")
                self.assertTrue((self.root / ".venv-host/Scripts/python.exe").exists())
            finally:
                # Remove only the verified fixture junction itself, never its target.
                self.assertTrue(link.parent.is_relative_to(self.base))
                self.assertEqual(link.resolve(), target.resolve())
                os.rmdir(link)

    def test_registration_guard_allows_owned_tasks_but_stops_inventory_failure(self):
        process, result = self.run_script([self.task()], mode="guard")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        process, result = self.run_script(mode="guard", queryFailure=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result["calls"], [])

    def test_reparse_point_attribute_blocks_removal_even_without_junction_privileges(self):
        process, result = self.run_script([self.task()], apply=True, simulateLink=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("link or junction", process.stdout)
        self.assertEqual(result["calls"], [])
        self.assertTrue((self.root / ".venv-host/Scripts/python.exe").exists())
        self.assert_private_preserved()

    def test_private_runtime_data_inside_environment_blocks_removal(self):
        for directory in ('.venv-host', '.venv-host/private'):
            (self.root / 'config/settings.json').write_text(json.dumps({'storage': {'data_directory': directory}}))
            process, result = self.run_script([self.task()], apply=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(result['calls'], [])
            self.assertTrue((self.root / '.venv-host/Scripts/python.exe').exists())
        (self.root / 'config/settings.json').write_text('{"storage":{"data_directory":"."}}')
        (self.root / '.venv-host/private/issues').mkdir(parents=True)
        process, result = self.run_script(apply=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue((self.root / '.venv-host/Scripts/python.exe').exists())

    def test_installers_generate_local_task_actions_and_remove_temporary_xml(self):
        import xml.etree.ElementTree as ET
        (self.root / "scripts/schedule.py").write_text("# Synthetic validation; no services or files accessed\n")
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for stem, task_xml, script in (("delivery", "delivery-task.xml", "send_due.py"),
                                       ("final_build", "final-build-task.xml", "schedule.py")):
            shutil.copy2(PROJECT / "config" / task_xml, self.root / "config" / task_xml)
            process, result = self.run_script(mode="install", script_name=f"install_{stem}_task.ps1")
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(result["calls"], ["create"])
            self.assertFalse(result["xmlTemporaryExists"])
            xml = ET.fromstring(result["xml"])
            self.assertEqual(xml.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), sys.executable)
            self.assertEqual(xml.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns), str(self.root))
            arguments = xml.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
            self.assertTrue(arguments.startswith('"' + str(self.root / "scripts" / script) + '"'))

    def test_installers_can_register_disabled_without_changing_default_activation(self):
        import xml.etree.ElementTree as ET
        (self.root / "scripts/schedule.py").write_text("# Synthetic validation only\n")
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for stem, filename in (("delivery", "delivery-task.xml"), ("final_build", "final-build-task.xml")):
            shutil.copy2(PROJECT / "config" / filename, self.root / "config" / filename)
            for disabled in (True, False):
                process, result = self.run_script(mode="install", script_name=f"install_{stem}_task.ps1",
                                                  installDisabled=disabled)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                xml = ET.fromstring(result["xml"])
                self.assertEqual(xml.findtext("t:Settings/t:Enabled", namespaces=ns),
                                 "false" if disabled else "true")
                self.assertEqual(result["calls"], ["create"])

    def test_installer_does_not_reach_registration_when_name_is_owned_elsewhere(self):
        (self.root / "scripts/schedule.py").write_text("# Synthetic validation only\n")
        shutil.copy2(PROJECT / "config/delivery-task.xml", self.root / "config/delivery-task.xml")
        process, result = self.run_script([self.task(root=Path("Z:/OtherNewsletter"))], mode="install",
                                          script_name="install_delivery_task.ps1")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result["calls"], [])


if __name__ == "__main__":
    unittest.main()
