param(
    [string]$Upstream = (Join-Path (Split-Path -Parent $PSScriptRoot) '../raddebugger'),
    [string]$Ref = '771f63dd35d3d17f0c7914c5031959282802be5f'
)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$sourceRoot = Join-Path $repoRoot '.build/raddebugger'
$patchPath = Join-Path $repoRoot 'native/upstream.patch'
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot '.git'))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $sourceRoot) | Out-Null
    & git clone --local --no-hardlinks --no-checkout $Upstream $sourceRoot
    if ($LASTEXITCODE) { throw 'Could not clone the local RAD checkout.' }
    & git -C $sourceRoot -c core.autocrlf=false checkout --detach $Ref
    if ($LASTEXITCODE) { throw 'Could not check out the pinned upstream revision.' }
}
$actualRef = & git -C $sourceRoot rev-parse HEAD
if ($actualRef -ne $Ref) { throw "Unexpected backend revision: $actualRef. Use a separate build directory for another revision." }
# PS 5.1 turns native stderr into terminating errors under Stop, even when it is
# redirected. A failed reverse check is expected for an unpatched checkout.
$savedErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & git -C $sourceRoot apply --reverse --check $patchPath 2>$null
    $patchAlreadyApplied = $LASTEXITCODE -eq 0
} finally { $ErrorActionPreference = $savedErrorActionPreference }
if (-not $patchAlreadyApplied) {
    & git -C $sourceRoot apply --check $patchPath
    if ($LASTEXITCODE) { throw 'Backend patch conflicts with local changes; files have been preserved.' }
    & git -C $sourceRoot apply $patchPath
    if ($LASTEXITCODE) { throw 'Could not apply the backend patch.' }
}
Copy-Item -LiteralPath (Join-Path $repoRoot 'native/mcp_extension.c') -Destination (Join-Path $sourceRoot 'src/raddbg/mcp_extension.c') -Force
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vsRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsRoot) { throw 'MSVC x64 Build Tools and Windows SDK are required.' }
$vcvars = Join-Path $vsRoot 'VC/Auxiliary/Build/vcvars64.bat'
$buildLine = '"{0}" && build.bat raddbg_non_graphical release' -f $vcvars
Push-Location $sourceRoot
try {
    & $env:ComSpec /d /c $buildLine
    if ($LASTEXITCODE) { throw 'RAD backend build failed.' }
} finally { Pop-Location }
$exePath = Join-Path $sourceRoot 'build/raddbg_non_graphical.exe'
$manifest = [ordered]@{
    upstream = 'https://github.com/EpicGames/raddebugger'
    revision = $Ref
    bridge_version = 1
    patch_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $patchPath).Hash.ToLowerInvariant()
    extension_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repoRoot 'native/mcp_extension.c')).Hash.ToLowerInvariant()
    binary_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash.ToLowerInvariant()
    built_at_utc = [DateTime]::UtcNow.ToString('o')
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $repoRoot '.build/backend-manifest.json') -Encoding utf8
Write-Host "Backend built: $exePath"
