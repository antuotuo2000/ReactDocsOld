#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
王安安的数据中心 — 本地轻量一键刷新 & 目录实时监听同步脚本 (sync_docs_index.py)

零第三方依赖（仅使用 Python 3 标准库），直接放在 `docs` 文件夹同级目录（或 `docs` 目录内部）即可使用：
  1. 默认实时监听模式（启动时先立即同步一次，随后持续监听增删改并自动重写 index.html）：
       python3 sync_docs_index.py
  2. 单次一键刷新模式（扫描重写完成后立即退出）：
       python3 sync_docs_index.py --once
  3. 指定自定义目录或轮询间隔：
       python3 sync_docs_index.py --docs-dir ./docs --interval 1.0 --notify
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

CATALOG_START = "<!-- DOCS_CATALOG_START -->"
CATALOG_END = "<!-- DOCS_CATALOG_END -->"

STANDARD_SEVEN_DIMS = (
    "Company",
    "SeniorExecutive",
    "CorporateCulture",
    "hardware",
    "software",
    "patent",
    "PhysicalStores",
)

IGNORED_DIRS = {
    ".git",
    ".github",
    "__MACOSX",
    "node_modules",
    ".report-cache",
    "__pycache__",
    ".idea",
    ".vscode",
}

IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".nojekyll",
}

TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".part", ".crdownload", ".bak", "~")


def is_ignored_file(name: str) -> bool:
    if name in IGNORED_FILES or name.startswith(".") or name.startswith("~$"):
        return True
    lower = name.lower()
    return lower.endswith(TEMP_SUFFIXES)


def detect_docs_dir(explicit_dir: str | None = None) -> Path:
    """Auto-locates the docs directory containing index.html when placed next to or inside docs/."""
    if explicit_dir:
        p = Path(explicit_dir).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"指定的目录不存在: {p}")
        if not (p / "index.html").is_file():
            raise FileNotFoundError(f"指定目录下未找到 index.html: {p / 'index.html'}")
        return p

    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd().resolve()

    candidates = [
        script_dir / "docs",          # 脚本放在 docs/ 文件夹旁（最常见）
        script_dir,                   # 脚本直接放在 docs/ 文件夹内部
        script_dir.parent / "docs",   # 脚本放在 scripts/ 子目录中
        cwd / "docs",                 # 从工作目录旁运行
        cwd,                          # 从 docs/ 工作目录内运行
    ]

    for cand in candidates:
        idx = cand / "index.html"
        if idx.is_file():
            try:
                head = idx.read_text(encoding="utf-8", errors="ignore")[:200000]
                if "DOCS_CATALOG" in head:
                    return cand.resolve()
            except OSError:
                continue

    # Fallback: scan immediate sibling folders next to script_dir
    for sub in sorted(script_dir.iterdir()):
        if sub.is_dir() and sub.name not in IGNORED_DIRS and not sub.name.startswith("."):
            idx = sub / "index.html"
            if idx.is_file():
                try:
                    if "DOCS_CATALOG" in idx.read_text(encoding="utf-8", errors="ignore"):
                        return sub.resolve()
                except OSError:
                    continue

    raise FileNotFoundError(
        "未能自动定位包含 index.html 的 docs 文件夹。\n"
        "请将本脚本放在 docs 文件夹同级目录（或内部），或使用 --docs-dir 指定路径。"
    )


def extract_existing_catalog(html_text: str) -> dict:
    m = re.search(
        r"window\.DOCS_CATALOG\s*=\s*(\{[\s\S]*?\});\s*</script>",
        html_text,
    )
    if not m:
        return {}
    raw_json = m.group(1).replace("<\\/", "</")
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return {}


