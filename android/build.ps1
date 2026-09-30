# 打包手机侧取图 dex（android/icondump.dex）。
#
# 取图程序跑在手机的 app_process 里，不装 App、不要权限，所以不需要清单、资源、
# 对齐、签名那一整套 —— javac 编译 + d8 转 dex 两步就出产物。
#
# 前置：把 JDK 与 Android SDK 放在 .build\toolchain 下（见 README 的构建说明）
#   .build\toolchain\jdk-17.0.20.1+1
#   .build\toolchain\sdk\build-tools\35.0.1
#   .build\toolchain\sdk\platforms\android-35\android.jar
#
# 用法：powershell -ExecutionPolicy Bypass -File android\build.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$tc = Join-Path $root ".build\toolchain"
$jdk = Join-Path $tc "jdk-17.0.20.1+1"
$bt = Join-Path $tc "sdk\build-tools\35.0.1"
$androidJar = Join-Path $tc "sdk\platforms\android-35\android.jar"
$src = Join-Path $PSScriptRoot "icondump"
$work = Join-Path $root ".build\dex"
$dex = Join-Path $PSScriptRoot "icondump.dex"

$env:JAVA_HOME = $jdk

foreach ($p in @($jdk, $bt, $androidJar)) {
    if (-not (Test-Path $p)) { throw "缺少构建工具：$p" }
}

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$work\classes", "$work\dex" | Out-Null

# 1) 编译：-source/-target 8 是为了和 minSdk 24 对齐，避免用到手机上还没有的 API。
#    源码是 UTF-8（含中文注释），必须显式指定，否则 javac 按系统 GBK 编码读会直接报错
$sources = Get-ChildItem "$src\src" -Recurse -Filter "*.java" | ForEach-Object { $_.FullName }
& "$jdk\bin\javac.exe" -nowarn -encoding UTF-8 -source 8 -target 8 -bootclasspath $androidJar `
    -d "$work\classes" $sources
if ($LASTEXITCODE -ne 0) { throw "javac 编译失败" }

# 2) 转 dex
$classes = Get-ChildItem "$work\classes" -Recurse -Filter "*.class" | ForEach-Object { $_.FullName }
& "$bt\d8.bat" --release --min-api 24 --lib $androidJar --output "$work\dex" $classes
if ($LASTEXITCODE -ne 0) { throw "d8 转换失败" }

Copy-Item "$work\dex\classes.dex" $dex -Force
$size = (Get-Item $dex).Length
Write-Host "已生成 $dex（$size 字节）"
