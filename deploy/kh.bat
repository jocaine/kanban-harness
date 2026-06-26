@echo off
chcp 65001 >nul 2>&1

set "KH_DIR=%USERPROFILE%\kanban-harness"
set "BASE_URL=https://aipitabox.site/docker-images"

if "%~1"=="" goto :help
if "%~1"=="install" goto :do_install
if "%~1"=="start" goto :do_start
if "%~1"=="stop" goto :do_stop
if "%~1"=="update" goto :do_update
if "%~1"=="status" goto :do_status
if "%~1"=="logs" goto :do_logs
if "%~1"=="config" goto :do_config
if "%~1"=="uninstall" goto :do_uninstall
goto :help

:help
echo.
echo  Kanban Harness CLI
echo.
echo  用法: kh ^<命令^>
echo.
echo  命令:
echo    install    首次安装（下载镜像+脚本，配置并启动）
echo    start      启动服务
echo    stop       停止服务
echo    update     更新到最新版本
echo    status     查看运行状态
echo    logs       查看日志
echo    config     重新配置 API 密钥/地址/模型
echo    uninstall  卸载（删除容器和镜像，保留数据）
echo.
exit /b 0

:ensure_docker
docker --version >nul 2>&1
if %errorlevel% equ 0 (
    docker info >nul 2>&1
    if %errorlevel% neq 0 (
        :: 尝试通过 WSL 启动
        wsl --list >nul 2>&1
        if %errorlevel% equ 0 (
            echo  [检测] 正在通过 WSL 启动 Docker...
            wsl -u root -- service docker start >nul 2>&1
            timeout /t 3 /nobreak >nul
        )
        docker info >nul 2>&1
        if %errorlevel% neq 0 (
            echo  [等待] Docker 未就绪，等待中...
            :wait_loop
            timeout /t 3 /nobreak >nul
            docker info >nul 2>&1
            if %errorlevel% neq 0 goto wait_loop
        )
    )
    exit /b 0
)
:: Docker 不存在，引导安装
echo.
echo  [检测] 未找到 Docker，需要先安装。
echo.
echo  请选择安装方式：
echo.
echo    [1] 通过 WSL 安装（推荐，国内可直接使用）
echo    [2] 安装 Docker Desktop（需要能访问 docker.com）
echo.
set /p INSTALL_CHOICE="  请输入 1 或 2: "
if "%INSTALL_CHOICE%"=="2" (
    echo.
    echo  正在打开 Docker Desktop 下载页面...
    start https://www.docker.com/products/docker-desktop/
    echo  请安装完成后重新运行: kh install
    exit /b 1
)
echo.
echo  [步骤1] 正在开启 WSL...
powershell -Command "Start-Process wsl -ArgumentList '--install --no-distribution' -Verb RunAs -Wait" 2>nul
echo  [步骤2] 正在安装 Ubuntu...
wsl --install -d Ubuntu --no-launch >nul 2>&1
echo.
echo  [提示] 需要重启电脑，重启后运行: kh install
exit /b 1

:do_install
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Kanban Harness 安装               ║
echo  ╚══════════════════════════════════════╝
echo.
call :ensure_docker
if %errorlevel% neq 0 (
    pause
    exit /b 1
)
echo  [OK] Docker 已就绪
echo.

if not exist "%KH_DIR%" mkdir "%KH_DIR%"
cd /d "%KH_DIR%"

:: 下载脚本
echo  [下载] 获取启动脚本...
powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/start.bat' -OutFile 'start.bat'" 2>nul
powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/stop.bat' -OutFile 'stop.bat'" 2>nul

:: 下载镜像
docker image inspect kh-web >nul 2>&1
if %errorlevel% neq 0 (
    echo  [下载] 获取 Docker 镜像（约 429MB，请耐心等待）...
    powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/kh-web.tar.gz' -OutFile 'kh-web.tar.gz'"
    echo  [加载] 导入镜像...
    docker load < kh-web.tar.gz
    del /f kh-web.tar.gz >nul 2>&1
    echo  [OK] 镜像就绪
) else (
    echo  [OK] 镜像已存在，跳过下载
)

:: 配置
if not exist ".env" call :do_config

echo.
echo  [OK] 安装完成！
echo  [OK] 工作目录: %KH_DIR%
echo.
echo  运行 kh start 启动服务
echo.
pause
exit /b 0