def stable_local_id(prefix: str, rel_path: str) -> str:
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def infer_category(rel_parts: tuple[str, ...]) -> tuple[str, str]:
    if len(rel_parts) == 1:
        return ("Portal" if rel_parts[0].lower() == "index.html" else "Company", "Root")
    if len(rel_parts) >= 3:
        sub = rel_parts[1]
        for dim in STANDARD_SEVEN_DIMS:
            if sub.lower() == dim.lower():
                return (dim, dim)
        return (sub, sub)
    fname = rel_parts[-1].lower()
    if "culture" in fname:
        return ("CorporateCulture", "CompanyRoot")
    if "ceo" in fname or "executive" in fname or "founder" in fname:
        return ("SeniorExecutive", "CompanyRoot")
    if fname in {"iphone.html", "mac.html", "ipad.html"}:
        return ("hardware", "CompanyRoot")
    return ("Company", "CompanyRoot")


class MetadataCache:
    """Caches parsed HTML metadata and file timestamps by (rel_path, size, mtime_ns) for instant rebuilds."""

    def __init__(self, docs_dir: Path) -> None:
        self.docs_dir = docs_dir
        self.has_git = (docs_dir.parent / ".git").exists() or (docs_dir / ".git").exists()
        self._meta_cache: dict[tuple[str, int, int], dict] = {}
        self._mtime_cache: dict[tuple[str, int, int], str] = {}

    def get_parsed_html(self, file_path: Path, rel_path: str, size: int, mtime_ns: int) -> dict:
        key = (rel_path, size, mtime_ns)
        cached = self._meta_cache.get(key)
        if cached is not None:
            return cached
        parsed = parse_html_metadata(file_path)
        # Evict stale keys for the same rel_path
        stale = [k for k in self._meta_cache if k[0] == rel_path and k != key]
        for k in stale:
            self._meta_cache.pop(k, None)
        self._meta_cache[key] = parsed
        return parsed

    def get_modified_iso(self, file_path: Path, rel_path: str, size: int, mtime_ns: int, fallback_iso: str = "") -> str:
        key = (rel_path, size, mtime_ns)
        cached = self._mtime_cache.get(key)
        if cached is not None:
            return cached

        iso_out = ""
        if self.has_git:
            try:
                res = subprocess.run(
                    ["git", "log", "-1", "--format=%cI", "--", str(file_path)],
                    cwd=str(self.docs_dir),
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                raw_iso = res.stdout.strip()
                if raw_iso:
                    dt = datetime.fromisoformat(raw_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
                    iso_out = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                iso_out = ""

        if not iso_out:
            if fallback_iso and not self.has_git:
                iso_out = fallback_iso
            else:
                iso_out = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        stale = [k for k in self._mtime_cache if k[0] == rel_path and k != key]
        for k in stale:
            self._mtime_cache.pop(k, None)
        self._mtime_cache[key] = iso_out
        return iso_out


def parse_html_metadata(file_path: Path) -> dict:
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", raw, flags=re.IGNORECASE)
    if title_match:
        title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
    else:
        title = file_path.stem

    body_clean = re.sub(r"<(style|script)[^>]*>[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
    body_clean = re.sub(r"<(aside|nav)[^>]*>[\s\S]*?</\1>", " ", body_clean, flags=re.IGNORECASE)

    headings: list[str] = []
    for hm in re.finditer(r"<h[1-3][^>]*>([\s\S]*?)</h[1-3]>", body_clean, flags=re.IGNORECASE):
        ht = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", hm.group(1)))).strip()
        if ht and ht != title and len(ht) > 2 and ht not in headings:
            headings.append(ht)
        if len(headings) >= 8:
            break

    summary = ""
    for pm in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", body_clean, flags=re.IGNORECASE):
        pt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", pm.group(1)))).strip()
        if len(pt) >= 25:
            summary = pt[:180] + ("…" if len(pt) > 180 else "")
            break

    plain_all = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body_clean))).strip()
    if not summary and plain_all:
        summary = plain_all[:180] + ("…" if len(plain_all) > 180 else "")

    excerpt = plain_all[140:320].strip() if len(plain_all) > 140 else ""

    ticker_match = re.search(
        r"[\(（](?:NASDAQ|NYSE|NYSE\s+Arca|OTC\s+ADR|HKEX)\s*[:：]\s*([A-Z0-9.\-]+)[\)）]",
        title,
        flags=re.IGNORECASE,
    )
    inferred_ticker = ticker_match.group(1).upper() if ticker_match else ""

    inferred_label = ""
    bilingual_match = re.match(
        r"^([\u4e00-\u9fa5]{2,12}?)(?:集团|公司|控股|股份)?\s+([A-Z][A-Za-z0-9.&'\-\s]{1,32}?)\s*[\(（](?:NASDAQ|NYSE|NYSE\s+Arca|OTC\s+ADR|HKEX)",
        title,
    )
    if bilingual_match:
        zh_part = bilingual_match.group(1).strip()
        en_part = re.sub(
            r"(?:,\s*Inc\.|\s+Inc\.|\s+Corp\.|\s+Corporation|\s+Group,?\s*Inc\.|\s+Group|\s+Holdings?|\s+PLC|\s+Ltd\.)+$",
            "",
            bilingual_match.group(2).strip(),
            flags=re.IGNORECASE,
        ).strip()
        inferred_label = f"{zh_part} {en_part}".strip()
    else:
        zh_paren_match = re.search(
            r"([\u4e00-\u9fa5]{2,10}?)(?:集团|公司|控股)?[\(（](?:NASDAQ|NYSE|HKEX)\s*[:：]\s*[A-Z0-9.\-]+[\)）]",
            title,
        )
        if zh_paren_match:
            inferred_label = zh_paren_match.group(1).strip()

    return {
        "title": title,
        "headings": headings,
        "summary": summary,
        "excerpt": excerpt,
        "inferredTicker": inferred_ticker,
        "inferredLabel": inferred_label,
    }


