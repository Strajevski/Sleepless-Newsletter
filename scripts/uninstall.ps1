# Preview by default. This script needs PowerShell, not the environment it removes.
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Apply,
    [switch]$Yes,
    [switch]$RemoveCredentials
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'task_ownership.ps1')

function Assert-LocalPlainPath([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { throw 'Uninstall requires a local installation, not a network share.' }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -eq [System.IO.DriveType]::Network) { throw 'Uninstall refuses mapped network drives.' }
    $driveName = [System.IO.Path]::GetPathRoot($full).TrimEnd(':', '\', '/')
    $psDrive = Get-PSDrive -Name $driveName -ErrorAction Stop
    if (([string]$psDrive.Root).StartsWith('\\') -or ([string]$psDrive.DisplayRoot).StartsWith('\\')) {
        throw 'Uninstall refuses network-backed PowerShell drives.'
    }
    if ($drive.DriveType -in @([System.IO.DriveType]::Unknown, [System.IO.DriveType]::NoRootDirectory)) {
        throw 'The installation drive is not a recognised local volume.'
    }
    # Check each existing ancestor so a junction cannot redirect deletion to NAS
    # or a different installation, even when the final folder itself is ordinary.
    $itemPath = $full
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -LiteralPath $itemPath -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a link or junction in the removal path: $itemPath"
            }
        }
        $parent = Split-Path -Parent $itemPath
        if ($parent -eq $itemPath) { break }
        $itemPath = $parent
    }
    return $full
}

