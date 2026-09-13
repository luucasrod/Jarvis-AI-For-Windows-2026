param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

# One entry point for the existing .bat launchers. Does not import main.py,
# own its PID file, or replace its watchdog. Requires Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class JarvisArguments {
    [DllImport("shell32.dll", SetLastError=true)]
    static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string line, out int count);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Parse(string line) {
        int count;
        IntPtr memory = CommandLineToArgvW(line, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("Cannot parse process arguments");
        try {
            string[] args = new string[count];
            for (int i = 0; i < count; i++) args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
            return args;
        } finally { LocalFree(memory); }
    }
}
'@

function Get-FullPath([string]$Value) {
    return [IO.Path]::GetFullPath($Value).TrimEnd('\', '/')
}

$root = Get-FullPath ((Resolve-Path -LiteralPath $ProjectRoot).Path)
$main = Join-Path $root 'main.py'
$pidFile = Join-Path $root '.jarvis.pid'
$logFile = Join-Path $root 'jarvis.log'
$venvScripts = Join-Path $root '.venv\Scripts'

function Test-JarvisProcess($Process) {
    if ($Process.Name -notin @('python.exe', 'pythonw.exe') -or !$Process.CommandLine) { return $false }
    $arguments = [JarvisArguments]::Parse($Process.CommandLine)
    # Accept only a script invocation, never python -c/-m or a filename buried
    # in another program's arguments. Our launcher always uses absolute main.py.
    if ($arguments.Count -lt 2) { return $false }
    $scriptIndex = 1
    while ($scriptIndex -lt $arguments.Count -and $arguments[$scriptIndex] -in @('-u', '-B', '-E', '-s', '-S', '-I')) {
        $scriptIndex++
    }
    if ($scriptIndex -ge $arguments.Count) { return $false }
    $script = $arguments[$scriptIndex]
    if ([IO.Path]::IsPathRooted($script)) {
        return (Get-FullPath $script) -eq $main
    }
    # Compatibility with the old .bat: project venv + literal main.py. A bare
    # system Python with a relative script has no provable project identity.
    if ($script -notin @('main.py', '.\main.py', './main.py')) { return $false }
    if (![IO.Path]::IsPathRooted($arguments[0])) { return $false }
    return (Get-FullPath $arguments[0]) -in @(
        (Join-Path $venvScripts 'python.exe'), (Join-Path $venvScripts 'pythonw.exe')
    )
}

function Get-JarvisProcesses {
    # Failure to enumerate is an error, never interpreted as an empty list.
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { Test-JarvisProcess $_ })
}

function Get-PidState($Processes) {
    if (!(Test-Path -LiteralPath $pidFile)) { return 'absent' }
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $recordedId = 0
    if (![int]::TryParse($raw, [ref]$recordedId) -or $recordedId -le 0) { return 'invalid' }
    if (@($Processes | Where-Object { $_.ProcessId -eq $recordedId }).Count) { return 'matched' }
    if (Get-Process -Id $recordedId -ErrorAction SilentlyContinue) { return 'foreign' }
    return 'stale'
}

function Show-Status {
    $processes = @(Get-JarvisProcesses)
    $pidState = Get-PidState $processes
    $state = if ($processes.Count) {
        if ($pidState -eq 'matched') { 'running' } else { 'starting_or_pid_missing' }
    } elseif ($pidState -eq 'foreign') { 'identity_mismatch' } else { 'stopped' }
    [pscustomobject]@{
        state = $state
        process_ids = @($processes | ForEach-Object { [int]$_.ProcessId })
        pid_file = $pidFile
        pid_state = $pidState
        log_file = $logFile
    } | ConvertTo-Json -Compress
}

function Stop-Jarvis {
    $processes = @(Get-JarvisProcesses)
    $before = if (Test-Path -LiteralPath $pidFile) { Get-Content -LiteralPath $pidFile -Raw } else { $null }
    foreach ($candidate in $processes) {
        # Obtain a handle before checking creation time: PID reuse must not
        # turn a stale CIM snapshot into permission to kill a different process.
        $live = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
        if (!$live) { continue }
        try {
            $null = $live.Handle
            $current = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $candidate.ProcessId)
            if (!$current) { continue }
            if ($current.CreationDate -ne $candidate.CreationDate -or !(Test-JarvisProcess $current)) {
                throw 'Process identity changed; retry status before stopping.'
            }
            $live.Kill()
            if (!$live.WaitForExit(5000)) { throw 'Jarvis process did not exit in time.' }
        } finally { $live.Dispose() }
    }
    if (@(Get-JarvisProcesses).Count) { throw 'A Jarvis instance reappeared. Close the old looping launcher before retrying.' }
    # Remove only the unchanged PID file after all verified instances exited.
    # A PID belonging to another process is evidence to inspect, not to erase.
    if ($null -ne $before -and (Test-Path -LiteralPath $pidFile) -and
        (Get-Content -LiteralPath $pidFile -Raw) -eq $before -and
        (Get-PidState @()) -ne 'foreign') {
        Remove-Item -LiteralPath $pidFile
    }
}

function Start-Jarvis {
    $processes = @(Get-JarvisProcesses)
    if ($processes.Count) { return }
    if ((Get-PidState $processes) -eq 'foreign') { throw 'PID file identifies another process; startup refused.' }
    if (!(Test-Path -LiteralPath $main -PathType Leaf)) { throw 'main.py not found in project root.' }
    $python = Join-Path $venvScripts 'pythonw.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the project .venv before starting Jarvis.' }
    # pythonw retains main.py's existing jarvis.log redirection. No console loop
    # remains behind to resurrect a process after an intentional STOP.
    $started = Start-Process -FilePath $python -ArgumentList ('"' + $main + '"') -WorkingDirectory $root -WindowStyle Hidden -PassThru
    try {
        Start-Sleep -Milliseconds 800
        if ($started.HasExited -and !@(Get-JarvisProcesses).Count) { throw 'Jarvis exited during startup; inspect jarvis.log.' }
    } finally { $started.Dispose() }
}

$mutex = $null
$acquired = $false
try {
    if ($Action -eq 'logs') {
        [pscustomobject]@{ log_file = $logFile; exists = (Test-Path -LiteralPath $logFile -PathType Leaf) } | ConvertTo-Json -Compress
    } elseif ($Action -eq 'status') {
        Show-Status
    } else {
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $key = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '') }
        finally { $hash.Dispose() }
        $mutex = New-Object Threading.Mutex($false, ('Local\JarvisCommands-' + $key))
        try { $acquired = $mutex.WaitOne(10000) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (!$acquired) { throw 'Another Jarvis command is active; retry later.' }
        if ($Action -in @('stop', 'restart')) { Stop-Jarvis }
        if ($Action -in @('start', 'restart')) { Start-Jarvis }
        Show-Status
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
