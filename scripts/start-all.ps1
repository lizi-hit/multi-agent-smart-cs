param(
    [switch]$RunHarness = $true,
    [string]$ProjectRoot = "",
    [string]$ApiKey = "",
    [string]$BaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    [string]$ModelName = "qwen-plus",
    [string]$EmbeddingModel = "text-embedding-v3",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Resolve-ProjectRoot {
    if ($ProjectRoot -and (Test-Path $ProjectRoot)) {
        return (Resolve-Path $ProjectRoot).Path
    }
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $root = Resolve-Path (Join-Path $scriptRoot "..")
    return $root.Path
}

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py" }
    throw "未检测到 python/py，请先安装 Python 并加入 PATH。"
}

$root = Resolve-ProjectRoot
$pythonImpl = Join-Path $root "python-impl"
$harnessScript = Join-Path $root "harness\run_harness.py"
$apiMain = Join-Path $pythonImpl "api\main.py"
$envFile = Join-Path $pythonImpl ".env"

if (!(Test-Path $pythonImpl)) { throw "未找到 python-impl 目录：$pythonImpl" }
if (!(Test-Path $apiMain)) { throw "未找到 API 入口：$apiMain" }

$py = Get-PythonCommand

Write-Step "检查 Docker 与 Redis"
if (!(Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未检测到 docker 命令，请先安装并启动 Docker Desktop。"
}

try {
    $null = docker ps | Out-Null
} catch {
    throw "Docker 似乎未启动，请先启动 Docker Desktop。"
}

$redisName = "smartcs-redis"
$exists = (docker ps -a --format "{{.Names}}" | Select-String "^$redisName$") -ne $null
if ($exists) {
    $running = (docker ps --format "{{.Names}}" | Select-String "^$redisName$") -ne $null
    if (-not $running) {
        docker start $redisName | Out-Null
    }
} else {
    docker run -d --name $redisName -p 6379:6379 redis:7-alpine | Out-Null
}

Write-Step "准备环境变量"
if (!(Test-Path $envFile)) {
    Copy-Item (Join-Path $pythonImpl ".env.example") $envFile
    Write-Host "已创建 .env：$envFile"
}

$content = Get-Content $envFile -Raw -Encoding UTF8
function Upsert-Env([string]$key, [string]$val) {
    if ($content -match "(?m)^$([regex]::Escape($key))=") {
        $script:content = [regex]::Replace($script:content, "(?m)^$([regex]::Escape($key))=.*$", "$key=$val")
    } else {
        $script:content += "`r`n$key=$val"
    }
}

if ($ApiKey) { Upsert-Env "OPENAI_API_KEY" $ApiKey }
Upsert-Env "OPENAI_BASE_URL" $BaseUrl
Upsert-Env "MODEL_NAME" $ModelName
Upsert-Env "EMBEDDING_MODEL" $EmbeddingModel
Upsert-Env "REDIS_URL" "redis://127.0.0.1:6379/0"
Upsert-Env "PORT" "$Port"

Set-Content -Path $envFile -Value $content -Encoding UTF8

Write-Step "启动 Python API（新窗口）"
$apiCmd = "cd /d `"$pythonImpl`" && $py -m api.main"
Start-Process -FilePath "cmd.exe" -ArgumentList "/k $apiCmd" | Out-Null

Write-Step "等待健康检查"
$healthUrl = "http://127.0.0.1:$Port/health"
$ready = $false
for ($i = 1; $i -le 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw "API 启动超时，请检查新打开终端的报错信息。" }

Write-Host "API 已就绪：$healthUrl" -ForegroundColor Green

if ($RunHarness) {
    if (!(Test-Path $harnessScript)) {
        Write-Host "未找到 harness 脚本，跳过评测。" -ForegroundColor Yellow
    } else {
        Write-Step "运行 Harness 评测"
        & $py $harnessScript --base-url "http://127.0.0.1:$Port" --config (Join-Path $root "harness\config.yaml")
    }
}

Write-Step "完成"
Write-Host "Redis: docker container '$redisName'" -ForegroundColor Green
Write-Host "API: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "Swagger: http://127.0.0.1:$Port/docs" -ForegroundColor Green
