param([switch]$NoBrowser, [switch]$Restart)

$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspacePath '.venv\Scripts\python.exe'
$configPath = Join-Path $workspacePath 'agent-config.local.json'
$dataDirectory = Join-Path $workspacePath '.temp\platform-preview'
$workbenchUrl = 'http://127.0.0.1:8765/'

function Get-WorkbenchListener {
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Assert-WorkbenchIdentity($listener) {
    $serverProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener.OwningProcess)
    if (-not $serverProcess -or
        -not $serverProcess.CommandLine.Contains('code_agent_platform') -or
        -not $serverProcess.CommandLine.Contains($dataDirectory)) {
        throw 'Port 8765 belongs to another service. No process was stopped.'
    }
}

$startupMutex = New-Object System.Threading.Mutex($false, 'Local\AI-Agent-Workbench-8765')
$lockHeld = $false
try {
    $lockHeld = $startupMutex.WaitOne(45000)
    if (-not $lockHeld) { throw 'Another workbench launch is still in progress.' }
    foreach ($requiredPath in @($pythonPath, $configPath,
            (Join-Path $dataDirectory 'platform.sqlite'), (Join-Path $dataDirectory 'access-token'))) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw ('Required local setup is missing: ' + $requiredPath + '. Existing data was not reset.')
        }
    }

    $listener = Get-WorkbenchListener
    if ($listener) {
        Assert-WorkbenchIdentity $listener
        if ($Restart) {
            # The venv launcher may already have exited; stop the verified
            # listener rather than relying on its recorded parent PID.
            Stop-Process -Id $listener.OwningProcess -ErrorAction Stop
            $stopDeadline = (Get-Date).AddSeconds(10)
            do {
                Start-Sleep -Milliseconds 200
                $listener = Get-WorkbenchListener
            } while ($listener -and (Get-Date) -lt $stopDeadline)
            if ($listener) { throw 'Workbench did not release port 8765 after restart.' }
        }
    }
    if (-not $listener) {
        $logStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $outputLog = Join-Path $dataDirectory ('server-' + $logStamp + '.log')
        $errorLog = Join-Path $dataDirectory ('server-' + $logStamp + '.err.log')
        $serverArguments = @('-m', 'code_agent_platform', '--config', ('"' + $configPath + '"'),
            '--data-dir', ('"' + $dataDirectory + '"'), 'serve', '--host', '127.0.0.1', '--port', '8765')
        # Keep the existing SQLite store, irrespective of a shell's database override.
        $previousDatabaseUrl = $env:AI_AGENT_DATABASE_URL
        try {
            $env:AI_AGENT_DATABASE_URL = ''
            $serverProcess = Start-Process -FilePath $pythonPath -ArgumentList $serverArguments `
                -WorkingDirectory $workspacePath -WindowStyle Hidden `
                -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog -PassThru
        } finally {
            $env:AI_AGENT_DATABASE_URL = $previousDatabaseUrl
        }
        Set-Content -LiteralPath (Join-Path $dataDirectory 'server.pid') -Value $serverProcess.Id
        Set-Content -LiteralPath (Join-Path $dataDirectory 'server.port') -Value 8765
    }

    $ready = $false
    $deadline = (Get-Date).AddSeconds(45)
    do {
        try {
            $listener = Get-WorkbenchListener
            if ($listener) {
                Assert-WorkbenchIdentity $listener
                $health = Invoke-RestMethod -Uri ($workbenchUrl + 'health') -TimeoutSec 2
                $ready = $health.status -eq 'ok'
            }
        } catch {
            if ($_.Exception.Message -like 'Port 8765 belongs*') { throw }
        }
        if (-not $ready) { Start-Sleep -Milliseconds 500 }
    } while (-not $ready -and (Get-Date) -lt $deadline)

    if (-not $ready) {
        throw ('Workbench startup did not finish. Check server logs in ' + $dataDirectory)
    }
    Set-Content -LiteralPath (Join-Path $dataDirectory 'server.pid') -Value $listener.OwningProcess
    Write-Output ('Workbench ready: ' + $workbenchUrl)
    if (-not $NoBrowser) { Start-Process $workbenchUrl }
} finally {
    if ($lockHeld) { $startupMutex.ReleaseMutex() }
    $startupMutex.Dispose()
}
