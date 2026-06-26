@echo off
chcp 65001 >nul 2>&1
title Kanban Harness 启动器

echo.
echo  ╔══════════════════════════════════════╗
echo  ║     Kanban Harness 一键启动器       ║
echo  ╚══════════════════════════════════════╝
echo.

:: 检查 Docker 是否可用
docker --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [检测] 未找到 Docker，需要先安装。
    echo.
    echo  请选择安装方式：
    echo.
    echo    [1] 通过 WSL 安装（推荐，国内可直接使用）
    echo    [2] 安装 Docker Desktop（需要能访问 docker.com）
    echo.
    set /p INSTALL_CHOICE="  请输入 1 或 2: "
    if "%INSTALL_CHOICE%"=="2" goto :install_desktop
    goto :install_wsl
)

goto :docker_ready

:install_wsl
echo.
echo  ═══════════════════════════════════════
echo   通过 WSL 安装 Docker
echo  ═══════════════════════════════════════
echo.
echo  [步骤1] 正在开启 WSL...
echo  （如果弹出权限确认窗口，请点「是」）
echo.
powershell -Command "Start-Process wsl -ArgumentList '--install --no-distribution' -Verb RunAs -Wait" 2>nul
echo.
echo  [步骤2] 正在安装 Ubuntu...
wsl --install -d Ubuntu --no-launch >nul 2>&1
echo.
echo  [提示] WSL 安装完成后需要重启电脑。
echo.
echo  重启后请重新双击 start.bat，届时脚本会自动继续安装 Docker。
echo.
pause
exit /b 0

:install_desktop
echo.
echo  ═══════════════════════════════════════
echo   安装 Docker Desktop
echo  ═══════════════════════════════════════
echo.
echo  正在打开 Docker Desktop 下载页面...
start https://www.docker.com/products/docker-desktop/
echo.
echo  请下载并安装 Docker Desktop，安装完成后重新运行本脚本。
echo.
pause
exit /b 0

:docker_ready

:: 如果是 WSL 里的 docker，检查 WSL docker 是否可用
wsl --list >nul 2>&1
if %errorlevel% equ 0 (
    docker info >nul 2>&1
    if %errorlevel% neq 0 (
        echo  [检测] 正在通过 WSL 启动 Docker...
        wsl -u root -- service docker start >nul 2>&1
        timeout /t 3 /nobreak >nul
    )
)

:: 检查 Docker 是否在运行
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo  [等待] Docker 还没启动，正在等待...
    echo  （如果等太久，请手动打开 Docker Desktop 或检查 WSL）
    echo.
    :wait_docker
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if %errorlevel% neq 0 goto wait_docker
)

echo  [OK] Docker 已就绪
echo.

:: 检查是否已有配置
if exist ".env" (
    echo  [OK] 检测到已有配置文件 .env
    goto :start_container
)

:: 首次运行，收集配置
echo  ═══════════════════════════════════════
echo   首次运行，需要配置以下信息
echo  ═══════════════════════════════════════
echo.
echo  支持 OpenAI / Anthropic / DeepSeek / 中转站等任何兼容服务商
echo.

:input_key
set /p API_KEY="  [1/3] API 密钥: "
if "%API_KEY%"=="" (
    echo        不能为空，请重新输入
    goto :input_key
)

:input_base
echo.
set /p API_BASE="  [2/3] API 地址 (如 https://api.openai.com/v1): "
if "%API_BASE%"=="" (
    echo        不能为空，请重新输入
    goto :input_base
)

:input_model
echo.
set /p MODEL="  [3/3] 模型名称 (如 claude-sonnet-4-6, gpt-4o, deepseek-chat): "
if "%MODEL%"=="" (
    echo        不能为空，请重新输入
    goto :input_model
)

:: 写入 .env
(
echo API_KEY=%API_KEY%
echo API_BASE_URL=%API_BASE%
echo CHAT_MODEL=%MODEL%
echo ANTHROPIC_AUTH_TOKEN=%API_KEY%
echo ANTHROPIC_BASE_URL=%API_BASE%
) > .env

echo.
echo  [OK] 配置已保存到 .env
echo.

:start_container

:: 检查镜像是否已加载
docker image inspect kh-web >nul 2>&1
if %errorlevel% neq 0 (
    echo  [加载] 正在导入 Docker 镜像，请稍候...
    if exist "kh-web.tar.gz" (
        docker load < kh-web.tar.gz
        if %errorlevel% neq 0 (
            echo  [错误] 镜像加载失败！
            pause
            exit /b 1
        )
        echo  [OK] 镜像加载完成
    ) else (
        echo  [错误] 找不到 kh-web.tar.gz！
        echo  请确保本脚本和 kh-web.tar.gz 在同一个文件夹。
        pause
        exit /b 1
    )
) else (
    echo  [OK] 镜像已就绪
)

echo.

:: 检查容器是否已存在
docker ps -a --format "{{.Names}}" | findstr /x "kanban-harness" >nul 2>&1
if %errorlevel% equ 0 (
    echo  [启动] 正在启动 Kanban Harness...
    docker start kanban-harness >nul 2>&1
) else (
    echo  [启动] 正在创建并启动 Kanban Harness...
    if not exist "data" mkdir data
    docker run -d --name kanban-harness --network host --env-file .env -v "%cd%\data:/app/data" kh-web >nul 2>&1
)

if %errorlevel% neq 0 (
    echo  [错误] 启动失败！尝试重建容器...
    docker rm -f kanban-harness >nul 2>&1
    if not exist "data" mkdir data
    docker run -d --name kanban-harness --network host --env-file .env -v "%cd%\data:/app/data" kh-web >nul 2>&1
)

:: 等待服务就绪
echo  [等待] 服务启动中...
timeout /t 3 /nobreak >nul

echo.
echo  ╔══════════════════════════════════════╗
echo  ║  ✓ 启动成功！                       ║
echo  ║                                      ║
echo  ║  浏览器访问: http://localhost:8765   ║
echo  ║                                      ║
echo  ║  关闭本窗口不影响运行               ║
echo  ║  停止服务: 双击 stop.bat             ║
echo  ╚══════════════════════════════════════╝
echo.

:: 自动打开浏览器
start http://localhost:8765

pause
