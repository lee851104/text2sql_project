[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status", "credentials", "roster")]
    [string]$Mode = "start"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StateDirectory = Join-Path $ProjectRoot ".powerquery-public"
$LogDirectory = Join-Path $ProjectRoot "logs"
$PidPath = Join-Path $StateDirectory "public-offline.pid"
$MetadataPath = Join-Path $StateDirectory "public-offline.json"
$CredentialsPath = Join-Path $StateDirectory "admin-credentials.json"
$RosterPath = Join-Path $StateDirectory "accounts.yaml"
$LauncherLog = Join-Path $LogDirectory "public-offline-launcher.log"
$PowerQueryExecutable = Join-Path $ProjectRoot ".venv\Scripts\powerquery.exe"
$PowerQueryPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DatabasePath = Join-Path $ProjectRoot "data\processed\power.db"
$TailscaleExecutable = "C:\Program Files\Tailscale\tailscale.exe"
$LocalPort = 8766
$PublicPort = 8443
$LocalHealthUrl = "http://127.0.0.1:$LocalPort/api/health"

function Initialize-Directories {
    New-Item -ItemType Directory -Force -Path $StateDirectory, $LogDirectory | Out-Null
}

function Write-ServiceLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    Initialize-Directories
    $line = "{0} {1}" -f ([DateTimeOffset]::Now.ToString("o")), $Message
    Add-Content -LiteralPath $LauncherLog -Value $line -Encoding UTF8
    Write-Host $Message
}

function Get-TrackedProcess {
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }

    $rawPid = (Get-Content -LiteralPath $PidPath -Raw).Trim()
    $processId = 0
    if (-not [int]::TryParse($rawPid, [ref]$processId)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        return $null
    }

    $actualPath = $null
    try {
        $actualPath = $process.Path
    }
    catch {
        $actualPath = $null
    }
    if (-not $actualPath -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath($actualPath),
            [IO.Path]::GetFullPath($PowerQueryExecutable),
            [StringComparison]::OrdinalIgnoreCase
        )) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Get-OfflineHealth {
    try {
        $health = Invoke-RestMethod -Uri $LocalHealthUrl -TimeoutSec 2
        if ($health.success -eq $true -and
            $health.online_llm -eq $false -and
            $health.mode.active_mode -eq "offline") {
            return $health
        }
    }
    catch {
        return $null
    }
    return $null
}

function Test-LocalPortInUse {
    try {
        return $null -ne (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction Stop | Select-Object -First 1)
    }
    catch {
        return $false
    }
}

function Get-PublicUrl {
    $statusJson = & $TailscaleExecutable status --json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "無法讀取 Tailscale 狀態。"
    }
    $status = $statusJson | ConvertFrom-Json
    $dnsName = [string]$status.Self.DNSName
    $dnsName = $dnsName.Trim().TrimEnd(".")
    if (-not $dnsName) {
        throw "Tailscale 尚未提供本機 DNS 名稱。"
    }
    return "https://${dnsName}:$PublicPort/"
}

function Enable-PublicFunnel {
    $output = & $TailscaleExecutable funnel --bg "--https=$PublicPort" $LocalPort 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale Funnel 啟用失敗：$($output -join ' ')"
    }
    return Get-PublicUrl
}

function New-RandomAdminPassword {
    $bytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

function New-PublicAccountSet {
    # 兩個帳號，兩個都有審核權。四眼原則禁止提案人核准自己的變更，所以只給一個帳號
    # 會讓公開環境無法發布資料；兩個互為審核者，示範時也剛好能演完整流程。
    return @(
        [ordered]@{ username = "powerquery-admin-1"; password = New-RandomAdminPassword },
        [ordered]@{ username = "powerquery-admin-2"; password = New-RandomAdminPassword }
    )
}

function Get-OrCreateAdminCredentials {
    Initialize-Directories
    if (Test-Path -LiteralPath $CredentialsPath -PathType Leaf) {
        $saved = Get-Content -LiteralPath $CredentialsPath -Raw | ConvertFrom-Json
        if ($saved.accounts -and $saved.accounts.Count -ge 2) {
            return $saved
        }
        if ($saved.username -and $saved.password) {
            # 舊格式只有一組帳密。保留原本那組，補第二組，不讓既有使用者的密碼被換掉。
            $migrated = [ordered]@{
                accounts = @(
                    [ordered]@{ username = [string]$saved.username; password = [string]$saved.password },
                    [ordered]@{ username = "powerquery-admin-2"; password = New-RandomAdminPassword }
                )
                created_at = if ($saved.created_at) { [string]$saved.created_at } else { [DateTimeOffset]::Now.ToString("o") }
                migrated_at = [DateTimeOffset]::Now.ToString("o")
            }
            $migrated | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $CredentialsPath -Encoding UTF8
            return [pscustomobject]$migrated
        }
        throw "Public administrator credential file is invalid: $CredentialsPath"
    }

    $credentials = [ordered]@{
        accounts = New-PublicAccountSet
        created_at = [DateTimeOffset]::Now.ToString("o")
    }
    $credentials | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $CredentialsPath -Encoding UTF8
    return [pscustomobject]$credentials
}

function Get-PasswordHash {
    param([Parameter(Mandatory = $true)][string]$Password)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $hash = ($Password | & $PowerQueryPython -m serving.accounts hash) | Select-Object -Last 1
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($hash)) {
        throw "無法產生密碼雜湊；請確認 .venv 已建立且 serving.accounts 可執行。"
    }
    return $hash.Trim()
}