def scan_watch_snapshot(docs_dir: Path) -> tuple[set[str], dict[str, tuple[int, int]]]:
    """
    Fast recursive directory & HTML file fingerprint scan using os.walk.
    Excludes docs/index.html itself so rewriting index.html never triggers a feedback loop.
    Returns (dirs_set, html_files_map[rel_path -> (size, mtime_ns)]).
    """
    dirs_set: set[str] = set()
    files_map: dict[str, tuple[int, int]] = {}

    for root_str, dirnames, filenames in os.walk(docs_dir):
        dirnames[:] = sorted(
            [dn for dn in dirnames if dn not in IGNORED_DIRS and not dn.startswith(".")]
        )
        root_path = Path(root_str)
        if root_path != docs_dir:
            dirs_set.add(root_path.relative_to(docs_dir).as_posix())

        for fn in filenames:
            if is_ignored_file(fn):
                continue
            if not fn.lower().endswith((".html", ".htm")):
                continue
            fpath = root_path / fn
            rel_path = fpath.relative_to(docs_dir).as_posix()
            if rel_path.lower() == "index.html":
                continue
            try:
                st = fpath.stat()
                files_map[rel_path] = (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                continue

    return dirs_set, files_map


def diff_snapshots(
    old_snap: tuple[set[str], dict[str, tuple[int, int]]],
    new_snap: tuple[set[str], dict[str, tuple[int, int]]],
) -> list[str]:
    old_dirs, old_files = old_snap
    new_dirs, new_files = new_snap
    lines: list[str] = []

    added_files = sorted(set(new_files) - set(old_files))
    removed_files = sorted(set(old_files) - set(new_files))
    modified_files = sorted(
        k for k in (set(new_files) & set(old_files)) if new_files[k] != old_files[k]
    )
    added_dirs = sorted(new_dirs - old_dirs)
    removed_dirs = sorted(old_dirs - new_dirs)

    for p in added_files[:6]:
        lines.append(f"  + 新增报告: {p}")
    if len(added_files) > 6:
        lines.append(f"  + 其余新增报告 {len(added_files) - 6} 篇...")

    for p in modified_files[:6]:
        lines.append(f"  ~ 更新报告: {p}")
    if len(modified_files) > 6:
        lines.append(f"  ~ 其余更新报告 {len(modified_files) - 6} 篇...")

    for p in removed_files[:6]:
        lines.append(f"  - 移除报告: {p}")
    if len(removed_files) > 6:
        lines.append(f"  - 其余移除报告 {len(removed_files) - 6} 篇...")

    if not (added_files or modified_files or removed_files):
        for d in added_dirs[:4]:
            lines.append(f"  + 新增目录: {d}/")
        for d in removed_dirs[:4]:
            lines.append(f"  - 移除目录: {d}/")

    return lines


def build_updated_catalog(docs_dir: Path, prev_catalog: dict, cache: MetadataCache) -> dict:
    root_info = prev_catalog.get("root") or {
        "id": "docs_root",
        "name": docs_dir.name,
        "driveUrl": "https://drive.google.com/drive/folders/1GEm-cxIC-tAvyiMlAs82b8QYzjUqhNBc",
    }
    default_drive_root = root_info.get("driveUrl", "#")

    prev_companies = {c["folder"]: c for c in prev_catalog.get("companies", []) if "folder" in c}
    prev_files_by_rel = {f["relPath"]: f for f in prev_catalog.get("files", []) if "relPath" in f}
    prev_subfolders_by_path = {}
    for item in prev_catalog.get("appleL3", []) + prev_catalog.get("extraSubfolders", []):
        if "path" in item:
            prev_subfolders_by_path[item["path"]] = item

    top_dirs = sorted(
        [d for d in docs_dir.iterdir() if d.is_dir() and d.name not in IGNORED_DIRS and not d.name.startswith(".")],
        key=lambda p: p.name.lower(),
    )

    folder_id_by_rel: dict[str, str] = {"": root_info["id"]}
    companies_out: list[dict] = []
    extra_subfolders: list[dict] = []

    for top_d in top_dirs:
        cname = top_d.name
        prev_c = prev_companies.get(cname, {})
        cid = prev_c.get("id") or stable_local_id("dir", cname)
        cdrive = prev_c.get("driveUrl") or default_drive_root
        folder_id_by_rel[cname] = cid

        prev_subs = prev_c.get("subs") or {}
        subs_out: dict[str, dict] = {}
        for dim in STANDARD_SEVEN_DIMS:
            dim_path = f"{cname}/{dim}"
            psub = prev_subs.get(dim) or {}
            sid = psub.get("id") or stable_local_id("dir", dim_path)
            sdrive = psub.get("driveUrl") or cdrive
            subs_out[dim] = {"id": sid, "driveUrl": sdrive}
            folder_id_by_rel[dim_path] = sid

        for sub_root, dirnames, _ in os.walk(top_d):
            dirnames[:] = sorted([dn for dn in dirnames if dn not in IGNORED_DIRS and not dn.startswith(".")])
            for dn in dirnames:
                full_sub = Path(sub_root) / dn
                rel_sub = full_sub.relative_to(docs_dir).as_posix()
                parts = rel_sub.split("/")
                if len(parts) == 2 and parts[1] in STANDARD_SEVEN_DIMS:
                    continue
                parent_rel = "/".join(parts[:-1])
                parent_id = folder_id_by_rel.get(parent_rel) or stable_local_id("dir", parent_rel)
                prev_sf = prev_subfolders_by_path.get(rel_sub, {})
                sf_id = prev_sf.get("id") or stable_local_id("dir", rel_sub)
                sf_drive = prev_sf.get("driveUrl") or cdrive
                folder_id_by_rel[rel_sub] = sf_id
                cat_guess = parts[1] if len(parts) >= 2 else "Company"
                extra_subfolders.append({
                    "name": dn,
                    "id": sf_id,
                    "parentId": parent_id,
                    "path": rel_sub,
                    "company": cname,
                    "category": cat_guess,
                    "level": len(parts),
                    "driveUrl": sf_drive,
                })

        companies_out.append({
            "folder": cname,
            "id": cid,
            "driveUrl": cdrive,
            "label": prev_c.get("label") or cname,
            "ticker": prev_c.get("ticker") or cname[:6].upper(),
            "sector": prev_c.get("sector") or "综合投研覆盖",
            "reportCount": 0,
            "subs": subs_out,
        })

    comp_map = {c["folder"]: c for c in companies_out}

    files_out: list[dict] = []
    for root_str, dirnames, filenames in os.walk(docs_dir):
        dirnames[:] = sorted([dn for dn in dirnames if dn not in IGNORED_DIRS and not dn.startswith(".")])
        for fn in sorted(filenames):
            if is_ignored_file(fn):
                continue
            if not fn.lower().endswith((".html", ".htm")):
                continue

            fpath = Path(root_str) / fn
            rel_path = fpath.relative_to(docs_dir).as_posix()
            rel_parts = tuple(rel_path.split("/"))
            is_portal = rel_path.lower() == "index.html"

            prev_f = prev_files_by_rel.get(rel_path, {})
            parent_rel = "/".join(rel_parts[:-1])
            parent_id = folder_id_by_rel.get(parent_rel, root_info["id"])

            if is_portal:
                title = "王安安的数据中心 (index.html)"
                headings, summary, excerpt, inferred_ticker, inferred_label = (
                    [],
                    "网站全局搜索与目录导航入口文件",
                    "",
                    "INDEX",
                    "",
                )
                fsize = 0
                mtime_iso = ""
            else:
                try:
                    st = fpath.stat()
                except FileNotFoundError:
                    continue
                fsize = st.st_size
                parsed = cache.get_parsed_html(fpath, rel_path, fsize, st.st_mtime_ns)
                title = parsed["title"]
                headings = parsed["headings"]
                summary = parsed["summary"]
                excerpt = parsed["excerpt"]
                inferred_ticker = parsed["inferredTicker"]
                inferred_label = parsed.get("inferredLabel", "")

                prev_same = (
                    prev_f.get("size") == fsize
                    and prev_f.get("title") == title
                    and prev_f.get("modifiedTime")
                )
                fallback_iso = prev_f["modifiedTime"] if prev_same else ""
                mtime_iso = cache.get_modified_iso(fpath, rel_path, fsize, st.st_mtime_ns, fallback_iso)

            comp_name = rel_parts[0] if len(rel_parts) > 1 else "根目录"
            cmeta = comp_map.get(comp_name)
            if cmeta and inferred_ticker and cmeta["ticker"] == comp_name[:6].upper():
                cmeta["ticker"] = inferred_ticker
            if cmeta and inferred_label and cmeta["label"] == comp_name:
                cmeta["label"] = inferred_label
                for existing_f in files_out:
                    if existing_f.get("company") == comp_name:
                        existing_f["companyLabel"] = inferred_label

            cat_display, folder_cat = infer_category(rel_parts)
            fid = prev_f.get("id") or stable_local_id("file", rel_path)
            drive_url = prev_f.get("driveUrl") or (cmeta["driveUrl"] if cmeta else default_drive_root)

            files_out.append({
                "id": fid,
                "parentId": parent_id,
                "name": fn,
                "relPath": rel_path,
                "size": fsize,
                "modifiedTime": mtime_iso,
                "title": title,
                "company": comp_name,
                "companyLabel": cmeta["label"] if cmeta else "根目录 docs",
                "ticker": (cmeta["ticker"] if cmeta else inferred_ticker or "DOC"),
                "sector": (cmeta["sector"] if cmeta else "门户与系统文件"),
                "category": prev_f.get("category") or cat_display,
                "folderCategory": prev_f.get("folderCategory") or folder_cat,
                "level": len(rel_parts),
                "driveUrl": drive_url,
                "isPortalIndex": is_portal,
                "headings": headings,
                "summary": summary,
                "excerpt": excerpt,
            })

    reports = [f for f in files_out if not f.get("isPortalIndex")]
    cat_counts: dict[str, int] = {}
    sector_counts: dict[str, int] = {}
    comp_counts: dict[str, int] = {}

    for r in reports:
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
        sector_counts[r["sector"]] = sector_counts.get(r["sector"], 0) + 1
        comp_counts[r["company"]] = comp_counts.get(r["company"], 0) + 1

    for c in companies_out:
        c["reportCount"] = comp_counts.get(c["folder"], 0)

    total_subfolders = len(companies_out) * (1 + len(STANDARD_SEVEN_DIMS)) + len(extra_subfolders)

    return {
        "root": root_info,
        "companies": companies_out,
        "appleL3": [sf for sf in extra_subfolders if sf["company"] == "Apple" and sf["level"] == 3],
        "extraSubfolders": extra_subfolders,
        "files": files_out,
        "systemFiles": prev_catalog.get("systemFiles", []),
        "stats": {
            "totalTopFolders": len(companies_out),
            "totalSubfolders": total_subfolders,
            "totalReports": len(reports),
            "totalHtmlFiles": len(files_out),
            "activeCompanies": sum(1 for c in companies_out if c["reportCount"] > 0),
            "categoryCounts": cat_counts,
            "sectorCounts": sector_counts,
            "reportBytes": sum(r["size"] for r in reports),
            "totalBytes": sum(f["size"] for f in files_out),
        },
    }


def atomic_write_index(index_path: Path, catalog: dict) -> bool:
    """Rewrites docs/index.html atomically via a temporary sibling file if catalog changed."""
    original = index_path.read_text(encoding="utf-8")
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    replacement_block = (
        f"{CATALOG_START}\n"
        f"  <script id=\"docsCatalogData\">\n"
        f"    window.DOCS_CATALOG = {payload};\n"
        f"  </script>\n"
        f"  {CATALOG_END}"
    )

    if CATALOG_START in original and CATALOG_END in original:
        pattern = re.compile(re.escape(CATALOG_START) + r"[\s\S]*?" + re.escape(CATALOG_END))
        updated = pattern.sub(lambda _: replacement_block, original, count=1)
    else:
        script_pattern = re.compile(r"<script[^>]*>\s*window\.DOCS_CATALOG\s*=[\s\S]*?</script>")
        if not script_pattern.search(original):
            raise RuntimeError(f"在 {index_path} 中未找到 window.DOCS_CATALOG 索引区块。")
        updated = script_pattern.sub(lambda _: replacement_block, original, count=1)

    if updated == original:
        return False

    tmp_path = index_path.parent / f".{index_path.name}.sync.tmp"
    tmp_path.write_text(updated, encoding="utf-8")
    os.replace(tmp_path, index_path)
    return True


def send_macos_notification(title: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        pass


def sync_once(docs_dir: Path, cache: MetadataCache, notify: bool = False) -> tuple[bool, dict, float]:
    t0 = time.perf_counter()
    index_path = docs_dir / "index.html"
    prev_catalog = extract_existing_catalog(index_path.read_text(encoding="utf-8"))
    new_catalog = build_updated_catalog(docs_dir, prev_catalog, cache)
    changed = atomic_write_index(index_path, new_catalog)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    stats = new_catalog["stats"]
    if changed and notify:
        send_macos_notification(
            "王安安的数据中心 · 索引已同步",
            f"共 {stats['totalTopFolders']} 个目录，{stats['totalReports']} 篇报告",
        )
    return changed, stats, elapsed_ms


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run_watch_loop(docs_dir: Path, cache: MetadataCache, interval: float, notify: bool) -> int:
    stop_requested = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    # 1. Perform immediate initial sync on launch
    changed, stats, ms = sync_once(docs_dir, cache, notify=notify and False)
    state_tag = "UPDATED 已更新" if changed else "OK 已是最新"
    print(
        f"[{now_hms()}] [{state_tag}] {docs_dir / 'index.html'}\n"
        f"           当前收录: {stats['totalTopFolders']} 个一级目录 | "
        f"{stats['totalSubfolders']} 个子目录 | {stats['totalReports']} 篇 HTML 报告 ({ms:.0f} ms)"
    )
    print(f"[{now_hms()}] 正在实时监听目录变动: {docs_dir} （每 {interval:.1f}s 扫描，按 Ctrl+C 停止）...")
    sys.stdout.flush()

    last_snap = scan_watch_snapshot(docs_dir)

    while not stop_requested:
        time.sleep(interval)
        if stop_requested:
            break

        cur_snap = scan_watch_snapshot(docs_dir)
        if cur_snap == last_snap:
            continue

        # Debounce: wait briefly until file copies/writes settle
        settle_deadline = time.monotonic() + 3.0
        while not stop_requested and time.monotonic() < settle_deadline:
            time.sleep(0.35)
            settled_snap = scan_watch_snapshot(docs_dir)
            if settled_snap == cur_snap:
                break
            cur_snap = settled_snap

        diff_lines = diff_snapshots(last_snap, cur_snap)
        try:
            changed, stats, ms = sync_once(docs_dir, cache, notify=notify)
            last_snap = scan_watch_snapshot(docs_dir)
            print(f"\n[{now_hms()}] 检测到目录变动:")
            for dl in diff_lines:
                print(dl)
            result_label = "已自动重写 index.html" if changed else "已校验 index.html（无需改动）"
            print(
                f"[{now_hms()}] ✓ {result_label}: "
                f"{stats['totalTopFolders']} 个目录 / {stats['totalReports']} 篇报告 ({ms:.0f} ms)"
            )
            sys.stdout.flush()
        except Exception as exc:
            print(f"[{now_hms()}] [警告] 同步重试中: {exc}", file=sys.stderr)

    print(f"\n[{now_hms()}] 已停止目录监听。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="扫描 docs 目录变动并自动重写 docs/index.html 索引（支持单次一键刷新与常驻实时监听）。"
    )
    parser.add_argument("--docs-dir", help="指定 docs 文件夹路径（默认自动识别同级或当前目录下的 docs）。")
    parser.add_argument(
        "--once",
        action="store_true",
        help="单次一键刷新模式：立即扫描重写 index.html 后退出（不进入常驻监听）。",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="常驻实时监听模式（默认模式）：启动即同步一次，随后持续监听目录增删改。",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.2,
        help="监听模式下的扫描间隔秒数（默认 1.2 秒）。",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="在 macOS 上每次自动更新 index.html 时发送系统通知横幅。",
    )
    args = parser.parse_args()

    try:
        docs_dir = detect_docs_dir(args.docs_dir)
    except FileNotFoundError as err:
        print(f"[错误] {err}", file=sys.stderr)
        return 1

    cache = MetadataCache(docs_dir)

    if args.once and not args.watch:
        changed, stats, ms = sync_once(docs_dir, cache, notify=args.notify)
        status = "UPDATED" if changed else "UNCHANGED"
        print(
            f"[{status}] {docs_dir / 'index.html'}: "
            f"{stats['totalTopFolders']} 个一级目录, "
            f"{stats['totalSubfolders']} 个子目录, "
            f"{stats['totalReports']} 篇 HTML 报告 ({ms:.0f} ms)."
        )
        return 0

    return run_watch_loop(docs_dir, cache, max(0.4, args.interval), args.notify)


if __name__ == "__main__":
    raise SystemExit(main())
