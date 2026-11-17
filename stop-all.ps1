# 结直肠手术围术期知识库 - 停止全部服务
#
# 用法：在项目根目录执行
#     powershell -ExecutionPolicy Bypass -File .\stop-all.ps1
# 或直接双击 stop.bat

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot

function Write-Step($text) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Stop-Port($port, $label) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "    端口 $port ($label) 未运行" -ForegroundColor DarkGray
        return
    }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host "    已停止 $label (端口 $port, PID $procId)" -ForegroundColor Green
        } catch {
            Write-Host "    停止 $label 失败: $_" -ForegroundColor Yellow
        }
    }
}

Write-Step "停止全部服务"

# ---------------------------------------------------------------
#  1. 应用进程
# ---------------------------------------------------------------
Write-Host "[1/3] 停止应用进程" -ForegroundColor White
Stop-Port 8000 "后端 FastAPI"
Stop-Port 5173 "前端 Vite"

# 清理可能残留的 uvicorn 进程（reloader 会派生多个子进程，
# 仅按端口杀可能留下孤儿进程继续占用显存）
$stray = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match 'uvicorn' }
if ($stray) {
    foreach ($p in $stray) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "    已清理残留 uvicorn 进程 PID $($p.ProcessId)" -ForegroundColor Green
        } catch { }
    }
} else {
    Write-Host "    无残留 uvicorn 进程" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------
#  2. 数据库容器
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[2/3] 停止数据库容器" -ForegroundColor White
Push-Location $ProjectRoot
try {
    # 用 stop 而不是 down：保留容器与数据卷，下次启动更快
    # 数据不会丢失（PostgreSQL 数据在命名卷里）
    docker compose stop
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    docker compose stop 返回非零退出码，请检查 Docker 是否运行" -ForegroundColor Yellow
    }
} catch {
    Write-Host "    停止容器失败: $_" -ForegroundColor Yellow
} finally {
    Pop-Location
}

# ---------------------------------------------------------------
#  3. 确认
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[3/3] 最终状态" -ForegroundColor White

$anyApp = $false
foreach ($port in @(8000, 5173)) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { $anyApp = $true }
}
if ($anyApp) {
    Write-Host "    ⚠ 仍有应用端口在监听" -ForegroundColor Yellow
} else {
    Write-Host "    应用端口 8000 / 5173 已全部释放" -ForegroundColor Green
}

Push-Location $ProjectRoot
try {
    $running = docker compose ps --format "{{.Name}}|{{.Status}}" 2>$null |
               Where-Object { $_ -match 'Up|running' }
    if ($running) {
        Write-Host "    ⚠ 仍有容器在运行:" -ForegroundColor Yellow
        $running | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
    } else {
        Write-Host "    数据库容器已全部停止" -ForegroundColor Green
    }
} catch { } finally { Pop-Location }

Write-Step "已全部停止"
Write-Host "  下次启动：" -ForegroundColor White
Write-Host "    双击 start.bat  （或 stop.bat 旁边那个）" -ForegroundColor Green
Write-Host ""
Write-Host "  另有一步需你手动做：" -ForegroundColor White
Write-Host "    关闭 VS Code 窗口（若已打开）" -ForegroundColor Gray
Write-Host "    —— 它的调试适配器会随窗口关闭自动退出" -ForegroundColor DarkGray
Write-Host ""
