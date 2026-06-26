@echo off
chcp 65001 >nul 2>&1

:: Kanban Harness CLI installer for Windows
:: Usage: Run this script to install the 'kh' command

echo.
echo  Installing Kanban Harness CLI...
echo.

set "KH_DIR=%USERPROFILE%\kanban-harness"
set "KH_BIN=%USERPROFILE%\kanban-harness\kh.bat"

:: Create directory
if not exist "%KH_DIR%" mkdir "%KH_DIR%"

:: Download kh.bat
powershell -Command "Invoke-WebRequest -Uri 'https://aipitabox.site/docker-images/kh.bat' -OutFile '%KH_BIN%'" 2>nul
if %errorlevel% neq 0 (
    echo  [错误] 下载失败，请检查网络连接
    pause
    exit /b 1
)

:: Add to PATH if not already there
echo %PATH% | findstr /i "%KH_DIR%" >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ';%KH_DIR%', 'User')"
    set "PATH=%PATH%;%KH_DIR%"
)

echo.
echo  [OK] kh 命令已安装
echo.
echo  用法:
echo    kh install   — 首次安装
echo    kh start     — 启动服务
echo    kh stop      — 停止服务
echo    kh update    — 更新版本
echo    kh status    — 查看状态
echo.
echo  请关闭此窗口，重新打开一个终端，然后运行: kh install
echo.
pause
