#!/usr/bin/env pwsh
<#
.SYNOPSIS
    把 qq-mail 技能安装到 DSH 技能根，并跑一次离线自检。

.DESCRIPTION
    复制本仓库（除 .git）到 <SkillsRoot>/qq-mail，然后用 Python 执行 selftest。
    DSH 只扫描技能根的直接子目录，所以目标目录名必须是 qq-mail。

.PARAMETER SkillsRoot
    技能根目录。默认 $env:DSH_AGENTS_HOME/skills，否则 ~/.agents/skills。

.PARAMETER Python
    Python 3.9+ 解释器路径。默认自动查找 DSH 运行时，其次 PATH 里的 python。

.EXAMPLE
    pwsh ./install.ps1
    pwsh ./install.ps1 -SkillsRoot "$HOME/.dsh/skills"
#>
[CmdletBinding()]
param(
    [string]$SkillsRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$SkillName = "qq-mail"
$Source = $PSScriptRoot

if (-not $SkillsRoot) {
    $SkillsRoot = if ($env:DSH_AGENTS_HOME) {
        Join-Path $env:DSH_AGENTS_HOME "skills"
    } else {
        Join-Path $HOME ".agents/skills"
    }
}
$Target = Join-Path $SkillsRoot $SkillName

Write-Host "源目录 : $Source"
Write-Host "技能根 : $SkillsRoot"
Write-Host "目标   : $Target"

$sourcePath = (Resolve-Path -LiteralPath $Source).Path
$targetPath = $null
if (Test-Path -LiteralPath $Target) {
    $targetPath = (Resolve-Path -LiteralPath $Target).Path
}

if ($sourcePath -ne $targetPath) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        if ($item.Name -eq ".git") { continue }
        Copy-Item -LiteralPath $item.FullName -Destination $Target -Recurse -Force
    }
    Write-Host "已复制技能文件"
} else {
    Write-Host "当前目录就是技能目录，跳过复制"
}

function Test-Python39 {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if ($Candidate -like "*WindowsApps*") { return $false }   # 应用商店占位符，不是解释器
    $resolved = $Candidate
    if ($Candidate -notmatch '[\\/]') {
        $command = Get-Command $Candidate -ErrorAction SilentlyContinue
        if (-not $command) { return $false }
        $resolved = $command.Source
    }
    if (-not (Test-Path -LiteralPath $resolved)) { return $false }
    & $resolved -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

$candidates = @()
if ($Python) { $candidates += $Python }
if ($env:DSH_PYTHON) { $candidates += $env:DSH_PYTHON }
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME ".dsh" }
$runtimes = Join-Path $dshHome "dsh-runtimes"
if (Test-Path -LiteralPath $runtimes) {
    $candidates += Get-ChildItem -Path $runtimes -Filter "python.exe" -Recurse -Depth 4 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
}
$candidates += @("python", "python3")

$Python = $null
foreach ($candidate in $candidates) {
    if (Test-Python39 $candidate) { $Python = $candidate; break }
}
if (-not $Python) {
    throw "找不到 Python 3.9+。请用 -Python 指定路径，或把 python 加入 PATH。"
}
if ($Python -notmatch '[\\/]') { $Python = (Get-Command $Python).Source }
Write-Host "Python : $Python"

$script = Join-Path $Target "scripts/mail.py"
& $Python $script selftest
if ($LASTEXITCODE -ne 0) {
    throw "selftest 未通过，请检查上面的 FAIL 项。"
}

Write-Host ""
Write-Host "安装完成。下一步配置 QQ 邮箱授权码（输入不回显）："
Write-Host "  & `"$Python`" `"$script`" accounts add --email you@qq.com"
Write-Host "然后验证："
Write-Host "  & `"$Python`" `"$script`" accounts test"
