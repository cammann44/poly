@echo off
REM Polymarket Copy-Trading Bot - Windows Launcher
REM Right-click this file > Create Shortcut > Move to Desktop

title Polymarket Copy-Trading Bot

echo.
echo  ========================================
echo   Polymarket Copy-Trading Bot
echo  ========================================
echo.
echo  1. Paper Trading (safe, no real trades)
echo  2. Live Trading (real money!)
echo  3. Dashboard Only
echo  4. Health Check
echo  5. Stop All Services
echo  6. View Logs
echo.

set /p choice="Select option (1-6): "

if "%choice%"=="1" (
    echo Starting Paper Trading...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && ./scripts/start.sh paper"
) else if "%choice%"=="2" (
    echo Starting Live Trading...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && ./scripts/start.sh live"
) else if "%choice%"=="3" (
    echo Starting Dashboard...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && ./scripts/start.sh monitor"
    echo.
    echo Opening browser...
    timeout /t 3 >nul
    start http://localhost:3001
) else if "%choice%"=="4" (
    echo Running Health Check...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && ./scripts/start.sh health"
) else if "%choice%"=="5" (
    echo Stopping services...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && ./scripts/start.sh stop"
) else if "%choice%"=="6" (
    echo Viewing logs (Ctrl+C to exit)...
    wsl -d Ubuntu -e bash -c "cd /home/ybrid22/projects/hybrid/poly && docker-compose logs -f"
) else (
    echo Invalid option
)

echo.
pause
