#!/bin/bash
# 双击一键刷新：立即扫描 docs 目录并重写 index.html，完成后自动退出
cd "$(dirname "$0")" || exit 1
python3 "./sync_docs_index.py" --once --notify
echo ""
echo "索引同步完成，可关闭此窗口。"
