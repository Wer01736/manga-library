$ErrorActionPreference = 'Stop'

$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$webUrl = 'http://127.0.0.1:8765'
$webHealthUrl = "$webUrl/api/health"
$bridgeHealthUrl = 'http://127.0.0.1:8766/api/18comic/health'
$outputLog = Join-Path $projectRoot 'launcher-server.log'
$errorLog = Join-Path $projectRoot 'launcher-server-error.log'
$mutex = [Threading.Mutex]::new($false, 'Local\ComicWebMvpLauncher')
$hasMutex = $false

function Get-Health([string]$Uri) {
    try {
        return Invoke-RestMethod -Uri $Uri -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Test-SameRuntime($Health) {
    if (-not $Health -or -not $Health.runtime_root) {
        return $false
    }
    try {
        $actual = [IO.Path]::GetFullPath([string]$Health.runtime_root).TrimEnd('\')
        return $actual.Equals($projectRoot, [StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

function Get-ListeningProcessIds([int[]]$Ports) {
    return @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in $Ports } |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Assert-NoUnknownListener([int]$Port, $Health) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0 -and -not (Test-SameRuntime $Health)) {
        throw "連接埠 $Port 已被其他或無法辨識的程式使用。為避免誤關程式，啟動已停止。"
    }
}

function Find-Python {
    $preferred = Join-Path $env:USERPROFILE 'miniconda3\python.exe'
    if (Test-Path -LiteralPath $preferred -PathType Leaf) {
        return $preferred
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw '找不到 Python。請確認 Miniconda 或 Python 已安裝。'
}

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) {
        throw '另一個漫畫網頁版啟動程序正在執行，請稍候再試。'
    }

    $webHealth = Get-Health $webHealthUrl
    $bridgeHealth = Get-Health $bridgeHealthUrl
    $webReady = Test-SameRuntime $webHealth
    $bridgeReady = Test-SameRuntime $bridgeHealth

    if ($webReady -and $bridgeReady) {
        Write-Host '漫畫網頁版已在運作。'
        if ($env:COMIC_LAUNCHER_NO_BROWSER -ne '1') {
            Start-Process $webUrl
        }
        exit 0
    }

    Assert-NoUnknownListener 8765 $webHealth
    Assert-NoUnknownListener 8766 $bridgeHealth

    $ownedPartialService = $webReady -or $bridgeReady
    if ($ownedPartialService) {
        $processIds = Get-ListeningProcessIds @(8765, 8766)
        if ($processIds.Count -gt 0) {
            Write-Host '偵測到未完整啟動的本專案服務，正在安全重新啟動。'
            foreach ($processId in $processIds) {
                Stop-Process -Id $processId -Force -ErrorAction Stop
            }
            Start-Sleep -Milliseconds 600
        }
    }

    $python = Find-Python
    Write-Host '正在啟動漫畫書庫與下載橋接……'
    $startParameters = @{
        FilePath = $python
        ArgumentList = @('main.py')
        WorkingDirectory = $projectRoot
        WindowStyle = 'Hidden'
        RedirectStandardOutput = $outputLog
        RedirectStandardError = $errorLog
        PassThru = $true
    }
    $server = Start-Process @startParameters

    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        Start-Sleep -Milliseconds 300
        if ($server.HasExited) {
            throw "背景程式已停止，結束代碼 $($server.ExitCode)。請查看 launcher-server-error.log。"
        }
        $webHealth = Get-Health $webHealthUrl
        $bridgeHealth = Get-Health $bridgeHealthUrl
        $webReady = Test-SameRuntime $webHealth
        $bridgeReady = Test-SameRuntime $bridgeHealth
    } while ((-not $webReady -or -not $bridgeReady) -and [DateTime]::UtcNow -lt $deadline)

    if (-not $webReady -or -not $bridgeReady) {
        throw '服務在 120 秒內沒有完整啟動。請查看 launcher-server-error.log。'
    }

    Write-Host '啟動完成。'
    if ($env:COMIC_LAUNCHER_NO_BROWSER -ne '1') {
        Start-Process $webUrl
    }
    exit 0
}
catch {
    Write-Host "[啟動失敗] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
