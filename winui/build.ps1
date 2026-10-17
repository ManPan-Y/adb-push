# 构建 WinUI3 版（自包含，单目录，双击 exe 即可运行）
# 用法： powershell -ExecutionPolicy Bypass -File build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$out = Join-Path $root "dist\YHGamePush"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }

Write-Host "==> 编译并发布（x64, 自包含）"
dotnet publish YHGamePush.csproj -c Release -p:Platform=x64 -r win-x64 `
    --self-contained true -o $out
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败" }

# dotnet publish 不会复制应用自身的 resources.pri，缺了它界面会在启动时崩溃
$pri = Join-Path $root "bin\x64\Release\net8.0-windows10.0.19041.0\win-x64\YHGamePush.pri"
if (Test-Path $pri) {
    Copy-Item $pri $out -Force
} else {
    Write-Warning "未找到 YHGamePush.pri，界面可能无法启动"
}

# 把 adb 一起放进去，用户直接双击即可
$ptCandidates = @(
    (Join-Path (Split-Path -Parent $root) "platform-tools"),
    (Join-Path (Split-Path -Parent (Split-Path -Parent $root)) "platform-tools")
)
foreach ($pt in $ptCandidates) {
    if (Test-Path (Join-Path $pt "adb.exe")) {
        Write-Host "==> 内置 platform-tools：$pt"
        Copy-Item $pt (Join-Path $out "platform-tools") -Recurse -Force
        break
    }
}

Write-Host ""
Write-Host "完成：$out\YHGamePush.exe"
Write-Host "整个文件夹可直接压缩分发（exe 不能单独拿走）。"
