# Shared, read-only task ownership checks. Names alone never establish ownership.
function Get-CircusTaskOwnership {
    param($Task, [string]$Root)
    $scripts = @{
        'Circus Newsletter Intake' = @('intake.py')
        'Circus Newsletter Final Build' = @('schedule.py', 'intake.py')
        'Circus Newsletter Delivery' = @('send_due.py')
    }
    if (-not $scripts.ContainsKey([string]$Task.TaskName)) { return $false }
    # These installers register in the root task folder; similarly named tasks
    # in other task folders are independent installations.
    if ($Task.TaskPath -ne '\') { return $false }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { return $false }
    $action = $actions[0]
    try {
        $base = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        if (-not [System.IO.Path]::IsPathRooted([string]$action.WorkingDirectory)) { return $false }
        if ([System.IO.Path]::GetFullPath($action.WorkingDirectory).TrimEnd('\', '/') -ne $base) { return $false }
        if (-not [System.IO.Path]::IsPathRooted([string]$action.Execute)) { return $false }
        if ([System.IO.Path]::GetFileName($action.Execute) -notin @('python.exe', 'pythonw.exe')) { return $false }
        $arguments = [string]$action.Arguments
        if ($arguments -notmatch '^\s*(?:"([^"]+)"|(\S+))(?:\s+(.*))?\s*$') { return $false }
        $script = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
        $tail = ([string]$Matches[3]).Trim()
        if (-not [System.IO.Path]::IsPathRooted($script)) { return $false }
        $script = [System.IO.Path]::GetFullPath($script)
        foreach ($name in $scripts[[string]$Task.TaskName]) {
            if ($script -ne (Join-Path $base "scripts\$name")) { continue }
            $expected = switch ($name) {
                'schedule.py' { '--run-intake' }
                'intake.py' { '--imap' }
                default { '' }
            }
            if ($tail -eq $expected) { return $true }
        }
    } catch { return $false }
    return $false
}

function Assert-CircusTaskCanBeRegistered {
    param([string]$TaskName, [string]$Root)
    # Query errors must stop installation, rather than allowing /Create /F to
    # replace an unknown task. Get-ScheduledTask with no filter lists all tasks.
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($task in $tasks) {
        if ($task.TaskName -eq $TaskName -and $task.TaskPath -eq '\' -and
            -not (Get-CircusTaskOwnership -Task $task -Root $Root)) {
            throw "Task '$TaskName' already exists for another or unrecognised installation. It was not replaced."
        }
    }
}
