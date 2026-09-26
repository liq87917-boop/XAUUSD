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
rem 旧行为：git 同步发生在 Orchestrator 进程内部（启动之后），
rem 会出现「磁盘上的代码已经更新，但运行中的进程仍执行旧代码」的窗口。
rem
rem 新行为：先在独立 Python 进程里做安全同步（fast-forward / rebase），
rem 打印 branch / HEAD 短 SHA / provider / model / 同步结果，
rem 只有同步成功（退出码 0）才启动 Orchestrator，保证加载的是新代码。
rem 任何失败（dirty worktree / 分支不符 / 网络失败 / rebase 冲突）
rem 一律 fail-closed：不启动 Orchestrator，不覆盖任何本地修改。
rem
py -u orchestrator\bootstrap_sync.py

if errorlevel 1 (
    echo.
    echo [FATAL] bootstrap sync FAILED - Orchestrator will NOT start.
    echo         本地修改与本地 commit 已保留，未做任何覆盖。
    echo         请人工处理后重新运行 start_agent.bat。
    echo.
    pause
    exit /b 1
)

echo.
py -u orchestrator\ai_orchestrator.py

pause
