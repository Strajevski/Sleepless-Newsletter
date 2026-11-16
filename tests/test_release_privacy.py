import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from circus_newsletter.core import load_relays

PROJECT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class RelayPrivacyTests(unittest.TestCase):
    def test_empty_public_default_and_private_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/relays.json").write_text('{"relays": {}}')
            self.assertEqual(load_relays(root), {})
            (root / "config/relays.local.json").write_text(json.dumps({"relays": {
                "relay@example.com": {"name": "Example relay"}}}))
            self.assertEqual(load_relays(root), {"relay@example.com": "Example relay"})
            (root / "config/relays.local.json").write_text('{"broken"')
            with self.assertRaises(ValueError):
                load_relays(root)  # An invalid private file must not silently disable the active relay.

    def test_release_relay_default_has_no_service_identity(self):
        self.assertEqual(json.loads((PROJECT / "config/relays.json").read_text()), {"relays": {}})


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows launcher tests")
class TrialPrivacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.app = self.base / "Local installation"
        (self.app / "scripts").mkdir(parents=True)
        shutil.copy2(PROJECT / "scripts/run_trial.ps1", self.app / "scripts/run_trial.ps1")
        (self.app / "scripts/data_directory.py").write_text('''import json,sys
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--root')+1])
with Path(__file__).with_name('directory-queries.txt').open('a') as output:
 output.write(str(root)+'\\n')
print(root)
''')
        (self.app / "scripts/trial_weekly.py").write_text('''import json,sys
from pathlib import Path
Path(__file__).with_name('trial-args.json').write_text(json.dumps(sys.argv[1:]))
''')
        self.harness = self.base / "launcher-test.ps1"
        self.harness.write_text('''param([string]$Script, [string]$Source = '')
$ErrorActionPreference = 'Stop'
$global:answers = @('2026-W47', 'n')
function global:Read-Host { param($Prompt)
 if (-not $global:answers.Count) { throw 'Synthetic stop: no local frozen input' }
 $answer=$global:answers[0]; $global:answers=@($global:answers | Select-Object -Skip 1); return $answer
}
function global:Get-Command { param($Name, $ErrorAction)
 if ($Name -eq 'codex') { return [pscustomobject]@{ Source='fixture-codex' } }
 throw 'Unexpected executable lookup'
}
if ($Source) { & $Script -DataRoot $Source } else { & $Script }
''')

    def run_launcher(self, source=None):
        environment = os.environ.copy()
        environment["CIRCUS_PYTHON"] = sys.executable
        environment.pop("CIRCUS_DATA_ROOT", None)
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(self.harness),
                   "-Script", str(self.app / "scripts/run_trial.ps1")]
        if source is not None:
            command.extend(["-Source", str(source)])
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=20)

    def test_default_reads_local_installation_only(self):
        issue = self.app / "issues/2026-W47"
        issue.mkdir(parents=True)
        (issue / "issue-input-final.md").write_text("Synthetic frozen input")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        queries = (self.app / "scripts/directory-queries.txt").read_text().splitlines()
        self.assertEqual(queries, [str(self.app)])
        arguments = json.loads((self.app / "scripts/trial-args.json").read_text())
        self.assertEqual(arguments, ["2026-W47", "--root", str(self.app), "--skip-captioner"])

    def test_other_installation_requires_explicit_selection(self):
        source = self.base / "Explicit source"
        issue = source / "issues/2026-W47"
        issue.mkdir(parents=True)
        (issue / "issue-input-final.md").write_text("Synthetic frozen input")
        result = self.run_launcher(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = json.loads((self.app / "scripts/trial-args.json").read_text())
        self.assertEqual(arguments[arguments.index("--root")+1], str(source))

    def test_missing_local_issue_never_discovers_an_external_installation(self):
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.app / "scripts/trial-args.json").exists())
        queries = (self.app / "scripts/directory-queries.txt").read_text().splitlines()
        self.assertEqual(queries, [str(self.app)])


if __name__ == "__main__":
    unittest.main()
