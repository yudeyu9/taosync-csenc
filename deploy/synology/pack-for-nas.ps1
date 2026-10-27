# 在仓库根目录执行：.\deploy\synology\pack-for-nas.ps1
# 生成 deploy/synology/taosync-src.zip 供上传群晖

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$OutZip = Join-Path $PSScriptRoot "taosync-src.zip"
$Stage = Join-Path $env:TEMP ("taosync-pack-" + [guid]::NewGuid().ToString("N"))

if (-not (Test-Path (Join-Path $Root "web\dist\index.html"))) {
    throw "缺少 web/dist，请先: npm run build --prefix web"
}

New-Item -ItemType Directory -Path $Stage | Out-Null
$Include = @(
    "main.py",
    "requirements.txt",
    "taoSync.spec",
    "common",
    "controller",
    "mapper",
    "service",
    "locales",
    "web\dist",
    "deploy\synology\Dockerfile",
    "deploy\synology\docker-compose.yml",
    "deploy\synology\build-on-nas.sh",
    "deploy\synology\README.md"
)

foreach ($rel in $Include) {
    $src = Join-Path $Root $rel
    if (-not (Test-Path $src)) { throw "缺少: $rel" }
    $dst = Join-Path $Stage $rel
    $dstParent = Split-Path $dst -Parent
    if (-not (Test-Path $dstParent)) {
        New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
    }
    Copy-Item -Path $src -Destination $dst -Recurse -Force
}

if (Test-Path $OutZip) { Remove-Item $OutZip -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $OutZip -CompressionLevel Optimal
Remove-Item $Stage -Recurse -Force

$sizeMB = [math]::Round((Get-Item $OutZip).Length / 1MB, 2)
Write-Host "已生成: $OutZip ($sizeMB MB)"
Write-Host "群晖布局: /volume1/docker/taosync/{data, docker-compose.yml, app/}"
Write-Host "  - data/ 与 docker-compose.yml 固定不动"
Write-Host "  - 本 zip 解压到 app/ 后执行: sh deploy/synology/build-on-nas.sh"
