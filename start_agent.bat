@echo off

cd /d %~dp0

title Cline AI Orchestrator

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

py -u orchestrator\ai_orchestrator.py

pause
