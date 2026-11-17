# =====================================================================
#  一键启动脚本 —— 答辩演示用
#
#  启动顺序：Docker 基础设施 → 后端 API → 模型预热 → 前端 dev server
#
#  用法：
#      .\start-all.ps1              正常启动
#      .\start-all.ps1 -SkipWarmup  跳过模型预热
#
#  编码：本文件为 UTF-8 with BOM。中文输出依赖 BOM 才能被
#        Windows PowerShell 5.1 正确解码，请勿改为无 BOM 保存。
# =====================================================================
[CmdletBinding()]
param(
    [switch]$SkipWarmup,
    [string]$PythonPath = $env:RAG_PYTHON,
    [switch]$RestartBackend,
    [switch]$CheckOnly
)

# 说明：这里刻意不使用 Stop。
# 本脚本需频繁检查原生命令的退出码，而 docker 等工具会把
# 正常状态信息（如 "Container xx Running"）写入 stderr。
# 若 ErrorActionPreference 为 Stop，这些 stderr 输出会被
# 当成致命错误并中止脚本 —— 这正是首版脚本失败的原因。
# 因此改为统一用 $LASTEXITCODE 判断，关键步骤单独 try/catch。
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = $PSScriptRoot
$BackendDir  = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$LogDir      = Join-Path $ProjectRoot "logs"

<#
  选择 Python 解释器。

  之前只按「.venv → PATH 里的 python」两档挑，结果双击 start.bat 时挑到
  系统 Python 3.13（C:\Program Files\Python313），它没装 fastapi/torch，
  于是报「缺少运行依赖」——而项目实际用的是别的虚拟环境。

  现在改为「候选列表 + 逐个验证」：
    1. -PythonPath 参数 / 环境变量 RAG_PYTHON
    2. 启动器配置 launcher.config.json 里的 python 字段
    3. 项目内常见虚拟环境目录（.venv / venv / backend\.venv / backend\venv）
    4. PATH 里的 python
  每个候选都用一次极短的 import 检查（fastapi + torch）确认依赖齐全，
  第一个通过的即采用；全都不通过时，把每个候选的失败原因打印出来。
#>
function Test-PythonUsable {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        & $Path -c "import fastapi, torch" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$PythonCandidates = [System.Collections.Generic.List[object]]::new()
if ($PythonPath) { $PythonCandidates.Add([pscustomobject]@{ Path = $PythonPath; From = '参数/环境变量 RAG_PYTHON' }) }

$configFile = Join-Path $ProjectRoot 'launcher.config.json'
if (Test-Path -LiteralPath $configFile) {
    try {
        $cfg = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.python) {
            $PythonCandidates.Add([pscustomobject]@{ Path = $cfg.python; From = 'launcher.config.json' })
        }
    } catch {
        Write-Host "    警告 launcher.config.json 解析失败，已忽略: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

foreach ($rel in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe',
                   'backend\.venv\Scripts\python.exe', 'backend\venv\Scripts\python.exe')) {
    $p = Join-Path $ProjectRoot $rel
    if (Test-Path -LiteralPath $p) {
        $PythonCandidates.Add([pscustomobject]@{ Path = $p; From = "项目内虚拟环境 $rel" })
    }
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $PythonCandidates.Add([pscustomobject]@{ Path = $pythonCommand.Source; From = 'PATH 中的 python' })
}

$Python = $null
$tried = @()
foreach ($cand in $PythonCandidates) {
    if (Test-PythonUsable -Path $cand.Path) {
        $Python = $cand.Path
        Write-Host "    采用 Python: $Python（来源：$($cand.From)）" -ForegroundColor DarkGray
        break
    }
    $tried += "$($cand.Path)（$($cand.From)）"
}

if (-not $Python) {
    $detail = if ($tried.Count) { "`n已尝试：`n  " + ($tried -join "`n  ") } else { "`n未找到任何候选解释器。" }
    throw @"
启动中止: 没有找到装了依赖的 Python 解释器。
$detail

解决办法（任选其一）：
  1. 在 launcher.config.json 里写明解释器路径，例如
         {"python": "D:\\你的虚拟环境\\Scripts\\python.exe"}
  2. 设置环境变量 RAG_PYTHON 指向该 python.exe
  3. 在项目根目录创建 .venv 并安装依赖：
         python -m venv .venv
         .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
         .venv\Scripts\python.exe -m pip install -r backend\requirements-ml.txt --extra-index-url https://download.pytorch.org/whl/cu128

  依赖检查命令（应无报错）：<python> -c "import fastapi, torch"
"@
}

function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }
function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    警告 $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "    失败 $msg" -ForegroundColor Red
    throw "启动中止: $msg"
}

<#
  运行原生命令并返回退出码。
  关键：不要用 2>&1 把 stderr 合并进成功流后再参与错误处理 ——
  docker 等工具会用 stderr 输出正常状态信息。
  这里只取退出码作为成败依据，输出原样透传以便排查。
#>
function Invoke-Native {
    param(
        [string]$Command,
        [string[]]$Arguments
    )
    $output = & $Command @Arguments 2>&1
    $code = $LASTEXITCODE
    return @{ Code = $code; Output = $output }
}

