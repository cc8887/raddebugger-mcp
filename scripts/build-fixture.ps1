$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vsRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsRoot) { throw 'MSVC x64 Build Tools are required.' }
$vcvars = Join-Path $vsRoot 'VC/Auxiliary/Build/vcvars64.bat'
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot '.build') | Out-Null
Push-Location $repoRoot
try {
    & $env:ComSpec /d /c ('"{0}" && cl /nologo /Zi /Od /W4 tests\fixtures\debuggee.c /Fe:.build\debuggee.exe /Fd:.build\fixture-compile.pdb /Fo:.build\debuggee.obj /link /DEBUG /INCREMENTAL:NO /PDB:.build\debuggee.pdb' -f $vcvars)
    if ($LASTEXITCODE) { throw 'Could not build the native test fixture.' }
} finally { Pop-Location }
