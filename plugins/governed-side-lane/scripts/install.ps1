param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("install", "check", "uninstall")]
    [string]$Mode,
    [Parameter(Position=1)]
    [ValidateSet("codex", "claude", "both")]
    [string]$HostName = "both"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$UserHome = if ($env:SIDE_LANE_HOME) { $env:SIDE_LANE_HOME } else { $HOME }
$LocalAppData = if ($env:SIDE_LANE_LOCALAPPDATA) { $env:SIDE_LANE_LOCALAPPDATA } else { $env:LOCALAPPDATA }
$InstallRoot = Join-Path $LocalAppData "governed-side-lane"
$ManifestPath = Join-Path $InstallRoot "install-manifest.json"
$RunnerDestination = Join-Path $LocalAppData "Microsoft\WindowsApps\side-lane.cmd"
$CodexDestination = Join-Path $UserHome ".codex\skills\side-lane"
$ClaudeDestination = Join-Path $UserHome ".claude\skills\side-lane"
$SideLaneSource = Join-Path $RepoRoot "skills\side-lane"
$ContextSource = Join-Path $RepoRoot "config\agent-context.md"
$ContextHelper = Join-Path $RepoRoot "scripts\context_entrypoint.py"
$PromptSource = Join-Path $RepoRoot "skills\prompt-it-side-lane-routing"
$CodexPromptDestination = Join-Path $UserHome ".codex\skills\prompt-it-side-lane-routing"
$ClaudePromptDestination = Join-Path $UserHome ".claude\skills\prompt-it-side-lane-routing"

function Managed-Items {
    $items = @(@{ Source = (Join-Path $RepoRoot "bin\side-lane"); Destination = $RunnerDestination; Kind = "runner" })
    if ($HostName -eq "codex" -or $HostName -eq "both") {
        $items += @{ Source = $SideLaneSource; Destination = $CodexDestination; Kind = "codex" }
        $items += @{ Source = $PromptSource; Destination = $CodexPromptDestination; Kind = "prompt-it-codex" }
    }
    if ($HostName -eq "claude" -or $HostName -eq "both") {
        $items += @{ Source = $SideLaneSource; Destination = $ClaudeDestination; Kind = "claude" }
        $items += @{ Source = $PromptSource; Destination = $ClaudePromptDestination; Kind = "prompt-it-claude" }
    }
    return $items
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $ManifestPath)) { return $null }
    return Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
}

function Get-ManagedHash([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $root = (Resolve-Path -LiteralPath $Path).Path.TrimEnd("\", "/")
    $lines = foreach ($file in Get-ChildItem -LiteralPath $Path -File -Recurse | Sort-Object FullName) {
        $relative = $file.FullName.Substring($root.Length).TrimStart("\", "/").Replace("\", "/")
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$relative`:$hash"
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Manifest-Destinations($Value) {
    if (-not $Value) { return @() }
    if ($Value.schema_version -eq 2) { return @($Value.items | ForEach-Object { $_.destination }) }
    return @($Value.destinations)
}

function Manifest-Item($Value, [string]$Destination) {
    if (-not $Value -or $Value.schema_version -ne 2) { return $null }
    return @($Value.items | Where-Object { $_.destination -eq $Destination }) | Select-Object -First 1
}

$manifest = Read-Manifest
$items = Managed-Items
if ($Mode -eq "install") {
    $ownedBefore = Manifest-Destinations $manifest
    foreach ($item in $items) {
        if ((Test-Path -LiteralPath $item.Destination) -and
            -not ($ownedBefore -contains $item.Destination)) {
            throw "Refusing unrelated destination: $($item.Destination)"
        }
    }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    $ownedItems = @()
    if ($manifest -and $manifest.schema_version -eq 2) { $ownedItems += @($manifest.items) }
    foreach ($item in $items) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $item.Destination) | Out-Null
        if ($item.Kind -eq "runner") {
            $lines = @("@echo off", ('python "' + $RepoRoot + '\bin\side-lane" %*'))
            Set-Content -LiteralPath $item.Destination -Value $lines -Encoding Ascii
        } else {
            if (Test-Path -LiteralPath $item.Destination) {
                Remove-Item -LiteralPath $item.Destination -Recurse -Force
            }
            Copy-Item -LiteralPath $item.Source -Destination $item.Destination -Recurse -Force
        }
        $ownedItems = @($ownedItems | Where-Object { $_.destination -ne $item.Destination })
        $ownedItems += @{ destination = $item.Destination; hash = (Get-ManagedHash $item.Destination) }
    }
    @{ schema_version = 2; source = $RepoRoot; items = $ownedItems } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
    & python $ContextHelper install $HostName --source $ContextSource --home $UserHome --codex-home (Join-Path $UserHome ".codex")
    if ($LASTEXITCODE -ne 0) { throw "Shared context installation failed" }
    Write-Output "install ($HostName) complete"
    exit 0
}
if (-not $manifest) {
    if ($Mode -eq "check") { throw "No managed installation manifest found" }
    Write-Output "Nothing managed to uninstall"
    exit 0
}
if ($Mode -eq "check") {
    foreach ($item in $items) {
        $record = Manifest-Item $manifest $item.Destination
        if (-not $record -or -not (Test-Path -LiteralPath $item.Destination)) {
            throw "Missing or unmanaged destination: $($item.Destination)"
        }
        if ((Get-ManagedHash $item.Destination) -ne $record.hash) {
            throw "Managed destination was modified: $($item.Destination)"
        }
    }
    & python $ContextHelper check $HostName --source $ContextSource --home $UserHome --codex-home (Join-Path $UserHome ".codex")
    if ($LASTEXITCODE -ne 0) { throw "Shared context check failed" }
    Write-Output "check ($HostName) complete"
    exit 0
}
$manifestDestinations = Manifest-Destinations $manifest
$KeepRunner = (($HostName -eq "codex") -and
    ($manifestDestinations -contains $ClaudeDestination) -and
    (Test-Path -LiteralPath $ClaudeDestination)) -or
    (($HostName -eq "claude") -and
    ($manifestDestinations -contains $CodexDestination) -and
    (Test-Path -LiteralPath $CodexDestination))
$removed = @()
foreach ($item in $items) {
    if (($item.Kind -eq "runner") -and $KeepRunner) { continue }
    if ($manifestDestinations -contains $item.Destination) {
        $record = Manifest-Item $manifest $item.Destination
        if (-not $record) { throw "Refusing to remove legacy manifest item without a content hash: $($item.Destination)" }
        if ((Test-Path -LiteralPath $item.Destination) -and
            (Get-ManagedHash $item.Destination) -ne $record.hash) {
            throw "Refusing to remove modified managed destination: $($item.Destination)"
        }
        if (Test-Path -LiteralPath $item.Destination) {
            Remove-Item -LiteralPath $item.Destination -Recurse -Force
        }
        $removed += $item.Destination
    }
}
& python $ContextHelper uninstall $HostName --source $ContextSource --home $UserHome --codex-home (Join-Path $UserHome ".codex")
if ($LASTEXITCODE -ne 0) { throw "Shared context uninstall failed" }
$remaining = @($manifest.items | Where-Object { $removed -notcontains $_.destination })
if ($remaining.Count -eq 0) {
    Remove-Item -LiteralPath $ManifestPath -Force
} else {
    @{ schema_version = 2; source = $manifest.source; items = $remaining } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
}
Write-Output "uninstall ($HostName) complete"
