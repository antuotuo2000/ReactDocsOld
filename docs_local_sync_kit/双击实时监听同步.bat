@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 王安安的数据中心 - 实时目录监听
echo ==========================================================
echo   王安安的数据中心 - 本地目录实时监听同步已启动
echo   往 docs 目录新增、修改或删除报告后会自动重写 index.html
echo   关闭此窗口或按 Ctrl+C 即可停止监听
echo ==========================================================
python "sync_docs_index.py" --watch
pause
