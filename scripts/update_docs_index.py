#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-refreshes the embedded window.DOCS_CATALOG index inside docs/index.html.

Works with zero third-party dependencies (Python 3.8+ standard library only).
Can be run automatically by GitHub Actions on every push or locally anytime:
    python3 scripts/update_docs_index.py
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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

IGNORED_DIRS = {".git", ".github", "__MACOSX", "node_modules", ".report-cache", "__pycache__"}
IGNORED_FILES = {".DS_Store", "Thumbs.db", ".nojekyll"}


def detect_docs_dir(explicit_dir: str | None) -> Path:
    if explicit_dir:
        p = Path(explicit_dir).resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"Specified docs directory does not exist: {p}")
        return p

    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        Path.cwd() / "docs",
        repo_root / "docs",
        Path.cwd(),
        Path(__file__).resolve().parent,
    ]
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand.resolve()
    raise FileNotFoundError("Could not locate docs/index.html. Pass --docs-dir explicitly.")


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


def git_modified_iso(file_path: Path, cwd: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(file_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        iso = res.stdout.strip()
        if iso:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    mtime = file_path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    if "ceo" in fname:
        return ("SeniorExecutive", "CompanyRoot")
    if fname in {"iphone.html", "mac.html", "ipad.html"}:
        return ("hardware", "CompanyRoot")
    return ("Company", "CompanyRoot")


def build_updated_catalog(docs_dir: Path, prev_catalog: dict) -> dict:
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

    # 1. Scan all directories inside docs_dir
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

        # Walk deeper or custom subdirectories under top_d
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

    # 2. Scan all HTML files inside docs_dir
    files_out: list[dict] = []
    for root_str, dirnames, filenames in os.walk(docs_dir):
        dirnames[:] = sorted([dn for dn in dirnames if dn not in IGNORED_DIRS and not dn.startswith(".")])
        for fn in sorted(filenames):
            if fn in IGNORED_FILES or fn.startswith("."):
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
                headings, summary, excerpt, inferred_ticker, inferred_label = [], "网站全局搜索与目录导航入口文件", "", "INDEX", ""
            else:
                parsed = parse_html_metadata(fpath)
                title = parsed["title"]
                headings = parsed["headings"]
                summary = parsed["summary"]
                excerpt = parsed["excerpt"]
                inferred_ticker = parsed["inferredTicker"]
                inferred_label = parsed.get("inferredLabel", "")

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
            if is_portal:
                fsize = 0
                mtime_iso = ""
            else:
                fsize = fpath.stat().st_size
                mtime_iso = git_modified_iso(fpath, docs_dir)
                if prev_f.get("size") == fsize and prev_f.get("title") == title and prev_f.get("modifiedTime") and not (docs_dir.parent / ".git").exists() and not (docs_dir / ".git").exists():
                    mtime_iso = prev_f["modifiedTime"]

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

    # 3. Aggregate exact counts
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


def update_index_html(index_path: Path, catalog: dict) -> bool:
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
        pattern = re.compile(
            re.escape(CATALOG_START) + r"[\s\S]*?" + re.escape(CATALOG_END)
        )
        updated = pattern.sub(lambda _: replacement_block, original, count=1)
    else:
        script_pattern = re.compile(r"<script[^>]*>\s*window\.DOCS_CATALOG\s*=[\s\S]*?</script>")
        if not script_pattern.search(original):
            raise RuntimeError(f"Could not find DOCS_CATALOG block in {index_path}")
        updated = script_pattern.sub(lambda _: replacement_block, original, count=1)

    if updated == original:
        return False
    index_path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan docs/ and refresh docs/index.html catalog.")
    parser.add_argument("--docs-dir", help="Path to docs directory (auto-detected if omitted).")
    args = parser.parse_args()

    docs_dir = detect_docs_dir(args.docs_dir)
    index_path = docs_dir / "index.html"
    if not index_path.is_file():
        print(f"Error: {index_path} not found.", file=sys.stderr)
        return 1

    prev_catalog = extract_existing_catalog(index_path.read_text(encoding="utf-8"))
    new_catalog = build_updated_catalog(docs_dir, prev_catalog)
    changed = update_index_html(index_path, new_catalog)

    stats = new_catalog["stats"]
    status = "UPDATED" if changed else "UNCHANGED"
    print(
        f"[{status}] {index_path}: "
        f"{stats['totalTopFolders']} top folders, "
        f"{stats['totalSubfolders']} total subfolders, "
        f"{stats['totalReports']} HTML reports."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())