:do_config
echo.
echo  ═══════════════════════════════════════
echo   配置 API 信息
echo  ═══════════════════════════════════════
echo.
echo  支持 OpenAI / Anthropic / DeepSeek / 中转站等
echo.

:cfg_key
set /p CFG_KEY="  [1/3] API 密钥: "
if "%CFG_KEY%"=="" (
    echo        不能为空，请重新输入
    goto :cfg_key
)

:cfg_base
echo.
set /p CFG_BASE="  [2/3] API 地址 (如 https://api.openai.com/v1): "
if "%CFG_BASE%"=="" (
    echo        不能为空，请重新输入
    goto :cfg_base
)

:cfg_model
echo.
set /p CFG_MODEL="  [3/3] 模型名称 (如 claude-sonnet-4-6, gpt-4o): "
if "%CFG_MODEL%"=="" (
    echo        不能为空，请重新输入
    goto :cfg_model
)

(
echo API_KEY=%CFG_KEY%
echo API_BASE_URL=%CFG_BASE%
echo CHAT_MODEL=%CFG_MODEL%
echo ANTHROPIC_AUTH_TOKEN=%CFG_KEY%
echo ANTHROPIC_BASE_URL=%CFG_BASE%
) > "%KH_DIR%\.env"

echo.
echo  [OK] 配置已保存
exit /b 0

:do_start
call :ensure_docker
if %errorlevel% neq 0 (
    pause
    exit /b 1
)
cd /d "%KH_DIR%"

if not exist ".env" (
    echo  [错误] 未找到配置，请先运行: kh install
    exit /b 1
)

docker ps --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    echo  [OK] 服务已在运行中
    echo  浏览器访问: http://localhost:8000
    exit /b 0
)

docker ps -a --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    echo  [启动] 正在启动...
    docker start kanban-harness >nul 2>&1
) else (
    echo  [启动] 正在创建并启动...
    if not exist "data" mkdir data
    docker run -d --name kanban-harness --network host --env-file .env -v "%KH_DIR%\data:/app/data" kh-web >nul 2>&1
)

timeout /t 2 /nobreak >nul
echo.
echo  ✓ 启动成功！浏览器访问: http://localhost:8000
start http://localhost:8000
exit /b 0

:do_stop
docker ps --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    docker stop kanban-harness >nul 2>&1
    echo  [OK] 已停止
) else (
    echo  [OK] 服务未在运行
)
exit /b 0

:do_update
echo.
echo  [更新] 正在检查最新版本...
echo.
call :ensure_docker
if %errorlevel% neq 0 (
    pause
    exit /b 1
)
cd /d "%KH_DIR%"

:: 更新脚本
powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/start.bat' -OutFile 'start.bat'" 2>nul
powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/stop.bat' -OutFile 'stop.bat'" 2>nul
echo  [OK] 脚本已更新

:: 更新镜像
echo  [下载] 获取最新镜像...
powershell -Command "Invoke-WebRequest -Uri '%BASE_URL%/kh-web.tar.gz' -OutFile 'kh-web.tar.gz'"
docker load < kh-web.tar.gz
del /f kh-web.tar.gz >nul 2>&1
echo  [OK] 镜像已更新

:: 重建容器
docker ps -a --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    echo  [重启] 用新镜像重建容器...
    docker rm -f kanban-harness >nul 2>&1
    call :do_start
) else (
    echo.
    echo  [OK] 更新完成！运行 kh start 启动
)
exit /b 0

:do_status
docker ps --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    echo  状态: 运行中
    echo  地址: http://localhost:8000
    docker ps --filter name=kanban-harness --format "  容器: {{.ID}}  运行时间: {{.RunningFor}}"
) else (
    docker ps -a --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
    if %errorlevel% equ 0 (
        echo  状态: 已停止
        echo  运行 kh start 启动
    ) else (
        echo  状态: 未安装
        echo  运行 kh install 安装
    )
)
exit /b 0

:do_logs
docker logs -f --tail 50 kanban-harness
exit /b 0

:do_uninstall
echo.
set /p CONFIRM="  确认卸载？容器和镜像会被删除，data/ 目录保留。[y/N] "
if /i not "%CONFIRM%"=="y" (
    echo  已取消
    exit /b 0
)
docker rm -f kanban-harness >nul 2>&1
docker rmi kh-web >nul 2>&1
echo  [OK] 已卸载。数据保留在 %KH_DIR%\data\
exit /b 0