function Test-PortListening {
    param([int]$Port)
    return [bool](Get-ListenerPid -Port $Port)
}

function Get-ListenerPid {
    param([int]$Port)
    foreach ($line in (& netstat -ano -p tcp)) {
        if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            return [int]$Matches[1]
        }
    }
    return 0
}

function Wait-Http {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 60,
        [int]$IntervalSeconds = 2
    )
    $attempts = [Math]::Ceiling($TimeoutSeconds / $IntervalSeconds)
    for ($i = 1; $i -le $attempts; $i++) {
        Start-Sleep -Seconds $IntervalSeconds
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

$script:ExitCode = 0

try {
    Say "======================================================================" "Cyan"
    Say " 结直肠手术围术期知识库智能问答系统 — 启动" "Cyan"
    Say "======================================================================" "Cyan"

    # ------------------------------------------------------------ 前置检查
    Step 1 "检查前置条件"

    if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
        Fail "找不到 Python 环境: $Python`n请确认已按 README 创建虚拟环境。"
    }
    Ok "Python: $Python"
    $basePython = (& $Python -c 'import sys; print(sys._base_executable)').Trim()
    $dependencyCheck = Invoke-Native -Command $Python -Arguments @('-c', 'import fastapi, sqlalchemy, psycopg, uvicorn, torch')
    if ($dependencyCheck.Code -ne 0) { Fail 'Python 缺少运行依赖，请安装 backend/requirements.txt。' }
    Ok 'Python 依赖齐全（fastapi / sqlalchemy / psycopg / uvicorn / torch）'

    if ($CheckOnly) {
        Say "`n[仅检查] 前置条件通过，未启动任何服务。" Green
        Say "  解释器: $Python" Green
        return
    }

    if (-not (Test-Path (Join-Path $BackendDir "app\main.py"))) {
        Fail "找不到后端代码: $BackendDir"
    }
    Ok "后端代码就绪"

    if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
        Warn "前端依赖未安装，正在执行 npm install（首次较慢）…"
        Push-Location $FrontendDir
        try { & npm install --no-audit --no-fund } finally { Pop-Location }
    }
    Ok "前端依赖就绪"

    New-Item -ItemType Directory -Force $LogDir | Out-Null

    # ------------------------------------------------------------ Docker
    Step 2 "启动基础设施（PostgreSQL / Neo4j / Redis）"

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "未找到 docker 命令，请确认 Docker Desktop 已安装并启动。"
    }

    $engineOk = $false
    try {
        & docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
        $engineOk = ($LASTEXITCODE -eq 0)
    } catch { $engineOk = $false }

    if (-not $engineOk) {
        Warn "Docker 引擎未运行，尝试启动 Docker Desktop…"
        $dd = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dd) {
            Start-Process $dd -WindowStyle Hidden
            Say "    等待引擎就绪（最多 120 秒）…"
            for ($i = 1; $i -le 24; $i++) {
                Start-Sleep -Seconds 5
                try {
                    & docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
                    if ($LASTEXITCODE -eq 0) { $engineOk = $true; break }
                } catch { }
            }
        }
    }

    if (-not $engineOk) {
        Fail "Docker 引擎未就绪，请手动启动 Docker Desktop 后重试。"
    }
    Ok "Docker 引擎已就绪"

    # 容器可能已在运行，此时 docker compose up 会把状态写入 stderr，
    # 属正常输出，故只按退出码判断成败。
    $up = Invoke-Native -Command "docker" -Arguments @(
        "compose", "--project-directory", $ProjectRoot, "up", "-d"
    )
    if ($up.Code -ne 0) {
        Fail "docker compose up 失败（退出码 $($up.Code)）。请检查 Docker Desktop 状态。"
    }
    Ok "容器已启动（或已在运行）"

    Start-Sleep -Seconds 5
    $ps = Invoke-Native -Command "docker" -Arguments @(
        "compose", "--project-directory", $ProjectRoot, "ps",
        "--format", "{{.Name}}|{{.Status}}"
    )
    foreach ($line in $ps.Output) {
        if ("$line".Trim()) { Say "      $line" }
    }

    # ------------------------------------------------------------ 后端
    Step 3 "启动后端 API（端口 8000）"

    Push-Location $BackendDir
    try {
        & $Python -m scripts.sync_schema --dry-run
        if ($LASTEXITCODE -ne 0) {
            Fail '数据库结构未就绪，请先运行 python -m scripts.init_db 和 python -m scripts.sync_schema。'
        }
    } finally { Pop-Location }

    # 若端口已被占用，先停掉旧进程 —— 否则新代码不会生效，
    # 前端会因缺少新接口而 404（这是实际踩过的坑）
    if (Test-PortListening -Port 8000) {
        if (-not $RestartBackend) { Fail '8000 已占用；需要更新本项目后端时加 -RestartBackend。' }
        $oldPid = Get-ListenerPid -Port 8000
        $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        $oldName = if ($oldProc) { $oldProc.Name } else { "未知" }
        $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $oldPid" -ErrorAction Stop
        if ($identity.CommandLine -notmatch 'uvicorn\s+app\.main:app' -or
            $identity.CommandLine -notmatch '--port\s+8000' -or
            $identity.ExecutablePath -notin @($Python, $basePython)) { Fail '端口属于无法确认身份的进程，未停止。' }
        Warn "重启本项目后端 PID $oldPid ($oldName)…"
        Stop-Process -Id $oldPid -Force -ErrorAction Stop
        Start-Sleep -Seconds 3
    }

    $backendOut = Join-Path $LogDir "backend.log"
    $backendErr = Join-Path $LogDir "backend.err.log"

    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONPATH = $BackendDir
    $env:HF_HUB_OFFLINE = "1"

    Start-Process -FilePath $Python `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr `
        -WindowStyle Hidden

    Say "    等待后端就绪…"
    if (-not (Wait-Http -Url "http://127.0.0.1:8000/api/health/live" -TimeoutSeconds 90)) {
        Fail "后端未在 90 秒内就绪，请查看 $backendErr"
    }
    Ok "后端已就绪 (http://127.0.0.1:8000)"

    # 校验接口版本：确认运行的是含问答接口的新代码
    try {
        $spec = Invoke-RestMethod -Uri "http://127.0.0.1:8000/openapi.json" -TimeoutSec 15
        if ($spec.paths.PSObject.Properties.Name -contains "/api/qa/stream") {
            Ok "接口版本正确（含 /api/qa/stream）"
        } else {
            Warn "后端缺少 /api/qa/stream 接口，可能是旧版本代码"
        }
    } catch {
        Warn "无法读取接口清单，跳过版本校验"
    }

    # ------------------------------------------------------------ 模型预热
    Step 4 "预热检索模型"

    if ($SkipWarmup) {
        Warn "已跳过（首次提问将额外等待加载模型，冷启动约 30~90 秒）"
    } else {
        # 实测：模型权重需从磁盘读入显存，冷启动 30~90 秒
        #（系统文件缓存命中时约 10~15 秒）。预热的价值是把这段等待
        # 从「用户首次提问」挪到「启动阶段」。
        Say "    加载 BGE-M3 与 reranker 到显存（冷启动约 30~90 秒，请耐心等待）…"
        try {
            $warmHeaders = @{}
            if ($env:RAG_ADMIN_TOKEN) { $warmHeaders.Authorization = "Bearer $env:RAG_ADMIN_TOKEN" }
            else { throw '请设置 RAG_ADMIN_TOKEN 为管理员令牌，或使用 -SkipWarmup。' }
            $warm = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/models/load" `
                -Headers $warmHeaders -Method Post -TimeoutSec 600
            Ok "$($warm.message)"
        } catch {
            Warn "预热失败：$($_.Exception.Message)"
            Warn "不影响使用，首次提问时会自动加载"
        }
    }

    # ------------------------------------------------------------ 前端
    Step 5 "启动前端（端口 5173）"

    if (Test-PortListening -Port 5173) {
        $frontendPid = Get-ListenerPid -Port 5173
        Ok "前端已在运行 (PID $frontendPid)，跳过启动"
    } else {
        $frontendOut = Join-Path $LogDir "frontend.log"
        Start-Process -FilePath "cmd.exe" `
            -ArgumentList "/c", "npm run dev > `"$frontendOut`" 2>&1" `
            -WorkingDirectory $FrontendDir `
            -WindowStyle Hidden

        Say "    等待前端就绪…"
        if (Wait-Http -Url "http://127.0.0.1:5173/" -TimeoutSeconds 90) {
            Ok "前端已就绪 (http://127.0.0.1:5173)"
        } else {
            Warn "前端未在 90 秒内就绪，请查看 $frontendOut"
        }
    }

    # ------------------------------------------------------------ 完成
    Say "`n======================================================================" "Green"
    Say " 启动完成" "Green"
    Say "======================================================================" "Green"
    Say ""
    Say "  问答界面    http://127.0.0.1:5173/" "White"
    Say "  知识库      http://127.0.0.1:5173/knowledge"
    Say "  系统状态    http://127.0.0.1:5173/system"
    Say "  API 文档    http://127.0.0.1:8000/docs"
    Say "  Neo4j       http://127.0.0.1:7474"
    Say ""
    Say "  日志：logs\backend.log  logs\frontend.log"
    Say ""
    Say "  免责声明：本系统输出仅供教学与科研参考，不构成诊疗建议。" "Yellow"
    Say ""
}
catch {
    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Red
    Write-Host " 启动失败" -ForegroundColor Red
    Write-Host "======================================================================" -ForegroundColor Red
    Write-Host " $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host " 排查建议：" -ForegroundColor Yellow
    Write-Host "   1. 确认 Docker Desktop 正在运行"
    Write-Host "   2. 查看日志：logs\backend.log 与 logs\backend.err.log"
    Write-Host "   3. 手动验证后端：http://127.0.0.1:8000/docs"
    Write-Host ""
    $script:ExitCode = 1
}

exit $script:ExitCode
