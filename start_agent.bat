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

echo Poll interval : %AI_POLL_SECONDS%s
echo Queue target  : %AI_QUEUE_TARGET_SIZE%
echo Idle log every: %AI_IDLE_LOG_SECONDS%s
echo.

py -u orchestrator\ai_orchestrator.py

pause
