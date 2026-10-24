$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vsRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsRoot) { throw 'MSVC x64 Build Tools are required.' }
$vcvars = Join-Path $vsRoot 'VC/Auxiliary/Build/vcvars64.bat'
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot '.build') | Out-Null
Push-Location $repoRoot
try {
    & $env:ComSpec /d /c ('"{0}" && cl /nologo /std:c++17 /utf-8 /DUNICODE /D_UNICODE /EHsc /GR /Zi /Od /W4 tests\fixtures\ue_types.cpp /Fe:.build\ue_types.exe /Fd:.build\ue-types-compile.pdb /Fo:.build\ue_types.obj /link /DEBUG /INCREMENTAL:NO /PDB:.build\ue_types.pdb' -f $vcvars)
    if ($LASTEXITCODE) { throw 'Could not build the independent UE4 layout fixture.' }
} finally { Pop-Location }
