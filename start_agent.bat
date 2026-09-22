@echo off

cd /d %~dp0

title Cline AI Orchestrator

echo ========================================
echo        Cline AI Orchestrator
echo ========================================
echo.

python orchestrator\ai_orchestrator.py

pause