function Assert-EnvironmentSafe([string]$Path, [string]$Base) {
    $full = Assert-LocalPlainPath $Path
    if ($full -ne (Join-Path $Base '.venv-host') -or
        -not $full.StartsWith($Base.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Environment removal target is outside the selected installation.'
    }
    # Read local path metadata only. Never inspect an external/NAS data folder.
    # A user can configure data inside .venv-host; deleting it would lose journals.
    $settingsPath = Join-Path $Base 'config\settings.json'
    $null = Assert-LocalPlainPath $settingsPath
    if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $dataPath = $settings.storage.data_directory
        if ($null -eq $dataPath) { $dataPath = '.' }
        if ($dataPath -isnot [string] -or -not $dataPath.Trim()) { throw 'Invalid data directory setting; inspect it before uninstall.' }
        if ($dataPath -match '^~(?:[\\/]|$)') { $dataPath = Join-Path $env:USERPROFILE $dataPath.Substring(1).TrimStart('\', '/') }
        if ($dataPath.StartsWith('~') -or $dataPath -match '^[A-Za-z]:[^\\/]' -or $dataPath -match '^[\\/](?![\\/])') {
            throw 'Ambiguous data directory setting; inspect it before uninstall.'
        }
        $dataFull = if ([System.IO.Path]::IsPathRooted($dataPath)) {
            [System.IO.Path]::GetFullPath($dataPath)
        } else { [System.IO.Path]::GetFullPath((Join-Path $Base $dataPath)) }
        if ($dataFull.TrimEnd('\', '/') -eq $full -or $dataFull.StartsWith($full + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Private runtime data is configured inside the environment. Move and verify it before uninstall.'
        }
    }
    if (Test-Path -LiteralPath $full) {
        if (-not (Test-Path -LiteralPath (Join-Path $full 'pyvenv.cfg') -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $full 'Scripts\python.exe') -PathType Leaf)) {
            throw 'The environment is not recognisable. Inspect it manually; nothing will be deleted.'
        }
        foreach ($item in @(Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction Stop)) {
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw 'The environment contains a link or junction. Inspect it manually; nothing will be deleted.'
            }
            if ($item.PSIsContainer -and $item.Name -in @('issues', 'state', 'revisions', 'announcements', 'submissions', 'attachments')) {
                throw 'The environment contains possible private runtime folders. Inspect it manually; nothing will be deleted.'
            }
        }
    }
}

function Get-EnvironmentTaskReferences($Tasks, [string]$Environment) {
    foreach ($task in $Tasks) {
        foreach ($action in @($task.Actions)) {
            try {
                $executable = [System.IO.Path]::GetFullPath([string]$action.Execute)
                if ($executable.StartsWith($Environment.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
                    $task
                    break
                }
            } catch { continue }
        }
    }
}

if ($Yes -and -not $Apply) { throw '-Yes requires -Apply.' }
$Root = Assert-LocalPlainPath $Root
$Root = (Resolve-Path -LiteralPath $Root).Path
if ($Root.TrimEnd('\', '/') -eq [System.IO.Path]::GetPathRoot($Root).TrimEnd('\', '/')) {
    throw 'An installation cannot be uninstalled using a volume root.'
}
$Root = $Root.TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath (Join-Path $Root 'circus_newsletter') -PathType Container) -or
    -not (Test-Path -LiteralPath (Join-Path $Root 'scripts') -PathType Container)) {
    throw 'The selected folder is not recognisable as a newsletter installation.'
}
$environment = Join-Path $Root '.venv-host'
Assert-EnvironmentSafe -Path $environment -Base $Root
$tasks = @(Get-ScheduledTask -ErrorAction Stop)
$owned = @($tasks | Where-Object { Get-CircusTaskOwnership -Task $_ -Root $Root })
$references = @(Get-EnvironmentTaskReferences -Tasks $tasks -Environment $environment)
foreach ($task in $references) {
    if (-not (Get-CircusTaskOwnership -Task $task -Root $Root)) {
        throw 'An unrecognised scheduled task uses this environment. Nothing was changed; inspect task ownership first.'
    }
}
$credentials = @()
if ($RemoveCredentials) {
    foreach ($name in @('imap-password.dpapi', 'discord-webhook.dpapi')) {
        $path = Join-Path $Root "config\secrets\$name"
        $null = Assert-LocalPlainPath $path
        if (Test-Path -LiteralPath $path -PathType Leaf) { $credentials += $path }
    }
}
Write-Host "Installation: $Root"
foreach ($task in $owned) { Write-Host "Remove task: $($task.TaskPath)$($task.TaskName)" }
if (Test-Path -LiteralPath $environment) { Write-Host "Remove local environment: $environment" }
foreach ($path in $credentials) { Write-Host "Remove local credential: $path" }
foreach ($task in $tasks) {
    if ($task.TaskName -like 'Circus Newsletter*' -and -not (Get-CircusTaskOwnership -Task $task -Root $Root)) {
        Write-Host "Leave task belonging elsewhere or unrecognised: $($task.TaskPath)$($task.TaskName)"
    }
}
Write-Host 'Preserve application files, settings, roster, archives, journals, logs, state, and all external data.'
if (-not $RemoveCredentials) { Write-Host 'Preserve credentials.' }
if (-not $Apply) { Write-Host 'Preview only. Use -Apply to perform this plan.'; return }
if (-not $Yes -and (Read-Host 'Type UNINSTALL to apply this plan') -cne 'UNINSTALL') {
    Write-Host 'Cancelled; nothing changed.'
    return
}

# Recheck task actions immediately before changing them. Any failure stops before
# removing the environment or credentials; partial task removal can be rerun.
foreach ($task in $owned) {
    $current = Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
    if (-not (Get-CircusTaskOwnership -Task $current -Root $Root)) { throw 'Task ownership changed; uninstall stopped.' }
    $null = Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
    $current = Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
    if (-not (Get-CircusTaskOwnership -Task $current -Root $Root)) { throw 'Task ownership changed; uninstall stopped.' }
    if ($current.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $current = Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
        if (-not (Get-CircusTaskOwnership -Task $current -Root $Root)) { throw 'Task ownership changed; uninstall stopped.' }
        if ($current.State -ne 'Running') { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($current.State -eq 'Running') { throw 'A task is still running; environment and credentials were preserved.' }
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
}
$remaining = @(Get-ScheduledTask -ErrorAction Stop)
if (@(Get-EnvironmentTaskReferences -Tasks $remaining -Environment $environment).Count) {
    throw 'A scheduled task still references the environment; environment and credentials were preserved.'
}
Assert-EnvironmentSafe -Path $environment -Base $Root
if (Test-Path -LiteralPath $environment) { Remove-Item -LiteralPath $environment -Recurse -Force -ErrorAction Stop }
foreach ($path in $credentials) {
    $null = Assert-LocalPlainPath $path
    Remove-Item -LiteralPath $path -Force -ErrorAction Stop
}
Write-Host 'Uninstall complete. Private data and application files were preserved; shared Python and Codex were not removed.'
