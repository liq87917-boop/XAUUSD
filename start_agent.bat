@echo off

cd /d %~dp0

title XAUUSD Orchestrator

echo ========================================
echo        Cline AI Orchestrator
echo ========================================
echo.

rem Rolling queue defaults. Existing environment variables can override them.
if not defined AI_POLL_SECONDS set AI_POLL_SECONDS=20
if not defined AI_QUEUE_TARGET_SIZE set AI_QUEUE_TARGET_SIZE=3
if not defined AI_IDLE_LOG_SECONDS set AI_IDLE_LOG_SECONDS=1800

rem XAUUSD automation is intentionally pinned to the user's direct DeepSeek provider.
set AI_CLINE_PROVIDER=deepseek

echo Poll interval : %AI_POLL_SECONDS%s
echo Queue target  : %AI_QUEUE_TARGET_SIZE%
echo Idle log every: %AI_IDLE_LOG_SECONDS%s
echo Cline provider: %AI_CLINE_PROVIDER%
if defined AI_CLINE_MODEL echo Cline model   : %AI_CLINE_MODEL%
echo.

rem ==========================================================
rem GOLD-021: bootstrap sync MUST run before the Python
rem Orchestrator process starts.
rem ==========================================================
rem
rem Old behavior: git sync happened inside the Orchestrator process (after startup),
rem causing a window where on-disk code was updated but the running process still ran old code.
rem
rem New behavior: perform a safe sync (fast-forward / rebase) in a separate Python process first,
rem printing branch / HEAD short SHA / provider / model / sync result,
rem and start the Orchestrator only on success (exit code 0), guaranteeing fresh code is loaded.
rem Any failure (dirty worktree / branch mismatch / network failure / rebase conflict)
rem fails closed: do not start the Orchestrator and do not overwrite any local changes.
rem
py -u orchestrator\bootstrap_sync.py

if errorlevel 1 (
    echo.
    echo [FATAL] bootstrap sync FAILED - Orchestrator will NOT start.
    echo         Local changes and local commits preserved; nothing overwritten.
    echo         Please resolve manually and re-run start_agent.bat.
    echo.
    timeout /t 3 /nobreak >nul
    exit /b 1
)

echo.
py -u orchestrator\ai_orchestrator.py

timeout /t 3 /nobreak >nul
