# Re-register once for the minute clock. Later schedule.json timing edits need no reinstall.
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [string]$Python = '',
    [switch]$Disabled
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $Python) { $Python = Join-Path $Root '.venv-host\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable not found: $Python" }
& $Python (Join-Path $Root 'scripts\schedule.py') --root $Root
if ($LASTEXITCODE -ne 0) { throw 'Schedule validation failed; task was not changed' }
$source = Join-Path $Root 'config\final-build-task.xml'
$taskName = 'Circus Newsletter Final Build'
. (Join-Path $PSScriptRoot 'task_ownership.ps1')
Assert-CircusTaskCanBeRegistered -TaskName $taskName -Root $Root
$runtime = [System.IO.Path]::GetTempFileName()
try {
    [xml]$xml = Get-Content -LiteralPath $source -Raw
    $xml.Task.Principals.Principal.UserId = $Account
    $xml.Task.Actions.Exec.Command = $Python
    $xml.Task.Actions.Exec.WorkingDirectory = $Root
    $xml.Task.Actions.Exec.Arguments = '"' + (Join-Path $Root 'scripts\schedule.py') + '" --run-intake'
    # Set the task-level switch before registration, avoiding any enabled interval.
    $enabledNode = $xml.Task.Settings.SelectSingleNode("*[local-name()='Enabled']")
    if ($null -eq $enabledNode) {
        $enabledNode = $xml.CreateElement('Enabled', $xml.DocumentElement.NamespaceURI)
        $null = $xml.Task.Settings.AppendChild($enabledNode)
    }
    $enabledNode.InnerText = if ($Disabled) { 'false' } else { 'true' }
    [System.IO.File]::WriteAllText($runtime, $xml.OuterXml, [System.Text.Encoding]::Unicode)
    & schtasks.exe /Create /F /TN $taskName /XML $runtime /RU $Account /RP '*'
    if ($LASTEXITCODE -ne 0) { throw "Task Scheduler registration failed with code $LASTEXITCODE" }
} finally {
    Remove-Item -LiteralPath $runtime -ErrorAction SilentlyContinue
}
