#!/usr/bin/env python3
"""
秀悦视频下载工具 - 本地 Web 服务版
运行后自动打开浏览器，操作完全在网页上进行。
用法：python3 src/app_server.py
"""
import base64
import gzip
import json
import os
import re
import threading
import uuid
from pathlib import Path

import subprocess
from urllib.parse import urlencode

import webview


def _curl_get(url: str, params: dict = None, cookies: dict = None,
              headers: dict = None, timeout: int = 20) -> dict:
    """
    用系统 curl 发 GET 请求，返回解析后的 JSON。
    绕过 macOS LibreSSL 版本过旧导致的 SSLEOFError。
    """
    if params:
        url = f"{url}?{urlencode(params)}"

    cmd = ["curl", "-s", "-k", "--max-time", str(timeout), "--compressed"]

    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]

    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        cmd += ["-H", f"Cookie: {cookie_str}"]

    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    raw = result.stdout.decode("utf-8").strip()
    if not raw:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl 返回空响应 (exit={result.returncode}), stderr: {stderr}")
    return json.loads(raw)


def _curl_post(url: str, body: dict, headers: dict = None,
               cookies: dict = None, timeout: int = 60,
               retries: int = 1, retry_delay: float = 3.0) -> dict:
    """
    用系统 curl 发 POST JSON 请求，返回解析后的 JSON。
    绕过 macOS LibreSSL 版本过旧导致的 SSLEOFError。
    超时或空响应时自动重试 `retries` 次（默认 1 次，间隔 retry_delay 秒）。
    """
    cmd = ["curl", "-s", "-k", "--max-time", str(timeout),
           "-X", "POST", "-H", "Content-Type: application/json"]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        cmd += ["-H", f"Cookie: {cookie_str}"]
    cmd += ["-d", json.dumps(body, ensure_ascii=False), url]

    import time as _t, sys as _sys
    last_err = None
    total_attempts = max(1, retries + 1)
    for attempt in range(1, total_attempts + 1):
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        raw = result.stdout.decode("utf-8").strip()
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError as je:
                last_err = f"JSON 解析失败 (exit={result.returncode}): {str(je)[:120]} / raw[:200]={raw[:200]}"
        else:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            last_err = f"curl POST 返回空响应 (exit={result.returncode}), stderr: {stderr[:200]}"
        if attempt < total_attempts:
            print(f"[curl-retry] 第 {attempt}/{total_attempts} 次失败：{last_err[:160]}  → {retry_delay}s 后重试",
                  file=_sys.stderr, flush=True)
            _t.sleep(retry_delay)
    raise RuntimeError(f"{last_err}（已重试 {retries} 次）")


def _get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """
    用 app_id + app_secret 获取飞书 tenant_access_token。
    错误时抛出异常。
    """
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = _curl_post(url, {"app_id": app_id, "app_secret": app_secret}, timeout=15)
    code = resp.get("code", -1)
    if code != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败：code={code} {resp.get('msg','')}")
    token = resp.get("tenant_access_token", "")
    if not token:
        raise RuntimeError("tenant_access_token 为空，请检查 App ID / App Secret")
    return token


def _write_bitable_records(token: str, results: list,
                           app_id: str = "", app_secret: str = "") -> list:
    """
    将 debug_url 结果批量写入飞书多维表格。
    目标表格固定：PSgJbhr21aHRGosg4uHclshgnDe / tblZrh3e6CWvvSwp
    results: [{name, debug_url, execute_id}]
    若提供 app_id + app_secret，自动获取 tenant_access_token（推荐）；
    否则直接使用 token（user_access_token）。
    返回: [(ok: bool, msg: str)]
    """
    output = []
    # 优先用 tenant_access_token
    if app_id and app_secret:
        try:
            token = _get_tenant_access_token(app_id, app_secret)
        except Exception as e:
            return [(False, f"  ❌ 获取 tenant_access_token 失败：{e}")]
    if not token:
        return [(False, "  ❌ 未填写飞书令牌（user_access_token 或 App ID+Secret）")]

    BASE_TOKEN = "PSgJbhr21aHRGosg4uHclshgnDe"
    TABLE_ID   = "tblZrh3e6CWvvSwp"
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
           f"{BASE_TOKEN}/tables/{TABLE_ID}/records/batch_create")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_UA,
    }
    batch_size = 50
    for i in range(0, len(results), batch_size):
        batch = results[i: i + batch_size]
        records = [{"fields": {"任务名称": r.get("name", ""), "debugurl": r.get("debug_url", "")}}
                   for r in batch]
        try:
            resp = _curl_post(url, {"records": records}, headers=headers, timeout=30)
            code = resp.get("code", -1)
            if code == 0:
                created = len(resp.get("data", {}).get("records", []))
                output.append((True, f"  ✅ 写入 {created} 条记录"))
            else:
                msg = resp.get('msg', '')
                hint = ""
                if code == 99991672:
                    hint = " → 应用缺少 bitable:app 权限，请改用 App ID + App Secret"
                output.append((False, f"  ❌ 写入失败：code={code} {msg[:120]}{hint}"))
        except Exception as e:
            output.append((False, f"  ❌ 写入异常：{str(e)[:150]}"))
    return output


def _curl_download(url: str, dest_path: str, timeout: int = 120) -> int:
    """
    用系统 curl 下载文件到 dest_path，返回文件字节数。
    """
    cmd = [
        "curl", "-s", "-k", "-L",
        "--max-time", str(timeout),
        "-H", f"User-Agent: {BROWSER_UA}",
        "-o", dest_path,
        url,
    ]
    subprocess.run(cmd, timeout=timeout + 5, check=True)
    return Path(dest_path).stat().st_size
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context

# 钉钉在线表格同步
from dingtalk_client import (
    DingTalkClient, DingTalkSession,
    scan_dingtalk_products, parse_mode_map, parse_demand_map,
    filter_templates_by_date, find_prev_date, find_recent_dates, find_section,
    _norm_product as _dt_norm_product,
)
from dingtalk_login import open_login_window

# ============================================================
# 配置
# ============================================================
DEFAULT_BASE_TOKEN = "J90LbKuJnaJvfWsjrIYcDJHlnSc"
DEFAULT_FIELD_ID   = "fldksNMEZ4"
DEFAULT_OUTPUT_DIR = str(Path("data/output/下载视频").resolve())
FEISHU_HOST        = "https://my.feishu.cn"
BROWSER_UA         = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36")

# 列表/下载/建表 时统一忽略名字包含以下任一关键词的副表（大小写不敏感）
SKIP_TABLE_KEYWORDS = ("ai修改",)

# 自动补全台词阈值：当 台词 与 最终台词 差异比例 ≥ 该值时，把 最终台词 复制到 台词
SUBTITLE_DIFF_THRESHOLD = 0.6

# 台词/最终台词列名匹配候选（按优先级，命中第一个就用）
SCRIPT_COL_KEYWORDS    = ("台词", "人物说的话", "人物说的台词")
SUBTITLE_COL_KEYWORDS  = ("最终台词", "最终字幕", "字幕")

# ─── 视频比例规范化 ───────────────────────────────────────
# 标准比例：(竖屏小, 横屏小, 竖屏大, 横屏大)
STANDARD_RATIOS = [(3, 4), (4, 3), (9, 16), (16, 9)]
# 家族判定阈值：化简后 max(W,H) <= 该值 → 小家族(3:4/4:3)，否则大家族(9:16/16:9)
RATIO_FAMILY_THRESHOLD = 10
# 上下文白名单：冒号前后 4 字内出现以下任一关键词才认为是视频比例
RATIO_CONTEXT_KEYWORDS = ("比例", "屏", "竖", "横", "视频", "尺寸", "分辨率", "画面", "像素")
RATIO_CONTEXT_WINDOW = 4
# 数字合理范围
RATIO_NUM_MIN = 1
RATIO_NUM_MAX = 9999
# 比例值合理范围（W/H），过滤掉时间戳/分数等
RATIO_VALUE_MIN = 0.2
RATIO_VALUE_MAX = 5.0


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _normalize_ratio(w: int, h: int) -> tuple:
    """
    把任意 W:H 规范化到 STANDARD_RATIOS 中最合适的一个：
    1) 1:1 → 3:4（默认竖屏）
    2) 先用 GCD 化简到最简分数
    3) 保持方向（横/竖）不变
    4) 根据化简后 max(W,H) 决定家族（≤阈值 小家族，> 阈值 大家族）
    """
    if w == h:
        return (3, 4)
    g = _gcd(w, h)
    sw, sh = w // g, h // g
    big = max(sw, sh)
    vertical = sw < sh  # True 竖屏
    if big <= RATIO_FAMILY_THRESHOLD:
        return (3, 4) if vertical else (4, 3)
    else:
        return (9, 16) if vertical else (16, 9)


def _has_ratio_context(text: str, start: int, end: int) -> bool:
    """检查 text[start:end] 这段冒号片段前后 RATIO_CONTEXT_WINDOW 字符内是否含上下文关键词"""
    lo = max(0, start - RATIO_CONTEXT_WINDOW)
    hi = min(len(text), end + RATIO_CONTEXT_WINDOW)
    window = text[lo:hi]
    return any(k in window for k in RATIO_CONTEXT_KEYWORDS)


_RATIO_RE = re.compile(r"(\d{1,4})[:：](\d{1,4})")


