[CmdletBinding()]
param(
    [Parameter()]
    [string]$RuntimePath,

    [Parameter()]
    [string]$ModelsDir
)

$ErrorActionPreference = "Stop"
$ollamaHost = "http://127.0.0.1:11434"
$repoParent = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if ([string]::IsNullOrWhiteSpace($RuntimePath)) {
    $RuntimePath = Join-Path $repoParent ".runtimes\ollama-v0.40.11\bin\ollama.exe"
}
if ([string]::IsNullOrWhiteSpace($ModelsDir)) {
    $ModelsDir = Join-Path $repoParent ".runtimes\ollama-models"
}

function Get-LocalOllamaStatus {
    try {
        $tags = Invoke-RestMethod -Uri "$ollamaHost/api/tags" -TimeoutSec 2
        $version = $null
        try {
            $version = Invoke-RestMethod -Uri "$ollamaHost/api/version" -TimeoutSec 2
        }
        catch {
            # Older compatible servers may not expose /api/version.
        }
        $names = @($tags.models | ForEach-Object { $_.name } | Where-Object { $_ })
        return [PSCustomObject]@{
            Reachable = $true
            Version   = [string]$version.version
            Models    = $names
        }
    }
    catch {
        return [PSCustomObject]@{
            Reachable = $false
            Version   = $null
            Models    = @()
        }
    }
}

$status = Get-LocalOllamaStatus
if ($status.Reachable) {
    $modelSummary = if ($status.Models.Count -gt 0) { $status.Models -join ", " } else { "none installed" }
    Write-Output "Ollama is already reachable at $ollamaHost (version $($status.Version); models: $modelSummary)."
    return
}

if (-not (Test-Path -LiteralPath $RuntimePath -PathType Leaf)) {
    throw "Ollama executable was not found. Pass -RuntimePath with the path to ollama.exe."
}
if (-not (Test-Path -LiteralPath $ModelsDir -PathType Container)) {
    New-Item -ItemType Directory -Path $ModelsDir -Force | Out-Null
}

$outputsDir = Join-Path $PSScriptRoot "..\outputs"
New-Item -ItemType Directory -Path $outputsDir -Force | Out-Null
$stdoutLog = Join-Path $outputsDir "ollama-stdout.log"
$stderrLog = Join-Path $outputsDir "ollama-stderr.log"

# Start-Process inherits this PowerShell process environment. Restore the values
# immediately afterward; nothing is written to the user's or machine's environment.
$environmentNames = @("OLLAMA_HOST", "OLLAMA_MODELS", "OLLAMA_NO_CLOUD")
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

try {
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    $env:OLLAMA_MODELS = (Resolve-Path -LiteralPath $ModelsDir).Path
    $env:OLLAMA_NO_CLOUD = "1"
    $process = Start-Process `
        -FilePath (Resolve-Path -LiteralPath $RuntimePath).Path `
        -ArgumentList "serve" `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
}
finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        throw "Ollama exited before becoming ready (exit code $($process.ExitCode)); inspect outputs/ollama-stderr.log."
    }
    $status = Get-LocalOllamaStatus
    if ($status.Reachable) {
        $modelSummary = if ($status.Models.Count -gt 0) { $status.Models -join ", " } else { "none installed" }
        Write-Output "Ollama started (process $($process.Id); version $($status.Version); models: $modelSummary)."
        return
    }
} while ([DateTime]::UtcNow -lt $deadline)

throw "Ollama did not become reachable at $ollamaHost within 30 seconds; inspect outputs/ollama-stderr.log."
