@echo off

cd /d %~dp0

title Cline AI Orchestrator

echo ========================================
echo        Cline AI Orchestrator
echo ========================================
echo.

py -u orchestrator\ai_orchestrator.py

pause