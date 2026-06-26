@echo off
chcp 65001 >nul 2>&1
title 停止 Kanban Harness

echo.
echo  正在停止 Kanban Harness...
docker stop kanban-harness >nul 2>&1
echo.
echo  已停止。下次双击「启动.bat」即可重新运行。
echo.
pause
