param([string]$DataRoot = '')
$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$trialScript = Join-Path $projectRoot 'scripts\trial_weekly.py'
if (-not $DataRoot -and $env:CIRCUS_DATA_ROOT) { $DataRoot = $env:CIRCUS_DATA_ROOT }
$selectedInstallation = $DataRoot

$pythonCandidates = @(
    $(if ($env:CIRCUS_PYTHON) { $env:CIRCUS_PYTHON }),
    (Join-Path $projectRoot '.venv-host\Scripts\python.exe')
) | Where-Object { $_ }
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $python = $pythonCommand.Source }
}

if (-not $python) {
    throw 'Python was not found. Install the project environment or set CIRCUS_PYTHON.'
}
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw 'Codex CLI is not available in this session. Run as an account with the Codex login.'
}

function Get-NewsletterDataDirectory([string]$InstallationRoot) {
    $resolvedDirectory = & $python (Join-Path $projectRoot 'scripts\data_directory.py') --root $InstallationRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not read data directory for $InstallationRoot" }
    return [string]$resolvedDirectory
}
$projectDataDirectory = Get-NewsletterDataDirectory $projectRoot


$issueWeek = ''
do {
    $candidateWeek = (Read-Host 'Which issue week should I test? (YYYY-Www)').Trim().ToUpperInvariant()
    if ($candidateWeek -notmatch '^\d{4}-W\d{2}$') {
        Write-Host 'Enter the issue week in the form 2026-W43.' -ForegroundColor Yellow
        continue
    }
    $projectInput = Join-Path $projectDataDirectory "issues\$candidateWeek\issue-input-final.md"
    if ($selectedInstallation) {
        $dataRoot = (Resolve-Path -LiteralPath $selectedInstallation).Path
        $frozenInput = Join-Path (Get-NewsletterDataDirectory $dataRoot) "issues\$candidateWeek\issue-input-final.md"
        if (-not (Test-Path -LiteralPath $frozenInput -PathType Leaf)) {
            Write-Host "No frozen input found for $candidateWeek in the explicitly selected installation." -ForegroundColor Yellow
            $dataRoot = $null
        }
    } elseif (Test-Path -LiteralPath $projectInput -PathType Leaf) {
        $dataRoot = $projectRoot
    } else {
        $dataRoot = $null
        Write-Host "No local frozen input found for $candidateWeek. Select another installation explicitly with -DataRoot if needed." -ForegroundColor Yellow
    }
    if ($dataRoot) {
        $issueWeek = $candidateWeek
    }
} while (-not $issueWeek)

do {
    $captionChoice = (Read-Host 'Run the image captioner too? [y/N]').Trim()
    if ($captionChoice -notmatch '^(?i:y|yes|n|no)?$') {
        Write-Host 'Enter y or n (press Enter for no).' -ForegroundColor Yellow
    }
} while ($captionChoice -notmatch '^(?i:y|yes|n|no)?$')

$trialArgs = @($trialScript, $issueWeek, '--root', $dataRoot)
if ($captionChoice -notmatch '^(?i:y|yes)$') {
    $trialArgs += '--skip-captioner'
}

Write-Host "Starting isolated trial for $issueWeek."
Write-Host "Data source: $dataRoot"
Write-Host "Project and output folder: $projectRoot"
Write-Host 'The trial creates review files in the project output folder; it does not send mail.'
& $python @trialArgs
exit $LASTEXITCODE