def _fix_ratios_in_text(text: str) -> tuple:
    """
    扫描文本中的 W:H 模式，命中上下文白名单 + 非标准比例时替换成最近的标准比例。
    返回：(新文本, [(原始, 规范化, 起始位置), ...])
    """
    if not text or not isinstance(text, str):
        return text, []
    fixes = []
    out = []
    last = 0
    for m in _RATIO_RE.finditer(text):
        w_s, h_s = m.group(1), m.group(2)
        w, h = int(w_s), int(h_s)
        s, e = m.span()
        # 数字边界保护：左侧/右侧紧挨数字时跳过（避免 1080:1920 被匹配为 080:192）
        if s > 0 and text[s - 1].isdigit():
            continue
        if e < len(text) and text[e].isdigit():
            continue
        # 范围过滤
        if not (RATIO_NUM_MIN <= w <= RATIO_NUM_MAX and RATIO_NUM_MIN <= h <= RATIO_NUM_MAX):
            continue
        val = w / h
        if not (RATIO_VALUE_MIN <= val <= RATIO_VALUE_MAX):
            continue
        # 已经是标准比例（化简后比对，比如 1080:1920 化简成 9:16 算标准）
        g = _gcd(w, h)
        simp = (w // g, h // g)
        if simp in STANDARD_RATIOS:
            continue
        # 上下文白名单：
        # 例外：整个单元格就是一个纯比例值（如 "1:1" 单独成格），直接规范化，无需关键词
        is_pure_ratio_cell = _RATIO_RE.fullmatch(text.strip()) is not None
        if not is_pure_ratio_cell and not _has_ratio_context(text, s, e):
            continue
        target = _normalize_ratio(w, h)
        new_repr = f"{target[0]}:{target[1]}"
        old_repr = f"{w}:{h}"  # 用原始数字+英文冒号，方便日志比对
        # 累积输出
        out.append(text[last:s])
        out.append(new_repr)
        last = e
        fixes.append((m.group(0), new_repr, s))
    if not fixes:
        return text, []
    out.append(text[last:])
    return "".join(out), fixes


def _should_skip_table(name: str) -> bool:
    """副表名是否命中跳过关键词（大小写不敏感）"""
    if not name:
        return False
    low = name.lower()
    return any(k.lower() in low for k in SKIP_TABLE_KEYWORDS)


def _find_col_idx(headers, keywords):
    """
    在 headers 列表里按 keywords 顺序找首个匹配的列索引；
    匹配规则：列名包含 keyword（去空格后做子串判断）。
    找不到返回 -1。
    """
    norm_headers = [(h or "").strip() for h in headers]
    for kw in keywords:
        for i, h in enumerate(norm_headers):
            if kw in h:
                return i
    return -1


def _char_diff_ratio(a: str, b: str) -> float:
    """
    返回两段文本的"字符级差异比例"，范围 [0, 1]。
    使用 difflib.SequenceMatcher.ratio() 得到相似度 → 差异 = 1 - 相似度。
    空串/全空格视为完全不同（返回 1.0），便于触发自动补全。
    """
    import difflib
    sa = (a or "").strip()
    sb = (b or "").strip()
    if not sa and not sb:
        return 0.0  # 两边都空：不算差异
    if not sa or not sb:
        return 1.0  # 一边空一边非空：差异满格
    return 1.0 - difflib.SequenceMatcher(None, sa, sb).ratio()


def _auto_fix_script_rows(headers, rows, threshold: float = SUBTITLE_DIFF_THRESHOLD):
    """
    检查每行的 "台词" 与 "最终台词"，差异 ≥ threshold 则用最终台词覆盖台词。
    返回 (fixed_count, details)：
      fixed_count: 被修复的行数
      details:     [{"row": 1-based行号, "diff": 差异比例, "old": 旧值简略, "new": 新值简略}]
    若 headers 里找不到任一列，直接返回 (0, [])。
    """
    s_idx = _find_col_idx(headers, SCRIPT_COL_KEYWORDS)
    f_idx = _find_col_idx(headers, SUBTITLE_COL_KEYWORDS)
    if s_idx < 0 or f_idx < 0 or s_idx == f_idx:
        return 0, []
    fixed, details = 0, []
    for r_idx, row in enumerate(rows):
        if s_idx >= len(row) or f_idx >= len(row):
            continue
        script   = row[s_idx]
        subtitle = row[f_idx]
        if not (subtitle or "").strip():
            continue  # 最终台词为空，无可补全
        diff = _char_diff_ratio(script, subtitle)
        if diff >= threshold:
            row[s_idx] = subtitle
            fixed += 1
            details.append({
                "row":  r_idx + 1,
                "diff": round(diff, 2),
                "old":  (script or "")[:30],
                "new":  (subtitle or "")[:30],
            })
    return fixed, details


def _auto_fix_ratios_product(product: dict) -> tuple:
    """
    扫描单个产品的 title 与 rows 所有单元格，修正非标准视频比例。
    会就地修改 product 字典。
    返回 (fixed_count, details)：
      fixed_count: 命中并替换的次数
      details:     [{"where": "title"/"row N col M", "old": "5:16", "new": "9:16"}]
    """
    fixed, details = 0, []
    # title
    title = product.get("title") or ""
    new_title, t_fixes = _fix_ratios_in_text(title)
    if t_fixes:
        product["title"] = new_title
        for old, new, _ in t_fixes:
            fixed += 1
            details.append({"where": "title", "old": old, "new": new})
    # rows
    rows = product.get("rows") or []
    for r_idx, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        for c_idx, cell in enumerate(row):
            if not isinstance(cell, str):
                continue
            new_cell, c_fixes = _fix_ratios_in_text(cell)
            if c_fixes:
                row[c_idx] = new_cell
                for old, new, _ in c_fixes:
                    fixed += 1
                    details.append({
                        "where": f"row{r_idx + 1} col{c_idx + 1}",
                        "old": old, "new": new,
                    })
    return fixed, details


app = Flask(__name__)

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# ============================================================
# 工具函数
# ============================================================
def decode_gzip_b64(b64_str: str) -> dict:
    return json.loads(gzip.decompress(base64.b64decode(b64_str)).decode("utf-8"))


def parse_cookie_str(cookie_str: str) -> dict:
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def parse_table_name(name: str):
    """'5.15_刘原原组_娇茵舒凝胶' → ('刘原原组', '娇茵舒凝胶')"""
    parts = name.split("_")
    start = 1 if len(parts) >= 3 and "." in parts[0] else 0
    if start + 1 < len(parts):
        return parts[start], "_".join(parts[start + 1:])
    return name, name


def parse_table_name3(name: str):
    """
    '5.15_刘原原组_娇茵舒凝胶' → ('5.15', '刘原原组', '娇茵舒凝胶')
    无日期前缀时返回 ('', group, product)；解析失败时返回 ('', name, name)。
    """
    parts = name.split("_")
    if len(parts) >= 3 and "." in parts[0]:
        return parts[0], parts[1], "_".join(parts[2:])
    if len(parts) >= 2:
        return "", parts[0], "_".join(parts[1:])
    return "", name, name


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def fetch_clientvars(base_token, table_id, cookies, record_limit=1, need_base=False, timeout=30):
    url = f"{FEISHU_HOST}/space/api/v1/bitable/{base_token}/clientvars"
    params = {
        "tableID": table_id, "recordLimit": record_limit,
        "ondemandLimit": record_limit,
        "needBase": "true" if need_base else "false",
        "viewLazyLoad": "false", "ondemandVer": 2,
        "openType": 0, "noMissCS": "true",
        "optimizationFlag": 1, "removeFmlExtra": "false",
    }
    headers = {"User-Agent": BROWSER_UA,
               "Referer": f"{FEISHU_HOST}/base/{base_token}"}
    return _curl_get(url, params=params, cookies=cookies,
                     headers=headers, timeout=timeout)


def extract_videos_sorted(table_json: dict, field_id: str) -> list:
    record_map = table_json.get("recordMap", {})
    rank_map   = table_json.get("rankInfo", {}).get("rankMap", {})
    out = []
    for rec_id, rec in record_map.items():
        val = rec.get(field_id, {}).get("value")
        url = val[0].get("text", "") if isinstance(val, list) and val else ""
        if url.startswith("http"):
            out.append({"rank": rank_map.get(rec_id, "z999"), "url": url})
    return sorted(out, key=lambda r: r["rank"])


def diagnose_table_json(table_json: dict, field_id: str) -> dict:
    """诊断 clientvars 返回的 table_json，分析记录数差异"""
    record_map = table_json.get("recordMap", {})
    rank_map   = table_json.get("rankInfo", {}).get("rankMap", {})
    record_ids = set(record_map.keys())
    rank_ids   = set(rank_map.keys())
    has_video  = 0
    no_video   = 0
    empty_val  = 0
    for rec_id, rec in record_map.items():
        val = rec.get(field_id, {}).get("value")
        if not val:
            empty_val += 1
        elif isinstance(val, list) and val and isinstance(val[0], dict) and val[0].get("text", "").startswith("http"):
            has_video += 1
        else:
            no_video += 1
    return {
        "rank_total":        len(rank_ids),         # rankMap 中的记录总数
        "record_total":      len(record_map),       # recordMap 中的记录总数
        "missing_in_record": len(rank_ids - record_ids),  # rankMap 有但 recordMap 没有
        "has_video":         has_video,             # 有有效视频URL的记录数
        "no_video":          no_video,              # 视频字段格式异常
        "empty_val":         empty_val,             # 视频字段为空
    }


# 内存中暂存最新 Cookie
_cached_cookie = {"value": ""}

# ============================================================
# 持久化状态文件
# ============================================================
STATE_FILE = Path("data/user_state.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_state(data: dict):
    try:
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[state] 保存失败: {e}")

# ============================================================
# 副表自动复制任务队列
# ============================================================
_task_lock = threading.Lock()
_task_list = []   # [{"id": str, "name": str, "status": "pending"|"done"}]

# ============================================================
# API 路由
# ============================================================

@app.route("/api/state", methods=["GET"])
def get_state():
    """加载持久化状态（配置 + 清洗数据）"""
    return jsonify(_load_state())

@app.route("/api/state", methods=["POST"])
def save_state():
    """保存持久化状态"""
    body = request.get_json(force=True, silent=True) or {}
    current = _load_state()
    # 合并：前端传什么就更新什么，不覆盖其他字段
    if "config" in body:
        current.setdefault("config", {}).update(body["config"])
    if "parsed_products" in body:
        current["parsed_products"] = body["parsed_products"]
    _save_state(current)
    return jsonify({"ok": True})


# ── 单个清洗产品 JSON 的写回 / 删除 ─────────────────────────────────────────
def _parsed_json_path(key: str) -> Path:
    """key 形如 5.16_刘原原组_娇茵舒凝胶 → data/input/parsed/5.16/5.16_刘原原组_娇茵舒凝胶.json"""
    date = key.split('_', 1)[0] if key else '未知日期'
    return Path("data/input/parsed") / date / f"{key}.json"


@app.route("/api/save-parsed-product", methods=["POST"])
def save_parsed_product():
    """把单个清洗产品的最新内容写回磁盘 JSON"""
    body = request.get_json(force=True, silent=True) or {}
    key     = (body.get("key") or "").strip()
    title   = body.get("title", "")
    headers = body.get("headers", []) or []
    rows    = body.get("rows", []) or []

    if not key:
        return jsonify({"ok": False, "msg": "缺少 key"})

    out_file = _parsed_json_path(key)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    header_str = ' // '.join(str(h) for h in headers)
    row_strs   = [' // '.join('' if c is None else str(c) for c in row) for row in rows]
    json_data  = [title, header_str] + row_strs

    try:
        out_file.write_text(
            json.dumps(json_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return jsonify({"ok": True, "path": str(out_file)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/delete-parsed-product", methods=["POST"])
def delete_parsed_product():
    """删除磁盘上的单个清洗产品 JSON 文件"""
    body = request.get_json(force=True, silent=True) or {}
    key  = (body.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "msg": "缺少 key"})

    out_file = _parsed_json_path(key)
    try:
        if out_file.exists():
            out_file.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/set-cookie", methods=["POST", "OPTIONS"])
def set_cookie():
    """篡改猴脚本调用此接口自动注入 Cookie"""
    # 处理 CORS 预检
    if request.method == "OPTIONS":
        resp = Response("", status=200)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp
    body = request.json or {}
    cookie_val = body.get("cookie", "").strip()
    if cookie_val:
        _cached_cookie["value"] = cookie_val
        return jsonify({"ok": True, "msg": f"Cookie 已接收，共 {len(cookie_val)} 字符"})
    return jsonify({"ok": False, "msg": "Cookie 为空"})


@app.route("/api/get-cookie", methods=["GET"])
def get_cookie():
    """前端页面轮询拿最新 Cookie"""
    return jsonify({"ok": True, "cookie": _cached_cookie["value"]})


# ============================================================
# 副表自动复制任务管理接口（供篡改猴脚本调用）
# ============================================================

@app.route("/api/add-tasks", methods=["POST", "OPTIONS"])
def add_tasks():
    """UI 批量添加复制任务"""
    if request.method == "OPTIONS":
        return Response("", status=200)
    body = request.json or {}
    names = body.get("names", [])
    added = 0
    with _task_lock:
        for name in names:
            name = name.strip()
            if name:
                _task_list.append({"id": str(uuid.uuid4())[:8], "name": name, "status": "pending"})
                added += 1
    return jsonify({"ok": True, "added": added, "total": len(_task_list)})


@app.route("/api/task-list", methods=["GET"])
def task_list_api():
    """获取全部任务状态"""
    with _task_lock:
        return jsonify({"ok": True, "tasks": list(_task_list)})


@app.route("/api/get-task", methods=["GET", "OPTIONS"])
def get_task():
    """篡改猴脚本轮询：获取下一个 pending 任务"""
    if request.method == "OPTIONS":
        return Response("", status=200)
    with _task_lock:
        for task in _task_list:
            if task["status"] == "pending":
                return jsonify({"ok": True, "task": task})
    return jsonify({"ok": True, "task": None})


@app.route("/api/complete-task", methods=["POST", "OPTIONS"])
def complete_task():
    """篡改猴脚本回报：任务完成"""
    if request.method == "OPTIONS":
        return Response("", status=200)
    body = request.json or {}
    task_id = body.get("id", "")
    with _task_lock:
        for task in _task_list:
            if task["id"] == task_id:
                task["status"] = "done"
                return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "任务不存在"})


@app.route("/api/clear-tasks", methods=["POST", "OPTIONS"])
def clear_tasks():
    """清空任务列表"""
    if request.method == "OPTIONS":
        return Response("", status=200)
    with _task_lock:
        _task_list.clear()
    return jsonify({"ok": True})


@app.route("/api/right-click", methods=["POST", "OPTIONS"])
def do_right_click():
    """
    用 PyAutoGUI 执行真实系统级右键点击。
    篡改猴脚本把屏幕坐标发过来，这里触发 OS-level 鼠标事件，
    浏览器收到的是可信事件（isTrusted=true），飞书菜单正常弹出。
    """
    if request.method == "OPTIONS":
        return Response("", status=200)
    body = request.json or {}
    x = int(body.get("x", 0))
    y = int(body.get("y", 0))
    try:
        import pyautogui
        pyautogui.rightClick(x, y)
        return jsonify({"ok": True})
    except ImportError:
        return jsonify({"ok": False, "msg": "pyautogui 未安装，请运行: pip3 install pyautogui"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/launch-edge", methods=["POST", "OPTIONS"])
def launch_edge():
    """启动 Edge 调试模式，供 Playwright CDP 连接（不占鼠标）"""
    if request.method == "OPTIONS":
        return Response("", status=200)
    import platform
    system = platform.system()
    body = request.json or {}
    feishu_url = body.get("url", "")
    # 独立的用户数据目录，避免被已运行的 Edge 实例接管
    debug_profile = str(Path.home() / ".edge-debug-profile")
    try:
        if system == "Darwin":
            edge_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
            if not Path(edge_path).exists():
                return jsonify({"ok": False, "msg": "未找到 Microsoft Edge，请手动启动"})
            cmd = [edge_path,
                   "--remote-debugging-port=9222",
                   "--no-first-run",
                   f"--user-data-dir={debug_profile}"]
            if feishu_url:
                cmd.append(feishu_url)
            subprocess.Popen(cmd)
        elif system == "Windows":
            edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
            cmd = [edge_path,
                   "--remote-debugging-port=9222",
                   "--no-first-run",
                   f"--user-data-dir={debug_profile}"]
            if feishu_url:
                cmd.append(feishu_url)
            subprocess.Popen(cmd)
        else:
            return jsonify({"ok": False, "msg": f"不支持的系统：{system}"})
        tip = "并已自动打开飞书多维表格，稍等页面加载完再点「一键执行」" if feishu_url else "请在调试版 Edge 里打开飞书多维表格"
        return jsonify({"ok": True, "msg": f"Edge 已启动（调试端口 9222），{tip}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/cdp-copy", methods=["POST", "OPTIONS"])
def cdp_copy():
    """一键全流程：自动加队列 → 自动启动Edge → 自动执行复制，完全不占鼠标"""
    if request.method == "OPTIONS":
        return Response("", status=200)

    body       = request.json or {}
    names      = body.get("names", [])   # 前端直接传名称列表
    base_token = body.get("baseToken", DEFAULT_BASE_TOKEN)
    feishu_url = f"https://my.feishu.cn/base/{base_token}" if base_token else ""

    # 如果前端传来了名称，先加入队列
    if names:
        import uuid
        with _task_lock:
            for n in names:
                n = n.strip()
                if n:
                    _task_list.append({"id": uuid.uuid4().hex[:8], "name": n, "status": "pending"})

    def generate():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            yield f"data: {json.dumps({'msg': '❌ playwright 未安装，请运行: pip3 install playwright && playwright install chromium'})}\n\n"
            yield "data: __DONE__\n\n"
            return

        import platform, time
        system = platform.system()
        debug_profile = str(Path.home() / ".edge-debug-profile")

        # ① 尝试连接，连不上就自动启动 Edge
        yield f"data: {json.dumps({'msg': '🔌 正在连接 Edge 调试端口...'})}\n\n"

        try:
            browser = None
            with sync_playwright() as p:
                for attempt in range(1, 4):
                    try:
                        browser = p.chromium.connect_over_cdp("http://localhost:9222")
                        break
                    except Exception:
                        if attempt == 1:
                            yield f"data: {json.dumps({'msg': '🚀 Edge 未启动，正在自动启动...'})}\n\n"
                            try:
                                if system == "Darwin":
                                    edge_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
                                    cmd = [edge_path, "--remote-debugging-port=9222",
                                           "--no-first-run", f"--user-data-dir={debug_profile}"]
                                    if feishu_url:
                                        cmd.append(feishu_url)
                                    subprocess.Popen(cmd)
                                elif system == "Windows":
                                    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
                                    cmd = [edge_path, "--remote-debugging-port=9222",
                                           "--no-first-run", f"--user-data-dir={debug_profile}"]
                                    if feishu_url:
                                        cmd.append(feishu_url)
                                    subprocess.Popen(cmd)
                            except Exception as e2:
                                yield f"data: {json.dumps({'msg': '❌ 启动 Edge 失败：' + str(e2)[:100]})}\n\n"
                                yield "data: __DONE__\n\n"
                                return
                        wait = attempt * 4
                        yield f"data: {json.dumps({'msg': f'⏳ 等待 Edge 启动... ({wait}s)'})}\n\n"
                        time.sleep(wait)

                if browser is None:
                    yield f"data: {json.dumps({'msg': '❌ 无法连接 Edge，请手动点击「启动 Edge 调试模式」后重试'})}\n\n"
                    yield "data: __DONE__\n\n"
                    return

                # ② 找飞书多维表格页面，找不到就等待加载
                yield f"data: {json.dumps({'msg': '🔍 正在查找飞书多维表格页面...'})}\n\n"
                page = None
                for _ in range(10):
                    for context in browser.contexts:
                        for pg in context.pages:
                            if "feishu.cn/base/" in pg.url:
                                page = pg
                                break
                        if page:
                            break
                    if page:
                        break
                    time.sleep(2)

                if not page:
                    yield f"data: {json.dumps({'msg': '❌ 未找到飞书多维表格页面，请在 Edge 中打开 my.feishu.cn/base/...'})}\n\n"
                    yield "data: __DONE__\n\n"
                    browser.close()
                    return

                yield f"data: {json.dumps({'msg': '✅ 找到页面：' + page.url[:80]})}\n\n"

                # 等页面加载完毕
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

                # ③ 获取待执行任务
                with _task_lock:
                    pending = [t.copy() for t in _task_list if t["status"] == "pending"]

                if not pending:
                    yield f"data: {json.dumps({'msg': '⚠️ 没有待执行的任务，请先粘贴副表名称'})}\n\n"
                    yield "data: __DONE__\n\n"
                    browser.close()
                    return

                yield f"data: {json.dumps({'msg': f'📋 共 {len(pending)} 个任务，开始执行...'})}\n\n"

                for task in pending:
                    task_name = task['name']
                    task_id   = task['id']
                    try:
                        yield f"data: {json.dumps({'msg': '⏳ 执行：' + task_name})}\n\n"

                        # 右键当前激活（高亮）的副表标签
                        active_tab = page.locator('.bitable-new-table-item.bitable-new-table-item__active')
                        active_tab.click(button='right')

                        # 等菜单并点"复制数据表"
                        copy_item = page.locator('li.b-menu__item').filter(has_text='复制数据表').first
                        copy_item.click(timeout=6000)

                        # 等输入框、填名称
                        inp = page.locator('input[placeholder="请输入数据表名称"]').first
                        inp.wait_for(timeout=8000)
                        inp.fill(task_name)

                        # 点确认
                        confirm_btn = page.locator('.ud__portal button').filter(has_text='复制').first
                        confirm_btn.click(timeout=5000)

                        # 等待复制完成
                        page.wait_for_timeout(2500)

                        # 标记完成
                        with _task_lock:
                            for t in _task_list:
                                if t['id'] == task_id:
                                    t['status'] = 'done'

                        yield f"data: {json.dumps({'msg': '✅ 完成：' + task_name})}\n\n"

                    except Exception as e:
                        err = str(e)[:150]
                        yield f"data: {json.dumps({'msg': '❌ 失败：' + task_name + ' → ' + err})}\n\n"
                        # 关掉可能残留的菜单/弹窗
                        try:
                            page.keyboard.press('Escape')
                            page.wait_for_timeout(500)
                        except Exception:
                            pass

                yield f"data: {json.dumps({'msg': '🎉 所有任务执行完毕！'})}\n\n"
                browser.close()

        except Exception as e:
            yield f"data: {json.dumps({'msg': f'❌ 系统错误：{str(e)[:200]}'})}\n\n"

        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route("/api/tables", methods=["POST"])
def get_tables():
    body       = request.json
    cookies    = parse_cookie_str(body.get("cookie", ""))
    base_token = body.get("baseToken", DEFAULT_BASE_TOKEN)
    main_tid   = body.get("mainTableId", "tbl4VYxV0caXJEEn")

    if not cookies:
        return jsonify({"ok": False, "msg": "Cookie 不能为空"})
    try:
        data = fetch_clientvars(base_token, main_tid, cookies,
                                record_limit=1, need_base=True)
        if data.get("code") != 0:
            return jsonify({"ok": False, "msg": f"飞书接口错误: {data.get('msg', data)}"})
        block_infos = decode_gzip_b64(data["data"]["base"]).get("blockInfos", {})
        tables = [
            {"id": bid, "name": info.get("name", "")}
            for bid, info in block_infos.items()
            if bid.startswith("tbl") and info.get("name", "")
               and not _should_skip_table(info.get("name", ""))
        ]
        return jsonify({"ok": True, "tables": tables})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/tables-multi", methods=["POST"])
def get_tables_multi():
    """一次拉取多个 Base 的副表列表，按 Base 分组返回"""
    body        = request.json or {}
    cookies     = parse_cookie_str(body.get("cookie", ""))
    base_tokens = [t.strip() for t in (body.get("baseTokens") or []) if t and t.strip()]
    main_tid    = body.get("mainTableId", "tbl4VYxV0caXJEEn")

    if not cookies:
        return jsonify({"ok": False, "msg": "Cookie 不能为空"})
    if not base_tokens:
        return jsonify({"ok": False, "msg": "至少需要 1 个 Base Token"})

    results = []
    for bt in base_tokens:
        try:
            data = fetch_clientvars(bt, main_tid, cookies,
                                    record_limit=1, need_base=True)
            if data.get("code") != 0:
                results.append({"baseToken": bt, "ok": False,
                                "msg": f"飞书接口错误: {data.get('msg', data)}",
                                "tables": []})
                continue
            block_infos = decode_gzip_b64(data["data"]["base"]).get("blockInfos", {})
            tables = [
                {"id": bid, "name": info.get("name", "")}
                for bid, info in block_infos.items()
                if bid.startswith("tbl") and info.get("name", "")
                   and not _should_skip_table(info.get("name", ""))
            ]
            results.append({"baseToken": bt, "ok": True, "tables": tables})
        except Exception as e:
            results.append({"baseToken": bt, "ok": False,
                            "msg": str(e)[:200], "tables": []})
    return jsonify({"ok": True, "results": results})


def _download_one_base(tag, base_token, field_id, tables, cookies,
                       output_dir, force_redown, min_size_bytes, emit,
                       make_zip=True, delete_after_zip=False):
    """
    单 Base 串行下载所有副表。
    emit(msg) 把消息推到外面的 queue（带 tag 前缀）。
    make_zip: 整个副表下完后是否在第一层目录下生成 {date_product}.zip
    delete_after_zip: 打包成功后是否删除原文件夹（仅 make_zip=True 时生效）
    返回 stats dict：{new, skipped, repaired, zipped}
    """
    import zipfile, shutil
    stats = {"new": 0, "skipped": 0, "repaired": 0, "zipped": 0}
    for idx, table in enumerate(tables):
        name           = table["name"]
        tid            = table["id"]
        date, group, product = parse_table_name3(name)
        emit(f"\n📂 {tag} [{idx+1}/{len(tables)}] {name}")
        emit(f"   {tag} → 保存到: {safe_name(name)}/")
        try:
            data = fetch_clientvars(base_token, tid, cookies,
                                    record_limit=2000, need_base=False, timeout=60)
            if data.get("code") != 0:
                emit(f"   {tag} ❌ 接口错误: {data.get('msg')}")
                continue
            table_json = decode_gzip_b64(data["data"]["table"])
            diag = diagnose_table_json(table_json, field_id)
            emit(f"   {tag} 🔎 诊断: rankMap={diag['rank_total']} / recordMap={diag['record_total']}"
                 f" / 有视频={diag['has_video']} / 空={diag['empty_val']} / 格式异常={diag['no_video']}")
            if diag["missing_in_record"] > 0:
                emit(f"   {tag} ⚠️  飞书按需加载未返回完整数据：{diag['missing_in_record']} 条记录缺失")
            records = extract_videos_sorted(table_json, field_id)
            emit(f"   {tag} 共 {len(records)} 条视频")
            if not records:
                emit(f"   {tag} ⚠️  该副表暂无视频数据，跳过")
                continue
            # 文件夹1：任务日期；文件夹2：副表全名_zip；文件名前缀：副表全名
            date_folder = safe_name(date) if date else "未知日期"
            sub_folder  = safe_name(name) + "_zip"
            save_dir = output_dir / date_folder / sub_folder
            save_dir.mkdir(parents=True, exist_ok=True)
            prefix = safe_name(name)  # 文件名前缀 = 副表全名

            stat_complete = stat_broken = stat_missing = 0
            for i in range(1, len(records) + 1):
                fpath = save_dir / f"{prefix}_{i}.mp4"
                if not fpath.exists():
                    stat_missing += 1
                elif fpath.stat().st_size < min_size_bytes:
                    stat_broken += 1
                else:
                    stat_complete += 1
            emit(f"   {tag} 📋 本地状态: 完整={stat_complete} / 残缺={stat_broken} / 缺失={stat_missing}"
                 + ("  → 强制覆盖全部" if force_redown else ""))

            for i, rec in enumerate(records, 1):
                fname = f"{prefix}_{i}.mp4"
                fpath = save_dir / fname

                if fpath.exists() and not force_redown:
                    sz = fpath.stat().st_size
                    if sz >= min_size_bytes:
                        emit(f"   {tag} ⏭  已完整: {fname} ({sz/1024/1024:.1f} MB)")
                        stats["skipped"] += 1
                        continue
                    else:
                        emit(f"   {tag} 🔧 残缺重下: {fname} (本地 {sz/1024:.1f} KB < 阈值)")
                        try: fpath.unlink()
                        except Exception: pass
                        is_repair = True
                else:
                    is_repair = False
                    if fpath.exists():
                        try: fpath.unlink()
                        except Exception: pass
                        emit(f"   {tag} 🔄 覆盖重下: {fname}")
                    else:
                        emit(f"   {tag} ⬇  {fname}")

                try:
                    size = _curl_download(rec["url"], str(fpath), timeout=120)
                    mb = size / 1024 / 1024
                    if size < min_size_bytes:
                        emit(f"   {tag} ⚠️  {fname} 下完只有 {size/1024:.1f} KB（< 阈值），可能仍然异常")
                    else:
                        emit(f"   {tag} ✅ {fname}  ({mb:.1f} MB)")
                    stats["new"] += 1
                    if is_repair:
                        stats["repaired"] += 1
                except Exception as e:
                    emit(f"   {tag} ❌ 下载失败: {e}")

            # ── 副表全部下完：可选打包 zip / 删除原文件夹 ──
            if make_zip:
                try:
                    mp4_files = sorted(save_dir.glob("*.mp4"))
                    if not mp4_files:
                        emit(f"   {tag} ⚠️  没有 mp4 可打包，跳过 zip")
                    else:
                        zip_path = output_dir / date_folder / f"{safe_name(name)}.zip"
                        if zip_path.exists():
                            try: zip_path.unlink()
                            except Exception: pass
                        total_bytes = 0
                        with zipfile.ZipFile(zip_path, "w",
                                             compression=zipfile.ZIP_STORED) as zf:
                            for mp4 in mp4_files:
                                zf.write(mp4, arcname=mp4.name)
                                total_bytes += mp4.stat().st_size
                        zip_mb = zip_path.stat().st_size / 1024 / 1024
                        emit(f"   {tag} 📦 已打包: {zip_path.name}  "
                             f"({len(mp4_files)} 个 / {zip_mb:.1f} MB)")
                        stats["zipped"] += 1
                        if delete_after_zip:
                            try:
                                shutil.rmtree(save_dir)
                                emit(f"   {tag} 🗑  已删除原文件夹: {save_dir.name}/")
                            except Exception as _e:
                                emit(f"   {tag} ⚠️  删除原文件夹失败: {_e}")
                except Exception as e:
                    emit(f"   {tag} ❌ 打包 zip 失败: {e}")
        except Exception as e:
            emit(f"   {tag} ❌ 出错: {e}")
    return stats


@app.route("/api/download", methods=["POST"])
def download_videos():
    """SSE 流式推送下载进度（多 Base 并行 + 单 Base 内串行）"""
    body            = request.json or {}
    cookies         = parse_cookie_str(body.get("cookie", ""))
    output_dir      = Path(body.get("outputDir", DEFAULT_OUTPUT_DIR))
    default_field   = (body.get("defaultFieldId") or body.get("fieldId")
                       or DEFAULT_FIELD_ID).strip()
    force_redown    = bool(body.get("forceRedownload", False))
    min_size_bytes  = max(0, int(body.get("minSizeKb", 50))) * 1024
    make_zip        = bool(body.get("makeZip", True))
    delete_after_zip = bool(body.get("deleteAfterZip", False))

    # 兼容新版 bases 数组 + 旧版单 base 平铺字段
    bases = body.get("bases") or []
    if not bases:
        bases = [{
            "baseToken": body.get("baseToken", DEFAULT_BASE_TOKEN),
            "fieldId":   body.get("fieldId", DEFAULT_FIELD_ID),
            "tables":    body.get("tables", []),
        }]
    # 规整 + 过滤空桶
    cleaned = []
    for b in bases:
        bt = (b.get("baseToken") or "").strip()
        ts = b.get("tables") or []
        if not bt or not ts:
            continue
        cleaned.append({
            "baseToken": bt,
            "fieldId":  (b.get("fieldId") or "").strip() or default_field,
            "tables":   ts,
        })
    bases = cleaned

    def generate():
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor

        def send(msg):
            return f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"

        strategy = ("🔄 强制重新下载（覆盖已存在）" if force_redown
                    else f"📊 增量补齐模式（残缺阈值 {min_size_bytes // 1024} KB）")
        zip_strategy = ("📦 下完自动打包 zip" +
                        ("（打包后删除原文件夹）" if delete_after_zip else "（保留原文件夹）")
                        ) if make_zip else "📁 仅下载文件夹（不打包）"
        yield send(f"⚙️  策略: {strategy}")
        yield send(f"⚙️  打包: {zip_strategy}")
        if not bases:
            yield send("❌ 未选择任何副表，停止"); yield "data: __DONE__\n\n"; return

        def _base_tag(idx, b):
            nm = (b.get("name") or "").strip()
            return f"[Base #{idx+1} · {nm}]" if nm else f"[Base #{idx+1}]"

        yield send(f"🧱 共 {len(bases)} 个 Base 并行下载（单 Base 内串行）")
        for i, b in enumerate(bases, 1):
            yield send(f"  {_base_tag(i-1, b)} {b['baseToken'][:14]}...  fieldId={b['fieldId']}  副表={len(b['tables'])} 个")

        q = _queue.Queue()
        SENTINEL = object()

        def worker(idx, b):
            tag = _base_tag(idx, b)
            try:
                stats = _download_one_base(
                    tag, b["baseToken"], b["fieldId"], b["tables"], cookies,
                    output_dir, force_redown, min_size_bytes,
                    lambda m: q.put(m),
                    make_zip=make_zip, delete_after_zip=delete_after_zip)
                zip_tip = f"，打包 {stats.get('zipped', 0)} 个 zip" if make_zip else ""
                q.put(f"\n✅ {tag} 完成：新下载 {stats['new']}（含残缺修复 {stats['repaired']}），跳过 {stats['skipped']}{zip_tip}")
                return stats
            except Exception as e:
                q.put(f"\n❌ {tag} 异常退出：{str(e)[:200]}")
                return {"new": 0, "skipped": 0, "repaired": 0, "zipped": 0}
            finally:
                q.put(SENTINEL)

        with ThreadPoolExecutor(max_workers=len(bases)) as ex:
            futures = [ex.submit(worker, i, b) for i, b in enumerate(bases)]
            done_count = 0
            totals = {"new": 0, "skipped": 0, "repaired": 0, "zipped": 0}
            while done_count < len(bases):
                msg = q.get()
                if msg is SENTINEL:
                    done_count += 1
                    continue
                yield send(msg)
            for fut in futures:
                try:
                    s = fut.result()
                    for k in totals: totals[k] += s.get(k, 0)
                except Exception:
                    pass

        zip_summary = f"，共打包 {totals['zipped']} 个 zip" if make_zip else ""
        yield send(f"\n🎉 全部完成！新下载 {totals['new']} 个"
                   f"（其中残缺修复 {totals['repaired']} 个），跳过已完整 {totals['skipped']} 个{zip_summary}")
        yield send(f"   保存位置: {output_dir.resolve()}")
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()),
                    content_type="text/event-stream")



# ============================================================
# Excel 清洗路由
# ============================================================
@app.route("/api/parse-excel", methods=["POST"])
def parse_excel_route():
    """
    手动模式：接收上传的 Excel + 用户提供的批次日期。
    只扫描所有数据 sheet 的产品段落，不读需求汇总表，不扩展。
    返回每个产品的原始模版与表头，供前端展示给用户手动指定数量后再调 /api/expand-product。
    """
    import tempfile, os, traceback, time as _time, sys as _sys
    _t_route = _time.time()
    if 'file' not in request.files:
        return jsonify({"ok": False, "msg": "没有收到文件"})
    file = request.files['file']
    if not file.filename.lower().endswith('.xlsx'):
        return jsonify({"ok": False, "msg": "请上传 .xlsx 格式的文件"})

    batch_date = (request.form.get('batch_date') or '').strip()
    if not batch_date:
        return jsonify({"ok": False, "msg": "请先填写批次日期（如 5.21.）"})

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        try:
            _size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        except Exception:
            _size_mb = -1
        print(f"[parse-excel] 收到上传：{file.filename}  ({_size_mb:.2f} MB)  批次={batch_date}  → {tmp_path}", flush=True)

        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from excel_parser import scan_products

        _t_parse = _time.time()
        raw_sections = scan_products(tmp_path)  # [{sheet, title, product_name, header_str, col_count, templates, template_count}]
        print(f"[parse-excel] scan_products 完成：{len(raw_sections)} 个段落  耗时={_time.time()-_t_parse:.1f}s", flush=True)

        if not raw_sections:
            try:
                import openpyxl as _opx
                _wb = _opx.load_workbook(tmp_path, data_only=True)
                _sheets = [s for s in _wb.sheetnames if s != 'ai数量需求汇总']
                _diag = (f"📂 工作簿数据 Sheet 列表：{_sheets or '(空)'}\n"
                         f"💡 任何 Sheet 内都没有扫到合法的产品段落，请检查 Excel 结构（产品标题行 + 表头行 + 数据行）")
                return jsonify({"ok": False,
                                "msg": "未扫描到任何产品段落，请检查 Excel 结构",
                                "detail": _diag})
            except Exception as _de:
                return jsonify({"ok": False,
                                "msg": "未扫描到任何产品段落，且诊断失败",
                                "detail": str(_de)[:400]})

        parsed = []
        for sec in raw_sections:
            sheet_name   = sec['sheet']
            title        = sec['title']
            product_name = sec['product_name']
            header_str   = sec['header_str']
            headers      = [h.strip() for h in header_str.split(' // ')]
            file_key     = f'{batch_date}_{sheet_name}_{product_name}'
            parsed.append({
                "key":            file_key,
                "title":          title,
                "product_name":   product_name,
                "sheet":          sheet_name,
                "batch_date":     batch_date,
                "headers":        headers,
                "header_str":     header_str,
                "templates":      sec['templates'],
                "template_count": sec['template_count'],
                "rows":           [],
                "count":          0,
            })

        print(f"[parse-excel] 扫描完成：{len(parsed)} 个产品  总耗时={_time.time()-_t_route:.1f}s", flush=True)
        return jsonify({"ok": True, "results": parsed, "total": len(parsed),
                        "batch_date": batch_date,
                        # 字段保留以兼容旧前端代码
                        "auto_fixed_total": 0, "auto_fixed_summary": [],
                        "ratio_fixed_total": 0, "ratio_fixed_summary": []})
    except Exception as e:
        _tb = traceback.format_exc()
        print(f"[parse-excel] ❌ 异常（耗时={_time.time()-_t_route:.1f}s）：{e}\n{_tb}", file=_sys.stderr, flush=True)
        return jsonify({"ok": False, "msg": str(e), "detail": _tb[:2000]})
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@app.route("/api/expand-product", methods=["POST"])
def expand_product_route():
    """
    单产品扩展：前端在用户为某产品填入数量后调用本接口。
    入参 JSON：
      {
        templates: [str],      # 原始模版行
        header_str: str,       # 表头
        count: int,            # 用户指定的目标条数
        batch_date: str,       # 批次日期（落地目录用）
        sheet: str,            # 工作表名（文件名用）
        title: str,            # 产品标题（含括号），文件名取去括号版
      }
    返回 { ok, file_key, rows: [[col, ...]], headers, count, auto_fixed, ratio_fixed }
    """
    import traceback, sys as _sys
    try:
        data = request.get_json(silent=True) or {}
        templates  = data.get('templates') or []
        header_str = (data.get('header_str') or '').strip()
        count      = int(data.get('count') or 0)
        batch_date = (data.get('batch_date') or '').strip()
        sheet_name = (data.get('sheet') or '').strip()
        title      = (data.get('title') or '').strip()
        if not templates or not header_str or count <= 0 or not batch_date or not sheet_name or not title:
            return jsonify({"ok": False, "msg": "参数缺失（templates/header_str/count/batch_date/sheet/title 必填，且 count>0）"})

        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from excel_parser import expand_product

        result = expand_product(templates, header_str, count, batch_date, sheet_name, title)
        json_data = result['rows']  # [title, header_str, row1, row2, ...]
        headers = [h.strip() for h in header_str.split(' // ')]
        parsed_rows = []
        for row in json_data[2:]:
            cols = [c.strip() for c in row.split(' // ')]
            while len(cols) < len(headers):
                cols.append('/')
            parsed_rows.append(cols[:len(headers)])

        # 后处理：台词/比例修复（与旧 parse 路径保持一致）
        fixed_n, fix_details = _auto_fix_script_rows(headers, parsed_rows)
        product_obj = {"title": title, "rows": parsed_rows}
        r_fixed, r_details = _auto_fix_ratios_product(product_obj)
        title_fixed = product_obj["title"]

        if fixed_n or r_fixed:
            # 修复后回写磁盘，保证 JSON 文件与前端展示一致
            try:
                from pathlib import Path as _P
                import json as _json
                out_path = _P(result['out_path'])
                new_rows = [' // '.join(c for c in row) for row in parsed_rows]
                _data = [title_fixed, header_str] + new_rows
                with open(out_path, 'w', encoding='utf-8') as _f:
                    _json.dump(_data, _f, ensure_ascii=False, indent=2)
            except Exception as _we:
                print(f"[expand-product] 回写修复结果失败：{_we}", file=_sys.stderr, flush=True)

        return jsonify({
            "ok": True,
            "file_key":    result['file_key'],
            "title":       title_fixed,
            "headers":     headers,
            "header_str":  header_str,
            "rows":        parsed_rows,
            "count":       len(parsed_rows),
            "auto_fixed":  fixed_n,
            "ratio_fixed": r_fixed,
            "auto_fixed_summary":  fix_details[:5],
            "ratio_fixed_summary": r_details[:5],
        })
    except Exception as e:
        _tb = traceback.format_exc()
        print(f"[expand-product] ❌ 异常：{e}\n{_tb}", file=_sys.stderr, flush=True)
        return jsonify({"ok": False, "msg": str(e), "detail": _tb[:2000]})


# ============================================================
# 全流程自动化路由
# ============================================================
def _call_coze_sync(token: str, workflow_id: str, input_data: list, name: str,
                    timeout: int = 15) -> dict:
    """
    触发 Coze 工作流（fire-and-forget）。
    只要请求发出、Coze 服务端收到即视为成功，不等工作流执行结果。
    超时 15 秒：足够建连 + 发送请求体，到时间直接返回成功。
    retries=0：绝不重复触发。
    """
    url = 'https://api.coze.cn/v1/workflow/run'
    body = {
        'workflow_id': workflow_id,
        'parameters': {
            'input': input_data,
            'name':  name,
        }
    }
    headers = {'Authorization': f'Bearer {token}'}
    try:
        return _curl_post(url, body, headers=headers, timeout=timeout, retries=0)
    except RuntimeError:
        # curl 超时 = 请求已发出，Coze 正在执行中，视为成功
        return {"code": 0, "msg": "已触发"}


def _next_subtable_candidate(name: str) -> str:
    """末尾 _数字 → 自增；否则追加 _2。用于副表撞名时换名重试。"""
    m = re.match(r'^(.*)_(\d+)$', name)
    if m:
        return f'{m.group(1)}_{int(m.group(2)) + 1}'
    return f'{name}_2'


# ─── 本地副表名记录（按 base_token 分桶）─────────────────────
_SUBTABLE_RECORD_PATH = Path("data/created_subtables.json")
_subtable_record_lock = threading.Lock()


def _load_subtable_records() -> dict:
    """读取本地副表记录：{ base_token: [names] }"""
    try:
        if _SUBTABLE_RECORD_PATH.exists():
            return json.loads(_SUBTABLE_RECORD_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _record_created_subtable(base_token: str, name: str):
    """追加一条 (base_token, name) 到本地记录。"""
    if not base_token or not name:
        return
    with _subtable_record_lock:
        data = _load_subtable_records()
        bucket = set(data.get(base_token, []))
        if name in bucket:
            return
        bucket.add(name)
        data[base_token] = sorted(bucket)
        try:
            _SUBTABLE_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SUBTABLE_RECORD_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[subtable_record] 保存失败: {e}")


def _resolve_unique_subtable_name(base_token: str, requested: str) -> str:
    """据本地记录预先 bump 名字到未占用候选。无 base_token 则原样返回。"""
    if not base_token or not requested:
        return requested
    used = set(_load_subtable_records().get(base_token, []))
    candidate = requested
    while candidate in used:
        candidate = _next_subtable_candidate(candidate)
    return candidate


def _cdp_copy_one_subtable(page, requested_name: str, max_retries: int = 30,
                           base_token: str = "", on_dup_found=None) -> str:
    """fill 名字 → 等校验 → 检测"已存在"红字 → 自动递增重试 → 点确认。
    返回实际成功使用的名字（可能与 requested 不同）。失败抛 RuntimeError。
    前置：调用方已触发右键 → "复制数据表"，弹窗已弹出。
    on_dup_found(name): 检测到某候选名已存在时回调（用于自我学习记录已占用名）。
    """
    inp = page.locator('input[placeholder="请输入数据表名称"]').first
    inp.wait_for(timeout=8000)
    # "复制"按钮：飞书弹窗里仅有「取消 / 复制」两个按钮，按文字精准定位
    confirm_btn = page.get_by_role('button', name='复制').last
    dialog = page.locator('div.semi-modal-content, div[role="dialog"]').filter(
        has_text='复制数据表'
    ).last

    def _dup_visible() -> bool:
        """扫页面所有"已存在"文本节点，命中可见的则判定撞名。"""
        try:
            for loc in page.get_by_text('已存在').all():
                try:
                    if loc.is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _poll_dup(total_ms: int = 1800, step_ms: int = 200) -> bool:
        """轮询检测撞名：红字出现或按钮禁用就立即返回 True。"""
        waited = 0
        while waited < total_ms:
            if _dup_visible():
                return True
            try:
                if confirm_btn.is_disabled():
                    return True
            except Exception:
                pass
            page.wait_for_timeout(step_ms)
            waited += step_ms
        return False

    candidate = requested_name
    print(f"[dedup] 起始候选名: {candidate}")
    for attempt in range(max_retries):
        # 清空 + 重新填，强制触发 React onChange 校验
        try:
            inp.click(timeout=3000)
            inp.fill('')
            page.wait_for_timeout(150)
            inp.fill(candidate)
        except Exception as e:
            print(f"[dedup] fill 异常: {e}")
            raise

        # 轮询等到撞名信号或超时通过（1.8s 上限，飞书校验最慢约 1s）
        dup_found = _poll_dup(total_ms=1800, step_ms=200)
        print(f"[dedup] 尝试 #{attempt+1} candidate={candidate} dup={dup_found}")

        if not dup_found:
            try:
                confirm_btn.click(timeout=5000)
            except Exception as e:
                print(f"[dedup] 点击复制按钮失败: {e}")
                raise
            # 点击后等弹窗消失；如 1.5s 后还在 → 仍是撞名兜底
            try:
                dialog.wait_for(state='detached', timeout=3000)
                print(f"[dedup] ✅ 创建成功: {candidate}")
                page.wait_for_timeout(1500)  # 等飞书侧栏刷新
                return candidate
            except Exception:
                pass
            if _dup_visible() or dialog.is_visible():
                print(f"[dedup] click 后弹窗仍在/红字出现 → 视为撞名: {candidate}")
                dup_found = True

        if dup_found:
            # 撞名 → 自我学习这个名字已被占用 → 换下一个候选
            if on_dup_found:
                try: on_dup_found(candidate)
                except Exception as e: print(f"[dedup] on_dup_found 回调异常: {e}")
            candidate = _next_subtable_candidate(candidate)
            print(f"[dedup] bump → {candidate}")

    raise RuntimeError(f'连续 {max_retries} 个候选名字都已存在')


def _cdp_create_tables(entries: list, base_token: str):
    """
    生成器：用 Playwright CDP 逐一复制飞书副表并重命名。
    entries: list[dict]，每个 dict 必须有 "key" 字段（要使用的副表名）。
            撞名时函数会自动改名，并把最终名字回写到 entry["key"]，
            供后续 Coze 调用拿到对的名字。
    yield: (ok: bool, msg: str)
    """
    import platform, time
    from playwright.sync_api import sync_playwright

    feishu_url  = f"https://my.feishu.cn/base/{base_token}" if base_token else ""
    debug_profile = str(Path.home() / ".edge-debug-profile")
    system = platform.system()

    browser = None
    with sync_playwright() as p:
        # 尝试连接，连不上就启动 Edge
        for attempt in range(1, 4):
            try:
                browser = p.chromium.connect_over_cdp("http://localhost:9222")
                break
            except Exception:
                if attempt == 1:
                    yield (False, "🚀 Edge 未启动，正在自动启动...")
                    try:
                        if system == "Darwin":
                            edge_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
                        else:
                            edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
                        cmd = [edge_path, "--remote-debugging-port=9222",
                               "--no-first-run", f"--user-data-dir={debug_profile}"]
                        if feishu_url:
                            cmd.append(feishu_url)
                        subprocess.Popen(cmd)
                    except Exception as e2:
                        yield (False, f"❌ 启动 Edge 失败：{str(e2)[:100]}")
                        return
                wait = attempt * 4
                yield (False, f"⏳ 等待 Edge 启动... ({wait}s)")
                time.sleep(wait)

        if browser is None:
            yield (False, "❌ 无法连接 Edge，请手动启动后重试")
            return

        # 找飞书多维表格页面
        page = None
        for _ in range(10):
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if "feishu.cn/base/" in pg.url:
                        page = pg
                        break
                if page:
                    break
            if page:
                break
            time.sleep(2)

        if not page:
            yield (False, "❌ 未找到飞书多维表格页面，请在 Edge 中打开")
            return

        yield (True, f"✅ 已连接飞书页面：{page.url[:80]}")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        for entry in entries:
            requested = entry['key']
            # 预先按本地记录避让已用名（避免每次都依赖 UI 检测）
            bumped = _resolve_unique_subtable_name(base_token, requested)
            if bumped != requested:
                yield (True, f"📒 本地记录显示 {requested} 已建过，预换为 {bumped}")
            try:
                active_tab = page.locator('.bitable-new-table-item.bitable-new-table-item__active')
                active_tab.click(button='right')
                copy_item = page.locator('li.b-menu__item').filter(has_text='复制数据表').first
                copy_item.click(timeout=6000)
                final_name = _cdp_copy_one_subtable(
                    page, bumped,
                    base_token=base_token,
                    on_dup_found=lambda n: _record_created_subtable(base_token, n),
                )
                if final_name != requested:
                    entry['key'] = final_name
                    yield (True, f"⚠️ 副表 {requested} 已存在，自动改名为 {final_name}")
                _record_created_subtable(base_token, final_name)
                yield (True, f"✅ 副表已创建：{final_name}")
            except Exception as e:
                yield (False, f"❌ 副表创建失败：{requested} → {str(e)[:120]}")
                try:
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(500)
                except Exception:
                    pass


def _cdp_create_tables_multi(buckets):
    """
    生成器：多 Base 串行处理。
    buckets: [(base_token, [entries]), ...]
            每个 entries 是 list[dict]，每个 dict 必须含 "key" 字段。
            撞名时会自动改名并把最终名字回写到 entry["key"]。
    yield: (ok: bool, msg: str)

    工作方式：
      1) 连接（或启动）Edge 调试浏览器
      2) 找到或新开一个飞书 Base 标签页
      3) 对每个 bucket：goto 到对应 Base URL → 等待加载 → 串行创建副表
    若同账号下多个 Base：URL 自动切换即可；
    若跨飞书账号：goto 后页面会停在登录页，会自动 yield 错误提示老板手动处理。
    """
    import platform, time
    from playwright.sync_api import sync_playwright

    if not buckets:
        return

    debug_profile = str(Path.home() / ".edge-debug-profile")
    system = platform.system()
    first_url = f"https://my.feishu.cn/base/{buckets[0][0]}" if buckets[0][0] else ""

    browser = None
    with sync_playwright() as p:
        for attempt in range(1, 4):
            try:
                browser = p.chromium.connect_over_cdp("http://localhost:9222")
                break
            except Exception:
                if attempt == 1:
                    yield (False, "🚀 Edge 未启动，正在自动启动...")
                    try:
                        if system == "Darwin":
                            edge_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
                        else:
                            edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
                        cmd = [edge_path, "--remote-debugging-port=9222",
                               "--no-first-run", f"--user-data-dir={debug_profile}"]
                        if first_url:
                            cmd.append(first_url)
                        subprocess.Popen(cmd)
                    except Exception as e2:
                        yield (False, f"❌ 启动 Edge 失败：{str(e2)[:100]}")
                        return
                wait = attempt * 4
                yield (False, f"⏳ 等待 Edge 启动... ({wait}s)")
                time.sleep(wait)

        if browser is None:
            yield (False, "❌ 无法连接 Edge，请手动启动后重试")
            return

        # 找一个已存在的飞书 base 页面作为工作 page；若没有就新开
        page = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "feishu.cn/base/" in pg.url:
                    page = pg
                    break
            if page:
                break
        if page is None:
            # 取第一个 context（即 default）新开 tab
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

        # 依次处理每个 bucket
        for b_idx, (base_token, entries) in enumerate(buckets, 1):
            if not base_token or not entries:
                yield (False, f"⏭️  桶 {b_idx} 跳过：base_token 或 entries 为空")
                continue
            target_url = f"https://my.feishu.cn/base/{base_token}"
            yield (True, f"🧭 [桶 {b_idx}/{len(buckets)}] 切换到 Base：{base_token[:12]}... ({len(entries)} 个副表)")
            try:
                page.bring_to_front()
                page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                yield (False, f"❌ 加载 Base 页面失败：{str(e)[:120]}")
                continue
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # 校验是否真的进了 base（跨账号场景这里会是登录页）
            cur_url = page.url
            if "feishu.cn/base/" not in cur_url:
                yield (False, f"❌ 当前不在飞书 Base 页面（疑似未登录或跨账号），停在：{cur_url[:120]}")
                yield (False, "👉 请在 Edge 中手动切换账号 / 完成登录，然后重新执行本桶")
                continue

            # 等"激活的副表"元素出现，最长 15s
            try:
                page.wait_for_selector('.bitable-new-table-item.bitable-new-table-item__active',
                                       timeout=15000)
            except Exception:
                yield (False, f"⚠️  未找到激活的副表元素，仍继续尝试...")

            yield (True, f"✅ 已就绪：{page.url[:80]}")

            for entry in entries:
                requested = entry['key']
                # 预先按本地记录避让已用名
                bumped = _resolve_unique_subtable_name(base_token, requested)
                if bumped != requested:
                    yield (True, f"📒 本地记录显示 {requested} 已建过，预换为 {bumped}")
                try:
                    active_tab = page.locator('.bitable-new-table-item.bitable-new-table-item__active')
                    active_tab.click(button='right')
                    copy_item = page.locator('li.b-menu__item').filter(has_text='复制数据表').first
                    copy_item.click(timeout=6000)
                    final_name = _cdp_copy_one_subtable(
                        page, bumped,
                        base_token=base_token,
                        on_dup_found=lambda n, _bt=base_token: _record_created_subtable(_bt, n),
                    )
                    if final_name != requested:
                        entry['key'] = final_name
                        yield (True, f"⚠️ 副表 {requested} 已存在，自动改名为 {final_name}")
                    _record_created_subtable(base_token, final_name)
                    yield (True, f"✅ 副表已创建：{final_name}")
                except Exception as e:
                    yield (False, f"❌ 副表创建失败：{requested} → {str(e)[:120]}")
                    try:
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(500)
                    except Exception:
                        pass


@app.route("/api/run-automation", methods=["POST"])
def run_automation():
    """
    全流程自动化 SSE 接口
    接收：multipart/form-data
      - file        : .xlsx 文件
      - cozeToken   : Coze 令牌
      - workflowId  : Coze 工作流 ID
      - baseToken   : 飞书 Base Token
    流程：清洗 → Coze 工作流 → 飞书建副表
    """
    import tempfile, os, traceback, time

    file        = request.files.get('file')
    coze_token  = request.form.get('cozeToken', '').strip()
    workflow_id = request.form.get('workflowId', '').strip()
    base_token  = request.form.get('baseToken', DEFAULT_BASE_TOKEN).strip()

    def send(msg):
        return f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"

    def generate():
        # ── 参数校验 ──────────────────────────────────────
        if not file or not file.filename.lower().endswith('.xlsx'):
            yield send("❌ 请上传 .xlsx 格式的 Excel 文件")
            yield "data: __DONE__\n\n"; return
        if not coze_token:
            yield send("❌ 请填写 Coze Token")
            yield "data: __DONE__\n\n"; return
        if not workflow_id:
            yield send("❌ 请填写 Coze Workflow ID")
            yield "data: __DONE__\n\n"; return

        # ── Step 1: 清洗 Excel ────────────────────────────
        yield send("📊 Step 1/3  正在清洗 Excel 数据...")
        tmp_path = None
        products = []   # [{key, title, json_data}]
        try:
            with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name
            import sys
            sys.path.insert(0, str(Path(__file__).parent))
            from excel_parser import parse_excel
            raw = parse_excel(tmp_path)
            for file_key, json_data in raw.items():
                products.append({"key": file_key, "title": json_data[0] if json_data else file_key, "data": json_data})
            yield send(f"✅ Excel 清洗完成：共 {len(products)} 个产品")
        except Exception as e:
            yield send(f"❌ Excel 清洗失败：{str(e)[:200]}")
            yield "data: __DONE__\n\n"; return
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except Exception: pass

        if not products:
            yield send("⚠️ 未解析到任何产品数据，请检查 Excel 格式")
            yield "data: __DONE__\n\n"; return

        # ── Step 2: 调用 Coze 工作流 ──────────────────────
        yield send(f"🤖 Step 2/3  正在逐个触发 Coze 工作流（共 {len(products)} 个）...")
        coze_ok, coze_fail = 0, 0
        for p in products:
            name     = p["key"]     # 日期_组名_产品名
            inp_data = p["data"]    # JSON 数组
            try:
                result = _call_coze_sync(coze_token, workflow_id, inp_data, name)
                code   = result.get('code', -1)
                if code == 0:
                    coze_ok += 1
                    yield send(f"  ✅ 工作流已启动：{name}")
                else:
                    coze_fail += 1
                    yield send(f"  ❌ 工作流失败：{name} → code={code} {result.get('msg','')[:80]}")
            except Exception as e:
                coze_fail += 1
                yield send(f"  ❌ 工作流请求异常：{name} → {str(e)[:100]}")
            time.sleep(0.5)   # 稍微限速，避免 Coze 限流

        yield send(f"✅ Coze 工作流触发完毕：成功 {coze_ok} 个，失败 {coze_fail} 个")

        # ── Step 3: 飞书 CDP 建副表 ───────────────────────
        yield send(f"🗂️  Step 3/3  正在飞书多维表格创建 {len(products)} 个副表...")
        try:
            for ok, msg in _cdp_create_tables(products, base_token):
                yield send(msg)
        except Exception as e:
            yield send(f"❌ CDP 执行异常：{str(e)[:200]}")

        yield send("🎉 全流程自动化完成！")
        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route("/api/run-coze-cdp-makeup", methods=["POST"])
def run_coze_cdp_makeup():
    """
    🩹 补视频接口（SSE）：
    接收 JSON:
      makeups  : [{key, count, accountIndex}]
      accounts : [{name, cozeToken, workflowId, baseToken,
                   feishuUserToken, feishuAppId, feishuAppSecret}, ...]

    流程：
      1) 对每个 makeup 入口：读 data/input/parsed/{date}/{key}.json
         → 用 _random_combine 重组 count 行新数据
         → 副表名 补{date}_{group}_{product}，重名自动 _2/_3
         → 写新 JSON 到磁盘
         → 按 accountIndex 归桶
      2) 复用 CDP 建副表 + Coze 并发 + 飞书台账写入
    """
    from excel_parser import _random_combine

    body     = request.json or {}
    makeups  = body.get("makeups", []) or []
    accounts = body.get("accounts", []) or []

    for a in accounts:
        for k_ in list(a.keys()):
            if isinstance(a[k_], str):
                a[k_] = a[k_].strip()

    def send(msg):
        return f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"

    def _acc_tag(i):
        a = accounts[i] if 0 <= i < len(accounts) else {}
        nm = (a.get("name") or "").strip()
        return f"#{i+1}" + (f"·{nm}" if nm else "")

    def _build_makeup_key(date: str, group: str, product: str) -> str:
        """补{date}_{group}_{product}，文件已存在则 _2/_3 递增"""
        base = f"补{date}_{group}_{product}"
        folder = Path("data/input/parsed") / date
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / f"{base}.json").exists():
            return base
        n = 2
        while (folder / f"{base}_{n}.json").exists():
            n += 1
        return f"{base}_{n}"

    def generate():
        if not makeups:
            yield send("❌ 没有要补的产品"); yield "data: __DONE__\n\n"; return
        if not accounts:
            yield send("❌ 没有账号配置"); yield "data: __DONE__\n\n"; return
        for i, a in enumerate(accounts, 1):
            if not a.get("cozeToken"):
                yield send(f"❌ 账号 #{i} 缺少 Coze Token"); yield "data: __DONE__\n\n"; return
            if not a.get("workflowId"):
                yield send(f"❌ 账号 #{i} 缺少 Workflow ID"); yield "data: __DONE__\n\n"; return

        yield send(f"🩹 准备补 {len(makeups)} 个产品...")

        # ── Step 0: 读磁盘 + 重组 + 写新 JSON ─────────────────────
        products_by_acc = {i: [] for i in range(len(accounts))}
        skipped = 0
        for m in makeups:
            src_key = (m.get("key") or "").strip()
            count   = int(m.get("count") or 0)
            acc_idx = int(m.get("accountIndex") or 0)
            if not src_key or count < 1:
                continue
            if not (0 <= acc_idx < len(accounts)):
                yield send(f"  ⚠️ {src_key}：账号下标 {acc_idx} 越界，跳过"); skipped += 1; continue

            src_path = _parsed_json_path(src_key)
            if not src_path.exists():
                yield send(f"  ❌ {src_key}：源 JSON 不存在 {src_path}，跳过"); skipped += 1; continue

            try:
                with open(src_path, "r", encoding="utf-8") as f:
                    src_data = json.load(f)
            except Exception as e:
                yield send(f"  ❌ {src_key}：读 JSON 失败 {str(e)[:120]}，跳过"); skipped += 1; continue
            if not isinstance(src_data, list) or len(src_data) < 3:
                yield send(f"  ❌ {src_key}：JSON 结构异常（需 [title, header, rows...]），跳过"); skipped += 1; continue

            title      = src_data[0]
            header_str = src_data[1]
            templates  = src_data[2:]

            # 解析 date / group / product（key 形如 5.16_冯会宁组_teenlab维生素d）
            parts = src_key.split("_", 2)
            date    = parts[0] if len(parts) > 0 else "未知"
            group   = parts[1] if len(parts) > 1 else "默认组"
            product = parts[2] if len(parts) > 2 else src_key

            try:
                new_rows = _random_combine(templates, header_str, count)
            except Exception as e:
                yield send(f"  ❌ {src_key}：重组失败 {str(e)[:150]}，跳过"); skipped += 1; continue
            if not new_rows:
                yield send(f"  ⚠️ {src_key}：重组返回空，跳过"); skipped += 1; continue

            new_key  = _build_makeup_key(date, group, product)
            new_data = [title, header_str] + new_rows
            new_path = Path("data/input/parsed") / date / f"{new_key}.json"
            try:
                with open(new_path, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                yield send(f"  ❌ {src_key}：写 {new_path.name} 失败 {str(e)[:120]}，跳过"); skipped += 1; continue

            yield send(f"  ✅ {src_key} → {new_key}（{count} 条）→ 账号 {_acc_tag(acc_idx)}")
            products_by_acc[acc_idx].append({
                "key": new_key, "title": title, "data": new_data,
            })

        total_new = sum(len(v) for v in products_by_acc.values())
        if total_new == 0:
            yield send(f"❌ 没有可用的补单条目（跳过 {skipped}）"); yield "data: __DONE__\n\n"; return
        yield send(f"📦 生成 {total_new} 个补单产品（跳过 {skipped}）")

        # ── 按指定 accountIndex 分桶（不再平均分） ─────────────
        chunks = [(idx, accounts[idx], ps) for idx, ps in products_by_acc.items() if ps]
        for idx, acc, ps in chunks:
            preview = ", ".join(p["key"] for p in ps[:3]) + (" ..." if len(ps) > 3 else "")
            yield send(f"  🅰️ 账号 {_acc_tag(idx)} 分到 {len(ps)} 个补单：{preview}")

        # ── Step 1: CDP 串行建副表 ─────────────────────────────
        # 传 ps 本身（list[dict]），CDP 撞名时会就地改写 entry["key"]，Coze 步骤会自动拿到新名字
        buckets = []
        for idx, acc, ps in chunks:
            bt = acc.get("baseToken", "")
            if bt and ps:
                buckets.append((bt, ps))

        if buckets:
            yield send(f"🗂️  Step 1/3  CDP 串行建副表（共 {len(buckets)} 个 Base）...")
            try:
                from playwright.sync_api import sync_playwright  # noqa
                for ok, msg in _cdp_create_tables_multi(buckets):
                    yield send(msg)
            except ImportError:
                yield send("⚠️  playwright 未安装，跳过建副表，继续调 Coze。")
            except Exception as e:
                yield send(f"⚠️  CDP 建副表异常（跳过，继续调 Coze）：{str(e)[:150]}")
        else:
            yield send("⏭️  Step 1/3  无任何账号填写 Base Token，跳过建副表")

        # ── Step 2: 全局并行调 Coze ─────────────────────────────
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        import sys as _sys, time as _time

        def _call_one(product, account, account_idx):
            _name     = product.get("key", "")
            _inp_data = product.get("data", [])
            _tag      = f"[{_acc_tag(account_idx)}]"
            print(f"{_tag} ▶ 触发 Coze: {_name}  workflow={account.get('workflowId','')[:12]}...",
                  file=_sys.stderr, flush=True)
            _t0 = _time.time()
            try:
                _result = _call_coze_sync(account["cozeToken"], account["workflowId"],
                                          _inp_data, _name, timeout=60)
                _raw = _result.get('data', {})
                if isinstance(_raw, str):
                    try:    _raw = json.loads(_raw)
                    except: _raw = {}
                _debug_url  = (_raw.get('debug_url','') or _raw.get('debugUrl','')
                               or _result.get('debug_url',''))
                _execute_id = _raw.get('execute_id','') or _result.get('execute_id','')
                _elapsed = _time.time() - _t0
                return {
                    'name': _name, 'account_idx': account_idx,
                    'ok': _result.get('code') == 0,
                    'debug_url': _debug_url, 'execute_id': _execute_id,
                    'code': _result.get('code'), 'msg': _result.get('msg',''),
                    'resp_preview': json.dumps(_result, ensure_ascii=False)[:500],
                    'elapsed': _elapsed,
                }
            except Exception as _e:
                _elapsed = _time.time() - _t0
                return {'name': _name, 'account_idx': account_idx, 'ok': False,
                        'debug_url': '', 'execute_id': '',
                        'code': -1, 'msg': str(_e)[:200], 'resp_preview': '',
                        'elapsed': _elapsed}

        k_acc = len(accounts)
        total_workers = min(total_new, max(1, k_acc) * 5)
        yield send(f"🤖 Step 2/3  并行调用 Coze（全局并发 {total_workers}，单次超时 60s，失败不重试）...")

        debug_by_account = {i: [] for i in range(k_acc)}
        coze_ok, coze_fail = 0, 0

        with ThreadPoolExecutor(max_workers=total_workers) as executor:
            future_map = {}
            for acc_idx, acc, ps in chunks:
                for p in ps:
                    fut = executor.submit(_call_one, p, acc, acc_idx)
                    future_map[fut] = (p["key"], acc_idx)
                    yield send(f"  📤 [{_acc_tag(acc_idx)}] 已提交：{p['key']}")

            _total_n = len(future_map)
            _done_n  = 0
            _t_start = _time.time()
            _pending = set(future_map.keys())
            while _pending:
                done, _pending = wait(_pending, timeout=5, return_when=FIRST_COMPLETED)
                if not done:
                    _now = _time.time()
                    pending_names = [future_map[f][0] for f in _pending][:5]
                    yield send(f"  ⏳ 已等待 {int(_now-_t_start)}s，剩 {len(_pending)}/{_total_n} 未返回："
                               f"{', '.join(pending_names)}{'...' if len(_pending)>5 else ''}")
                    continue
                for future in done:
                    _done_n += 1
                    r = future.result()
                    tag = f"[{_acc_tag(r['account_idx'])}]"
                    yield send(f"  📥 {tag} ({_done_n}/{_total_n}) {r['name']}  耗时={r.get('elapsed',0):.1f}s  → {r['resp_preview']}")
                    if r['ok']:
                        coze_ok += 1
                        debug_by_account[r['account_idx']].append({
                            'name': r['name'], 'debug_url': r['debug_url'],
                            'execute_id': r['execute_id']
                        })
                        url_hint = f"\n  🔗 {r['debug_url']}" if r['debug_url'] else "（未返回 debug_url）"
                        yield send(f"  ✅ {tag} 工作流已接受：{r['name']}{url_hint}")
                    else:
                        coze_fail += 1
                        url_hint = f"\n     🔗 debug_url: {r['debug_url']}" if r['debug_url'] else ""
                        yield send(f"  ❌ {tag} 工作流失败：{r['name']} → code={r['code']} msg={r['msg'][:150]}{url_hint}")

        yield send(f"✅ Coze 触发完毕：成功 {coze_ok} 个，失败 {coze_fail} 个")

        # ── Step 3: 按账号分别写台账 ─────────────────────────
        yield send(f"📋 Step 3/3  按账号分别写飞书台账...")
        for acc_idx, results in debug_by_account.items():
            acc = accounts[acc_idx]
            tag = f"[{_acc_tag(acc_idx)}]"
            has_token = acc.get("feishuUserToken") or (acc.get("feishuAppId") and acc.get("feishuAppSecret"))
            if not results:
                continue
            if not has_token:
                yield send(f"  ⚠️ {tag} 未填写飞书令牌，仅打印 debug_url：")
                for r in results:
                    yield send(f"     📌 {r['name']}  →  {r['debug_url'] or '（无）'}")
                continue
            auth = "App ID+Secret" if (acc.get("feishuAppId") and acc.get("feishuAppSecret")) \
                                    else "user_access_token"
            yield send(f"  📋 {tag} 写入 {len(results)} 条（认证：{auth}）...")
            write_results = _write_bitable_records(
                acc.get("feishuUserToken", ""), results,
                app_id=acc.get("feishuAppId", ""),
                app_secret=acc.get("feishuAppSecret", "")
            )
            ok_cnt   = sum(1 for ok, _ in write_results if ok)
            fail_cnt = len(write_results) - ok_cnt
            for ok, msg in write_results:
                yield send(f"    {tag} {msg}")
            yield send(f"  ✅ {tag} 台账写入完毕：成功 {ok_cnt} 批，失败 {fail_cnt} 批")

        yield send("🎉 补视频全部完成！")
        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route("/api/run-coze-cdp", methods=["POST"])
def run_coze_cdp():
    """
    Stage 2 执行接口（多账号并行版）：
    接收 JSON:
      products : [{key, title, data: [...]}]
      accounts : [{cozeToken, workflowId, baseToken,
                   feishuUserToken, feishuAppId, feishuAppSecret}, ...]
    向后兼容：未传 accounts 时，会从顶层平铺字段构造 accounts[0]。

    流程：
      平均分桶（前 N/k 给 #1，后 N/k 给 #2 …）
      → Step 1 CDP 串行依次切换 Base 建副表
      → Step 2 全局并发调 Coze（每桶各用自己的 Coze 凭证）
      → Step 3 按账号分别写飞书台账
    """
    body     = request.json or {}
    products = body.get("products", [])
    accounts = body.get("accounts", []) or []

    # 向后兼容：旧的平铺字段当作 accounts[0]
    if not accounts:
        accounts = [{
            "cozeToken":       body.get("cozeToken", ""),
            "workflowId":      body.get("workflowId", ""),
            "baseToken":       body.get("baseToken", DEFAULT_BASE_TOKEN),
            "feishuUserToken": body.get("feishuUserToken", ""),
            "feishuAppId":     body.get("feishuAppId", ""),
            "feishuAppSecret": body.get("feishuAppSecret", ""),
        }]

    # strip 所有字符串字段
    for a in accounts:
        for k_ in list(a.keys()):
            if isinstance(a[k_], str):
                a[k_] = a[k_].strip()

    def send(msg):
        return f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"

    def generate():
        if not products:
            yield send("❌ 没有产品数据，请重新清洗"); yield "data: __DONE__\n\n"; return
        for i, a in enumerate(accounts, 1):
            if not a.get("cozeToken"):
                yield send(f"❌ 账号 #{i} 缺少 Coze Token"); yield "data: __DONE__\n\n"; return
            if not a.get("workflowId"):
                yield send(f"❌ 账号 #{i} 缺少 Workflow ID"); yield "data: __DONE__\n\n"; return

        k = len(accounts)
        n = len(products)
        yield send(f"⚙️  并行配置：{k} 个账号 × {n} 个产品，平均分桶")

        # ── 自动检查并修复"台词 vs 最终台词"差异过大的行 ─────
        fix_total = 0
        for p in products:
            d = p.get("data") or []
            if len(d) < 2:
                continue
            try:
                hdrs = [h.strip() for h in str(d[1]).split(" // ")]
                rows = [[c.strip() for c in str(r).split(" // ")] for r in d[2:]]
                # 补齐列数
                for r in rows:
                    while len(r) < len(hdrs): r.append("/")
                fixed_n, details = _auto_fix_script_rows(hdrs, rows)
                if fixed_n:
                    fix_total += fixed_n
                    # 回写到 product.data
                    p["data"] = [d[0], " // ".join(hdrs)] + [" // ".join(r) for r in rows]
                    yield send(f"  🔁 {p.get('key','?')}：{fixed_n} 行差异≥{int(SUBTITLE_DIFF_THRESHOLD*100)}%，已用最终台词覆盖台词")
                    for det in details[:3]:
                        yield send(f"     ↳ 行 {det['row']}  差异 {int(det['diff']*100)}%"
                                   f"  原台词:{det['old']!r}  →  覆盖为:{det['new']!r}")
            except Exception as _e:
                yield send(f"  ⚠️ {p.get('key','?')}：自动覆盖台词异常（跳过）: {str(_e)[:80]}")
        if fix_total:
            yield send(f"✏️  共 {fix_total} 行台词被最终台词覆盖（差异阈值 {int(SUBTITLE_DIFF_THRESHOLD*100)}%）")
        else:
            yield send(f"✔️  台词检查通过：无行差异≥{int(SUBTITLE_DIFF_THRESHOLD*100)}%")

        # ── 自动规范化视频比例（非标准 → 最近标准）─────────────
        ratio_total = 0
        for p in products:
            d = p.get("data") or []
            if len(d) < 2:
                continue
            try:
                title = d[0]
                rows  = [[c for c in str(r).split(" // ")] for r in d[2:]]
                obj   = {"title": title, "rows": rows}
                r_fixed, r_details = _auto_fix_ratios_product(obj)
                if r_fixed:
                    ratio_total += r_fixed
                    # 回写：title 可能被改，rows 单元格可能被改
                    p["data"] = [obj["title"], d[1]] + [" // ".join(r) for r in obj["rows"]]
                    yield send(f"  📐 {p.get('key','?')}：规范化 {r_fixed} 处非标准比例")
                    for det in r_details[:3]:
                        yield send(f"     ↳ {det['where']}  {det['old']} → {det['new']}")
            except Exception as _e:
                yield send(f"  ⚠️ {p.get('key','?')}：比例规范化异常（跳过）: {str(_e)[:80]}")
        if ratio_total:
            yield send(f"📐 共规范化 {ratio_total} 处非标准比例（标准: 3:4 / 4:3 / 9:16 / 16:9）")
        else:
            yield send("✔️  比例检查通过：无需规范化")

        # 平均分桶：前 N/k 给 #1，依次往后
        chunks = []  # [(account_idx, account, [products])]
        for i, acc in enumerate(accounts):
            start = i * n // k
            end   = (i + 1) * n // k
            chunks.append((i, acc, products[start:end]))

        for idx, acc, ps in chunks:
            preview = ", ".join(p.get("key", "") for p in ps[:3])
            if len(ps) > 3: preview += " ..."
            yield send(f"  🅰️ 账号 #{idx+1}（Coze: {acc.get('cozeToken','')[:12]}...）"
                       f"分到 {len(ps)} 个产品：{preview}")

        # ── Step 1: CDP 串行建副表 ─────────────────────────────
        # 传 ps 本身（list[dict]），CDP 撞名时会就地改写 entry["key"]，Coze 步骤会自动拿到新名字
        buckets = []  # [(base_token, [entries])]
        for idx, acc, ps in chunks:
            bt = acc.get("baseToken", "")
            if bt and ps:
                buckets.append((bt, ps))

        if buckets:
            yield send(f"🗂️  Step 1/3  CDP 串行建副表（共 {len(buckets)} 个 Base）...")
            try:
                from playwright.sync_api import sync_playwright  # noqa
                for ok, msg in _cdp_create_tables_multi(buckets):
                    yield send(msg)
            except ImportError:
                yield send("⚠️  playwright 未安装，跳过建副表，继续调 Coze。"
                           "如需建表请运行: pip3 install playwright && playwright install chromium")
            except Exception as e:
                yield send(f"⚠️  CDP 建副表异常（跳过，继续调 Coze）：{str(e)[:150]}")
        else:
            yield send("⏭️  Step 1/3  无任何账号填写 Base Token，跳过建副表")

        # ── Step 2: 全局并行调 Coze（每桶各用自己的凭证） ────
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        import sys as _sys, time as _time

        def _call_one(product, account, account_idx):
            _name     = product.get("key", "")
            _inp_data = product.get("data", [])
            _tag      = f"[#{account_idx+1}]"
            # 服务端 stderr 也打一份，方便从终端定位卡点
            print(f"{_tag} ▶ 触发 Coze: {_name}  workflow={account.get('workflowId','')[:12]}...",
                  file=_sys.stderr, flush=True)
            _t0 = _time.time()
            try:
                _result = _call_coze_sync(account["cozeToken"], account["workflowId"],
                                          _inp_data, _name, timeout=60)
                _raw    = _result.get('data', {})
                if isinstance(_raw, str):
                    try:    _raw = json.loads(_raw)
                    except: _raw = {}
                _debug_url  = (_raw.get('debug_url','') or _raw.get('debugUrl','')
                               or _result.get('debug_url',''))
                _execute_id = _raw.get('execute_id','') or _result.get('execute_id','')
                _elapsed = _time.time() - _t0
                print(f"{_tag} ◀ Coze 返回 {_name}  code={_result.get('code')}  耗时={_elapsed:.1f}s",
                      file=_sys.stderr, flush=True)
                return {
                    'name': _name, 'account_idx': account_idx,
                    'ok': _result.get('code') == 0,
                    'debug_url': _debug_url, 'execute_id': _execute_id,
                    'code': _result.get('code'), 'msg': _result.get('msg',''),
                    'resp_preview': json.dumps(_result, ensure_ascii=False)[:500],
                    'elapsed': _elapsed,
                }
            except Exception as _e:
                _elapsed = _time.time() - _t0
                print(f"{_tag} ✗ Coze 异常 {_name}  耗时={_elapsed:.1f}s  err={str(_e)[:120]}",
                      file=_sys.stderr, flush=True)
                return {'name': _name, 'account_idx': account_idx, 'ok': False,
                        'debug_url': '', 'execute_id': '',
                        'code': -1, 'msg': str(_e)[:200], 'resp_preview': '',
                        'elapsed': _elapsed}

        total_workers = min(n, k * 5)
        yield send(f"🤖 Step 2/3  并行调用 Coze（全局并发 {total_workers}，每账号 ≈ 5 线程，单次超时 60s，失败不重试）...")

        debug_by_account = {i: [] for i in range(k)}
        coze_ok, coze_fail = 0, 0

        with ThreadPoolExecutor(max_workers=total_workers) as executor:
            future_map = {}
            for acc_idx, acc, ps in chunks:
                for p in ps:
                    fut = executor.submit(_call_one, p, acc, acc_idx)
                    future_map[fut] = (p.get("key", ""), acc_idx)
                    yield send(f"  📤 [#{acc_idx+1}] 已提交：{p.get('key','')}")

            _total_n = len(future_map)
            _done_n  = 0
            _t_start = _time.time()
            _pending = set(future_map.keys())
            while _pending:
                # 最多等 5s，再没完成就喷一行心跳
                done, _pending = wait(_pending, timeout=5, return_when=FIRST_COMPLETED)
                if not done:
                    _now = _time.time()
                    pending_names = [future_map[f][0] for f in _pending][:5]
                    yield send(f"  ⏳ 已等待 {int(_now-_t_start)}s，剩 {len(_pending)}/{_total_n} 未返回："
                               f"{', '.join(pending_names)}{'...' if len(_pending)>5 else ''}")
                    continue
                for future in done:
                    _done_n += 1
                    r = future.result()
                    tag = f"[#{r['account_idx']+1}]"
                    yield send(f"  📥 {tag} ({_done_n}/{_total_n}) {r['name']}  耗时={r.get('elapsed',0):.1f}s  → {r['resp_preview']}")
                    if r['ok']:
                        coze_ok += 1
                        debug_by_account[r['account_idx']].append({
                            'name': r['name'], 'debug_url': r['debug_url'],
                            'execute_id': r['execute_id']
                        })
                        url_hint = f"\n  🔗 {r['debug_url']}" if r['debug_url'] else "（未返回 debug_url）"
                        yield send(f"  ✅ {tag} 工作流已接受：{r['name']}{url_hint}")
                    else:
                        coze_fail += 1
                        url_hint = f"\n     🔗 debug_url: {r['debug_url']}" if r['debug_url'] else ""
                        yield send(f"  ❌ {tag} 工作流失败：{r['name']} → code={r['code']} msg={r['msg'][:150]}{url_hint}")

        yield send(f"✅ Coze 触发完毕：成功 {coze_ok} 个，失败 {coze_fail} 个")

        # ── Step 3: 按账号分别写台账 ─────────────────────────
        yield send(f"📋 Step 3/3  按账号分别写飞书台账...")
        for acc_idx, results in debug_by_account.items():
            acc = accounts[acc_idx]
            tag = f"[#{acc_idx+1}]"
            has_token = acc.get("feishuUserToken") or (acc.get("feishuAppId") and acc.get("feishuAppSecret"))
            if not results:
                yield send(f"  ⚠️ {tag} 无 debug_url，跳过")
                continue
            if not has_token:
                yield send(f"  ⚠️ {tag} 未填写飞书令牌，仅打印 debug_url：")
                for r in results:
                    yield send(f"     📌 {r['name']}  →  {r['debug_url'] or '（无）'}")
                continue
            auth = "App ID+Secret" if (acc.get("feishuAppId") and acc.get("feishuAppSecret")) \
                                    else "user_access_token"
            yield send(f"  📋 {tag} 写入 {len(results)} 条（认证：{auth}）...")
            write_results = _write_bitable_records(
                acc.get("feishuUserToken", ""), results,
                app_id=acc.get("feishuAppId", ""),
                app_secret=acc.get("feishuAppSecret", "")
            )
            ok_cnt   = sum(1 for ok, _ in write_results if ok)
            fail_cnt = len(write_results) - ok_cnt
            for ok, msg in write_results:
                yield send(f"    {tag} {msg}")
            yield send(f"  ✅ {tag} 台账写入完毕：成功 {ok_cnt} 批，失败 {fail_cnt} 批")

        yield send("🎉 全部完成！")
        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ============================================================
# 前端页面
# ============================================================
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>秀悦视频下载工具</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f0f2f5; color: #222; }
  .container { max-width: 900px; margin: 30px auto; padding: 0 16px; }
  h1 { font-size: 22px; margin-bottom: 20px; color: #1a1a2e; }
  .card { background: #fff; border-radius: 12px; padding: 20px;
           box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 16px; }
  .card h2 { font-size: 14px; color: #666; margin-bottom: 12px; text-transform: uppercase; letter-spacing: .5px; }
  label { display: block; font-size: 13px; color: #555; margin-bottom: 4px; margin-top: 10px; }
  input, textarea { width: 100%; padding: 8px 12px; border: 1px solid #ddd;
                    border-radius: 8px; font-size: 13px; outline: none; }
  input:focus, textarea:focus { border-color: #4f46e5; }
  textarea { height: 72px; resize: vertical; font-family: monospace; font-size: 12px; }
  .row { display: flex; gap: 10px; }
  .row input { flex: 1; }
  button { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer;
           font-size: 14px; font-weight: 600; transition: opacity .2s; }
  button:hover { opacity: .85; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  .btn-primary { background: #4f46e5; color: #fff; }
  .btn-green   { background: #16a34a; color: #fff; }
  .hint { font-size: 12px; color: #888; margin-top: 4px; }
  /* 副表列表 */
  .check-all-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .check-all-row button { padding: 5px 12px; font-size: 12px; background: #e5e7eb; color: #333; }
  .table-scroll { max-height: 240px; overflow-y: auto; border: 1px solid #eee;
                   border-radius: 8px; padding: 6px 10px; }
  .table-item { display: flex; align-items: center; gap: 8px; padding: 5px 0;
                border-bottom: 1px solid #f3f4f6; font-size: 13px; }
  .table-item:last-child { border-bottom: none; }
  .table-item input[type=checkbox] { width: auto; }
  .badge { font-size: 11px; color: #6b7280; margin-left: auto; white-space: nowrap; }
  /* 日志 */
  #log-box { background: #1e1e2e; color: #a6e22e; border-radius: 8px;
              padding: 14px; height: 320px; overflow-y: auto;
              font-family: "Menlo", monospace; font-size: 12px; line-height: 1.7;
              white-space: pre-wrap; display: none; }
  .progress-bar-wrap { background: #e5e7eb; border-radius: 99px; height: 8px; margin: 10px 0; display: none; }
  .progress-bar { height: 100%; border-radius: 99px; background: #16a34a;
                   transition: width .3s; width: 0%; }
  #status { font-size: 13px; color: #555; min-height: 20px; }
  /* Excel 清洗结果样式 */
  .product-card { border:1px solid #e5e7eb; border-radius:10px; margin-bottom:12px; overflow:hidden; }
  .product-card-header { background:#f5f3ff; padding:10px 14px; display:flex; align-items:center; gap:10px;
                          cursor:pointer; user-select:none; }
  .product-card-header:hover { background:#ede9fe; }
  .product-title { font-size:13px; font-weight:600; color:#3730a3; flex:1; }
  .product-badge { background:#7c3aed; color:#fff; font-size:11px; padding:2px 8px; border-radius:99px; white-space:nowrap; }
  .product-table-wrap { overflow-x:auto; max-height:240px; overflow-y:auto; }
  .product-table { width:100%; border-collapse:collapse; font-size:12px; }
  .product-table th { background:#ede9fe; color:#4c1d95; padding:6px 10px; text-align:left;
                       white-space:nowrap; position:sticky; top:0; z-index:1; }
  .product-table td { padding:5px 10px; border-bottom:1px solid #f3f4f6; color:#374151;
                       max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .product-table tr:last-child td { border-bottom:none; }
  .product-table tr:hover td { background:#faf5ff; }
  .product-toggle { color:#7c3aed; font-size:14px; transition:transform .2s; }
  .product-toggle.collapsed { transform:rotate(-90deg); }
  /* 可编辑单元格 */
  .product-table th[contenteditable], .product-table td[contenteditable] {
    cursor:text; white-space:pre-wrap; word-break:break-all; max-width:280px; }
  .product-table th[contenteditable]:focus, .product-table td[contenteditable]:focus {
    outline:2px solid #7c3aed; outline-offset:-2px; background:#fff; }
  .product-table td.cell-saving { background:#fef3c7 !important; }
  .product-table td.cell-saved  { background:#d1fae5 !important; transition:background .8s; }
  .product-table td.cell-error  { background:#fee2e2 !important; }
  /* 行操作列：拖拽手柄 + 删除按钮 */
  .product-table th.row-ops, .product-table td.row-ops {
    width:54px; min-width:54px; text-align:center; color:#9ca3af; padding:4px 2px;
    background:#faf5ff; position:sticky; left:0; z-index:2; }
  .product-table th.row-ops { background:#ede9fe; }
  .row-drag  { cursor:grab; user-select:none; font-size:14px; color:#a78bfa; padding:0 3px; }
  .row-drag:active { cursor:grabbing; }
  .row-del   { cursor:pointer; color:#ef4444; padding:0 3px; font-size:13px; background:none; border:none; }
  .row-del:hover { color:#b91c1c; }
  .product-table tr.dragging { opacity:.35; }
  .product-table tr.drag-over td { border-top:2px solid #7c3aed; }
  /* 表底新增按钮 */
  .row-add-bar { padding:6px 12px; background:#f5f3ff; border-top:1px solid #ede9fe;
                 display:flex; gap:8px; align-items:center; }
  .row-add-bar button { padding:4px 12px; font-size:12px; background:#7c3aed; color:#fff;
                        border-radius:6px; font-weight:500; }
  /* 可编辑标题 */
  .product-title[contenteditable]:focus { outline:2px solid #fff; outline-offset:-2px;
    background:rgba(255,255,255,.6); border-radius:4px; padding:0 4px; }
  /* 保存状态提示（卡片头部右上角） */
  .save-hint { font-size:11px; color:#9ca3af; margin-right:8px; }
  .save-hint.saving { color:#d97706; }
  .save-hint.saved  { color:#059669; }
  .save-hint.error  { color:#dc2626; }
  /* 项目/组 分层样式 */
  .project-block { margin-bottom:14px; border-radius:10px; border:1px solid #ddd6fe; overflow:hidden; }
  .project-header { display:flex; align-items:center; gap:10px; padding:10px 14px;
                    background:linear-gradient(135deg,#7c3aed,#6d28d9); color:#fff; cursor:pointer;
                    user-select:none; }
  .project-header:hover { opacity:.92; }
  .project-name { font-size:14px; font-weight:700; flex:1; }
  .project-stats { font-size:12px; opacity:.85; white-space:nowrap; }
  .project-toggle { font-size:13px; transition:transform .2s; }
  .project-toggle.collapsed { transform:rotate(-90deg); }
  .group-section { }
  .group-header { display:flex; align-items:center; gap:8px; padding:7px 14px;
                  background:#ede9fe; color:#5b21b6; font-size:12px; font-weight:700;
                  border-top:1px solid #ddd6fe; }
  .project-body { }
  /* 🔍 未建副表高亮 */
  .product-card.missing-subtable { border:2px solid #ef4444; box-shadow:0 0 0 3px rgba(239,68,68,.15); }
  .product-card.missing-subtable .product-card-header { background:#fef2f2; }
  .product-card.missing-subtable .product-card-header::before {
    content:'❌ 未建副表'; background:#dc2626; color:#fff; font-size:11px; font-weight:700;
    padding:2px 8px; border-radius:99px; margin-right:8px;
  }
  /* 检查结果模态框 */
  .check-modal-mask { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:9999;
                      display:none; align-items:center; justify-content:center; }
  .check-modal-mask.show { display:flex; }
  .check-modal { background:#fff; border-radius:12px; width:min(680px,92vw); max-height:80vh;
                 display:flex; flex-direction:column; box-shadow:0 20px 50px rgba(0,0,0,.3); }
  .check-modal-head { padding:14px 18px; border-bottom:1px solid #e5e7eb; display:flex;
                      align-items:center; gap:10px; }
  .check-modal-body { padding:14px 18px; overflow:auto; flex:1; font-size:13px; line-height:1.6; }
  .check-modal-foot { padding:12px 18px; border-top:1px solid #e5e7eb; display:flex; gap:8px;
                      justify-content:flex-end; }
  .check-modal-btn { padding:8px 16px; border-radius:6px; border:none; cursor:pointer;
                     font-size:13px; font-weight:600; }
  /* ── 顶部 Tab 切换（钉钉同步 / Excel 离线） ── */
  .mode-tabs { display:flex; gap:6px; margin-bottom:12px; border-bottom:2px solid #e5e7eb; }
  .mode-tab  { padding:8px 18px; border:none; background:transparent; cursor:pointer;
               font-size:13px; font-weight:700; color:#6b7280; border-radius:8px 8px 0 0;
               border-bottom:3px solid transparent; margin-bottom:-2px; transition:all .15s; }
  .mode-tab:hover { color:#7c3aed; background:#faf5ff; }
  .mode-tab.active { color:#7c3aed; border-bottom-color:#7c3aed; background:#faf5ff; }
  .mode-panel { display:none; }
  .mode-panel.active { display:block; }
  /* 钉钉同步工具栏 */
  .dt-toolbar { display:flex; align-items:center; gap:10px; padding:10px 14px;
                background:#ecfdf5; border:1px solid #6ee7b7; border-radius:10px;
                margin-bottom:12px; flex-wrap:wrap; }
  .dt-status { font-size:12px; font-weight:700; padding:3px 10px; border-radius:99px;
               white-space:nowrap; }
  .dt-status.ok   { background:#d1fae5; color:#065f46; }
  .dt-status.no   { background:#fee2e2; color:#991b1b; }
  .dt-status.wait { background:#fef3c7; color:#92400e; }
  .dt-btn { padding:6px 14px; border-radius:6px; border:none; cursor:pointer;
            font-size:12px; font-weight:600; white-space:nowrap; }
  .dt-btn.primary { background:#10b981; color:#fff; }
  .dt-btn.primary:hover { background:#059669; }
  .dt-btn.ghost   { background:#fff; color:#065f46; border:1px solid #6ee7b7; }
  .dt-btn.ghost:hover { background:#ecfdf5; }
  .dt-btn:disabled { opacity:.55; cursor:not-allowed; }
  .dt-date-input { width:90px; padding:5px 8px; border:1px solid #6ee7b7;
                   border-radius:6px; font-size:12px; text-align:center;
                   background:#fff; color:#065f46; font-weight:600; }
  /* ── 扁平产品卡（钉钉 Tab） ── */
  .dt-sheet-group { margin-bottom:14px; }
  .dt-sheet-header { display:flex; align-items:center; gap:8px; padding:7px 12px;
                     background:linear-gradient(135deg,#10b981,#059669); color:#fff;
                     border-radius:8px 8px 0 0; font-size:13px; font-weight:700;
                     border:1px solid #059669; border-bottom:none; }
  .dt-sheet-body  { border:1px solid #bbf7d0; border-top:none; border-radius:0 0 8px 8px;
                    background:#fff; padding:8px; display:flex; flex-direction:column; gap:6px; }
  .dt-card { border:1px solid #e5e7eb; border-radius:8px; background:#fff;
             padding:8px 10px; display:flex; flex-direction:column; gap:6px;
             transition:border-color .15s; }
  .dt-card:hover { border-color:#10b981; }
  .dt-card.selected { border-color:#10b981; background:#f0fdf4; }
  .dt-card-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .dt-card-name { font-size:13px; font-weight:700; color:#111827; flex:1; min-width:140px; }
  .dt-card-meta { font-size:11px; color:#6b7280; white-space:nowrap; }
  .dt-card-meta b { color:#10b981; }
  .dt-task-rows { display:flex; flex-direction:column; gap:4px; padding-left:24px; }
  .dt-task-row  { display:flex; align-items:center; gap:6px; font-size:12px; }
  .dt-task-row select, .dt-task-row input {
    padding:3px 6px; border:1px solid #d1d5db; border-radius:5px; font-size:12px; }
  .dt-task-row select { min-width:80px; background:#fff; color:#374151; }
  .dt-task-row input.dt-count {
    width:55px; text-align:center; background:#fffbeb; border-color:#fcd34d;
    color:#92400e; font-weight:700; }
  .dt-task-row .dt-mode-tag {
    font-size:10px; padding:1px 6px; border-radius:99px; font-weight:700;
    background:#ede9fe; color:#5b21b6; white-space:nowrap; }
  .dt-task-row .dt-mode-tag.m2 { background:#fef3c7; color:#92400e; }
  .dt-task-row .dt-mode-tag.unknown { background:#f3f4f6; color:#9ca3af; }
  .dt-task-row .dt-row-del {
    background:none; border:none; color:#ef4444; cursor:pointer; font-size:14px;
    padding:0 4px; line-height:1; }
  .dt-task-row .dt-row-del:hover { color:#b91c1c; }
  .dt-task-add {
    align-self:flex-start; padding:2px 10px; font-size:11px;
    background:#ecfdf5; color:#065f46; border:1px dashed #6ee7b7; border-radius:5px;
    cursor:pointer; font-weight:600; margin-left:24px; }
  .dt-task-add:hover { background:#d1fae5; }
  .dt-bulk-bar { display:flex; align-items:center; gap:10px; padding:8px 12px;
                 background:#ecfdf5; border:1px solid #6ee7b7; border-radius:8px;
                 margin-bottom:10px; flex-wrap:wrap; }
  .dt-bulk-bar label { display:flex; align-items:center; gap:5px; font-size:12px;
                       color:#065f46; font-weight:600; cursor:pointer; margin:0; }
  .dt-bulk-bar .dt-sel-count { font-size:11px; color:#10b981; flex:1; }
  /* ── 右侧固定操作栏 ── */
  .side-bar { position:fixed; right:0; top:50%; transform:translateY(-50%);
              z-index:9000; display:flex; flex-direction:column;
              border-radius:12px 0 0 12px; overflow:hidden;
              box-shadow:-3px 0 16px rgba(0,0,0,.18); }
  .side-btn { width:54px; padding:14px 0; border:none; cursor:pointer;
              display:flex; flex-direction:column; align-items:center; justify-content:center;
              gap:5px; font-size:11px; font-weight:700; color:#fff;
              transition:filter .15s; }
  .side-btn:hover:not(:disabled) { filter:brightness(1.12); }
  .side-btn:active:not(:disabled) { filter:brightness(.92); }
  .side-btn:disabled { opacity:.38; cursor:not-allowed; }
  .side-btn .sb-icon { font-size:19px; line-height:1; }
  .side-btn.sb-preview  { background:#10b981; }
  .side-btn.sb-download { background:#f59e0b; }
  .side-bar-sep { height:1px; background:rgba(255,255,255,.35); }
  /* ── 预览模态框 ── */
  .dt-modal-mask { position:fixed; inset:0; background:rgba(15,23,42,.52);
                   z-index:9500; display:flex; align-items:center; justify-content:center;
                   padding:24px; box-sizing:border-box; }
  .dt-modal-box  { background:#fff; border-radius:16px; width:min(960px,100%);
                   max-height:calc(100vh - 48px); display:flex; flex-direction:column;
                   overflow:hidden; box-shadow:0 24px 64px rgba(0,0,0,.28); }
  .dt-modal-head { display:flex; align-items:center; gap:12px; padding:16px 20px;
                   border-bottom:1px solid #e5e7eb; flex-shrink:0; background:#fff;
                   border-radius:16px 16px 0 0; }
  .dt-modal-head .dm-title { flex:1; }
  .dt-modal-head .dm-title h3 { margin:0; font-size:15px; font-weight:700; color:#1f2937; }
  .dt-modal-head .dm-title p  { margin:3px 0 0; font-size:12px; color:#6b7280; }
  .dt-modal-exec { padding:9px 22px; font-size:13px; font-weight:600; border:none;
                   border-radius:8px; cursor:pointer; color:#fff;
                   background:linear-gradient(135deg,#10b981,#059669);
                   transition:opacity .15s; }
  .dt-modal-exec:hover:not(:disabled) { opacity:.88; }
  .dt-modal-exec:disabled { opacity:.4; cursor:not-allowed; }
  .dt-modal-close { width:36px; height:36px; border-radius:8px; border:1px solid #e5e7eb;
                    background:#f9fafb; cursor:pointer; font-size:18px; color:#6b7280;
                    display:flex; align-items:center; justify-content:center; transition:background .15s; }
  .dt-modal-close:hover { background:#f3f4f6; color:#374151; }
  .dt-modal-warn { display:none; flex-shrink:0; padding:8px 20px; font-size:12px;
                   color:#b91c1c; background:#fef2f2; border-bottom:1px solid #fecaca; }
  .dt-modal-body { flex:1; overflow:auto; background:#fafafa; }
</style>
</head>
<body>
<!-- 右侧固定操作栏 -->
<div class="side-bar">
  <button class="side-btn sb-preview" id="side-preview" onclick="sideAction('preview')">
    <span class="sb-icon">📋</span><span>预览</span>
  </button>
  <div class="side-bar-sep"></div>
  <button class="side-btn sb-download" id="side-download" onclick="sideAction('download')">
    <span class="sb-icon">⬇</span><span>下载</span>
  </button>
</div>

<!-- 📋 预览模态框（点击"展开预览"后弹出） -->
<div id="dt-preview-modal" class="dt-modal-mask" style="display:none"
     onclick="if(event.target===this) dtCancelPreview()">
  <div class="dt-modal-box">
    <!-- 固定头部：标题 + 开始执行 + 关闭 -->
    <div class="dt-modal-head">
      <div class="dm-title">
        <h3>📋 副表预览</h3>
        <p id="dt-preview-summary">正在加载...</p>
      </div>
      <button id="dt-btn-confirm" class="dt-modal-exec" onclick="dtConfirmExecute()">
        ⚡ 开始执行
      </button>
      <button class="dt-modal-close" onclick="dtCancelPreview()" title="关闭">✕</button>
    </div>
    <!-- 警告区（固定，不随内容滚动） -->
    <div id="dt-preview-warnings" class="dt-modal-warn"></div>
    <!-- 可滚动的副表列表 -->
    <div id="dt-preview-list" class="dt-modal-body"></div>
  </div>
</div>

<!-- 🔍 副表检查结果模态框 -->
<div id="check-modal-mask" class="check-modal-mask" onclick="if(event.target===this) closeCheckModal()">
  <div class="check-modal">
    <div class="check-modal-head">
      <span style="font-size:16px;font-weight:700;color:#1f2937;flex:1">🔍 副表检查结果</span>
      <button onclick="closeCheckModal()" class="check-modal-btn" style="background:#f3f4f6;color:#374151">✕</button>
    </div>
    <div id="check-modal-body" class="check-modal-body"></div>
    <div class="check-modal-foot">
      <button id="btn-highlight-missing" onclick="highlightMissingProducts()" class="check-modal-btn"
              style="background:#dc2626;color:#fff">📌 高亮未建产品</button>
      <button id="btn-copy-missing" onclick="copyMissingList()" class="check-modal-btn"
              style="background:#7c3aed;color:#fff">📋 复制清单</button>
      <button onclick="closeCheckModal()" class="check-modal-btn"
              style="background:#e5e7eb;color:#374151">关闭</button>
    </div>
  </div>
</div>

<div class="container">
  <h1>🎬 秀悦自动视频平台</h1>

  <!-- 全流程自动化 -->
  <div class="card" style="border:2px solid #7c3aed;background:linear-gradient(135deg,#faf5ff 0%,#fff 60%)">
    <h2 style="color:#6d28d9">⚡ 全流程自动化（清洗预览 → 确认 → Coze + 飞书建表）</h2>

    <!-- ── 账号卡片（支持多账号并行）── -->
    <div style="margin-bottom:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px">
        <div style="font-size:13px;color:#6d28d9;font-weight:700;flex:1">
          🔑 账号配置（每张卡 = 一组 Coze + 飞书，多账号并行执行）
        </div>
        <button onclick="_toggleAllAccountCards(true)" title="一键展开所有账号卡"
                style="padding:5px 10px;background:#ede9fe;color:#6d28d9;border:1px solid #ddd6fe;
                       border-radius:6px;cursor:pointer;font-size:12px">
          ▼ 全部展开
        </button>
        <button onclick="_toggleAllAccountCards(false)" title="一键收起所有账号卡"
                style="padding:5px 10px;background:#ede9fe;color:#6d28d9;border:1px solid #ddd6fe;
                       border-radius:6px;cursor:pointer;font-size:12px">
          ▶ 全部收起
        </button>
        <button onclick="addAccountCard()" style="padding:6px 12px;background:#7c3aed;color:white;
                border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
          ➕ 添加账号
        </button>
      </div>
      <div id="auto-accounts-list"></div>
    </div>

    <!-- ── 顶部 Tab 切换：钉钉同步 / Excel 离线 ── -->
    <div class="mode-tabs">
      <button class="mode-tab active" data-mode="dingtalk" onclick="switchMode('dingtalk')">📥 钉钉同步</button>
      <button class="mode-tab" data-mode="excel" onclick="switchMode('excel')">📊 Excel 离线（兜底）</button>
    </div>

    <!-- ── 钉钉同步面板 ── -->
    <div id="mode-dingtalk" class="mode-panel active">
      <div class="dt-toolbar">
        <span id="dt-status" class="dt-status wait" title="钉钉登录状态">⚪ 检查中…</span>
        <button id="dt-btn-login" class="dt-btn primary" onclick="dtLogin()">🔑 钉钉登录</button>
        <button id="dt-btn-logout" class="dt-btn ghost" onclick="dtLogout()" style="display:none">↩ 登出</button>
        <span style="flex:1"></span>
        <span style="font-size:12px;color:#065f46;font-weight:700">📅 默认日期</span>
        <input type="text" id="dt-default-date" class="dt-date-input" placeholder="5.22"
               oninput="_persistDtDefaultDate(this.value)" title="新增任务行时默认填这一天（格式 月.日）">
        <button id="dt-btn-sync" class="dt-btn primary" onclick="dtSync()" disabled>📥 同步钉钉数据</button>
      </div>
      <div id="dt-summary" style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;
           padding:10px 14px;font-size:12px;color:#166534;margin-bottom:10px;display:none"></div>
      <div id="dt-bulk-bar" class="dt-bulk-bar" style="display:none">
        <label>
          <input type="checkbox" id="dt-chk-all" onclick="dtToggleSelectAll(); return false;"
                 style="width:auto;accent-color:#10b981">
          全选
        </label>
        <span id="dt-sel-count" class="dt-sel-count">尚未选中任何产品</span>
        <button onclick="dtAutoFill('all')"
                style="padding:3px 10px;font-size:11px;background:#d1fae5;color:#065f46;
                       border:1px solid #6ee7b7;border-radius:6px;cursor:pointer;font-weight:600">
          ✨ 自动填入
        </button>
        <button onclick="dtClearFill('all')"
                style="padding:3px 10px;font-size:11px;background:#fee2e2;color:#b91c1c;
                       border:1px solid #fca5a5;border-radius:6px;cursor:pointer;font-weight:600">
          🗑 清除全部
        </button>
        <span style="font-size:11px;color:#065f46">📅 工具栏默认日期 = 新任务行的初始日期</span>
      </div>
      <div id="dt-product-list" style="font-size:13px;color:#6b7280;padding:30px;text-align:center;
           background:#f9fafb;border:1px dashed #d1d5db;border-radius:10px">
        请先登录钉钉，再点「📥 同步钉钉数据」拉取产品列表
      </div>
      <div id="dt-actions" style="display:none;margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button id="dt-btn-exec" class="dt-btn primary" style="background:linear-gradient(135deg,#10b981,#059669);
                padding:12px 32px;font-size:14px" onclick="dtPreview()">
          📋 展开预览
        </button>
        <span id="dt-task-summary" style="font-size:12px;color:#065f46;font-weight:600"></span>
      </div>

      <!-- 预览区：展开后的副表列表 + 二次确认按钮 -->

      <!-- 执行日志区：复用 SSE 流 -->
      <div id="dt-log-wrap" style="display:none;margin-top:14px;background:#0f172a;border-radius:10px;padding:12px 14px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <strong style="color:#a78bfa;font-size:13px">📜 执行日志</strong>
          <button class="dt-btn ghost" onclick="dtCopyLog()" style="font-size:11px">📋 复制</button>
        </div>
        <div id="dt-log" style="font-family:Menlo,monospace;font-size:12px;color:#e5e7eb;max-height:360px;
             overflow:auto;white-space:pre-wrap;line-height:1.5"></div>
      </div>
    </div>

    <!-- ── Excel 离线面板（原有 Stage 1/2/3 包裹） ── -->
    <div id="mode-excel" class="mode-panel">

    <!-- ── Stage 1：上传 + 清洗预览 ── -->
    <div id="auto-s1">
      <div class="hint" style="margin-bottom:14px;color:#7c3aed">
        💡 填批次日期 → 上传 Excel → 点「清洗预览」→ 给每个产品填条数 → 点「确认执行」
      </div>
      <!-- 批次日期输入 -->
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;padding:10px 14px;
                  background:#fef3c7;border:1px solid #fcd34d;border-radius:8px">
        <span style="font-size:13px;color:#92400e;font-weight:700">📅 批次日期</span>
        <input type="text" id="auto-batch-date" placeholder="例：5.21."
               oninput="_persistBatchDate(this.value)"
               style="flex:1;max-width:200px;padding:6px 10px;border:1px solid #fcd34d;
                      border-radius:6px;font-size:13px;background:#fff">
        <span style="font-size:11px;color:#b45309;flex:1">
          ⚠ 本次上传 Excel 里所有产品都会归到该日期下，请按甲方文档命名填写
        </span>
      </div>
      <!-- 文件上传区 -->
      <div id="auto-drop-zone" onclick="document.getElementById('auto-file-input').click()"
           style="border:2px dashed #c4b5fd;border-radius:10px;padding:18px;text-align:center;cursor:pointer;
                  background:#f5f3ff;transition:background .2s"
           ondragover="event.preventDefault();this.style.background='#ede9fe'"
           ondragleave="this.style.background='#f5f3ff'"
           ondrop="handleAutoDrop(event)">
        <div style="font-size:24px">📂</div>
        <div style="font-size:13px;color:#6d28d9;margin-top:4px;font-weight:600">点击选择 或 拖拽 甲方 Excel（.xlsx）</div>
        <input type="file" id="auto-file-input" accept=".xlsx" style="display:none" onchange="handleAutoFile(this.files[0])">
      </div>
      <div id="auto-file-name" style="font-size:12px;color:#6d28d9;margin-top:6px;display:none"></div>
      <div style="margin-top:12px">
        <button id="btn-auto-clean" onclick="autoCleanPreview()"
                style="background:#7c3aed;color:#fff;border:none;padding:10px 28px;
                       border-radius:8px;cursor:pointer;font-size:14px;font-weight:600">
          📊 清洗并预览
        </button>
      </div>
    </div>

    <!-- ── Stage 2：预览结果 + 人工确认 ── -->
    <div id="auto-s2" style="display:none">
      <hr style="margin:14px 0;border:none;border-top:1px solid #e9d5ff">
      <div id="auto-preview-summary" style="background:#f5f3ff;border-radius:8px;padding:10px 14px;
           font-size:13px;color:#5b21b6;font-weight:600;margin-bottom:10px"></div>

      <!-- 全选工具栏 -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;padding:8px 12px;
                  background:#ede9fe;border-radius:8px;border:1px solid #c4b5fd">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#6d28d9;
                       font-weight:600;cursor:pointer;margin:0">
          <input type="checkbox" id="chk-all-auto" onchange="toggleSelectAll(this.checked)"
                 style="width:auto;cursor:pointer;accent-color:#7c3aed">
          全选
        </label>
        <span id="auto-select-count" style="font-size:12px;color:#7c3aed;flex:1"></span>
        <button onclick="deleteSelected()"
                style="background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;padding:5px 12px;
                       border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
          🗑 删除所选
        </button>
      </div>

      <div id="auto-preview-list"></div>
      <div style="margin-top:14px;padding:12px;background:#fefce8;border-radius:8px;border:1px solid #fde047;
           font-size:13px;color:#713f12">
        ⚠️ 勾选需要执行的产品，确认后将触发 Coze 工作流并自动在飞书创建对应副表
      </div>
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <button id="btn-auto-exec" onclick="autoExecute()"
                style="background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff;border:none;
                       padding:12px 32px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:700;
                       box-shadow:0 4px 12px rgba(124,58,237,.3)">
          ⚡ 确认，开始执行
        </button>
        <button id="btn-auto-makeup" onclick="autoMakeup()"
                title="只对填了"补?"数量的产品生效；副表名前自动加"补"前缀，重名自动 _2/_3"
                style="background:linear-gradient(135deg,#f59e0b,#ea580c);color:#fff;border:none;
                       padding:12px 22px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:700;
                       box-shadow:0 4px 12px rgba(245,158,11,.3)">
          🩹 批量补视频
        </button>
        <button id="btn-check-subtables" onclick="checkMissingSubtables()"
                title="对比飞书 Base 里已建副表，找出当前产品列表里还没建副表的产品"
                style="background:#fef9c3;color:#854d0e;border:1px solid #fde047;
                       padding:12px 18px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">
          🔍 检查未建副表
        </button>
        <!-- 追加上传：日期输入框 + 按钮成对出现，避免在 Stage 2 找不到 Stage 1 的批次框 -->
        <span style="display:inline-flex;align-items:center;gap:6px;padding:6px 10px;
                     background:#fef3c7;border:1px solid #fcd34d;border-radius:8px">
          <span style="font-size:12px;color:#92400e;font-weight:700">📅 追加批次</span>
          <input type="text" id="auto-batch-date-append" placeholder="例：5.21."
                 style="width:110px;padding:5px 8px;border:1px solid #fcd34d;
                        border-radius:6px;font-size:12px;background:#fff">
        </span>
        <button onclick="triggerAppendUpload()"
                title="不清空已有数据，再上传一份 Excel 追加到列表（同 key 的产品会被新数据覆盖）"
                style="background:#dcfce7;color:#15803d;border:1px solid #86efac;
                       padding:12px 18px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">
          ➕ 追加上传
        </button>
        <input type="file" id="auto-append-input" accept=".xlsx" style="display:none"
               onchange="handleAppendFile(this.files[0])">
        <button onclick="resetAutoStage()"
                title="清空当前所有清洗结果，回到上传页"
                style="background:#e5e7eb;color:#555;border:none;padding:12px 18px;border-radius:8px;cursor:pointer;font-size:13px">
          ↩ 重新上传（清空全部）
        </button>
      </div>
    </div>

    <!-- ── Stage 3：执行日志 ── -->
    <div id="auto-log-wrap" style="display:none;margin-top:14px">
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:6px">
        <button onclick="copyAutoLog()" style="padding:4px 10px;background:#475569;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px">📋 复制全部日志</button>
        <button onclick="document.getElementById('auto-log').innerHTML=''" style="padding:4px 10px;background:#64748b;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px">🧹 清空</button>
      </div>
      <div id="auto-log" style="background:#1a1a2e;color:#e0e0e0;
           padding:12px 16px;border-radius:10px;font-size:12px;font-family:monospace;
           max-height:380px;overflow-y:auto;line-height:1.9;user-select:text;-webkit-user-select:text"></div>
    </div>

    </div><!-- /#mode-excel -->
  </div>

  <!-- 下载板块 -->
  <div class="card">
    <h2>⬇️ 视频下载</h2>
    <!-- 配置区 -->
    <label>飞书 Cookie
      <span style="float:right;font-size:11px;color:#4f46e5;cursor:pointer" onclick="showHelp()">❓ 如何获取</span>
    </label>
    <textarea id="cookie" placeholder="从 Chrome DevTools → Network → 任意请求 → Request Headers → cookie 复制粘贴" oninput="autoSaveConfig()"></textarea>
    <div class="row" style="margin-top:10px">
      <div style="flex:1">
        <label>视频字段 ID（默认，所有 Base 共用，可在每张 Base 卡片内单独覆盖）</label>
        <input id="fieldId" value="fldksNMEZ4" oninput="autoSaveConfig()">
      </div>
    </div>
    <label>本地保存目录</label>
    <div style="display:flex;gap:8px;align-items:center">
      <input id="outputDir" value="{{ output_dir }}" style="flex:1">
      <button onclick="pickFolder()" style="padding:8px 14px;background:#6366f1;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;white-space:nowrap">📂 选择文件夹</button>
    </div>
    <div class="hint">📁 视频将按「组名 / 日期_产品名」两层文件夹分类存放，文件名格式：日期_产品名_序号.mp4</div>

    <!-- 下载策略选项 -->
    <div style="margin-top:12px;padding:10px 12px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
      <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;white-space:nowrap">
          <input type="checkbox" id="forceRedownload" onchange="autoSaveConfig()" style="width:auto;flex:none;margin:0">
          <span>🔄 强制重新下载（覆盖本地已存在文件）</span>
        </label>
        <span style="color:#cbd5e1">|</span>
        <label style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap">
          <span>残缺文件阈值</span>
          <input id="minSizeKb" type="number" value="50" min="0" step="10" style="width:70px;flex:none;padding:4px 6px;font-size:12px" oninput="autoSaveConfig()">
          <span style="color:#666">KB（小于此值视为下载残缺，自动重下）</span>
        </label>
      </div>
      <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-top:8px">
        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;white-space:nowrap">
          <input type="checkbox" id="makeZip" checked onchange="autoSaveConfig()" style="width:auto;flex:none;margin:0">
          <span>📦 下完自动打包成 zip（每个副表一个压缩包，放在组文件夹下）</span>
        </label>
        <span style="color:#cbd5e1">|</span>
        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;white-space:nowrap">
          <input type="checkbox" id="deleteAfterZip" onchange="autoSaveConfig()" style="width:auto;flex:none;margin:0">
          <span>🗑 打包成功后删除原文件夹（省空间，但失去断点续传）</span>
        </label>
      </div>
    </div>

    <!-- 多 Base 卡片区 -->
    <div style="margin-top:18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h3 style="margin:0;font-size:15px">📚 下载 Base 列表</h3>
      <button class="btn-primary" id="btn-fetch-multi" onclick="fetchAllDlTables()">🔗 一键拉取全部副表</button>
      <button onclick="addDlBase()" style="margin-left:auto;padding:6px 14px;background:#10b981;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px">➕ 添加 Base</button>
    </div>
    <div id="dl-bases-container" style="margin-top:12px"></div>

    <!-- 全局开始下载 -->
    <div style="margin-top:14px;display:flex;justify-content:flex-end">
      <button class="btn-green" id="btn-dl" onclick="startDownload()">⬇ 开始下载</button>
    </div>

    <!-- 下载进度（动态显示） -->
    <div id="progress-card" style="display:none;margin-top:16px">
      <hr style="border:none;border-top:1px solid #e5e7eb;margin-bottom:12px">
      <div id="status" style="font-size:13px;color:#555;min-height:20px">准备中…</div>
      <div class="progress-bar-wrap" id="pb-wrap"><div class="progress-bar" id="pb"></div></div>
      <div id="log-box"></div>
    </div>
  </div>

  <!-- ============================================================ -->
  <!-- 飞书多维表格自动监控卡片（多表格 × 多账号）                    -->
  <!-- ============================================================ -->
  <div class="card" id="bitable-monitor-card"
       style="border:2px solid #0ea5e9;background:linear-gradient(135deg,#f0f9ff 0%,#fff 60%);margin-top:20px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="color:#0369a1;margin:0">🤖 飞书多维表格自动监控</h2>
      <button onclick="bmAddGroup()"
              style="padding:5px 14px;background:#0ea5e9;color:#fff;border:none;
                     border-radius:6px;cursor:pointer;font-size:13px;font-weight:600">
        ➕ 添加监控组
      </button>
    </div>
    <p style="font-size:12px;color:#64748b;margin-bottom:14px">
      替代「字段捷径」，支持多个多维表格 × 多个 Coze 账号同时监控，无并发限制。
    </p>

    <!-- 监控组列表 -->
    <div id="bm-groups-list"></div>

    <!-- 全局控制栏 -->
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;
                padding:10px 14px;background:#f0f9ff;border:1px solid #bae6fd;
                border-radius:8px;margin-top:10px">
      <button id="bm-btn-start-all"
              style="padding:7px 20px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:#fff;
                     border:none;border-radius:7px;cursor:pointer;font-size:13px;font-weight:700"
              onclick="bmStartAll()">▶ 全部启动</button>
      <button id="bm-btn-stop-all"
              style="padding:7px 20px;background:#e2e8f0;color:#64748b;
                     border:none;border-radius:7px;cursor:pointer;font-size:13px;font-weight:700"
              onclick="bmStopAll()">⏹ 全部停止</button>
      <span id="bm-global-badge"
            style="padding:3px 10px;border-radius:16px;font-size:12px;font-weight:700;
                   background:#f1f5f9;color:#64748b">⚪ 未运行</span>
      <div style="margin-left:auto;display:flex;gap:14px;font-size:12px;color:#475569">
        <span>🎨 <b id="bm-tot2">0</b></span>
        <span>🎬 <b id="bm-tot3">0</b></span>
        <span>📝 <b id="bm-tot4">0</b></span>
        <span style="color:#ef4444">❌ <b id="bm-tot-err">0</b></span>
      </div>
    </div>

    <!-- 实时日志 -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin:10px 0 4px">
      <span style="font-size:12px;color:#64748b;font-weight:600">📋 实时日志</span>
      <button onclick="document.getElementById('bm-log').textContent=''"
              style="font-size:11px;padding:2px 8px;background:#f1f5f9;border:1px solid #e2e8f0;
                     border-radius:4px;cursor:pointer;color:#64748b">清空</button>
    </div>
    <div id="bm-log"
         style="background:#0f172a;color:#94a3b8;font-family:monospace;font-size:12px;
                padding:12px;border-radius:8px;height:220px;overflow-y:auto;white-space:pre-wrap">
等待启动监控…
    </div>
  </div>

</div>

<script>
let allTables = [];

// ============================================================
// 账号卡片（多账号并行配置）
// ============================================================
function _accountCardHTML(idx, cfg) {
  cfg = cfg || {};
  const removable = idx > 0 ? '' : 'display:none';
  // 已有 Coze Token 视为"配过的卡片"，默认折叠；新加的空卡默认展开
  const collapsed = (cfg.cozeToken || '').trim() !== '';
  const bodyStyle = collapsed ? 'display:none' : '';
  const toggleIcon = collapsed ? '▶' : '▼';
  return `
  <div class="account-card" data-acc-idx="${idx}"
       style="padding:12px 14px;background:#f5f3ff;border-radius:10px;margin-bottom:10px;border:1px solid #e9d5ff;position:relative">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px">
      <span class="acc-toggle" onclick="_toggleAccountCard(this)"
            title="点击展开 / 收起"
            style="cursor:pointer;user-select:none;font-size:12px;color:#7c3aed;width:14px;display:inline-block;text-align:center">${toggleIcon}</span>
      <div onclick="_toggleAccountCard(this)"
           title="点击展开 / 收起"
           style="cursor:pointer;font-size:12px;color:#6d28d9;font-weight:700;white-space:nowrap">
        账号 #<span class="acc-num">${idx+1}</span><span class="acc-name-tag" style="color:#a78bfa;margin-left:4px"></span>
      </div>
      <input class="acc-name" placeholder="📛 别名（如：冯会宁组）" value="${cfg.name||''}"
             oninput="_onAccountNameInput(this)" onclick="event.stopPropagation()"
             style="flex:1;max-width:240px;padding:4px 8px;font-size:12px;border:1px solid #ddd6fe;border-radius:6px">
      <button onclick="event.stopPropagation();removeAccountCard(this)" title="移除此账号"
              style="${removable};padding:3px 9px;background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;
                     border-radius:5px;cursor:pointer;font-size:11px">🗑 移除</button>
    </div>
    <div class="acc-body" style="${bodyStyle}">
    <div style="display:flex;gap:10px;margin-bottom:8px">
      <div style="flex:1">
        <label style="font-size:12px;color:#6d28d9;font-weight:600">Coze Token</label>
        <input class="acc-coze-token" placeholder="sat_..." value="${cfg.cozeToken||''}"
               style="border-color:#c4b5fd" oninput="autoSaveConfig()">
      </div>
      <div style="flex:1">
        <label style="font-size:12px;color:#6d28d9;font-weight:600">Workflow ID</label>
        <input class="acc-workflow-id" placeholder="7xxxxxxxxxxxxxxxxx" value="${cfg.workflowId||''}"
               style="border-color:#c4b5fd" oninput="autoSaveConfig()">
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-top:8px">
      <div style="flex:1">
        <label style="font-size:12px;color:#6d28d9;font-weight:600">飞书 Base Token（留空则读取下方下载区配置）</label>
        <input class="acc-base-token" placeholder="留空则使用下方 Base Token" value="${cfg.baseToken||''}"
               oninput="autoSaveConfig()">
      </div>
      <div style="flex:1">
        <label style="font-size:12px;color:#6d28d9;font-weight:600">飞书用户Token
          <span style="font-weight:400;color:#9a3fbf">（写台账，有权限时用）</span>
        </label>
        <input class="acc-feishu-utoken" placeholder="u-xxxxxxxx..." value="${cfg.feishuUserToken||''}"
               oninput="autoSaveConfig()">
      </div>
    </div>
    <div style="margin-top:8px;padding:8px 10px;background:#faf5ff;border-radius:8px;border:1px solid #e9d5ff">
      <div style="font-size:11px;color:#7c3aed;font-weight:700;margin-bottom:6px">
        🔑 飞书 App 认证（推荐，替代用户Token）
      </div>
      <div style="display:flex;gap:10px">
        <div style="flex:1">
          <label style="font-size:11px;color:#6d28d9;font-weight:600">App ID</label>
          <input class="acc-feishu-appid" placeholder="cli_xxxxxxxx" value="${cfg.feishuAppId||''}"
                 style="border-color:#c4b5fd;font-size:12px" oninput="autoSaveConfig()">
        </div>
        <div style="flex:1">
          <label style="font-size:11px;color:#6d28d9;font-weight:600">App Secret</label>
          <input class="acc-feishu-appsecret" placeholder="xxxxxxxxxxxxxxxx" value="${cfg.feishuAppSecret||''}"
                 type="password" style="border-color:#c4b5fd;font-size:12px" oninput="autoSaveConfig()">
        </div>
      </div>
    </div>
    </div>
  </div>`;
}

// 切换账号卡的展开/折叠（el 可以是 toggle 图标，也可以是标题文字 div）
function _toggleAccountCard(el) {
  const card = el.closest('.account-card');
  if (!card) return;
  const body   = card.querySelector('.acc-body');
  const toggle = card.querySelector('.acc-toggle');
  if (!body) return;
  const isHidden = body.style.display === 'none';
  body.style.display = isHidden ? '' : 'none';
  if (toggle) toggle.textContent = isHidden ? '▼' : '▶';
}

// 一键展开/收起所有账号卡
function _toggleAllAccountCards(expand) {
  document.querySelectorAll('#auto-accounts-list .account-card').forEach(card => {
    const body = card.querySelector('.acc-body');
    const toggle = card.querySelector('.acc-toggle');
    if (!body) return;
    body.style.display = expand ? '' : 'none';
    if (toggle) toggle.textContent = expand ? '▼' : '▶';
  });
}

function _renumberAccountCards() {
  document.querySelectorAll('#auto-accounts-list .account-card').forEach((card, i) => {
    card.setAttribute('data-acc-idx', i);
    const num = card.querySelector('.acc-num'); if (num) num.textContent = i + 1;
    const rm  = card.querySelector('button[onclick^="removeAccountCard"]');
    if (rm) rm.style.display = (i === 0 ? 'none' : '');
    // 同步标题中的别名 tag
    const name = (card.querySelector('.acc-name')?.value || '').trim();
    const tag  = card.querySelector('.acc-name-tag');
    if (tag) tag.textContent = name ? (' · ' + name) : '';
  });
  // 账号增减/重命名后，刷新所有产品卡的"补到账号"下拉
  if (typeof _refreshMakeupAccountOptions === 'function') _refreshMakeupAccountOptions();
}

// 别名输入：只更新本卡 tag + 下拉框，不重渲（避免输入框失焦）
function _onAccountNameInput(input) {
  const card = input.closest('.account-card');
  if (!card) return;
  const tag = card.querySelector('.acc-name-tag');
  const name = (input.value || '').trim();
  if (tag) tag.textContent = name ? (' · ' + name) : '';
  if (typeof _refreshMakeupAccountOptions === 'function') _refreshMakeupAccountOptions();
  autoSaveConfig();
}

function addAccountCard(cfg) {
  const list = document.getElementById('auto-accounts-list');
  const idx  = list.querySelectorAll('.account-card').length;
  list.insertAdjacentHTML('beforeend', _accountCardHTML(idx, cfg || {}));
  _renumberAccountCards();
  autoSaveConfig();
}

function removeAccountCard(btn) {
  const card = btn.closest('.account-card');
  if (!card) return;
  card.remove();
  _renumberAccountCards();
  autoSaveConfig();
}

function getAccounts() {
  return Array.from(document.querySelectorAll('#auto-accounts-list .account-card')).map(card => ({
    name:            (card.querySelector('.acc-name')?.value || '').trim(),
    cozeToken:       (card.querySelector('.acc-coze-token').value || '').trim(),
    workflowId:      (card.querySelector('.acc-workflow-id').value || '').trim(),
    baseToken:       (card.querySelector('.acc-base-token').value || '').trim(),
    feishuUserToken: (card.querySelector('.acc-feishu-utoken').value || '').trim(),
    feishuAppId:     (card.querySelector('.acc-feishu-appid').value || '').trim(),
    feishuAppSecret: (card.querySelector('.acc-feishu-appsecret').value || '').trim(),
  }));
}

function renderAccountCards(accounts) {
  const list = document.getElementById('auto-accounts-list');
  list.innerHTML = '';
  (accounts && accounts.length ? accounts : [{}]).forEach((cfg, i) => {
    list.insertAdjacentHTML('beforeend', _accountCardHTML(i, cfg));
  });
  _renumberAccountCards();
}

// ============================================================
// 持久化：加载 & 保存状态
// ============================================================
async function loadState() {
  try {
    const res = await fetch('/api/state');
    const state = await res.json();

    // 1. 还原账号卡片：优先用新版 accounts 数组，否则兼容旧版平铺字段
    const cfg = state.config || {};
    let accounts = Array.isArray(cfg.accounts) ? cfg.accounts : null;
    if (!accounts || !accounts.length) {
      accounts = [{
        cozeToken:       cfg.coze_token       || '',
        workflowId:      cfg.workflow_id      || '',
        baseToken:       cfg.auto_base_token  || '',
        feishuUserToken: cfg.feishu_utoken    || '',
        feishuAppId:     cfg.feishu_appid     || '',
        feishuAppSecret: cfg.feishu_appsecret || '',
      }];
    }
    renderAccountCards(accounts);

    if (cfg.cookie)           document.getElementById('cookie').value = cfg.cookie;
    if (cfg.field_id)         document.getElementById('fieldId').value = cfg.field_id;
    if (cfg.output_dir)       document.getElementById('outputDir').value = cfg.output_dir;
    if (typeof cfg.force_redownload === 'boolean') document.getElementById('forceRedownload').checked = cfg.force_redownload;
    if (typeof cfg.min_size_kb === 'number')       document.getElementById('minSizeKb').value = cfg.min_size_kb;
    if (typeof cfg.make_zip === 'boolean')         document.getElementById('makeZip').checked = cfg.make_zip;
    if (typeof cfg.delete_after_zip === 'boolean') document.getElementById('deleteAfterZip').checked = cfg.delete_after_zip;

    // 多 Base 卡片：优先用新的 dl_bases；否则从旧版 dl_base_token 迁移
    if (Array.isArray(cfg.dl_bases) && cfg.dl_bases.length > 0) {
      _dlBases = cfg.dl_bases.map(b => _newDlBase(b.baseToken || '', b.fieldId || '', b.name || ''));
    } else if (cfg.dl_base_token) {
      _dlBases = [_newDlBase(cfg.dl_base_token, '', '')];
    } else {
      ensureAtLeastOneDlBase();
    }
    renderDlBases();

    // 2. 还原清洗数据（若有）
    const products = state.parsed_products;
    if (Array.isArray(products) && products.length > 0) {
      _parsedProducts = products;
      // 重建摘要
      const total = _parsedProducts.reduce((s, p) => s + p.count, 0);
      const groups = new Set(_parsedProducts.map(p => p.key.split('_')[1] || ''));
      const batchDate = (_parsedProducts[0].key.split('_')[0] || '-');
      document.getElementById('auto-preview-summary').innerHTML =
        '📅 批次: <b>' + batchDate + '</b>'
        + '&nbsp;&nbsp;|&nbsp;&nbsp;🏢 ' + groups.size + ' 个组'
        + '&nbsp;&nbsp;|&nbsp;&nbsp;📦 ' + _parsedProducts.length + ' 个产品'
        + '&nbsp;&nbsp;|&nbsp;&nbsp;📝 共 <b>' + total + '</b> 条'
        + '&nbsp;&nbsp;<span style="font-size:11px;color:#a78bfa">（已从上次保存恢复）</span>';
      rerenderProductList();
      updateAutoSelectCount();
      // 切到 Stage 2
      document.getElementById('auto-s1').style.display = 'none';
      document.getElementById('auto-s2').style.display = 'block';
    }
  } catch(e) { console.warn('loadState 失败:', e); }
  // 防御：即使前面流程异常，也保证至少 1 张 Base 卡片可见
  if (!Array.isArray(_dlBases) || _dlBases.length === 0) {
    ensureAtLeastOneDlBase();
    renderDlBases();
  }
}

// 保存配置字段（防抖 800ms）
let _cfgTimer = null;
function autoSaveConfig() {
  clearTimeout(_cfgTimer);
  _cfgTimer = setTimeout(() => {
    fetch('/api/state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ config: {
        accounts:        getAccounts(),
        dl_bases:        _dlBases.map(b => ({ name: b.name, baseToken: b.baseToken, fieldId: b.fieldId })),
        cookie:          document.getElementById('cookie').value.trim(),
        field_id:        document.getElementById('fieldId').value.trim(),
        output_dir:      document.getElementById('outputDir').value.trim(),
        force_redownload: document.getElementById('forceRedownload').checked,
        min_size_kb:     parseInt(document.getElementById('minSizeKb').value || '50', 10),
        make_zip:        document.getElementById('makeZip').checked,
        delete_after_zip: document.getElementById('deleteAfterZip').checked,
      }})
    });
  }, 800);
}

// 保存清洗数据
function saveParsedProducts() {
  fetch('/api/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ parsed_products: _parsedProducts })
  });
}

// 页面加载完成后自动还原
window.addEventListener('DOMContentLoaded', () => {
  loadState();
  if (typeof _restoreBatchDate === 'function') _restoreBatchDate();
  // 钉钉 Tab 初始化
  if (typeof _restoreDtDefaultDate === 'function') _restoreDtDefaultDate();
  if (typeof dtRefreshStatus === 'function') dtRefreshStatus();
});

// ============================================================
// 📥 钉钉同步 Tab —— 顶部 Tab 切换 + 登录 + 同步基础壳
// ============================================================

// Tab 切换：dingtalk / excel
function switchMode(mode) {
  document.querySelectorAll('.mode-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.mode === mode);
  });
  document.querySelectorAll('.mode-panel').forEach(p => {
    p.classList.toggle('active', p.id === ('mode-' + mode));
  });
  try { localStorage.setItem('mode_tab', mode); } catch(e) {}
}

// 启动时恢复上次选择的 Tab（默认 dingtalk）
(function _restoreModeTab(){
  try {
    const m = localStorage.getItem('mode_tab');
    if (m === 'excel' || m === 'dingtalk') {
      // 等 DOM 渲染后再切换
      window.addEventListener('DOMContentLoaded', () => switchMode(m));
    }
  } catch(e) {}
})();

// 今天的月.日 格式
function _todayMD() {
  const d = new Date();
  return (d.getMonth() + 1) + '.' + d.getDate();
}

// 默认日期持久化（localStorage）
function _persistDtDefaultDate(v) {
  try { localStorage.setItem('dt_default_date', v || ''); } catch(e) {}
}
function _restoreDtDefaultDate() {
  const inp = document.getElementById('dt-default-date');
  if (!inp) return;
  let v = '';
  try { v = localStorage.getItem('dt_default_date') || ''; } catch(e) {}
  inp.value = v || _todayMD();
}

// 刷新登录状态：调 /api/dingtalk/status
async function dtRefreshStatus() {
  const sEl = document.getElementById('dt-status');
  const bL  = document.getElementById('dt-btn-login');
  const bO  = document.getElementById('dt-btn-logout');
  const bS  = document.getElementById('dt-btn-sync');
  if (!sEl) return;
  sEl.className = 'dt-status wait';
  sEl.textContent = '⚪ 检查中…';
  try {
    const r = await fetch('/api/dingtalk/status').then(r => r.json());
    if (r.logged_in) {
      sEl.className = 'dt-status ok';
      sEl.textContent = '✅ 钉钉已登录';
      if (bL) bL.style.display = 'none';
      if (bO) bO.style.display = '';
      if (bS) bS.disabled = false;
    } else {
      sEl.className = 'dt-status no';
      sEl.textContent = '❌ 未登录钉钉';
      if (bL) bL.style.display = '';
      if (bO) bO.style.display = 'none';
      if (bS) bS.disabled = true;
    }
  } catch(e) {
    sEl.className = 'dt-status no';
    sEl.textContent = '⚠️ 状态获取失败';
    if (bS) bS.disabled = true;
  }
}

// 触发扫码登录（弹出 pywebview 子窗口）
async function dtLogin() {
  const b = document.getElementById('dt-btn-login');
  if (b) { b.disabled = true; b.textContent = '⏳ 请在弹出窗口扫码…'; }
  try {
    const r = await fetch('/api/dingtalk/login', { method: 'POST' }).then(r => r.json());
    if (r.ok) {
      alert('✅ 钉钉登录成功');
    } else {
      alert('❌ 登录失败：' + (r.msg || '未知错误'));
    }
  } catch(e) {
    alert('❌ 登录请求异常：' + e.message);
  } finally {
    if (b) { b.disabled = false; b.textContent = '🔑 钉钉登录'; }
    dtRefreshStatus();
  }
}

// 登出（清本地 session）
async function dtLogout() {
  if (!confirm('确定要清除本地钉钉登录信息吗？\\n（下次同步前需要重新扫码）')) return;
  try {
    await fetch('/api/dingtalk/logout', { method: 'POST' });
  } catch(e) {}
  dtRefreshStatus();
}

// 缓存：上一次同步出的产品列表 + 模式映射 + 需求数量映射
let _dtProducts   = [];   // [{group, product_name, sheet, key, title, headers, rows, available_dates, ...}]
let _dtModeMap    = {};   // { "组||品类||日期": 1 or 2 }
let _dtDemandMap  = {};   // { "组||品类||日期": count(需求数量) }

// 同步钉钉数据：调 /api/dingtalk/sync
async function dtSync() {
  const b = document.getElementById('dt-btn-sync');
  const list = document.getElementById('dt-product-list');
  const sum = document.getElementById('dt-summary');
  if (b) { b.disabled = true; b.textContent = '⏳ 同步中…'; }
  if (list) list.innerHTML = '<div style="text-align:center;padding:30px;color:#10b981">⏳ 正在从钉钉拉取数据…</div>';
  try {
    const r = await fetch('/api/dingtalk/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then(r => r.json());
    if (!r.ok) {
      if (list) list.innerHTML = '<div style="color:#dc2626;padding:20px">❌ 同步失败：' + (r.msg || '未知错误') + '</div>';
      return;
    }
    _dtProducts  = r.products || [];
    _dtModeMap   = r.mode_map || {};
    _dtDemandMap = r.demand_map || {};
    if (sum) {
      sum.style.display = '';
      sum.innerHTML = '✅ 同步成功 · <b>' + (r.product_count || 0) + '</b> 个产品 · <b>'
                    + (r.mode_map_count || 0) + '</b> 条模式映射 · 时间 ' + new Date().toLocaleTimeString();
    }
    _renderDtProducts();
  } catch(e) {
    if (list) list.innerHTML = '<div style="color:#dc2626;padding:20px">❌ 同步异常：' + e.message + '</div>';
  } finally {
    if (b) { b.disabled = false; b.textContent = '📥 同步钉钉数据'; }
  }
}

// ── 扁平产品卡 + 多任务行（阶段 2.2） ──
// 任务行状态：{ productKey: [{date, count}, ...] }
let _dtTaskRows = {};
// 选中产品（productKey 集合）
let _dtSelected = new Set();

// 名称归一（与后端 _norm_name 保持一致：去括号备注 + 转小写 + 去空白）
function _dtNormName(s) {
  return String(s || '')
    .replace(/[\\(（][^\\)）]*[\\)）]/g, '')
    .replace(/\\s+/g, '')
    .toLowerCase();
}

// 日期归一（与后端 _date_key 保持一致：M.D，无前导 0；尾点去掉）
function _dtNormDate(s) {
  if (s === null || s === undefined) return '';
  let v = String(s).trim().replace(/\\.$/, '');
  // 月.日
  const m = v.match(/^(\\d{1,2})[\\.\\-月](\\d{1,2})$/);
  if (m) return String(parseInt(m[1], 10)) + '.' + String(parseInt(m[2], 10));
  // M月D日
  const m2 = v.match(/^(\\d{1,2})月(\\d{1,2})日?$/);
  if (m2) return String(parseInt(m2[1], 10)) + '.' + String(parseInt(m2[2], 10));
  return v;
}

// 模式查询：(sheet 即组, product_name, date) → 1 / 2 / 0
function _dtLookupMode(sheet, productName, date) {
  const k = (sheet || '') + '||' + _dtNormName(productName) + '||' + _dtNormDate(date);
  return _dtModeMap[k] || 0;
}

// 产品唯一 key：sheet :: productName（用于状态映射，不进副表名）
function _dtProductKey(p) {
  return (p.sheet || '') + '::' + (p.product_name || p.title || '');
}

// 在 ['5.15','5.16','5.22'] 这种 M.D 字符串数组里取最晚日期
function _dtLatestDate(dates) {
  if (!dates || !dates.length) return '';
  return dates.slice().sort((a, b) => {
    const [am, ad] = String(a).split('.').map(Number);
    const [bm, bd] = String(b).split('.').map(Number);
    return (am - bm) || (ad - bd);
  }).pop();
}

// 反查汇总表里某产品被排了任务的所有日期（按 M.D 排序）
function _dtTaskDatesForProduct(p) {
  const sheet = p.sheet || '';
  const pn = _dtNormName(p.product_name || p.title || '');
  const prefix = sheet + '||' + pn + '||';
  const out = [];
  for (const k of Object.keys(_dtModeMap)) {
    if (k.startsWith(prefix)) out.push(k.slice(prefix.length));
  }
  return out.sort((a, b) => {
    const [am, ad] = String(a).split('.').map(Number);
    const [bm, bd] = String(b).split('.').map(Number);
    return (am - bm) || (ad - bd);
  });
}

// 卡片下拉里要列出的日期：
//   - 汇总表有任务 → 只列任务日期（方案 A 核心）
//   - 汇总表无任务 → 兜底列模板日期，让产品仍可手动跑（模式 1 fallback）
function _dtSelectableDates(p) {
  const tasks = _dtTaskDatesForProduct(p);
  if (tasks.length) return tasks;
  return (p.available_dates || []).slice();
}

// 决定某产品的默认日期：
//   1. 工具栏默认日期（前提：它在可选日期里）
//   2. 否则取可选日期的最晚那个
//   3. 仍空 → 用工具栏默认（兜底，避免 undefined）
function _dtPickDefaultDate(p) {
  const defaultDate = (document.getElementById('dt-default-date')?.value || '').trim() || _todayMD();
  const opts = _dtSelectableDates(p);
  if (opts.indexOf(defaultDate) >= 0) return defaultDate;
  if (opts.length) return _dtLatestDate(opts);
  return defaultDate;
}

// 确保某产品至少有 1 行任务（默认日期 = 工具栏默认日期，条数空）
function _dtEnsureTaskRows(p) {
  const k = _dtProductKey(p);
  if (!_dtTaskRows[k] || _dtTaskRows[k].length === 0) {
    _dtTaskRows[k] = [{ date: _dtPickDefaultDate(p), count: '' }];
  }
  return _dtTaskRows[k];
}

// 渲染产品列表（扁平产品卡 + 多任务行）
function _renderDtProducts() {
  const list = document.getElementById('dt-product-list');
  const acts = document.getElementById('dt-actions');
  const bulk = document.getElementById('dt-bulk-bar');
  if (!list) return;
  if (!_dtProducts.length) {
    list.innerHTML = '<div style="padding:30px;text-align:center;color:#9ca3af">同步成功但没有任何产品</div>';
    if (acts) acts.style.display = 'none';
    if (bulk) bulk.style.display = 'none';
    return;
  }
  // 清理已不存在的产品的任务/选中状态
  const currentKeys = new Set(_dtProducts.map(_dtProductKey));
  Object.keys(_dtTaskRows).forEach(k => { if (!currentKeys.has(k)) delete _dtTaskRows[k]; });
  _dtSelected.forEach(k => { if (!currentKeys.has(k)) _dtSelected.delete(k); });

  // 还原 list 容器为默认样式（去掉空态背景）
  list.style.background = 'transparent';
  list.style.border = 'none';
  list.style.padding = '0';
  list.style.color = '#374151';
  list.style.textAlign = 'left';

  // 按 sheet 分组
  const bySheet = {};
  _dtProducts.forEach(p => {
    const s = p.sheet || '未知组';
    if (!bySheet[s]) bySheet[s] = [];
    bySheet[s].push(p);
  });

  let html = '';
  Object.keys(bySheet).sort().forEach(s => {
    const items = bySheet[s];
    const safeS = escHtml(s).replace(/'/g, "\\\\'");
    html += '<div class="dt-sheet-group">'
         +   '<div class="dt-sheet-header">'
         +     '<span style="flex:1">👥 ' + escHtml(s) + '（' + items.length + ' 个产品）</span>'
         +     '<button class="dt-btn ghost" style="padding:3px 10px;font-size:11px"'
         +       ' onclick="dtToggleSheetSelect(\\'' + safeS + '\\')">'
         +       '☑ 本组全选/反选</button>'
         +     '<button style="padding:3px 10px;font-size:11px;background:#d1fae5;color:#065f46;'
         +       'border:1px solid #6ee7b7;border-radius:6px;cursor:pointer;font-weight:600"'
         +       ' onclick="dtAutoFill(\\'sheet\\',\\'' + safeS + '\\')">'
         +       '✨ 自动填入</button>'
         +     '<button style="padding:3px 10px;font-size:11px;background:#fee2e2;color:#b91c1c;'
         +       'border:1px solid #fca5a5;border-radius:6px;cursor:pointer;font-weight:600"'
         +       ' onclick="dtClearFill(\\'sheet\\',\\'' + safeS + '\\')">'
         +       '🗑 清除</button>'
         +   '</div>'
         +   '<div class="dt-sheet-body">';
    items.forEach(p => { html += _dtCardHTML(p); });
    html += '</div></div>';
  });
  list.innerHTML = html;
  dtUpdateSelectCount();
  dtUpdateTaskSummary();
  if (acts) { acts.style.display = 'flex'; }
  if (bulk) bulk.style.display = 'flex';
}

// 单张产品卡 HTML
function _dtCardHTML(p) {
  const k = _dtProductKey(p);
  const rows = _dtEnsureTaskRows(p);
  const isSel = _dtSelected.has(k);
  const avail = p.available_dates || [];
  const taskDates = _dtTaskDatesForProduct(p);   // 汇总表里该产品被排任务的日期（已按 M.D 排序）
  const hasTasks = taskDates.length > 0;
  const safeK = k.replace(/'/g, "\\\\'");

  // 多任务行
  const rowsHtml = rows.map((row, ri) => {
    const mode = _dtLookupMode(p.sheet, p.product_name, row.date);
    const tagCls = mode === 2 ? 'm2' : (mode === 1 ? '' : 'unknown');
    const tagTxt = mode === 0 ? '未配置' : ('模式 ' + mode);
    const tagTitle = mode === 2 ? '模式 2：当前日期 + 最近前一天各自随机组合，条数均分'
                                  : (mode === 1 ? '模式 1：仅该日期模板随机组合'
                                                  : '汇总表未排该产品任务，执行时按模式 1 处理（兜底）');
    // 日期下拉：方案 A —— 只列汇总表任务日期；若无任务则兜底列模板日期
    const dateOpts = _dtSelectableDates(p);
    if (row.date && dateOpts.indexOf(row.date) < 0) dateOpts.unshift(row.date);
    const optsHtml = dateOpts.map(d =>
      '<option value="' + escHtml(d) + '"' + (d === row.date ? ' selected' : '') + '>'
      + escHtml(d) + '</option>'
    ).join('');
    return '<div class="dt-task-row" data-row="' + ri + '">'
         +   '<span style="color:#9ca3af;width:18px">#' + (ri + 1) + '</span>'
         +   '<select onchange="dtUpdateTaskDate(\\'' + safeK + '\\',' + ri + ',this.value)"'
         +     ' title="任务日期">' + optsHtml + '</select>'
         +   '<span style="color:#6b7280">×</span>'
         +   '<input type="number" class="dt-count" min="1" max="999" placeholder="?"'
         +     ' value="' + (row.count || '') + '"'
         +     ' oninput="dtUpdateTaskCount(\\'' + safeK + '\\',' + ri + ',this.value)"'
         +     ' title="想生成多少条">'
         +   '<span style="color:#6b7280">条</span>'
         +   '<span class="dt-mode-tag ' + tagCls + '" title="' + escHtml(tagTitle) + '">' + tagTxt + '</span>'
         +   '<button class="dt-row-del" title="删除该任务行"'
         +     ' onclick="dtDeleteTaskRow(\\'' + safeK + '\\',' + ri + ')">🗑</button>'
         + '</div>';
  }).join('');

  // 卡片头：汇总表任务日期为主；原料模板日期作为灰色辅助信息
  const taskText = hasTasks
    ? ('<b>' + escHtml(taskDates.join(' / ')) + '</b>')
    : '<span style="color:#dc2626">未排任务</span>';
  const tplText = avail.length ? avail.join(' / ') : '无';
  return '<div class="dt-card' + (isSel ? ' selected' : '') + '" data-key="' + escHtml(k) + '">'
       +   '<div class="dt-card-head">'
       +     '<input type="checkbox" class="dt-chk" data-key="' + escHtml(k) + '"'
       +       (isSel ? ' checked' : '')
       +       ' onchange="dtToggleProductSelect(\\'' + safeK + '\\',this.checked)"'
       +       ' style="width:auto;accent-color:#10b981;cursor:pointer">'
       +     '<span class="dt-card-name">' + escHtml(p.product_name || p.title || '?') + '</span>'
       +     '<span class="dt-card-meta">汇总表任务: ' + taskText + '</span>'
       +     '<span class="dt-card-meta" style="color:#9ca3af">原料模板: '
       +       escHtml(tplText) + '（' + (p.template_count || 0) + ' 行）</span>'
       +   '</div>'
       +   '<div class="dt-task-rows">' + rowsHtml + '</div>'
       +   '<button class="dt-task-add" onclick="dtAddTaskRow(\\'' + safeK + '\\')">+ 新增任务行</button>'
       + '</div>';
}

// 通过 productKey 查到 product 对象
function _dtFindByKey(k) {
  for (const p of _dtProducts) { if (_dtProductKey(p) === k) return p; }
  return null;
}

// 增任务行（默认日期复用 _dtPickDefaultDate：汇总表任务日期优先）
function dtAddTaskRow(k) {
  const p = _dtFindByKey(k);
  if (!p) return;
  const rows = _dtTaskRows[k] || (_dtTaskRows[k] = []);
  rows.push({ date: _dtPickDefaultDate(p), count: '' });
  _renderDtProducts();
}

// 删任务行（若删完了，自动补一条空行）
function dtDeleteTaskRow(k, ri) {
  const rows = _dtTaskRows[k];
  if (!rows) return;
  rows.splice(ri, 1);
  if (rows.length === 0) delete _dtTaskRows[k];   // _dtEnsureTaskRows 会重建
  _renderDtProducts();
}

// 改任务行日期 → 局部更新模式 tag，避免整页重渲（保留焦点）
function dtUpdateTaskDate(k, ri, val) {
  const rows = _dtTaskRows[k];
  if (!rows || !rows[ri]) return;
  rows[ri].date = val;
  const p = _dtFindByKey(k);
  if (!p) return;
  // 局部刷新该行的 mode-tag
  const card = document.querySelector('.dt-card[data-key="' + CSS.escape(k) + '"]');
  if (!card) return;
  const rowEl = card.querySelector('.dt-task-row[data-row="' + ri + '"]');
  if (!rowEl) return;
  const tag = rowEl.querySelector('.dt-mode-tag');
  if (!tag) return;
  const mode = _dtLookupMode(p.sheet, p.product_name, val);
  tag.classList.remove('m2', 'unknown');
  if (mode === 2) tag.classList.add('m2');
  else if (mode === 0) tag.classList.add('unknown');
  tag.textContent = mode === 0 ? '未配置' : ('模式 ' + mode);
  tag.title = mode === 2 ? '模式 2：当前日期 + 最近前一天各自随机组合，条数均分'
                            : (mode === 1 ? '模式 1：仅该日期模板随机组合'
                                            : '汇总表未排该产品任务，执行时按模式 1 处理（兜底）');
  dtUpdateTaskSummary();
}

// 改任务行条数（input 事件，频繁触发；不重渲，只更状态 + 摘要）
function dtUpdateTaskCount(k, ri, val) {
  const rows = _dtTaskRows[k];
  if (!rows || !rows[ri]) return;
  const n = parseInt(val, 10);
  rows[ri].count = (isNaN(n) || n <= 0) ? '' : n;
  dtUpdateTaskSummary();
}

// 自动填入需求数量
// scope: 'all' | 'sheet' | 'product'
// key:   scope='sheet' 时为组名，scope='product' 时为 productKey
function dtAutoFill(scope, key) {
  let targets = [];
  if (scope === 'all') {
    targets = _dtProducts;
  } else if (scope === 'sheet') {
    targets = _dtProducts.filter(p => p.sheet === key);
  } else {
    targets = _dtProducts.filter(p => _dtProductKey(p) === key);
  }
  let filled = 0;
  targets.forEach(p => {
    const k = _dtProductKey(p);
    const rows = _dtEnsureTaskRows(p);
    rows.forEach((row, ri) => {
      const demandKey = p.sheet + '||' + _dtNormName(p.product_name) + '||' + _dtNormDate(row.date);
      const cnt = _dtDemandMap[demandKey];
      if (cnt && cnt > 0) {
        rows[ri].count = cnt;
        filled++;
      }
    });
  });
  if (filled > 0) {
    _renderDtProducts();
    dtUpdateTaskSummary();
  } else {
    alert('没有找到对应的需求数量（请确认汇总表里该产品/日期有填写需求数量）');
  }
}

// 清除需求数量
// scope: 'all' | 'sheet' | 'product'
// key:   scope='sheet' 时为组名，scope='product' 时为 productKey
function dtClearFill(scope, key) {
  let targets = [];
  if (scope === 'all') {
    targets = _dtProducts;
  } else if (scope === 'sheet') {
    targets = _dtProducts.filter(p => p.sheet === key);
  } else {
    targets = _dtProducts.filter(p => _dtProductKey(p) === key);
  }
  targets.forEach(p => {
    const k = _dtProductKey(p);
    if (_dtTaskRows[k]) {
      _dtTaskRows[k].forEach((row, ri) => { _dtTaskRows[k][ri].count = ''; });
    }
  });
  _renderDtProducts();
  dtUpdateTaskSummary();
}

// 选中切换
function dtToggleProductSelect(k, checked) {
  if (checked) _dtSelected.add(k); else _dtSelected.delete(k);
  const card = document.querySelector('.dt-card[data-key="' + CSS.escape(k) + '"]');
  if (card) card.classList.toggle('selected', checked);
  dtUpdateSelectCount();
}

// 全选 / 全不选(三态切换:0→全选,部分→清空,全选→清空)
function dtToggleSelectAll() {
  const total = _dtProducts.length;
  const target = (_dtSelected.size === 0);   // 任何已选状态(含部分)都视为"取消"
  if (target) _dtProducts.forEach(p => _dtSelected.add(_dtProductKey(p)));
  else _dtSelected.clear();
  document.querySelectorAll('.dt-chk').forEach(c => { c.checked = target; });
  document.querySelectorAll('.dt-card').forEach(c => c.classList.toggle('selected', target));
  dtUpdateSelectCount();
}

// 组级全选/反选（同 sheet 下全部）
function dtToggleSheetSelect(sheet) {
  const inSheet = _dtProducts.filter(p => (p.sheet || '未知组') === sheet);
  const keys = inSheet.map(_dtProductKey);
  const allOn = keys.every(k => _dtSelected.has(k));
  keys.forEach(k => {
    if (allOn) _dtSelected.delete(k); else _dtSelected.add(k);
    const card = document.querySelector('.dt-card[data-key="' + CSS.escape(k) + '"]');
    if (card) {
      card.classList.toggle('selected', !allOn);
      const chk = card.querySelector('.dt-chk');
      if (chk) chk.checked = !allOn;
    }
  });
  dtUpdateSelectCount();
}

// 更新「全选」+「选中产品数」UI
function dtUpdateSelectCount() {
  const total = _dtProducts.length;
  const sel = _dtSelected.size;
  const span = document.getElementById('dt-sel-count');
  const chk = document.getElementById('dt-chk-all');
  if (span) span.textContent = sel === 0 ? '尚未选中任何产品' : '已选 ' + sel + ' / ' + total + ' 个产品';
  if (chk) {
    chk.checked = (sel === total && total > 0);
    chk.indeterminate = (sel > 0 && sel < total);
  }
  dtUpdateTaskSummary();
}

// 更新执行区的任务摘要（选中产品 / 总任务行 / 总条数）
function dtUpdateTaskSummary() {
  const span = document.getElementById('dt-task-summary');
  if (!span) return;
  let nProd = 0, nRow = 0, nCnt = 0;
  _dtSelected.forEach(k => {
    const rows = (_dtTaskRows[k] || []).filter(r => parseInt(r.count, 10) > 0);
    if (!rows.length) return;
    nProd++;
    nRow += rows.length;
    rows.forEach(r => { nCnt += parseInt(r.count, 10); });
  });
  span.textContent = nProd === 0
    ? '请勾选产品并填条数后执行'
    : '将执行：' + nProd + ' 个产品 · ' + nRow + ' 个任务行 · 共 ' + nCnt + ' 条视频';
}

// ============================================================
// 钉钉同步 → 步骤 1：展开预览
// ============================================================
let _dtPreviewResults = null;   // 缓存最近一次 expand-tasks 的 results，供「确认执行」用

function _dtCollectPayload() {
  const payload = [];
  _dtSelected.forEach(k => {
    const p = _dtFindByKey(k);
    if (!p) return;
    const rows = (_dtTaskRows[k] || []).filter(r => parseInt(r.count, 10) > 0);
    if (!rows.length) return;
    payload.push({
      sheet: p.sheet, product_name: p.product_name,
      tasks: rows.map(r => ({ date: r.date, count: parseInt(r.count, 10) })),
    });
  });
  return payload;
}

function dtPreview() {
  const payload = _dtCollectPayload();
  if (!payload.length) {
    alert('请先勾选产品并在任务行填条数（≥1）');
    return;
  }
  const btn = document.getElementById('dt-btn-exec');
  btn.disabled = true; btn.textContent = '⏳ 展开中...';

  fetch('/api/dingtalk/expand-tasks', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ products: payload }),
  })
  .then(r => r.json())
  .then(d => {
    btn.disabled = false; btn.textContent = '📋 展开预览';
    if (!d.ok) { alert('展开失败：' + (d.error || '未知错误')); return; }
    _dtPreviewResults = d.results || [];
    _dtRenderPreview(d.summary || {}, _dtPreviewResults);
  })
  .catch(e => {
    btn.disabled = false; btn.textContent = '📋 展开预览';
    alert('请求失败：' + e);
  });
}

function _dtRenderPreview(summary, results) {
  const modal = document.getElementById('dt-preview-modal');
  const sum   = document.getElementById('dt-preview-summary');
  const warn  = document.getElementById('dt-preview-warnings');
  const list  = document.getElementById('dt-preview-list');

  sum.textContent = `${summary.product_count || 0} 个产品 · ${summary.task_count || 0} 个副表 · 共 ${summary.total_rows || 0} 行视频`;

  const warns = summary.warnings || [];
  if (warns.length) {
    warn.style.display = '';
    warn.innerHTML = warns.map(w => '<div>' + escHtml(w) + '</div>').join('');
  } else {
    warn.style.display = 'none';
  }

  if (!results.length) {
    list.innerHTML = '<div style="padding:40px 20px;text-align:center;color:#6b7280;font-size:14px">没有可展开的任务（请检查日期和条数）</div>';
    document.getElementById('dt-btn-confirm').disabled = true;
  } else {
    document.getElementById('dt-btn-confirm').disabled = false;
    list.innerHTML = results.map((r, i) => {
      const modeTag = r.mode === 2
        ? '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600">模式2 混合</span>'
        : '<span style="background:#ede9fe;color:#6d28d9;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600">模式1</span>';
      const downgraded = (r.mode_requested === 2 && r.mode === 1)
        ? '<span style="color:#dc2626;font-size:11px;margin-left:4px">⚠ 已降级</span>' : '';
      return `<div style="padding:10px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;gap:12px;background:#fff;transition:background .1s"
              onmouseenter="this.style.background='#f9fafb'" onmouseleave="this.style.background='#fff'">
        <span style="color:#d1d5db;font-size:12px;width:30px;text-align:right">#${i+1}</span>
        <code style="background:#f3f4f6;padding:3px 8px;border-radius:5px;font-size:12px;color:#111;flex:1;word-break:break-all">${escHtml(r.key)}</code>
        <span style="font-size:12px;color:#6b7280;white-space:nowrap">${r.count} 行</span>
        ${modeTag}${downgraded}
      </div>`;
    }).join('');
  }
  // 弹出模态框
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';
}

function dtCancelPreview() {
  document.getElementById('dt-preview-modal').style.display = 'none';
  document.body.style.overflow = '';
  _dtPreviewResults = null;
}

// ============================================================
// 钉钉同步 → 步骤 2：确认执行（复用 /api/run-coze-cdp）
// ============================================================
function dtConfirmExecute() {
  if (!_dtPreviewResults || !_dtPreviewResults.length) {
    alert('没有可执行的任务，请先展开预览');
    return;
  }

  // 收集账号（沿用现有账号区）
  const dlBase = (_dlBases && _dlBases[0] && _dlBases[0].baseToken) ? _dlBases[0].baseToken : '';
  const accounts = (typeof getAccounts === 'function' ? getAccounts() : []).map(a => ({
    ...a, baseToken: a.baseToken || dlBase,
  }));
  if (!accounts.length) { alert('请至少配置 1 个账号（顶部账号区）'); return; }
  for (let i = 0; i < accounts.length; i++) {
    if (!accounts[i].cozeToken)  { alert(`账号 #${i+1} 缺少 Coze Token`); return; }
    if (!accounts[i].workflowId) { alert(`账号 #${i+1} 缺少 Workflow ID`); return; }
  }

  // results → /api/run-coze-cdp 期望的 {key, title, data} 结构
  const products = _dtPreviewResults.map(r => ({
    key:   r.key,
    title: r.title,
    data:  [r.title,
            (r.headers || []).join(' // '),
            ...(r.rows || []).map(row => row.join(' // '))],
  }));

  const btn     = document.getElementById('dt-btn-confirm');
  const logWrap = document.getElementById('dt-log-wrap');
  const log     = document.getElementById('dt-log');
  btn.disabled = true; btn.textContent = '⏳ 执行中...';
  logWrap.style.display = '';
  log.innerHTML = '<div style="color:#a78bfa">⚡ 开始执行：' + accounts.length + ' 个账号并行 · ' + products.length + ' 个副表（Coze + 飞书建表）...</div>';

  fetch('/api/run-coze-cdp', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ products, accounts }),
  }).then(res => {
    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) { btn.disabled = false; btn.textContent = '⚡ 确认执行（Coze + 飞书建表）'; return; }
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const payload = line.slice(6);
          if (payload === '__DONE__') {
            btn.disabled = false;
            btn.textContent = '⚡ 确认执行（Coze + 飞书建表）';
            const tip = document.createElement('div');
            tip.style.color = '#4ade80'; tip.style.marginTop = '6px';
            tip.textContent = '🎉 全部完成';
            log.appendChild(tip);
            log.scrollTop = log.scrollHeight;
            return;
          }
          try {
            const obj = JSON.parse(payload);
            const div = document.createElement('div');
            const msg = obj.msg || '';
            div.textContent = msg;
            if (msg.startsWith('✅') || msg.startsWith('🎉')) div.style.color = '#4ade80';
            else if (msg.startsWith('❌')) div.style.color = '#f87171';
            else if (msg.startsWith('⏳') || msg.startsWith('🤖') || msg.startsWith('🗂')) div.style.color = '#60a5fa';
            else if (msg.startsWith('  ✅')) div.style.color = '#86efac';
            else if (msg.startsWith('  ❌')) div.style.color = '#fca5a5';
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
          } catch(e) {}
        });
        read();
      });
    }
    read();
  }).catch(e => {
    log.innerHTML += '<div style="color:#f87171">❌ 请求失败: ' + e + '</div>';
    btn.disabled = false; btn.textContent = '⚡ 确认执行（Coze + 飞书建表）';
  });
}

function dtCopyLog() {
  const log = document.getElementById('dt-log');
  const text = log.innerText || log.textContent || '';
  if (!text.trim()) { alert('日志为空'); return; }
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('#dt-log-wrap button');
    if (btn) {
      const old = btn.textContent;
      btn.textContent = '✓ 已复制'; setTimeout(() => btn.textContent = old, 1500);
    }
  });
}

async function pickFolder() {
  try {
    const path = await window.pywebview.api.pick_folder();
    if (path) document.getElementById('outputDir').value = path;
  } catch(e) {
    alert('文件夹选择功能仅在桌面窗口中可用');
  }
}

function showHelp() {
  alert("如何获取飞书 Cookie：\\n\\n" +
    "1. 用 Chrome 打开 my.feishu.cn 并登录\\n" +
    "2. 按 F12 → Network（网络）\\n" +
    "3. 刷新页面，点任意一个请求\\n" +
    "4. 在 Request Headers 里找到 cookie 行\\n" +
    "5. 右键 → 复制值，粘贴到 Cookie 输入框\\n\\n" +
    "或者：Application → Cookies → https://my.feishu.cn → 全选 → 复制");
}

// ============================================================
// 下载：多 Base 卡片管理
// ============================================================
// 每个元素：{ baseToken, fieldId, tables: [{id, name}], checked: Set<id> }
let _dlBases = [];

function _newDlBase(token='', fieldId='', name='') {
  return { name: name, baseToken: token, fieldId: fieldId, tables: [], checked: new Set() };
}

function ensureAtLeastOneDlBase() {
  if (_dlBases.length === 0) _dlBases.push(_newDlBase('J90LbKuJnaJvfWsjrIYcDJHlnSc'));
}

function addDlBase() {
  _dlBases.push(_newDlBase());
  renderDlBases();
  autoSaveConfig();
}

function removeDlBase(idx) {
  if (_dlBases.length <= 1) { alert('至少保留 1 个 Base'); return; }
  _dlBases.splice(idx, 1);
  renderDlBases();
  autoSaveConfig();
}

function _onDlBaseTokenInput(idx, val) {
  _dlBases[idx].baseToken = val.trim();
  autoSaveConfig();
}
function _onDlBaseFieldIdInput(idx, val) {
  _dlBases[idx].fieldId = val.trim();
  autoSaveConfig();
}
function _onDlBaseNameInput(idx, val) {
  _dlBases[idx].name = val.trim();
  autoSaveConfig();
  // 仅更新该卡片标题，避免重渲染丢失输入焦点
  const lbl = document.getElementById('dl-base-title-' + idx);
  if (lbl) lbl.textContent = _renderDlBaseTitle(idx);
}

function _renderDlBaseTitle(idx) {
  const b = _dlBases[idx] || {};
  return b.name ? `Base #${idx+1} · ${b.name}` : `Base #${idx+1}`;
}

function renderDlBases() {
  const container = document.getElementById('dl-bases-container');
  container.innerHTML = _dlBases.map((b, idx) => {
    const tablesHtml = b.tables.length === 0
      ? '<div style="color:#94a3b8;font-size:13px;padding:12px;text-align:center">尚未拉取副表，点击右侧「🔄 拉取」或顶部「🔗 一键拉取」</div>'
      : b.tables.map((t, ti) => {
          const parts = t.name.split('_');
          const start = parts.length >= 3 && parts[0].includes('.') ? 1 : 0;
          const group   = parts[start]   || t.name;
          const product = parts.slice(start+1).join('_') || t.name;
          const checked = b.checked.has(t.id) ? 'checked' : '';
          return `<div class="table-item">
            <input type="checkbox" id="cb_${idx}_${ti}" value="${t.id}" ${checked}
              onchange="_onDlTableToggle(${idx}, '${t.id}', this.checked)">
            <label for="cb_${idx}_${ti}" style="cursor:pointer;flex:1">${t.name}</label>
            <span class="badge">📁 ${group} / ${product}</span>
          </div>`;
        }).join('');
    const checkedCount = b.tables.filter(t => b.checked.has(t.id)).length;
    return `<div style="margin-bottom:14px;padding:12px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <span id="dl-base-title-${idx}" style="font-weight:600;color:#4338ca;font-size:14px">${_renderDlBaseTitle(idx)}</span>
        <span style="font-size:12px;color:#64748b">${b.tables.length>0 ? `共 ${b.tables.length} 副表，已勾 ${checkedCount}` : ''}</span>
        <button onclick="fetchDlBaseTables(${idx})" style="margin-left:auto;padding:5px 12px;background:#6366f1;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px">🔄 拉取</button>
        <button onclick="removeDlBase(${idx})" style="padding:5px 10px;background:#ef4444;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px">🗑 移除</button>
      </div>
      <div class="row" style="gap:10px">
        <div style="flex:1">
          <label style="font-size:12px">📛 别名（日志和卡片标题显示）</label>
          <input value="${(b.name||'').replace(/"/g,'&quot;')}" placeholder="比如 冯会宁组 / 刘原原组"
            oninput="_onDlBaseNameInput(${idx}, this.value)">
        </div>
        <div style="flex:2">
          <label style="font-size:12px">Base Token</label>
          <input value="${(b.baseToken||'').replace(/"/g,'&quot;')}" placeholder="J90LbKuJnaJvfWsjrIYcDJHlnSc"
            oninput="_onDlBaseTokenInput(${idx}, this.value)">
        </div>
        <div style="flex:1">
          <label style="font-size:12px">视频字段 ID（留空则用默认）</label>
          <input value="${(b.fieldId||'').replace(/"/g,'&quot;')}" placeholder="fldksNMEZ4"
            oninput="_onDlBaseFieldIdInput(${idx}, this.value)">
        </div>
      </div>
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
        <button onclick="_checkAllDlBase(${idx}, true)" style="padding:4px 10px;font-size:12px">全选</button>
        <button onclick="_checkAllDlBase(${idx}, false)" style="padding:4px 10px;font-size:12px">取消全选</button>
      </div>
      <div class="table-scroll" style="margin-top:8px;max-height:260px">${tablesHtml}</div>
    </div>`;
  }).join('');
}

function _checkAllDlBase(idx, val) {
  const b = _dlBases[idx];
  if (val) b.tables.forEach(t => b.checked.add(t.id));
  else b.checked.clear();
  renderDlBases();
}

function _onDlTableToggle(idx, tid, checked) {
  const set = _dlBases[idx].checked;
  if (checked) set.add(tid); else set.delete(tid);
}

async function fetchDlBaseTables(idx) {
  const cookie = document.getElementById('cookie').value.trim();
  if (!cookie) { alert('请先填写飞书 Cookie'); return; }
  const b = _dlBases[idx];
  if (!b.baseToken) { alert(`Base #${idx+1} 未填写 Base Token`); return; }
  try {
    const res = await fetch('/api/tables-multi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ cookie, baseTokens: [b.baseToken] })
    });
    const data = await res.json();
    if (!data.ok) { alert('❌ ' + data.msg); return; }
    const r = (data.results || [])[0];
    if (!r || !r.ok) { alert(`Base #${idx+1} 拉取失败: ${r ? r.msg : '空结果'}`); return; }
    b.tables = r.tables;
    b.checked = new Set(r.tables.map(t => t.id));   // 默认全选
    renderDlBases();
  } catch(e) { alert('请求失败: ' + e); }
}

async function fetchAllDlTables() {
  const cookie = document.getElementById('cookie').value.trim();
  if (!cookie) { alert('请先填写飞书 Cookie'); return; }
  const tokens = _dlBases.map(b => b.baseToken).filter(t => t);
  if (tokens.length === 0) { alert('至少配置 1 个 Base Token'); return; }
  const btn = document.getElementById('btn-fetch-multi');
  btn.disabled = true; btn.textContent = '连接中…';
  try {
    const res = await fetch('/api/tables-multi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ cookie, baseTokens: tokens })
    });
    const data = await res.json();
    if (!data.ok) { alert('❌ ' + data.msg); return; }
    // 把结果按 baseToken 对应回卡片
    const map = {};
    (data.results || []).forEach(r => map[r.baseToken] = r);
    _dlBases.forEach(b => {
      const r = map[b.baseToken];
      if (r && r.ok) {
        b.tables = r.tables;
        b.checked = new Set(r.tables.map(t => t.id));   // 默认全选
      } else if (r) {
        b.tables = []; b.checked = new Set();
        console.warn(`Base ${b.baseToken} 拉取失败:`, r.msg);
      }
    });
    renderDlBases();
  } catch(e) { alert('请求失败: ' + e); }
  finally { btn.disabled = false; btn.textContent = '🔗 一键拉取全部副表'; }
}

function startDownload() {
  // 收集每个 Base 卡里勾选的副表
  const bases = _dlBases
    .filter(b => b.baseToken && b.tables.length > 0)
    .map(b => ({
      name:      b.name || '',
      baseToken: b.baseToken,
      fieldId:   b.fieldId,   // 留空后端会回退默认
      tables:    b.tables.filter(t => b.checked.has(t.id)),
    }))
    .filter(b => b.tables.length > 0);

  if (bases.length === 0) { alert('请至少勾选一个 Base 下的副表'); return; }
  const totalTables = bases.reduce((s, b) => s + b.tables.length, 0);

  const card = document.getElementById('progress-card');
  const logBox = document.getElementById('log-box');
  const pbWrap = document.getElementById('pb-wrap');
  card.style.display = 'block';
  logBox.style.display = 'block';
  pbWrap.style.display = 'block';
  logBox.textContent = '';
  document.getElementById('btn-dl').disabled = true;

  let done = 0;
  const total = totalTables;

  fetch('/api/download', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cookie: document.getElementById('cookie').value.trim(),
      defaultFieldId: document.getElementById('fieldId').value.trim(),
      outputDir: document.getElementById('outputDir').value.trim(),
      bases: bases,
      forceRedownload: document.getElementById('forceRedownload').checked,
      minSizeKb: parseInt(document.getElementById('minSizeKb').value || '50', 10),
      makeZip: document.getElementById('makeZip').checked,
      deleteAfterZip: document.getElementById('deleteAfterZip').checked
    })
  }).then(res => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    function read() {
      reader.read().then(({done: d, value}) => {
        if (d) { document.getElementById('btn-dl').disabled = false; return; }
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const payload = line.slice(6);
          if (payload === '__DONE__') {
            document.getElementById('pb').style.width = '100%';
            document.getElementById('status').textContent = '✅ 全部完成！';
            document.getElementById('btn-dl').disabled = false;
            return;
          }
          try {
            const obj = JSON.parse(payload);
            const msg = obj.msg || '';
            logBox.textContent += msg + '\\n';
            logBox.scrollTop = logBox.scrollHeight;
            if (msg.startsWith('\\n📂')) {
              done++;
              document.getElementById('pb').style.width = (done/total*100) + '%';
              document.getElementById('status').textContent = `[${done}/${total}] ${msg.trim()}`;
            }
          } catch(e) {}
        });
        read();
      });
    }
    read();
  }).catch(e => { alert('下载出错: ' + e); document.getElementById('btn-dl').disabled = false; });
}

function renderProductCard(p, i) {
  // 列头：可编辑，第一列固定为"行操作列"
  const headersHtml = '<th class="row-ops" title="拖拽排序 / 删除">#</th>'
    + p.headers.map((h, c) =>
        '<th contenteditable="true" data-pidx="' + i + '" data-cidx="' + c
        + '" data-kind="header">' + escHtml(h) + '</th>'
      ).join('');

  // 数据行：每行第一列是 [⋮ 拖拽手柄 + 🗑 删除按钮]，后面是可编辑 td
  const rowsHtml = p.rows.map((row, r) => {
    const cellsHtml = row.map((c, ci) =>
      '<td contenteditable="true" data-pidx="' + i + '" data-ridx="' + r
      + '" data-cidx="' + ci + '" data-kind="cell" title="' + escHtml(c)
      + '">' + escHtml(c) + '</td>'
    ).join('');
    return '<tr draggable="true" data-pidx="' + i + '" data-ridx="' + r + '">'
      + '<td class="row-ops">'
      + '<span class="row-drag" title="拖拽调整顺序">⋮⋮</span>'
      + '<button class="row-del" title="删除该行"'
      + ' onclick="deleteRow(' + i + ',' + r + ')">🗑</button>'
      + '</td>' + cellsHtml + '</tr>';
  }).join('');

  return '<div class="product-card" id="pcard-' + i + '">'
    + '<div class="product-card-header" style="display:flex;align-items:center;gap:8px">'
    // 复选框
    + '<label onclick="event.stopPropagation()" style="display:flex;align-items:center;margin:0;cursor:pointer">'
    + '<input type="checkbox" class="auto-chk" data-idx="' + i + '" onchange="updateAutoSelectCount()"'
    + ' style="width:auto;cursor:pointer;accent-color:#7c3aed;margin-right:4px"></label>'
    // 折叠箭头
    + '<span class="product-toggle collapsed" id="toggle-' + i + '" onclick="toggleCard(' + i + ')" style="cursor:pointer">▼</span>'
    // 产品标题（可编辑，单击编辑、双击展开/收起）
    + '<span class="product-title" contenteditable="true" spellcheck="false"'
    + ' data-pidx="' + i + '" data-kind="title"'
    + ' ondblclick="toggleCard(' + i + ')"'
    + ' style="flex:1;cursor:text" title="单击编辑，双击展开/收起">' + escHtml(p.title) + '</span>'
    // 保存状态提示
    + '<span class="save-hint" id="hint-' + i + '"></span>'
    // 原模版条数（只读，给用户参考）
    + '<span title="原 Excel 该产品的模版行数" id="tmpl-' + i + '"'
    + ' style="font-size:11px;color:#9ca3af;white-space:nowrap">模版 ' + (p.template_count || 0) + '</span>'
    // ✏️ 数量输入框：用户填多少条就生成多少条，失焦时触发后端扩展
    + '<span style="display:inline-flex;align-items:center;gap:3px;background:#ede9fe;border:1px solid #c4b5fd;'
    +        'border-radius:99px;padding:1px 6px 1px 8px">'
    + '<input type="number" class="qty-count" id="qty-' + i + '" data-idx="' + i + '"'
    + ' value="' + (p.count > 0 ? p.count : '') + '" min="0" max="999" placeholder="?"'
    + ' onclick="event.stopPropagation()"'
    + ' onchange="expandProductByIdx(' + i + ')"'
    + ' onblur="expandProductByIdx(' + i + ')"'
    + ' title="想生成多少条，留空 = 暂不生成。失焦或回车后自动按数量扩展。"'
    + ' style="width:48px;text-align:center;padding:2px 4px;font-size:12px;font-weight:700;'
    +        'color:#5b21b6;background:transparent;border:none;outline:none">'
    + '<span style="font-size:11px;color:#6d28d9;font-weight:700">条</span>'
    + '</span>'
    // 🩹 补视频：数量输入框（留空/0 = 不补该产品）
    + '<input type="number" class="makeup-count" data-idx="' + i + '" min="0" max="999" placeholder="补?"'
    + ' onclick="event.stopPropagation()"'
    + ' title="想补多少条新数据，留空或 0 = 不补该产品"'
    + ' style="width:54px;text-align:center;padding:3px 4px;font-size:11px;'
    +        'border:1px solid #fbbf24;border-radius:5px;background:#fffbeb;color:#92400e">'
    // 🩹 补视频：账号下拉（运行时由 _refreshMakeupAccountOptions 填充）
    + '<select class="makeup-account" data-idx="' + i + '"'
    + ' onclick="event.stopPropagation()" title="补视频用哪个账号 / Base"'
    + ' style="padding:3px 4px;font-size:11px;border:1px solid #fbbf24;border-radius:5px;'
    +        'background:#fffbeb;color:#92400e;max-width:140px"></select>'
    // 删除产品
    + '<button onclick="deleteProductCard(' + i + ')" title="删除该产品（同步删除磁盘 JSON）"'
    + ' style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px;padding:2px 6px;border-radius:4px;line-height:1">🗑</button>'
    + '</div>'
    + '<div id="card-body-' + i + '" class="product-table-wrap" style="display:none">'
    + '<table class="product-table" data-pidx="' + i + '">'
    + '<thead><tr>' + headersHtml + '</tr></thead>'
    + '<tbody>' + rowsHtml + '</tbody>'
    + '</table>'
    + '<div class="row-add-bar">'
    + '<button onclick="addRow(' + i + ')">+ 新增一行</button>'
    + '<span style="font-size:11px;color:#9ca3af">单元格可直接点击编辑，编辑后自动保存</span>'
    + '</div>'
    + '</div></div>';
}

function toggleCard(idx) {
  const body = document.getElementById('card-body-' + idx);
  const toggle = document.getElementById('toggle-' + idx);
  if (body.style.display === 'none') {
    body.style.display = 'block';
    toggle.classList.remove('collapsed');
  } else {
    body.style.display = 'none';
    toggle.classList.add('collapsed');
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ============================================================
// 产品卡片编辑：双重持久化（user_state.json + 磁盘 JSON）
// ============================================================
const _saveTimers = {};   // pidx → setTimeout id（防抖）

// 标记保存状态提示
function setSaveHint(pidx, state) {
  const el = document.getElementById('hint-' + pidx);
  if (!el) return;
  el.classList.remove('saving','saved','error');
  if (state === 'saving') { el.classList.add('saving'); el.textContent = '⏳ 保存中…'; }
  else if (state === 'saved')  { el.classList.add('saved');  el.textContent = '✅ 已保存'; setTimeout(()=>{ if(el.textContent==='✅ 已保存') el.textContent=''; }, 1500); }
  else if (state === 'error')  { el.classList.add('error');  el.textContent = '❌ 保存失败'; }
  else el.textContent = '';
}

// 防抖：把整个产品（标题/列头/所有行）写回磁盘 + state
function saveProductPersist(pidx) {
  clearTimeout(_saveTimers[pidx]);
  setSaveHint(pidx, 'saving');
  _saveTimers[pidx] = setTimeout(() => {
    const p = _parsedProducts[pidx];
    if (!p) return;
    // 1. 写回 user_state.json
    saveParsedProducts();
    // 2. 写回磁盘单产品 JSON
    fetch('/api/save-parsed-product', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ key: p.key, title: p.title, headers: p.headers, rows: p.rows })
    }).then(r => r.json()).then(d => {
      if (d.ok) setSaveHint(pidx, 'saved');
      else { setSaveHint(pidx, 'error'); console.warn('save-parsed-product 失败:', d.msg); }
    }).catch(e => { setSaveHint(pidx, 'error'); console.warn(e); });
  }, 600);
}

// 事件委托：监听整个产品列表区域里的可编辑元素
function attachEditDelegation() {
  const root = document.getElementById('auto-preview-list');
  if (!root || root.dataset.bound === '1') return;
  root.dataset.bound = '1';

  // 编辑：标题 / 列头 / 单元格
  root.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.matches('[contenteditable="true"][data-kind]')) return;
    const pidx = parseInt(el.dataset.pidx);
    const kind = el.dataset.kind;
    const p = _parsedProducts[pidx];
    if (!p) return;
    const val = el.innerText.replace(/\\r?\\n/g, ' ');  // 单行化，避免回车混入
    if (kind === 'title')  p.title = val;
    else if (kind === 'header') p.headers[parseInt(el.dataset.cidx)] = val;
    else if (kind === 'cell')   p.rows[parseInt(el.dataset.ridx)][parseInt(el.dataset.cidx)] = val;
    saveProductPersist(pidx);
  });

  // 回车不换行（直接 blur）
  root.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.matches('[contenteditable="true"][data-kind]')) {
      e.preventDefault();
      e.target.blur();
    }
  });

  // 拖拽排序
  let dragSrc = null;
  root.addEventListener('dragstart', (e) => {
    const tr = e.target.closest('tr[draggable="true"]');
    if (!tr) return;
    dragSrc = tr;
    tr.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
  });
  root.addEventListener('dragend', (e) => {
    const tr = e.target.closest('tr[draggable="true"]');
    if (tr) tr.classList.remove('dragging');
    root.querySelectorAll('tr.drag-over').forEach(el => el.classList.remove('drag-over'));
    dragSrc = null;
  });
  root.addEventListener('dragover', (e) => {
    const tr = e.target.closest('tr[draggable="true"]');
    if (!tr || !dragSrc || tr === dragSrc) return;
    if (tr.dataset.pidx !== dragSrc.dataset.pidx) return;  // 只允许同一产品内拖
    e.preventDefault();
    root.querySelectorAll('tr.drag-over').forEach(el => el.classList.remove('drag-over'));
    tr.classList.add('drag-over');
  });
  root.addEventListener('drop', (e) => {
    const tr = e.target.closest('tr[draggable="true"]');
    if (!tr || !dragSrc || tr === dragSrc) return;
    if (tr.dataset.pidx !== dragSrc.dataset.pidx) return;
    e.preventDefault();
    const pidx = parseInt(tr.dataset.pidx);
    const from = parseInt(dragSrc.dataset.ridx);
    const to   = parseInt(tr.dataset.ridx);
    moveRow(pidx, from, to);
  });
}

// 删除一行
function deleteRow(pidx, ridx) {
  const p = _parsedProducts[pidx];
  if (!p || !p.rows[ridx]) return;
  if (!confirm('确定删除第 ' + (ridx + 1) + ' 行？')) return;
  p.rows.splice(ridx, 1);
  p.count = p.rows.length;
  rerenderProductList();
  attachEditDelegation();
  updateAutoSelectCount();
  updateSummary();
  saveProductPersist(pidx);
}

// 新增一行（默认每列填 '/'）
function addRow(pidx) {
  const p = _parsedProducts[pidx];
  if (!p) return;
  p.rows.push(p.headers.map(() => '/'));
  p.count = p.rows.length;
  rerenderProductList();
  attachEditDelegation();
  updateAutoSelectCount();
  updateSummary();
  saveProductPersist(pidx);
  // 自动展开 & 聚焦到新行第一格
  const body = document.getElementById('card-body-' + pidx);
  if (body) {
    body.style.display = 'block';
    const toggle = document.getElementById('toggle-' + pidx);
    if (toggle) toggle.classList.remove('collapsed');
    const newCell = body.querySelector('td[data-kind="cell"][data-ridx="' + (p.rows.length - 1) + '"][data-cidx="0"]');
    if (newCell) { newCell.focus(); body.scrollTop = body.scrollHeight; }
  }
}

// 行排序：把第 from 行移到 to 行的位置
function moveRow(pidx, from, to) {
  const p = _parsedProducts[pidx];
  if (!p || from === to) return;
  const [moved] = p.rows.splice(from, 1);
  p.rows.splice(to, 0, moved);
  rerenderProductList();
  attachEditDelegation();
  updateAutoSelectCount();
  saveProductPersist(pidx);
}


// ============================================================
// 全流程自动化（两阶段：清洗预览 → 确认执行）
// ============================================================
let _autoFile = null;
let _parsedProducts = [];   // 存储清洗结果，供 Stage2 确认后使用

function handleAutoDrop(e) {
  e.preventDefault();
  document.getElementById('auto-drop-zone').style.background = '#f5f3ff';
  const file = e.dataTransfer.files[0];
  if (file) handleAutoFile(file);
}

function handleAutoFile(file) {
  if (!file || !file.name.toLowerCase().endsWith('.xlsx')) {
    alert('请选择 .xlsx 格式的 Excel 文件');
    return;
  }
  _autoFile = file;
  const nameDiv = document.getElementById('auto-file-name');
  nameDiv.textContent = '📄 已选择：' + file.name + '  (' + (file.size / 1024).toFixed(1) + ' KB)';
  nameDiv.style.display = 'block';
}

// 批次日期持久化（避免误关页面后丢失）
function _persistBatchDate(v) {
  try { localStorage.setItem('auto_batch_date', String(v || '').trim()); } catch(_){}
}
function _restoreBatchDate() {
  try {
    const v = localStorage.getItem('auto_batch_date') || '';
    const el = document.getElementById('auto-batch-date');
    if (el && v) el.value = v;
  } catch(_){}
}

// Stage 1 → 清洗并显示预览（appendFile 非空时走"追加上传"模式，不清空已有产品）
function autoCleanPreview(appendFile) {
  const isAppend = !!appendFile;
  const fileToUse = isAppend ? appendFile : _autoFile;
  if (!fileToUse) { alert('请先选择甲方 Excel 文件'); return; }
  // 追加模式从 Stage 2 的输入框读；首次上传从 Stage 1 的输入框读
  const batchDateEl = document.getElementById(isAppend ? 'auto-batch-date-append' : 'auto-batch-date');
  const batchDate = (batchDateEl && batchDateEl.value || '').trim();
  if (!batchDate) {
    alert(isAppend
      ? '请先在"📅 追加批次"输入框里填批次日期（例如 5.21.），追加的产品会归到该日期下'
      : '请先填写"📅 批次日期"（例如 5.21.），同 Excel 内所有产品都会归到该日期下');
    if (batchDateEl) batchDateEl.focus();
    return;
  }
  const btn = isAppend ? null : document.getElementById('btn-auto-clean');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 清洗中...'; }

  const formData = new FormData();
  formData.append('file', fileToUse);
  formData.append('batch_date', batchDate);

  // 给 fetch 一个明确的 5 分钟超时（避免 pywebview/WebKit 隐性中断为模糊的 "Load failed"）
  const _ctrl = new AbortController();
  const _timeoutMs = 5 * 60 * 1000;
  const _timer = setTimeout(() => _ctrl.abort(), _timeoutMs);
  const _fileSizeMB = (fileToUse.size / 1024 / 1024).toFixed(2);
  const _t0 = Date.now();
  console.log('[parse-excel] 开始上传：' + fileToUse.name + '  (' + _fileSizeMB + ' MB, 追加=' + isAppend + ')');

  fetch('/api/parse-excel', { method: 'POST', body: formData, signal: _ctrl.signal })
    .then(r => { clearTimeout(_timer); console.log('[parse-excel] HTTP ' + r.status + '  耗时=' + ((Date.now()-_t0)/1000).toFixed(1) + 's'); return r.json(); })
    .then(data => {
      if (btn) { btn.disabled = false; btn.textContent = '📊 清洗并预览'; }
      if (!data.ok) {
        const tip = data.detail ? ('\\n\\n详情：\\n' + data.detail) : '';
        alert('❌ 清洗失败：' + (data.msg || '未知错误') + tip);
        if (data.detail) console.warn('[parse-excel] detail:\\n' + data.detail);
        return;
      }

      const incoming = data.results || [];
      if (!incoming.length) { alert('⚠️ 未解析到任何产品，请检查 Excel 格式'); return; }

      if (isAppend) {
        // 追加模式：按 key 合并，同 key 用新数据覆盖
        const map = new Map(_parsedProducts.map(p => [p.key, p]));
        let added = 0, replaced = 0;
        incoming.forEach(np => {
          if (map.has(np.key)) replaced++; else added++;
          map.set(np.key, np);
        });
        _parsedProducts = Array.from(map.values());
        alert('✅ 追加完成：新增 ' + added + ' 个，覆盖 ' + replaced + ' 个，当前共 ' + _parsedProducts.length + ' 个产品');
      } else {
        _parsedProducts = incoming;
      }

      // 显示摘要（多批次自动用 updateSummary 兼容；本次还显示修复提示）
      updateSummary();
      const fixedTotal = data.auto_fixed_total || 0;
      const ratioTotal = data.ratio_fixed_total || 0;
      if (fixedTotal > 0 || ratioTotal > 0) {
        const sumEl = document.getElementById('auto-preview-summary');
        if (fixedTotal > 0)
          sumEl.innerHTML += '&nbsp;&nbsp;|&nbsp;&nbsp;<span style="color:#d97706">🔁 <b>' + fixedTotal + '</b> 行台词被最终台词覆盖</span>';
        if (ratioTotal > 0)
          sumEl.innerHTML += '&nbsp;&nbsp;|&nbsp;&nbsp;<span style="color:#ea580c">📐 <b>' + ratioTotal + '</b> 处比例已规范化</span>';
      }
      if (fixedTotal > 0 && Array.isArray(data.auto_fixed_summary)) {
        console.log('[auto-fix] 修复明细：', data.auto_fixed_summary);
      }
      if (ratioTotal > 0 && Array.isArray(data.ratio_fixed_summary)) {
        console.log('[ratio-fix] 规范化明细：', data.ratio_fixed_summary);
      }

      // 渲染产品预览卡片
      rerenderProductList();
      updateAutoSelectCount();
      saveParsedProducts();  // 持久化

      // 切换到 Stage 2
      document.getElementById('auto-s1').style.display = 'none';
      document.getElementById('auto-s2').style.display = 'block';
      document.getElementById('auto-log-wrap').style.display = 'none';
    })
    .catch(e => {
      clearTimeout(_timer);
      if (btn) { btn.disabled = false; btn.textContent = '📊 清洗并预览'; }
      const _elapsed = ((Date.now()-_t0)/1000).toFixed(1);
      const _name = (e && e.name) || 'Error';
      const _msg  = (e && e.message) || String(e);
      console.warn('[parse-excel] 失败：' + _name + ' / ' + _msg + '  (耗时 ' + _elapsed + 's, 文件 ' + _fileSizeMB + ' MB)');
      let _hint = '';
      if (_name === 'AbortError') {
        _hint = '\\n\\n⏱ 已超过 5 分钟未响应，已中断。\\n建议：把 Excel 拆小后再上传，或查看终端日志确认后端是否仍在解析。';
      } else if (_name === 'TypeError') {
        _hint = '\\n\\n💡 这通常是 pywebview/WebKit 提前断开连接。\\n请检查：\\n  1) Flask 终端是否打印了异常堆栈（后端可能崩了）\\n  2) Excel 文件是否过大（建议 < 50MB）\\n  3) 已等待时长 ' + _elapsed + 's，文件大小 ' + _fileSizeMB + ' MB';
      }
      alert('❌ 请求失败：' + _name + ' / ' + _msg + _hint);
    });
}

// Stage 2 → 追加上传：触发隐藏的 file input；先确保批次日期已填（自动从 Stage 1 / 当前批次 / localStorage 兜底）
function triggerAppendUpload() {
  const el = document.getElementById('auto-batch-date-append');
  if (el && !el.value.trim()) {
    // 兜底来源：Stage 1 输入框 → 当前已加载的批次 → localStorage
    let v = (document.getElementById('auto-batch-date')||{}).value || '';
    if (!v && _parsedProducts && _parsedProducts.length) {
      const dates = [...new Set(_parsedProducts.map(p => (p.key||'').split('_')[0]))].filter(Boolean);
      if (dates.length === 1) v = dates[0];
    }
    if (!v) { try { v = localStorage.getItem('auto_batch_date') || ''; } catch(_){} }
    if (v) el.value = v;
  }
  if (el && !el.value.trim()) {
    alert('请先在"📅 追加批次"输入框里填批次日期（例如 5.21.），再点"追加上传"');
    el.focus();
    return;
  }
  document.getElementById('auto-append-input').click();
}

// 追加上传：选中文件后立即清洗并合并到现有列表
function handleAppendFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.xlsx')) {
    alert('请选择 .xlsx 格式的 Excel 文件');
    return;
  }
  autoCleanPreview(file);
  // 清空 input，方便下次选同名文件也能触发 change
  document.getElementById('auto-append-input').value = '';
}

// Stage 2 → 确认执行（Coze + CDP）
function autoExecute() {
  // 收集所有账号卡，并把空的 baseToken 自动回退到下载区第一个 Base
  const dlBase = (_dlBases && _dlBases[0] && _dlBases[0].baseToken) ? _dlBases[0].baseToken : '';
  const accounts = getAccounts().map(a => ({
    ...a,
    baseToken: a.baseToken || dlBase,
  }));
  if (!accounts.length) { alert('请至少配置 1 个账号'); return; }
  for (let i = 0; i < accounts.length; i++) {
    if (!accounts[i].cozeToken)  { alert(`账号 #${i+1} 缺少 Coze Token`); return; }
    if (!accounts[i].workflowId) { alert(`账号 #${i+1} 缺少 Workflow ID`); return; }
  }

  const btn = document.getElementById('btn-auto-exec');
  const log = document.getElementById('auto-log');
  const logWrap = document.getElementById('auto-log-wrap');
  btn.disabled = true;
  btn.textContent = '⏳ 执行中...';
  logWrap.style.display = 'block';
  log.innerHTML = '<div style="color:#a78bfa">⚡ 开始执行：' + accounts.length + ' 个账号并行（Coze + 飞书建表）...</div>';

  // 只取勾选的产品
  const checkedIdxs = Array.from(document.querySelectorAll('.auto-chk:checked'))
                           .map(el => parseInt(el.dataset.idx));
  if (checkedIdxs.length === 0) {
    alert('请至少勾选一个产品');
    btn.disabled = false; btn.textContent = '⚡ 确认，开始执行';
    return;
  }

  // 防呆：勾选的产品里如果还有未填数量 / 未扩展的，直接拦下
  const _empty = checkedIdxs
    .map(i => _parsedProducts[i])
    .filter(p => !p || !p.rows || p.rows.length === 0);
  if (_empty.length) {
    alert('❌ 有 ' + _empty.length + ' 个勾选的产品还没填条数（或扩展未完成）：\\n\\n'
        + _empty.slice(0, 8).map(p => '  · ' + (p ? p.key : '?')).join('\\n')
        + (_empty.length > 8 ? '\\n  …还有 ' + (_empty.length - 8) + ' 个' : '')
        + '\\n\\n请回到产品卡填"条"数，等绿色 ✅ 出现后再执行。');
    btn.disabled = false; btn.textContent = '⚡ 确认，开始执行';
    return;
  }

  const products = checkedIdxs.map(i => _parsedProducts[i]).map(p => ({
    key:   p.key,
    title: p.title,
    data:  [p.title,
            p.headers.join(' // '),
            ...p.rows.map(r => r.join(' // '))]
  }));

  fetch('/api/run-coze-cdp', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ products, accounts })
  }).then(res => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) { btn.disabled = false; btn.textContent = '⚡ 确认，开始执行'; return; }
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const payload = line.slice(6);
          if (payload === '__DONE__') {
            btn.disabled = false;
            btn.textContent = '⚡ 确认，开始执行';
            toggleSelectAll(false);  // 执行完毕自动取消勾选
            return;
          }
          try {
            const obj = JSON.parse(payload);
            const div = document.createElement('div');
            const msg = obj.msg || '';
            div.textContent = msg;
            if (msg.startsWith('✅') || msg.startsWith('🎉')) div.style.color = '#4ade80';
            else if (msg.startsWith('❌')) div.style.color = '#f87171';
            else if (msg.startsWith('⏳') || msg.startsWith('🤖') || msg.startsWith('🗂')) div.style.color = '#60a5fa';
            else if (msg.startsWith('  ✅')) div.style.color = '#86efac';
            else if (msg.startsWith('  ❌')) div.style.color = '#fca5a5';
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
          } catch(e) {}
        });
        read();
      });
    }
    read();
  }).catch(e => {
    log.innerHTML += '<div style="color:#f87171">❌ 请求失败: ' + e + '</div>';
    btn.disabled = false;
    btn.textContent = '⚡ 确认，开始执行';
  });
}

// Stage 2 → 🩹 批量补视频
function autoMakeup() {
  const accounts = getAccounts();
  if (!accounts.length) { alert('请至少配置 1 个账号'); return; }
  for (let i = 0; i < accounts.length; i++) {
    if (!accounts[i].cozeToken)  { alert(`账号 #${i+1} 缺少 Coze Token`);  return; }
    if (!accounts[i].workflowId) { alert(`账号 #${i+1} 缺少 Workflow ID`); return; }
  }
  // 收集所有填了 >=1 的产品卡
  const makeups = [];
  const submittedIdxs = [];   // 记录本次提交的产品下标，完成后清空对应输入框
  document.querySelectorAll('.makeup-count').forEach(input => {
    const cnt = parseInt(input.value || '0', 10);
    if (!cnt || cnt < 1) return;
    const idx = parseInt(input.dataset.idx, 10);
    const p   = _parsedProducts[idx];
    if (!p) return;
    const accSel = document.querySelector(`.makeup-account[data-idx="${idx}"]`);
    const accIdx = parseInt(accSel?.value || '0', 10);
    if (accIdx < 0 || accIdx >= accounts.length) {
      console.warn('账号下标越界', idx, accIdx); return;
    }
    makeups.push({ key: p.key, count: cnt, accountIndex: accIdx });
    submittedIdxs.push(idx);
  });
  if (makeups.length === 0) {
    alert('没有要补的产品。\\n请先在产品卡的「补?」输入框里填数字（>=1），再点本按钮。');
    return;
  }
  const btn     = document.getElementById('btn-auto-makeup');
  const log     = document.getElementById('auto-log');
  const logWrap = document.getElementById('auto-log-wrap');
  btn.disabled = true;
  btn.textContent = '⏳ 补视频中...';
  logWrap.style.display = 'block';
  log.innerHTML = '<div style="color:#fbbf24">🩹 开始补视频：' + makeups.length + ' 个产品...</div>';

  // 本次完成后清空已提交的「补?」输入框（只清提交过的，没填的不动）
  function _clearSubmittedMakeupInputs() {
    submittedIdxs.forEach(i => {
      const inp = document.querySelector(`.makeup-count[data-idx="${i}"]`);
      if (inp) inp.value = '';
    });
  }

  fetch('/api/run-coze-cdp-makeup', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ makeups, accounts })
  }).then(res => {
    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) {
          btn.disabled = false; btn.textContent = '🩹 批量补视频';
          _clearSubmittedMakeupInputs();
          return;
        }
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const payload = line.slice(6);
          if (payload === '__DONE__') {
            btn.disabled = false; btn.textContent = '🩹 批量补视频';
            _clearSubmittedMakeupInputs();
            toggleSelectAll(false);  // 补视频完成后也自动取消勾选
            return;
          }
          try {
            const obj = JSON.parse(payload);
            const div = document.createElement('div');
            const msg = obj.msg || '';
            div.textContent = msg;
            if (msg.startsWith('✅') || msg.startsWith('🎉')) div.style.color = '#4ade80';
            else if (msg.startsWith('❌')) div.style.color = '#f87171';
            else if (msg.startsWith('🩹') || msg.startsWith('🤖') || msg.startsWith('🗂')) div.style.color = '#fbbf24';
            else if (msg.startsWith('  ✅')) div.style.color = '#86efac';
            else if (msg.startsWith('  ❌')) div.style.color = '#fca5a5';
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
          } catch(e) {}
        });
        read();
      });
    }
    read();
  }).catch(e => {
    log.innerHTML += '<div style="color:#f87171">❌ 请求失败: ' + e + '</div>';
    btn.disabled = false; btn.textContent = '🩹 批量补视频';
  });
}

// 重新渲染产品列表（按日期→组分层，索引重排）
function rerenderProductList() {
  // —— 保护现场：滚动位置 + 当前焦点输入框（避免渲染后页面跳顶、焦点丢失）
  const _scrollY = window.scrollY || window.pageYOffset || 0;
  const _active  = document.activeElement;
  let _focusInfo = null;
  if (_active && _active.id && /^qty-\d+$/.test(_active.id)) {
    _focusInfo = {
      id:    _active.id,
      start: _active.selectionStart,
      end:   _active.selectionEnd,
    };
  }
  // 按 key 的第一段（日期）和第二段（组名）分组
  const byDate = {};
  _parsedProducts.forEach((p, i) => {
    const parts = p.key.split('_');
    const date = parts[0] || '未知日期';
    const grp  = parts[1] || '默认组';
    if (!byDate[date]) byDate[date] = {};
    if (!byDate[date][grp]) byDate[date][grp] = [];
    byDate[date][grp].push({ p, i });
  });

  let html = '';
  Object.keys(byDate).sort().forEach(date => {
    const grpMap   = byDate[date];
    const dateCnt  = Object.values(grpMap).reduce((s, arr) => s + arr.length, 0);
    const dateRows = Object.values(grpMap).reduce((s, arr) => s + arr.reduce((ss, {p}) => ss + p.count, 0), 0);
    // 项目级同名重复检测：同一日期内 product_name 出现 >1 次的算重复
    const _nameMap = {};
    Object.values(grpMap).forEach(arr => arr.forEach(({p}) => {
      const nm = (p.product_name || (p.key.split('_').slice(2).join('_'))).trim();
      if (!_nameMap[nm]) _nameMap[nm] = [];
      _nameMap[nm].push(p);
    }));
    const _dups = Object.entries(_nameMap).filter(([_, arr]) => arr.length > 1);
    let _dupBadge = '';
    if (_dups.length > 0) {
      const _detail = _dups.map(([nm, arr]) =>
        nm + '：' + arr.map(p => (p.sheet || '?')).join(' / ')
      ).join('\\n');
      _dupBadge = '<span title="' + escHtml('⚠️ 检测到同名产品分布在多个段落：\\n' + _detail)
                + '" style="background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5;'
                + 'padding:2px 8px;border-radius:5px;font-size:11px;font-weight:700;cursor:help">'
                + '⚠️ ' + _dups.length + ' 组同名</span>';
    }
    html += '<div class="project-block">';
    html += '<div class="project-header" data-date="' + escHtml(date) + '" onclick="toggleProject(this.dataset.date)" style="display:flex;align-items:center;gap:8px">'
          + '<span class="project-toggle" id="ptoggle-' + date + '">▼</span>'
          + '<span class="project-name" style="flex:1">📁 ' + escHtml(date) + ' 项目</span>'
          + _dupBadge
          + '<span class="project-stats">' + dateCnt + ' 个产品 · ' + dateRows + ' 条数据</span>'
          + '<button data-date="' + escHtml(date) + '"'
          +   ' onclick="event.stopPropagation();toggleProjectSelect(this.dataset.date)"'
          +   ' title="勾选/取消勾选本项目下的全部产品"'
          +   ' style="background:#ede9fe;color:#6d28d9;border:1px solid #c4b5fd;'
          +          'padding:3px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">'
          +   '☑ 全部</button>'
          + '<button data-date="' + escHtml(date) + '"'
          +   ' onclick="event.stopPropagation();deleteDate(this.dataset.date)"'
          +   ' title="删除整个批次（含磁盘 JSON）"'
          +   ' style="background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;'
          +          'padding:3px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">'
          +   '🗑 删批次</button>'
          + '</div>';
    html += '<div class="project-body" id="pbody-' + date + '">';
    Object.keys(grpMap).sort().forEach(grp => {
      const items = grpMap[grp];
      html += '<div class="group-section">';
      html += '<div class="group-header" style="display:flex;align-items:center;gap:8px">'
            + '<span style="flex:1">👥 ' + escHtml(grp) + '（' + items.length + ' 个产品）</span>'
            + '<button data-date="' + escHtml(date) + '" data-group="' + escHtml(grp) + '"'
            +   ' onclick="event.stopPropagation();deleteGroup(this.dataset.date, this.dataset.group)"'
            +   ' title="删除该组所有产品（含磁盘 JSON）"'
            +   ' style="background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;'
            +          'padding:2px 8px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">'
            +   '🗑 删组</button>'
            + '</div>';
      items.forEach(({p, i}) => { html += renderProductCard(p, i); });
      html += '</div>';
    });
    html += '</div></div>';
  });
  document.getElementById('auto-preview-list').innerHTML = html;
  attachEditDelegation();
  _refreshMakeupAccountOptions();
  // —— 恢复现场
  window.scrollTo(0, _scrollY);
  if (_focusInfo) {
    const el = document.getElementById(_focusInfo.id);
    if (el) {
      el.focus({ preventScroll: true });
      try { el.setSelectionRange(_focusInfo.start, _focusInfo.end); } catch(_){}
    }
  }
}

// ✏️ 数量框失焦 → 调后端扩展该产品（去抖：同值不重复请求）
const _expandInflight = {};   // idx → true 防重入
const _expandLastVal  = {};   // idx → 上次成功扩展用的数量
function expandProductByIdx(idx) {
  const p = _parsedProducts[idx];
  const input = document.getElementById('qty-' + idx);
  if (!p || !input) return;
  const raw = (input.value || '').trim();
  const cnt = parseInt(raw, 10);
  // 空 / 0：清空 rows，回到"未生成"态
  if (!raw || isNaN(cnt) || cnt <= 0) {
    if (p.count > 0) {
      p.rows = []; p.count = 0;
      _expandLastVal[idx] = 0;
      rerenderProductList();
      updateSummary();
      saveParsedProducts();
    }
    return;
  }
  if (_expandInflight[idx]) return;
  if (_expandLastVal[idx] === cnt && (p.rows || []).length === cnt) return;
  if (!p.templates || !p.templates.length) {
    alert('该产品没有可用的原始模版，无法扩展（请重新清洗 Excel）');
    return;
  }
  _expandInflight[idx] = true;
  // 用 hint 元素显示进度
  const hint = document.getElementById('hint-' + idx);
  if (hint) { hint.textContent = '⏳ 扩展中...'; hint.style.color = '#9ca3af'; }
  fetch('/api/expand-product', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      templates:  p.templates,
      header_str: p.header_str,
      count:      cnt,
      batch_date: p.batch_date || (p.key.split('_')[0] || ''),
      sheet:      p.sheet || (p.key.split('_')[1] || ''),
      title:      p.title,
    })
  })
  .then(r => r.json())
  .then(data => {
    _expandInflight[idx] = false;
    if (!data.ok) {
      if (hint) { hint.textContent = '❌ ' + (data.msg || '失败'); hint.style.color = '#ef4444'; }
      alert('❌ 扩展失败：' + (data.msg || '未知错误') + (data.detail ? '\\n' + data.detail.slice(0, 400) : ''));
      return;
    }
    p.rows     = data.rows || [];
    p.headers  = data.headers || p.headers;
    p.count    = data.count || 0;
    p.title    = data.title || p.title;
    p.key      = data.file_key || p.key;
    _expandLastVal[idx] = cnt;
    if (hint) {
      const extras = [];
      if (data.auto_fixed)  extras.push('🔁 台词修 ' + data.auto_fixed);
      if (data.ratio_fixed) extras.push('📐 比例修 ' + data.ratio_fixed);
      hint.textContent = '✅ ' + p.count + ' 条' + (extras.length ? ' · ' + extras.join(' · ') : '');
      hint.style.color = '#16a34a';
      setTimeout(() => { if (hint) hint.textContent = ''; }, 4000);
    }
    rerenderProductList();
    updateSummary();
    saveParsedProducts();
  })
  .catch(e => {
    _expandInflight[idx] = false;
    if (hint) { hint.textContent = '❌ ' + e; hint.style.color = '#ef4444'; }
    alert('❌ 扩展请求失败：' + e);
  });
}

// 给所有产品卡的"补到账号"下拉重新生成 options（账号增删/重命名后调）
function _refreshMakeupAccountOptions() {
  const accs = (typeof getAccounts === 'function') ? getAccounts() : [];
  const opts = accs.map((a, i) => {
    const label = '#' + (i+1) + (a.name ? ' · ' + a.name : '');
    return '<option value="' + i + '">' + escHtml(label) + '</option>';
  }).join('') || '<option value="0">#1</option>';
  document.querySelectorAll('.makeup-account').forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = opts;
    // 尽量保持原选择；若原值越界则回 0
    sel.value = (prev !== '' && parseInt(prev) < accs.length) ? prev : '0';
  });
}

// ============================================================
// 🔍 检查未建副表
// ============================================================
let _missingKeys = [];   // 上次检查结果：未建产品的 key 列表

// 入口：拉取所有账号 Base 已建副表，与本地 _parsedProducts 对比
function checkMissingSubtables() {
  if (!_parsedProducts.length) { alert('当前没有产品，无法检查'); return; }
  const cookie = (document.getElementById('cookie').value || '').trim();
  if (!cookie) { alert('请先填写飞书 Cookie（页面下方）'); return; }
  // 收集所有不重复的 Base Token（账号 + 下载区 Base 作为兜底）
  const accs = getAccounts();
  const tokens = [];
  const seen = new Set();
  accs.forEach((a, i) => {
    const bt = (a.baseToken || '').trim();
    if (bt && !seen.has(bt)) { seen.add(bt); tokens.push({ token: bt, label: '账号 #' + (i+1) + (a.name ? ' · ' + a.name : '') }); }
  });
  if (!tokens.length && _dlBases && _dlBases.length) {
    _dlBases.forEach((b, i) => {
      const bt = (b.baseToken || '').trim();
      if (bt && !seen.has(bt)) { seen.add(bt); tokens.push({ token: bt, label: '下载Base #' + (i+1) }); }
    });
  }
  if (!tokens.length) { alert('未配置任何 Base Token，无法检查\\n请在账号卡或下载区填写 Base Token'); return; }

  const btn = document.getElementById('btn-check-subtables');
  btn.disabled = true; btn.textContent = '⏳ 检查中...';
  fetch('/api/tables-multi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cookie, baseTokens: tokens.map(t => t.token) })
  })
    .then(r => r.json())
    .then(data => {
      btn.disabled = false; btn.textContent = '🔍 检查未建副表';
      if (!data.ok) { alert('❌ 拉取副表失败：' + (data.msg || '未知错误')); return; }
      // 汇总所有 Base 里已存在的副表名
      const existing = new Set();
      const baseStats = [];
      (data.results || []).forEach((r, ri) => {
        const label = tokens[ri] ? tokens[ri].label : ('Base ' + (ri+1));
        if (r.ok) {
          (r.tables || []).forEach(t => { if (t.name) existing.add(t.name); });
          baseStats.push({ label, token: r.baseToken, ok: true, count: (r.tables || []).length });
        } else {
          baseStats.push({ label, token: r.baseToken, ok: false, msg: r.msg || '未知错误' });
        }
      });
      // 比对：哪些产品的 key 不在已建副表里
      const missing = _parsedProducts.filter(p => !existing.has(p.key));
      _missingKeys = missing.map(p => p.key);
      _renderCheckResult(baseStats, _parsedProducts.length, _parsedProducts.length - missing.length, missing);
    })
    .catch(e => {
      btn.disabled = false; btn.textContent = '🔍 检查未建副表';
      alert('请求失败：' + e);
    });
}

// 渲染检查结果到模态框
function _renderCheckResult(baseStats, total, doneCount, missing) {
  const head =
    '<div style="background:#f9fafb;padding:10px 12px;border-radius:6px;margin-bottom:12px">'
    + '<div style="font-size:13px"><b>📊 总产品：</b>' + total
    + ' &nbsp;|&nbsp; <span style="color:#15803d"><b>✅ 已建：</b>' + doneCount + '</span>'
    + ' &nbsp;|&nbsp; <span style="color:#dc2626"><b>❌ 未建：</b>' + missing.length + '</span></div>'
    + '<div style="font-size:11px;color:#6b7280;margin-top:6px">'
    + baseStats.map(s => s.ok
        ? ('✓ ' + escHtml(s.label) + ' (共 ' + s.count + ' 个副表)')
        : ('✗ ' + escHtml(s.label) + ' — ' + escHtml(s.msg || ''))
      ).join(' &nbsp; ') + '</div></div>';

  let body = head;
  if (!missing.length) {
    body += '<div style="text-align:center;padding:30px 0;color:#15803d;font-size:15px;font-weight:600">🎉 所有产品都已建好副表！</div>';
    document.getElementById('btn-highlight-missing').style.display = 'none';
    document.getElementById('btn-copy-missing').style.display = 'none';
  } else {
    // 按"日期 / 组"分组
    const grouped = {};
    missing.forEach(p => {
      const parts = (p.key || '').split('_');
      const date = parts[0] || '未知日期';
      const group = parts[1] || '默认组';
      const k = date + ' / ' + group;
      (grouped[k] = grouped[k] || []).push(p);
    });
    body += '<div style="border-top:1px solid #fecaca;padding-top:10px"><div style="color:#dc2626;font-weight:700;margin-bottom:8px">❌ 未建副表的产品 (' + missing.length + ')</div>';
    Object.keys(grouped).sort().forEach(k => {
      body += '<div style="margin-bottom:10px"><div style="font-size:12px;color:#6b7280;margin-bottom:4px">📅 ' + escHtml(k) + '</div>';
      grouped[k].forEach(p => {
        body += '<div style="padding:4px 10px;background:#fef2f2;border-left:3px solid #ef4444;margin-bottom:3px;border-radius:3px;font-size:12px;font-family:Menlo,Monaco,monospace">' + escHtml(p.key) + '</div>';
      });
      body += '</div>';
    });
    body += '</div>';
    document.getElementById('btn-highlight-missing').style.display = '';
    document.getElementById('btn-copy-missing').style.display = '';
  }
  document.getElementById('check-modal-body').innerHTML = body;
  document.getElementById('check-modal-mask').classList.add('show');
}

function closeCheckModal() {
  document.getElementById('check-modal-mask').classList.remove('show');
}

// 在产品列表里给未建的产品卡加红框，并滚到第一个
function highlightMissingProducts() {
  if (!_missingKeys.length) return;
  // 先清除旧高亮
  document.querySelectorAll('.product-card.missing-subtable').forEach(el => el.classList.remove('missing-subtable'));
  const missSet = new Set(_missingKeys);
  let firstEl = null;
  _parsedProducts.forEach((p, i) => {
    if (!missSet.has(p.key)) return;
    const card = document.getElementById('pcard-' + i);
    if (card) { card.classList.add('missing-subtable'); if (!firstEl) firstEl = card; }
  });
  closeCheckModal();
  if (firstEl) firstEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function copyMissingList() {
  if (!_missingKeys.length) return;
  const txt = _missingKeys.join('\\n');
  navigator.clipboard.writeText(txt).then(
    () => { const b = document.getElementById('btn-copy-missing'); const o = b.textContent; b.textContent = '✓ 已复制'; setTimeout(() => b.textContent = o, 1500); },
    () => alert('复制失败，请手动选择以下内容：\\n\\n' + txt)
  );
}

// 展开/折叠项目
function toggleProject(date) {
  const body   = document.getElementById('pbody-' + date);
  const toggle = document.getElementById('ptoggle-' + date);
  if (!body) return;
  if (body.style.display === 'none') {
    body.style.display = 'block';
    toggle.classList.remove('collapsed');
  } else {
    body.style.display = 'none';
    toggle.classList.add('collapsed');
  }
}

// 更新摘要行（公共函数）
function updateSummary() {
  const total = _parsedProducts.reduce((s, p) => s + p.count, 0);
  const groups = new Set(_parsedProducts.map(p => p.key.split('_')[1] || ''));
  const dates  = new Set(_parsedProducts.map(p => p.key.split('_')[0] || ''));
  const batchDate = dates.size === 1 ? [...dates][0] : dates.size + ' 个项目';
  document.getElementById('auto-preview-summary').innerHTML =
    '📅 批次: <b>' + batchDate + '</b>'
    + '&nbsp;&nbsp;|&nbsp;&nbsp;🏢 ' + groups.size + ' 个组'
    + '&nbsp;&nbsp;|&nbsp;&nbsp;📦 ' + _parsedProducts.length + ' 个产品'
    + '&nbsp;&nbsp;|&nbsp;&nbsp;📝 共 <b>' + total + '</b> 条';
}

// 删除磁盘上的产品 JSON（静默调用，失败仅 warn）
function deleteParsedProductOnDisk(key) {
  if (!key) return Promise.resolve();
  return fetch('/api/delete-parsed-product', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key })
  }).then(r => r.json()).then(d => {
    if (!d.ok) console.warn('delete-parsed-product 失败:', d.msg);
  }).catch(e => console.warn(e));
}

// 删除某条产品（从 _parsedProducts 移除，重渲，同步删磁盘 JSON）
function deleteProductCard(i) {
  const p = _parsedProducts[i];
  if (!p) return;
  if (!confirm('确定删除产品「' + (p.title || p.key) + '」？\\n（同时删除磁盘 JSON 文件，不可恢复）')) return;
  const key = p.key;
  _parsedProducts.splice(i, 1);
  rerenderProductList();
  updateAutoSelectCount();
  updateSummary();
  saveParsedProducts();
  deleteParsedProductOnDisk(key);
}

// 删除所有勾选的产品（同步删磁盘 JSON）
function deleteSelected() {
  const checked = document.querySelectorAll('.auto-chk:checked');
  if (checked.length === 0) { alert('请先勾选要删除的产品'); return; }
  if (!confirm('确定删除已勾选的 ' + checked.length + ' 个产品？\\n（同时删除磁盘 JSON 文件，不可恢复）')) return;
  // 收集要删除的原始索引（从大到小，避免 splice 偏移）
  const idxToRemove = Array.from(checked)
    .map(el => parseInt(el.dataset.idx))
    .sort((a, b) => b - a);
  const keysToDelete = idxToRemove.map(idx => _parsedProducts[idx]?.key).filter(Boolean);
  idxToRemove.forEach(idx => _parsedProducts.splice(idx, 1));
  rerenderProductList();
  updateAutoSelectCount();
  updateSummary();
  saveParsedProducts();
  keysToDelete.forEach(k => deleteParsedProductOnDisk(k));
}

// 删除某组下的所有产品（同步删磁盘 JSON）
function deleteGroup(date, group) {
  const victims = _parsedProducts.filter(p => {
    const parts = p.key.split('_');
    return (parts[0] || '未知日期') === date && (parts[1] || '默认组') === group;
  });
  if (victims.length === 0) { alert('该组没有产品'); return; }
  if (!confirm('确定删除「' + date + ' · ' + group + '」下的 ' + victims.length + ' 个产品？\\n（同时删除磁盘 JSON 文件，不可恢复）')) return;
  const keysToDelete = victims.map(p => p.key);
  _parsedProducts = _parsedProducts.filter(p => !keysToDelete.includes(p.key));
  rerenderProductList();
  updateAutoSelectCount();
  updateSummary();
  saveParsedProducts();
  keysToDelete.forEach(k => deleteParsedProductOnDisk(k));
}

// 删除某批次（日期）下的所有产品（同步删磁盘 JSON）
function deleteDate(date) {
  const victims = _parsedProducts.filter(p => (p.key.split('_')[0] || '未知日期') === date);
  if (victims.length === 0) { alert('该批次没有产品'); return; }
  if (!confirm('确定删除批次「' + date + '」下全部 ' + victims.length + ' 个产品？\\n（同时删除磁盘 JSON 文件，不可恢复）')) return;
  const keysToDelete = victims.map(p => p.key);
  _parsedProducts = _parsedProducts.filter(p => !keysToDelete.includes(p.key));
  rerenderProductList();
  updateAutoSelectCount();
  updateSummary();
  saveParsedProducts();
  keysToDelete.forEach(k => deleteParsedProductOnDisk(k));
}

// 更新"已选 x / 共 x"计数 + 全选框状态
function updateAutoSelectCount() {
  const all   = document.querySelectorAll('.auto-chk');
  const checked = document.querySelectorAll('.auto-chk:checked');
  const countEl = document.getElementById('auto-select-count');
  if (countEl) countEl.textContent = '已选 ' + checked.length + ' / 共 ' + all.length + ' 个产品';
  const chkAll = document.getElementById('chk-all-auto');
  if (chkAll) chkAll.checked = (all.length > 0 && checked.length === all.length);
}

// 全选 / 取消全选
function toggleSelectAll(checked) {
  document.querySelectorAll('.auto-chk').forEach(el => el.checked = checked);
  updateAutoSelectCount();
}

// 项目级全选切换：本批次（date）下所有产品 — 若已经全勾则取消，否则全勾
function toggleProjectSelect(date) {
  const idxs = [];
  _parsedProducts.forEach((p, i) => {
    const d = (p.key || '').split('_')[0] || '未知日期';
    if (d === date) idxs.push(i);
  });
  if (!idxs.length) return;
  const allChecked = idxs.every(i => {
    const el = document.querySelector('.auto-chk[data-idx="' + i + '"]');
    return el && el.checked;
  });
  const newState = !allChecked;
  idxs.forEach(i => {
    const el = document.querySelector('.auto-chk[data-idx="' + i + '"]');
    if (el) el.checked = newState;
  });
  updateAutoSelectCount();
}

// 重置回 Stage 1（同时清除持久化数据）
function resetAutoStage() {
  if (_parsedProducts.length > 0) {
    if (!confirm('确定清空当前全部 ' + _parsedProducts.length + ' 个产品并回到上传页？\\n（仅清空内存与 user_state.json，磁盘 JSON 文件不会删）\\n\\n💡 提示：如果只想再上传一份 Excel 合并进来，请用「➕ 追加上传」')) return;
  }
  _autoFile = null;
  _parsedProducts = [];
  // 清除服务端保存的产品数据
  fetch('/api/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ parsed_products: [] })
  });
  document.getElementById('auto-file-input').value = '';
  document.getElementById('auto-file-name').style.display = 'none';
  document.getElementById('auto-s1').style.display = 'block';
  document.getElementById('auto-s2').style.display = 'none';
  document.getElementById('auto-log-wrap').style.display = 'none';
  document.getElementById('auto-log').innerHTML = '';
}

// 复制全部执行日志到剪贴板
function copyAutoLog() {
  const log = document.getElementById('auto-log');
  const text = log.innerText || log.textContent || '';
  if (!text.trim()) { alert('日志为空'); return; }
  navigator.clipboard.writeText(text).then(() => {
    const btns = document.querySelectorAll('#auto-log-wrap button');
    if (btns.length) {
      const old = btns[0].textContent;
      btns[0].textContent = '✅ 已复制';
      setTimeout(() => { btns[0].textContent = old; }, 1500);
    }
  }).catch(err => {
    // 兼容老浏览器：选中再 execCommand
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); alert('已复制 ' + text.length + ' 字符'); }
    catch(e) { alert('复制失败：' + e); }
    document.body.removeChild(ta);
  });
}


// ============================================================
// 右侧固定操作栏
// ============================================================
function sideAction(type) {
  const idMap = { preview: 'dt-btn-exec', download: 'btn-dl' };
  const btn = document.getElementById(idMap[type]);
  if (!btn) return;
  btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
  if (!btn.disabled) btn.click();
}
// 同步侧边按钮 disabled 状态
(function _sideInit() {
  setInterval(function() {
    const map = { 'side-preview': 'dt-btn-exec', 'side-download': 'btn-dl' };
    Object.entries(map).forEach(([sid, tid]) => {
      const s = document.getElementById(sid);
      const t = document.getElementById(tid);
      if (s) s.disabled = !t || t.disabled;
    });
  }, 500);
})();


// ============================================================
// 飞书多维表格监控 UI 逻辑（多组版）
// ============================================================
let _bmGroups = [];      // [{name,appToken,tableId,cozeToken,wf2,wf3,wf4}, ...]
let _bmPollTimer = null;

// ── 渲染监控组列表 ──
function bmRenderGroups() {
  const list = document.getElementById('bm-groups-list');
  if (!list) return;
  list.innerHTML = _bmGroups.map((g, i) => bmGroupHTML(g, i)).join('');
  bmSaveGroups();
}

function bmGroupHTML(g, i) {
  const iStyle = 'border:1px solid #bae6fd;border-radius:9px;padding:10px 12px;margin-bottom:8px;background:#fff';
  const lbStyle = 'font-size:11px;color:#0369a1;font-weight:600;display:block;margin-bottom:2px';
  const inStyle = 'border-color:#7dd3fc;font-size:12px;padding:4px 8px;width:100%';
  return '<div style="' + iStyle + '" data-bm-idx="' + i + '">'
    + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">'
    +   '<span style="font-size:13px;font-weight:700;color:#0369a1;flex:1">监控组 #' + (i+1)
    +     (g.name ? (' · ' + g.name) : '') + '</span>'
    +   '<button onclick="bmRemoveGroup(' + i + ')" style="font-size:11px;padding:2px 8px;'
    +     'background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5;border-radius:5px;cursor:pointer">🗑 移除</button>'
    + '</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">'
    +   '<div><label style="' + lbStyle + '">别名（选填）</label>'
    +     '<input style="' + inStyle + '" placeholder="如：刘原原组" value="' + (g.name||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;name&quot;)">'
    +   '</div>'
    +   '<div><label style="' + lbStyle + '">Coze Token</label>'
    +     '<input style="' + inStyle + '" placeholder="sat_..." value="' + (g.cozeToken||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;cozeToken&quot;)">'
    +   '</div>'
    +   '<div style="grid-column:1/-1"><label style="' + lbStyle + '">多维表格 Base Token</label>'
    +     '<input style="' + inStyle + '" placeholder="NMwjbb...（留空=使用全局配置）" value="' + (g.appToken||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;appToken&quot;)">'
    +   '</div>'
    +   '<div><label style="' + lbStyle + '">阶段2 Workflow（生图）</label>'
    +     '<input style="' + inStyle + '" placeholder="7xxxxxxxxx（留空=跳过）" value="' + (g.wf2||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;wf2&quot;)">'
    +   '</div>'
    +   '<div><label style="' + lbStyle + '">阶段3 Workflow（生视频）</label>'
    +     '<input style="' + inStyle + '" placeholder="7xxxxxxxxx（留空=跳过）" value="' + (g.wf3||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;wf3&quot;)">'
    +   '</div>'
    +   '<div><label style="' + lbStyle + '">阶段4 Workflow（加字幕）</label>'
    +     '<input style="' + inStyle + '" placeholder="7xxxxxxxxx（留空=跳过）" value="' + (g.wf4||'') + '"'
    +     ' oninput="bmGroupChange(' + i + ',this,&quot;wf4&quot;)">'
    +   '</div>'
    + '</div>'
    + '</div>';
}

function bmGroupChange(i, el, field) {
  if (_bmGroups[i]) _bmGroups[i][field] = el.value.trim();
  bmSaveGroups();
}

function bmAddGroup() {
  _bmGroups.push({ name:'', appToken:'', cozeToken:'', wf2:'', wf3:'', wf4:'' });
  bmRenderGroups();
}

function bmRemoveGroup(i) {
  _bmGroups.splice(i, 1);
  bmRenderGroups();
}

function bmSaveGroups() {
  try { localStorage.setItem('bm_groups', JSON.stringify(_bmGroups)); } catch(e) {}
}

function bmLoadGroups() {
  try {
    const saved = JSON.parse(localStorage.getItem('bm_groups') || '[]');
    _bmGroups = Array.isArray(saved) ? saved : [];
  } catch(e) { _bmGroups = []; }
  if (!_bmGroups.length) _bmGroups = [{ name:'', appToken:'', cozeToken:'', wf2:'', wf3:'', wf4:'' }];
  bmRenderGroups();
}

// ── 读取当前输入框值（input value 可能和 _bmGroups 有延迟）──
function bmCollectGroups() {
  const cards = document.querySelectorAll('#bm-groups-list [data-bm-idx]');
  const groups = [];
  cards.forEach((card, i) => {
    const inputs = card.querySelectorAll('input');
    groups.push({
      name:       inputs[0].value.trim(),
      cozeToken:  inputs[1].value.trim(),
      appToken:   inputs[2].value.trim(),
      wf2:        inputs[3].value.trim(),
      wf3:        inputs[4].value.trim(),
      wf4:        inputs[5].value.trim(),
    });
  });
  return groups;
}

// ── 启动 / 停止 ──
function bmStartAll() {
  const groups = bmCollectGroups();
  if (!groups.length) { alert('请先添加至少一个监控组'); return; }
  fetch('/api/bitable/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ groups: groups }),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      bmSetGlobalBadge(true);
      bmStartPoll();
    } else {
      alert('启动失败: ' + (d.msg || '未知'));
    }
  }).catch(e => alert('网络错误: ' + e));
}

function bmStopAll() {
  fetch('/api/bitable/stop', { method: 'POST' })
    .then(r => r.json())
    .then(() => { bmSetGlobalBadge(false); if (_bmPollTimer) { clearInterval(_bmPollTimer); _bmPollTimer = null; } })
    .catch(e => console.warn('stop err', e));
}

function bmSetGlobalBadge(running) {
  const badge = document.getElementById('bm-global-badge');
  if (!badge) return;
  badge.textContent = running ? '🟢 运行中' : '⚪ 未运行';
  badge.style.background = running ? '#dcfce7' : '#f1f5f9';
  badge.style.color      = running ? '#15803d' : '#64748b';
}

function bmStartPoll() {
  if (_bmPollTimer) clearInterval(_bmPollTimer);
  _bmPollTimer = setInterval(() => {
    fetch('/api/bitable/status').then(r => r.json()).then(d => {
      bmSetGlobalBadge(d.running);
      if (!d.running) { clearInterval(_bmPollTimer); _bmPollTimer = null; }
      const s = d.stats || {};
      const el = id => document.getElementById(id);
      if (el('bm-tot2')) el('bm-tot2').textContent = s.stage2 || 0;
      if (el('bm-tot3')) el('bm-tot3').textContent = s.stage3 || 0;
      if (el('bm-tot4')) el('bm-tot4').textContent = s.stage4 || 0;
      if (el('bm-tot-err')) el('bm-tot-err').textContent = s.errors || 0;
      const logEl = el('bm-log');
      const logs = d.logs || [];
      if (logEl && logs.length) { logEl.textContent = logs.join('\\n'); logEl.scrollTop = logEl.scrollHeight; }
    }).catch(e => console.warn('bm poll err', e));
  }, 2000);
}

window.addEventListener('DOMContentLoaded', () => {
  bmLoadGroups();
  fetch('/api/bitable/status').then(r => r.json()).then(d => {
    if (d.running) { bmSetGlobalBadge(true); bmStartPoll(); }
  }).catch(() => {});
});

</script>
</body>
</html>"""


# ============================================================
# 飞书多维表格自动监控（替代字段捷径，无限并发）
# ============================================================
import concurrent.futures as _futures

_FEISHU_HOST_API = "https://open.feishu.cn"
_bitable_monitor_running = False
_bitable_monitor_thread  = None
_bitable_monitor_log     = []   # [(timestamp, msg), ...]
_bitable_monitor_lock    = threading.Lock()
_bitable_monitor_stats   = {"stage2": 0, "stage3": 0, "stage4": 0, "errors": 0}

def _bitable_log(msg: str):
    ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
    with _bitable_monitor_lock:
        _bitable_monitor_log.append(f"[{ts}] {msg}")
        if len(_bitable_monitor_log) > 500:
            _bitable_monitor_log.pop(0)
    print(f"[bitable] {msg}")

def _get_feishu_token() -> str:
    """获取飞书 tenant_access_token"""
    app_id     = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    resp = requests.post(
        f"{_FEISHU_HOST_API}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书token失败: {data}")
    return data["tenant_access_token"]

def _bitable_list_tables(token: str, app_token: str) -> list:
    """获取多维表格下所有子表，返回 [{table_id, name}, ...]"""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{_FEISHU_HOST_API}/open-apis/bitable/v1/apps/{app_token}/tables",
        headers=headers, timeout=15
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取子表列表失败: {data}")
    return data.get("data", {}).get("items", [])

def _bitable_list_records(token: str, app_token: str, table_id: str) -> list:
    """拉取多维表格所有记录（自动翻页）"""
    records = []
    page_token = ""
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"{_FEISHU_HOST_API}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=headers, params=params, timeout=15
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拉取记录失败: {data}")
        items = data.get("data", {}).get("items", [])
        records.extend(items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token", "")
    return records

def _bitable_update_record(token: str, app_token: str, table_id: str, record_id: str, fields: dict):
    """更新单条记录的字段"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.put(
        f"{_FEISHU_HOST_API}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        headers=headers, json={"fields": fields}, timeout=15
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"写回失败: {data}")

def _call_coze_workflow(workflow_id: str, params: dict, coze_token: str = "") -> dict:
    """调用 Coze 工作流，返回 output 字典。coze_token 优先于环境变量"""
    token = coze_token or os.getenv("COZE_TOKEN_1", "")
    base_url = os.getenv("COZE_BASE_URL", "https://api.coze.cn")
    resp = requests.post(
        f"{base_url}/v1/workflow/run",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"workflow_id": workflow_id, "parameters": params},
        timeout=300
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"工作流调用失败: {data}")
    output = data.get("data", {})
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except Exception:
            pass
    return output

def _get_field(record: dict, field_name: str) -> str:
    """从记录里取字段值，统一转字符串"""
    val = record.get("fields", {}).get(field_name, "")
    if isinstance(val, list):
        # 文本类字段是 [{"text": "..."}] 格式
        return "".join(v.get("text", "") for v in val if isinstance(v, dict))
    return str(val) if val is not None else ""

_bitable_monitor_configs = []   # 由 /api/bitable/start 写入的监控组列表

def _bitable_monitor_one_group(group: dict, executor, in_progress: set, tk_cache: dict):
    """处理单个监控组的一轮扫描（自动发现并遍历所有子表）"""
    # appToken 优先用配置里填的，留空则用 .env 里的全局配置
    app_token  = (group.get("appToken") or "").strip() or os.getenv("BITABLE_APP_TOKEN", "")
    wf2_id     = group.get("wf2", "").strip()
    wf3_id     = group.get("wf3", "").strip()
    wf4_id     = group.get("wf4", "").strip()
    coze_token = group.get("cozeToken", "").strip()
    label      = group.get("name", "") or app_token[:8]

    if not app_token:
        _bitable_log(f"⚠️ [{label}] 未配置 Base Token，跳过")
        return

    import time
    # 飞书 tenant_access_token 缓存
    if time.time() > tk_cache.get("expire", 0) - 60:
        tk_cache["val"]    = _get_feishu_token()
        tk_cache["expire"] = time.time() + 7000
    tk = tk_cache["val"]

    test_mode = not any([wf2_id, wf3_id, wf4_id])

    # ── 自动获取所有子表 ──
    try:
        tables = _bitable_list_tables(tk, app_token)
    except Exception as e:
        _bitable_log(f"❌ [{label}] 获取子表列表失败: {e}")
        return

    if not tables:
        _bitable_log(f"⚠️ [{label}] 该 Base 下没有子表")
        return

    # 诊断模式：首次输出所有子表名 + 字段预览
    if test_mode and not group.get("_diag_done"):
        group["_diag_done"] = True
        _bitable_log(f"🔍 [{label}] 诊断模式，共发现 {len(tables)} 个子表：")
        for t in tables:
            _bitable_log(f"  📋 {t.get('name','?')}  (id: {t.get('table_id','?')})")
        # 读第一个子表的前3条记录做字段预览
        try:
            first_id   = tables[0]["table_id"]
            first_name = tables[0].get("name", first_id)
            records = _bitable_list_records(tk, app_token, first_id)
            _bitable_log(f"  [预览子表: {first_name}] 共 {len(records)} 条记录")
            WATCH = ["生图提示词", "图片url", "启动视频", "视频提示词", "比例", "字幕", "视频生成", "时长"]
            for i, rec in enumerate(records[:3]):
                _bitable_log(f"  记录{i+1} [{rec['record_id'][:8]}]:")
                for fn in WATCH:
                    fval = _get_field(rec, fn)
                    disp = (fval[:35] + "…") if len(fval) > 35 else fval
                    _bitable_log(f"    {'✅' if fval else '○'} {fn}: {disp or '(空)'}")
        except Exception as e:
            _bitable_log(f"  ⚠️ 字段预览失败: {e}")
        _bitable_log(f"  ✅ [{label}] 诊断完成，可填入工作流ID后重启监控")
        return

    # ── 正式扫描：逐个子表处理 ──
    for table in tables:
        table_id   = table["table_id"]
        table_name = table.get("name", table_id)
        try:
            records = _bitable_list_records(tk, app_token, table_id)
        except Exception as e:
            _bitable_log(f"❌ [{label}][{table_name}] 拉取记录失败: {e}")
            continue

        tasks2, tasks3, tasks4 = [], [], []
        for rec in records:
            rid = rec["record_id"]
            if rid in in_progress:
                continue
            f = lambda n, r=rec: _get_field(r, n)
            if wf2_id and f("生图提示词") and f("比例") and not f("图片url"):
                tasks2.append(rec)
            elif wf3_id and f("图片url") and f("启动视频") == "1" and not f("视频生成"):
                tasks3.append(rec)
            elif wf4_id and f("视频生成") and f("字幕") and not f("视频url"):
                tasks4.append(rec)

        def run_stage2(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]
            in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _bitable_log(f"🎨 [{label}][{tname}] 生图 {rid[:8]}…")
                result = _call_coze_workflow(wf2_id, {"生图提示词": f("生图提示词"), "比例": f("比例")},
                                             coze_token=coze_token)
                url = result.get("图片url") or result.get("url") or result.get("image_url", "")
                if url:
                    _bitable_update_record(tk, app_token, tid, rid, {"图片url": url})
                    _bitable_log(f"✅ [{label}][{tname}] 生图完成 {rid[:8]}")
                    with _bitable_monitor_lock: _bitable_monitor_stats["stage2"] += 1
                else:
                    _bitable_log(f"⚠️ [{label}][{tname}] 生图无url {rid[:8]} {result}")
            except Exception as e:
                _bitable_log(f"❌ [{label}][{tname}] 生图失败 {rid[:8]}: {e}")
                with _bitable_monitor_lock: _bitable_monitor_stats["errors"] += 1
            finally:
                in_progress.discard(rid)

        def run_stage3(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]
            in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _bitable_log(f"🎬 [{label}][{tname}] 生视频 {rid[:8]}…")
                result = _call_coze_workflow(wf3_id, {"图片url": f("图片url"), "比例": f("比例"), "视频提示词": f("视频提示词")},
                                             coze_token=coze_token)
                url = result.get("视频url") or result.get("url") or result.get("video_url", "")
                if url:
                    _bitable_update_record(tk, app_token, tid, rid, {"视频生成": url})
                    _bitable_log(f"✅ [{label}][{tname}] 生视频完成 {rid[:8]}")
                    with _bitable_monitor_lock: _bitable_monitor_stats["stage3"] += 1
                else:
                    _bitable_log(f"⚠️ [{label}][{tname}] 生视频无url {rid[:8]} {result}")
            except Exception as e:
                _bitable_log(f"❌ [{label}][{tname}] 生视频失败 {rid[:8]}: {e}")
                with _bitable_monitor_lock: _bitable_monitor_stats["errors"] += 1
            finally:
                in_progress.discard(rid)

        def run_stage4(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]
            in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _bitable_log(f"📝 [{label}][{tname}] 加字幕 {rid[:8]}…")
                result = _call_coze_workflow(wf4_id, {"视频url": f("视频生成"), "字幕": f("字幕")},
                                             coze_token=coze_token)
                url = result.get("视频url") or result.get("url") or result.get("video_url", "")
                if url:
                    _bitable_update_record(tk, app_token, tid, rid, {"视频url": url})
                    _bitable_log(f"✅ [{label}][{tname}] 加字幕完成 {rid[:8]}")
                    with _bitable_monitor_lock: _bitable_monitor_stats["stage4"] += 1
                else:
                    _bitable_log(f"⚠️ [{label}][{tname}] 加字幕无url {rid[:8]} {result}")
            except Exception as e:
                _bitable_log(f"❌ [{label}][{tname}] 加字幕失败 {rid[:8]}: {e}")
                with _bitable_monitor_lock: _bitable_monitor_stats["errors"] += 1
            finally:
                in_progress.discard(rid)

        for rec in tasks2: executor.submit(run_stage2, rec)
        for rec in tasks3: executor.submit(run_stage3, rec)
        for rec in tasks4: executor.submit(run_stage4, rec)
        if tasks2 or tasks3 or tasks4:
            _bitable_log(f"📊 [{label}][{table_name}] 生图{len(tasks2)} 生视频{len(tasks3)} 字幕{len(tasks4)}")


def _bitable_monitor_loop():
    """监控主循环，每2秒轮询所有监控组"""
    global _bitable_monitor_stats
    import time
    _bitable_log(f"✅ 监控已启动，共 {len(_bitable_monitor_configs)} 个监控组")
    executor    = _futures.ThreadPoolExecutor(max_workers=30)
    in_progress = set()
    tk_cache    = {"val": "", "expire": 0}

    while _bitable_monitor_running:
        for group in _bitable_monitor_configs:
            try:
                _bitable_monitor_one_group(group, executor, in_progress, tk_cache)
            except Exception as e:
                label = group.get("name", group.get("appToken", "?")[:8])
                _bitable_log(f"❌ [{label}] 出错: {e}")
        time.sleep(2)

    _bitable_log("🛑 监控已停止")


@app.route("/api/bitable/start", methods=["POST"])
def bitable_start():
    global _bitable_monitor_running, _bitable_monitor_thread, _bitable_monitor_configs
    if _bitable_monitor_running:
        return jsonify({"ok": False, "msg": "监控已在运行中"})
    body = request.get_json(silent=True) or {}
    groups = body.get("groups", [])
    if not groups:
        return jsonify({"ok": False, "msg": "未提供监控组配置"})
    _bitable_monitor_configs = groups
    _bitable_monitor_running = True
    _bitable_monitor_thread = threading.Thread(target=_bitable_monitor_loop, daemon=True)
    _bitable_monitor_thread.start()
    return jsonify({"ok": True, "msg": "监控已启动"})


@app.route("/api/bitable/stop", methods=["POST"])
def bitable_stop():
    global _bitable_monitor_running
    _bitable_monitor_running = False
    return jsonify({"ok": True, "msg": "监控已停止"})


@app.route("/api/bitable/status", methods=["GET"])
def bitable_status():
    with _bitable_monitor_lock:
        logs = list(_bitable_monitor_log[-50:])
        stats = dict(_bitable_monitor_stats)
    return jsonify({
        "running": _bitable_monitor_running,
        "logs": logs,
        "stats": stats,
    })


# ============================================================
# 钉钉在线表格同步
# ============================================================
_dingtalk_session = DingTalkSession()
_dingtalk_client = DingTalkClient(_dingtalk_session)

# 最近一次 sync 的缓存，供 expand-tasks 复用（避免每次重拉文档 5-10s）
_dingtalk_cache = {"content": None, "products": None, "mode_map": None, "ts": 0}


@app.route("/api/dingtalk/status", methods=["GET"])
def dingtalk_status():
    """返回钉钉登录状态 + 已保存的文档 keys"""
    dentry, doc = _dingtalk_session.get_doc_keys()
    return jsonify({
        "logged_in": _dingtalk_client.is_logged_in(),
        "dentry_key": dentry,
        "doc_key": doc,
    })


@app.route("/api/dingtalk/login", methods=["POST"])
def dingtalk_login():
    """阻塞调用：弹出扫码窗口，用户扫完后返回结果"""
    try:
        result = open_login_window(_dingtalk_session)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": f"登录窗口异常：{e}"}), 500


@app.route("/api/dingtalk/sync", methods=["POST"])
def dingtalk_sync():
    """拉钉钉文档全量数据，返回产品列表 + 制作模式映射。

    可选 body: {"date": "5.22"} —— 用于前端高亮，后端不在此处过滤
    """
    if not _dingtalk_client.is_logged_in():
        return jsonify({"ok": False, "msg": "钉钉未登录，请先扫码"}), 401
    try:
        import time as _t
        content = _dingtalk_client.fetch_target_doc()
        products = scan_dingtalk_products(content)
        mode_map   = parse_mode_map(content)
        demand_map = parse_demand_map(content)
        # 写缓存（供 expand-tasks 用）
        _dingtalk_cache["content"]  = content
        _dingtalk_cache["products"] = products
        _dingtalk_cache["mode_map"] = mode_map
        _dingtalk_cache["ts"]       = int(_t.time())
        # tuple key 转 string 给前端
        mode_map_json   = {f"{g}||{p}||{d}": m for (g, p, d), m in mode_map.items()}
        demand_map_json = {f"{g}||{p}||{d}": c for (g, p, d), c in demand_map.items()}
        return jsonify({
            "ok": True,
            "products": products,
            "mode_map": mode_map_json,
            "demand_map": demand_map_json,
            "product_count": len(products),
            "mode_map_count": len(mode_map),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "msg": f"同步失败：{e}"}), 500


@app.route("/api/dingtalk/expand-tasks", methods=["POST"])
def dingtalk_expand_tasks():
    """把前端的 (产品 + 多任务行) 展开为 parsed_products 列表。

    入参:
      { "products": [
          {"sheet": "何欢组", "product_name": "予香琳喷雾",
           "tasks": [{"date": "5.22", "count": 8}, {"date": "5.16", "count": 3}]}
        ]
      }
    出参:
      { "ok": True,
        "results": [...parsed_products items...],
        "summary": {"product_count", "task_count", "total_rows", "warnings": [...]} }

    规则：
      - mode 1：日期池 >= count → 取前 N 行（不洗牌）；否则 _random_combine
      - mode 2：count 均分到 (当前日期 + 最近前一天)，各自 _random_combine
      - mode 2 找不到前一天 → 降级 mode 1，附 warning
    """
    if not _dingtalk_client.is_logged_in():
        return jsonify({"ok": False, "msg": "钉钉未登录，请先扫码"}), 401
    body = request.get_json(silent=True) or {}
    req_products = body.get("products") or []
    if not req_products:
        return jsonify({"ok": False, "msg": "products 为空"}), 400

    # 用缓存；若缓存为空（直接跳过 sync 进来）兜底拉一次
    if not _dingtalk_cache.get("products"):
        try:
            content = _dingtalk_client.fetch_target_doc()
            _dingtalk_cache["content"]  = content
            _dingtalk_cache["products"] = scan_dingtalk_products(content)
            _dingtalk_cache["mode_map"] = parse_mode_map(content)
        except Exception as e:
            return jsonify({"ok": False, "msg": f"未同步且自动拉取失败：{e}"}), 500

    sections = _dingtalk_cache["products"]
    mode_map = _dingtalk_cache["mode_map"]

    from excel_parser import _random_combine
    from dingtalk_client import _date_key as _dt_key, _normalize_date as _dt_norm

    results = []
    warnings = []
    total_rows = 0
    task_count = 0

    for req in req_products:
        sheet     = (req.get("sheet") or "").strip()
        prod_name = (req.get("product_name") or "").strip()
        tasks     = req.get("tasks") or []
        if not (sheet and prod_name and tasks):
            continue

        sec = find_section(sections, sheet, prod_name)
        if not sec:
            warnings.append(f"❌ 找不到产品段落：{sheet} · {prod_name}")
            continue

        header_str = sec["header_str"]
        headers    = [h.strip() for h in header_str.split(" // ")]
        title      = sec.get("title") or prod_name
        avail      = sec.get("available_dates") or []

        for task_idx, task in enumerate(tasks, start=1):
            date_raw = (task.get("date") or "").strip()
            count    = int(task.get("count") or 0)
            if not date_raw or count <= 0:
                continue
            date_norm = _dt_norm(date_raw)
            date_key  = _dt_key(date_norm) or date_norm

            # 查 mode_map：产品名需归一（大小写等），与 parse_mode_map 构建时一致
            _pn = _dt_norm_product(prod_name)
            mode = mode_map.get((sheet, _pn, date_key)) \
                or mode_map.get((sheet, _pn, date_norm)) \
                or 1

            actual_mode = mode
            rows_strs = []

            if mode == 2:
                # 模式 2 = 取「模板里 ≤ 任务日」的最近 2 个真实日期，各占一半混用
                # （任务日本身在模板里时也算"最近一个"，不强制要求当天有模板）
                recent = find_recent_dates(avail, date_norm, n=2)
                if not recent:
                    warnings.append(f"❌ {sheet}·{prod_name}·{date_norm} 模式2 无任何 ≤ 该日期的模板，跳过")
                    continue
                if len(recent) == 1:
                    d_only = recent[0]
                    pool_only = filter_templates_by_date(sec, d_only)
                    if not pool_only:
                        warnings.append(f"❌ {sheet}·{prod_name}·{date_norm} 模式2 仅有 {d_only} 但模板为空，跳过")
                        continue
                    warnings.append(
                        f"⚠️ {sheet}·{prod_name}·{date_norm} 模式2 仅找到 1 个可用日期 {d_only}，全部由该日期生成")
                    rows_strs = _random_combine(pool_only, header_str, count)
                else:
                    d_new, d_old = recent[0], recent[1]
                    pool_new = filter_templates_by_date(sec, d_new)
                    pool_old = filter_templates_by_date(sec, d_old)
                    if not pool_new and not pool_old:
                        warnings.append(f"❌ {sheet}·{prod_name}·{date_norm} 模式2 候选日期 {d_new}/{d_old} 模板均空，跳过")
                        continue
                    if not pool_new:
                        warnings.append(f"⚠️ {sheet}·{prod_name}·{date_norm} 模式2 {d_new} 模板为空，全部由 {d_old} 生成")
                        rows_strs = _random_combine(pool_old, header_str, count)
                    elif not pool_old:
                        warnings.append(f"⚠️ {sheet}·{prod_name}·{date_norm} 模式2 {d_old} 模板为空，全部由 {d_new} 生成")
                        rows_strs = _random_combine(pool_new, header_str, count)
                    else:
                        n_new = count // 2
                        n_old = count - n_new
                        rows_strs += _random_combine(pool_new, header_str, n_new)
                        rows_strs += _random_combine(pool_old, header_str, n_old)
                        warnings.append(f"ℹ️ {sheet}·{prod_name}·{date_norm} 模式2 混用 {d_new}({n_new}条) + {d_old}({n_old}条)")

            if actual_mode == 1:
                # 模式1：找距离任务日最近的 1 个资料日期，用那个日期的数据
                recent_1 = find_recent_dates(avail, date_norm, n=1)
                if not recent_1:
                    warnings.append(f"❌ {sheet}·{prod_name}·{date_norm} 模式1 无任何可用资料日期，跳过")
                    continue
                d1 = recent_1[0]
                pool_1 = filter_templates_by_date(sec, d1)
                if not pool_1:
                    warnings.append(f"❌ {sheet}·{prod_name}·{date_norm} 模式1 {d1} 模板为空，跳过")
                    continue
                if d1 != date_norm:
                    warnings.append(f"⚠️ {sheet}·{prod_name}·{date_norm} 模式1 当天无资料，使用最近日期 {d1}")
                if len(pool_1) >= count:
                    rows_strs = list(pool_1[:count])
                else:
                    rows_strs = _random_combine(pool_1, header_str, count)

            rows = [r.split(" // ") for r in rows_strs]
            # 列数对齐
            col_count = len(headers)
            rows = [r + ["/"] * (col_count - len(r)) if len(r) < col_count else r[:col_count]
                    for r in rows]

            file_key = f"{date_norm}_{sheet}_{prod_name}_{task_idx}"
            results.append({
                "key":            file_key,
                "title":          title,
                "product_name":   prod_name,
                "sheet":          sheet,
                "batch_date":     date_norm,
                "task_index":     task_idx,
                "mode":           actual_mode,
                "mode_requested": mode,
                "headers":        headers,
                "header_str":     header_str,
                "rows":           rows,
                "count":          len(rows),
            })
            total_rows += len(rows)
            task_count += 1

    return jsonify({
        "ok": True,
        "results": results,
        "summary": {
            "product_count": len({r["sheet"] + "::" + r["product_name"] for r in results}),
            "task_count":    task_count,
            "total_rows":    total_rows,
            "warnings":      warnings,
        },
    })


@app.route("/api/dingtalk/logout", methods=["POST"])
def dingtalk_logout():
    """清除 session 文件"""
    _dingtalk_session.clear()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template_string(HTML, output_dir=DEFAULT_OUTPUT_DIR)


# ============================================================
# PyWebView JS API：暴露给前端直接调用的 Python 函数
# ============================================================
class DesktopApi:
    def pick_folder(self):
        """弹出系统原生文件夹选择窗口，返回选中路径"""
        result = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        if result:
            return result[0]
        return None

    def dingtalk_login(self):
        """前端可直接调用的扫码登录入口（也走 webview 窗口）"""
        return open_login_window(_dingtalk_session)


# ============================================================
# 启动：Flask 后台线程 + PyWebView 原生窗口
# ============================================================
def run_flask(port):
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    import time, urllib.request
    port = 5678

    # 在后台线程启动 Flask
    t = threading.Thread(target=run_flask, args=(port,), daemon=True)
    t.start()

    # 等 Flask 就绪
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/")
            break
        except Exception:
            time.sleep(0.3)

    # 打开原生桌面窗口，注入 js_api
    api = DesktopApi()
    webview.create_window(
        title="🎬 秀悦视频下载工具",
        url=f"http://127.0.0.1:{port}/",
        width=960,
        height=760,
        min_size=(720, 560),
        resizable=True,
        js_api=api,
    )
    webview.start()