function Write-PublicRoster {
    param([Parameter(Mandatory = $true)]$Credentials)

    Initialize-Directories
    $lines = @(
        "# 公開離線服務自動產生，請勿手動編輯；停止服務不會刪除本檔。",
        "# 兩個帳號都有審核權：四眼原則禁止提案人核准自己的變更，只有一個帳號會無法發布。",
        "accounts:"
    )
    foreach ($account in $Credentials.accounts) {
        $hash = Get-PasswordHash -Password ([string]$account.password)
        $lines += "  - username: $($account.username)"
        $lines += "    password: `"$hash`""
        $lines += "    scope: all"
        $lines += "    can_review: true"
    }
    Set-Content -LiteralPath $RosterPath -Value $lines -Encoding UTF8
    return $RosterPath
}

function Write-PublicRosterFile {
    # 重建名冊而不啟動服務。啟動流程也會呼叫同一個寫入函式，所以這個模式驗證的
    # 就是正式路徑，不是另一份複製品。
    $credentials = Get-OrCreateAdminCredentials
    $path = Write-PublicRoster -Credentials $credentials
    Write-Host "Roster written: $path"
    foreach ($account in $credentials.accounts) {
        Write-Host "  $($account.username)  scope=all  can_review=true"
    }
}

function Show-AdminCredentials {
    $credentials = Get-OrCreateAdminCredentials
    Write-Host "Public data-center accounts (both may review):"
    foreach ($account in $credentials.accounts) {
        Write-Host "  $($account.username) / $($account.password)"
    }
    Write-Host "Credential file: $CredentialsPath"
    Write-Host "Roster file: $RosterPath"
    Write-Host "Publishing data needs a second account: a proposer cannot approve their own change."
}

function Start-PublicOfflineService {
    Initialize-Directories

    if (-not (Test-Path -LiteralPath $TailscaleExecutable -PathType Leaf)) {
        throw "找不到 Tailscale：$TailscaleExecutable"
    }
    if (-not (Test-Path -LiteralPath $PowerQueryExecutable -PathType Leaf)) {
        throw "找不到 PowerQuery 虛擬環境。請先執行專案根目錄的 啟動.bat。"
    }
    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "找不到資料庫：$DatabasePath"
    }

    $publicUrl = Get-PublicUrl
    $publicHost = ([Uri]$publicUrl).Host
    $credentials = Get-OrCreateAdminCredentials
    $tracked = Get-TrackedProcess
    if ($null -ne $tracked) {
        $health = Get-OfflineHealth
        if ($null -eq $health) {
            throw "已追蹤的公開程序存在，但離線健康檢查失敗。請先執行 停止公開服務.bat。"
        }
        $publicUrl = Enable-PublicFunnel
        Write-ServiceLog "公開離線服務已在執行：$publicUrl"
        Show-AdminCredentials
        return
    }

    if ($null -ne (Get-OfflineHealth) -or (Test-LocalPortInUse)) {
        throw "本機連接埠 $LocalPort 已被未追蹤的程序占用；為避免誤把線上服務公開，啟動已中止。"
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $LogDirectory "public-offline-$stamp.stdout.log"
    $stderrLog = Join-Path $LogDirectory "public-offline-$stamp.stderr.log"
    $rosterPath = Write-PublicRoster -Credentials $credentials
    $savedEnvironment = @{
        OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process")
        POWERQUERY_ADMIN_USERNAME = [Environment]::GetEnvironmentVariable("POWERQUERY_ADMIN_USERNAME", "Process")
        POWERQUERY_ADMIN_PASSWORD = [Environment]::GetEnvironmentVariable("POWERQUERY_ADMIN_PASSWORD", "Process")
        POWERQUERY_ADMIN_ALLOWED_HOSTS = [Environment]::GetEnvironmentVariable("POWERQUERY_ADMIN_ALLOWED_HOSTS", "Process")
        POWERQUERY_ACCOUNT_ROSTER = [Environment]::GetEnvironmentVariable("POWERQUERY_ACCOUNT_ROSTER", "Process")
        POWERQUERY_ANONYMOUS_QUERY_SCOPE = [Environment]::GetEnvironmentVariable("POWERQUERY_ANONYMOUS_QUERY_SCOPE", "Process")
        POWERQUERY_TRUSTED_PROXIES = [Environment]::GetEnvironmentVariable("POWERQUERY_TRUSTED_PROXIES", "Process")
        PYTHONUNBUFFERED = [Environment]::GetEnvironmentVariable("PYTHONUNBUFFERED", "Process")
    }

    try {
        [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $null, "Process")
        # 名冊生效時單一帳號的環境變數不再使用；清掉以免兩套帳號來源並存。
        [Environment]::SetEnvironmentVariable("POWERQUERY_ADMIN_USERNAME", $null, "Process")
        [Environment]::SetEnvironmentVariable("POWERQUERY_ADMIN_PASSWORD", $null, "Process")
        [Environment]::SetEnvironmentVariable("POWERQUERY_ACCOUNT_ROSTER", $rosterPath, "Process")
        [Environment]::SetEnvironmentVariable("POWERQUERY_ADMIN_ALLOWED_HOSTS", "127.0.0.1,localhost,::1,$publicHost", "Process")
        # Funnel 轉到 127.0.0.1，外部訪客在服務眼中全是 loopback；信任這個代理才能
        # 從 X-Forwarded-For 取回真正的來源，否則限速會變成全域共用一桶。
        [Environment]::SetEnvironmentVariable("POWERQUERY_TRUSTED_PROXIES", "127.0.0.1,::1", "Process")
        # 公開網址的查詢需要登入：執行模式與 API key 是行程全域的，匿名可查等於任何
        # 訪客都在燒管理員輸入的那把 key。
        [Environment]::SetEnvironmentVariable("POWERQUERY_ANONYMOUS_QUERY_SCOPE", "denied", "Process")
        [Environment]::SetEnvironmentVariable("PYTHONUNBUFFERED", "1", "Process")

        $startParameters = @{
            FilePath = $PowerQueryExecutable
            ArgumentList = @("--serve", "--host", "127.0.0.1", "--port", "$LocalPort")
            WorkingDirectory = $ProjectRoot
            WindowStyle = "Hidden"
            RedirectStandardOutput = $stdoutLog
            RedirectStandardError = $stderrLog
            PassThru = $true
        }
        $process = Start-Process @startParameters
    }
    finally {
        foreach ($name in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
        }
    }

    Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding ASCII
    $healthy = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if ($process.HasExited) {
            break
        }
        if ($null -ne (Get-OfflineHealth)) {
            $healthy = $true
            break
        }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }

    if (-not $healthy) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        throw "公開離線服務未能通過健康檢查。請查看 $stderrLog"
    }

    try {
        $publicUrl = Enable-PublicFunnel
    }
    catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        throw
    }

    $metadata = [ordered]@{
        mode = "offline"
        pid = $process.Id
        local_url = "http://127.0.0.1:$LocalPort/"
        public_url = $publicUrl
        public_port = $PublicPort
        started_at = [DateTimeOffset]::Now.ToString("o")
        stdout_log = $stdoutLog
        stderr_log = $stderrLog
        query_error_log = Join-Path $ProjectRoot "data\processed\.powerquery-data\query-errors.jsonl"
    }
    $metadata | ConvertTo-Json | Set-Content -LiteralPath $MetadataPath -Encoding UTF8
    Write-ServiceLog "公開離線服務已啟動：$publicUrl"
    Write-ServiceLog "服務日誌：$stdoutLog；錯誤日誌：$stderrLog"
    Show-AdminCredentials
}

function Stop-PublicOfflineService {
    Initialize-Directories

    if (Test-Path -LiteralPath $TailscaleExecutable -PathType Leaf) {
        $output = & $TailscaleExecutable funnel "--https=$PublicPort" off 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-ServiceLog "警告：無法關閉 $PublicPort Funnel：$($output -join ' ')"
        }
    }

    $process = Get-TrackedProcess
    if ($null -ne $process) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit(5000) | Out-Null
        Write-ServiceLog "公開離線程序已停止。"
    }
    else {
        Write-ServiceLog "沒有找到執行中的公開離線程序。"
    }

    Remove-Item -LiteralPath $PidPath, $MetadataPath -Force -ErrorAction SilentlyContinue
}

function Show-PublicOfflineStatus {
    Initialize-Directories
    $process = Get-TrackedProcess
    $health = Get-OfflineHealth
    if ($null -eq $process -or $null -eq $health) {
        Write-Host "狀態：未執行"
        exit 1
    }

    $publicUrl = Get-PublicUrl
    Write-Host "狀態：執行中（offline）"
    Write-Host "本機：http://127.0.0.1:$LocalPort/"
    Write-Host "公開：$publicUrl"
    Write-Host "PID：$($process.Id)"
}

try {
    switch ($Mode) {
        "start" { Start-PublicOfflineService }
        "stop" { Stop-PublicOfflineService }
        "status" { Show-PublicOfflineStatus }
        "credentials" { Show-AdminCredentials }
        "roster" { Write-PublicRosterFile }
    }
}
catch {
    Write-ServiceLog "錯誤：$($_.Exception.Message)"
    exit 1
}
