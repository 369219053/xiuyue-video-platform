"""钉钉在线表格客户端

职责：
  - cookie 持久化（扫码登录后存到 data/dingtalk_session.json）
  - 调用 /api/document/data 拿完整文档 JSON
  - 把钉钉 JSON 转换为兼容 excel_parser._scan_sections 的二维行格式
  - 解析汇总表 (组+品类+日期) → 制作模式 映射

对外主入口：
  - DingTalkClient(session_path).fetch_doc(url) → 内部统一格式
  - scan_dingtalk_products(content) → 跟 excel_parser.scan_products 兼容的产品列表
  - parse_mode_map(content) → {(group, product, date): mode}
"""
import json
import re
import gzip
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


DEFAULT_SESSION_PATH = Path("data/dingtalk_session.json")
API_URL = "https://alidocs.dingtalk.com/api/document/data"
SUMMARY_SHEET_TITLE = "ai数量需求汇总"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


# ============================================================
# Session 持久化
# ============================================================
class DingTalkSession:
    """封装 cookie 的加载/保存。文件格式跟 Playwright storageState 兼容。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_SESSION_PATH

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def _read(self) -> dict:
        if not self.exists():
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"cookies": data}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    def load_cookies(self) -> list:
        return self._read().get("cookies", []) or []

    def save_cookies(self, cookies: list, dentry_key: str = "", doc_key: str = "") -> None:
        data = self._read()
        data["cookies"] = cookies
        if dentry_key:
            data["dentry_key"] = dentry_key
        if doc_key:
            data["doc_key"] = doc_key
        self._write(data)

    def get_doc_keys(self) -> tuple:
        """返回 (dentry_key, doc_key)，没有就返回空字符串。"""
        data = self._read()
        return data.get("dentry_key", ""), data.get("doc_key", "")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def _cookies_for_host(cookies: list, host: str) -> tuple[str, str]:
    """从 cookies 列表里挑出指定 host 能用的，返回 (Cookie header, XSRF-TOKEN)"""
    pairs, seen, xsrf = [], set(), ""
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            name = c.get("name")
            if name and name not in seen:
                pairs.append(f"{name}={c.get('value', '')}")
                seen.add(name)
            if name == "XSRF-TOKEN" and domain == host:
                xsrf = c.get("value", "")
    return "; ".join(pairs), xsrf


# ============================================================
# HTTP 客户端
# ============================================================
class DingTalkClient:
    """钉钉文档 API 客户端，cookie 复用、自动 gzip 解压。"""

    def __init__(self, session: Optional[DingTalkSession] = None):
        self.session = session or DingTalkSession()

    def is_logged_in(self) -> bool:
        cookies = self.session.load_cookies()
        if not cookies:
            return False
        header, _ = _cookies_for_host(cookies, "alidocs.dingtalk.com")
        return "doc_atoken=" in header and "account=" in header

    def fetch_doc(self, doc_key: str, dentry_key: str, sheet_id: str = "") -> dict:
        """拉取整份文档数据，返回解析后的 content（dict）。"""
        cookies = self.session.load_cookies()
        cookie_header, xsrf = _cookies_for_host(cookies, "alidocs.dingtalk.com")
        if not cookie_header:
            raise RuntimeError("钉钉未登录或 cookie 失效，请扫码登录")

        payload = {
            "pageMode": 2,
            "orgGrayKeys": ["enable_notable_frontend"],
            "enableSlice": True,
            "anchor": {
                "needSheetSkeleton": True,
                "sheetId": sheet_id or "",
                "startRow": 0,
                "withFirstBlockRowCount": 100,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": DEFAULT_UA,
            "Referer": f"https://alidocs.dingtalk.com/spreadsheetv2/{dentry_key}/edit",
            "Origin": "https://alidocs.dingtalk.com",
            "Cookie": cookie_header,
            "a-doc-key": doc_key,
            "a-dentry-key": dentry_key,
        }
        if xsrf:
            headers["x-csrf-token"] = xsrf

        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"钉钉 API 失败 HTTP {e.code}：{body_text}") from None

        data = json.loads(text)
        content_str = (
            data.get("data", {}).get("documentContent", {})
            .get("checkpoint", {}).get("content")
        )
        if not content_str:
            raise RuntimeError(f"钉钉返回缺少 content 字段：top keys={list(data.keys())}")
        return json.loads(content_str)

    def fetch_target_doc(self) -> dict:
        """从 session 读 dentry_key/doc_key 自动调 fetch_doc。"""
        dentry_key, doc_key = self.session.get_doc_keys()
        if not dentry_key or not doc_key:
            raise RuntimeError("session 里缺 dentry_key/doc_key，请先扫码登录")
        return self.fetch_doc(doc_key, dentry_key)



# ============================================================
# JSON → 二维行转换（兼容 excel_parser._scan_sections）
# ============================================================
def _sheet_to_rows(sheet_data: dict) -> list:
    """钉钉 sheet 的稀疏结构展开为 list[tuple]，兼容 openpyxl 输出格式。

    钉钉结构：
        sheet_data = {
            'rows': [[rowIdx, meta, [[colIdx, {'value': X}], ...]], ...],
            'rowCount': int, 'colCount': int,
        }
    rowIdx / colIdx 均 0-based，稀疏存储（空 cell 不写）。
    """
    rows_list = sheet_data.get("rows", []) or []
    row_count = sheet_data.get("rowCount", 0) or 0
    col_count = sheet_data.get("colCount", 0) or 0

    max_row_idx = -1
    for r in rows_list:
        if isinstance(r, list) and r:
            max_row_idx = max(max_row_idx, int(r[0]))
    effective_rows = min(row_count, max_row_idx + 1) if max_row_idx >= 0 else row_count

    matrix = [[None] * col_count for _ in range(effective_rows)]
    for r in rows_list:
        if not isinstance(r, list) or len(r) < 3:
            continue
        row_idx = int(r[0])
        if row_idx >= effective_rows:
            continue
        cells = r[2] or []
        for c in cells:
            if not isinstance(c, list) or len(c) < 2:
                continue
            col_idx = int(c[0])
            if col_idx >= col_count:
                continue
            val_obj = c[1] or {}
            val = val_obj.get("value") if isinstance(val_obj, dict) else None
            matrix[row_idx][col_idx] = val
    return [tuple(r) for r in matrix]


def dingtalk_doc_to_sheets(content: dict) -> dict:
    """钉钉文档 content → {sheet_title: list[tuple]}，跟 openpyxl 等价。"""
    sheets_meta = content.get("sheetsMeta", []) or []
    content_map = content.get("content", {}) or {}
    result = {}
    for sm in sheets_meta:
        sid = sm.get("id")
        title = sm.get("title") or sid
        if not sid or sid not in content_map:
            continue
        result[title] = _sheet_to_rows(content_map[sid])
    return result


# ============================================================
# 产品扫描（输出格式跟 excel_parser.scan_products 完全一致）
# ============================================================
def scan_dingtalk_products(content: dict) -> list:
    """跟 excel_parser.scan_products 等价，但输入是钉钉 JSON。

    返回每个产品段落额外带 `available_dates`（该段落数据行里出现过的日期列表）。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from excel_parser import _scan_sections  # noqa: E402

    sheets = dingtalk_doc_to_sheets(content)
    results = []
    for sheet_name, rows in sheets.items():
        if sheet_name == SUMMARY_SHEET_TITLE:
            continue
        sections = _scan_sections(rows)
        if not sections:
            continue
        for sec in sections:
            if not sec.get("templates"):
                continue
            title = sec["title"]
            prod_clean = re.sub(r"[（(][^）)]*[）)]", "", title).strip()
            dates = _extract_section_dates(sec)
            results.append({
                "sheet": sheet_name,
                "title": title,
                "product_name": prod_clean,
                "header_str": sec["header_str"],
                "col_count": sec["col_count"],
                "templates": sec["templates"],
                "template_count": len(sec["templates"]),
                "available_dates": dates,
            })
    return results


def _extract_section_dates(sec: dict) -> list:
    """从段落模版行里抽出"日期"列的所有唯一值（按出现顺序）。"""
    headers = (sec.get("header_str") or "").split(" // ")
    date_col = None
    for i, h in enumerate(headers):
        if h.strip() == "日期":
            date_col = i
            break
    if date_col is None:
        return []
    seen, out = set(), []
    for tpl in sec.get("templates", []):
        parts = tpl.split(" // ")
        if date_col >= len(parts):
            continue
        d = _normalize_date(parts[date_col])
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _excel_serial_to_md(serial: int) -> str:
    """Excel 1900 序列号 → '月.日'（1900-01-01 = 1，含 Lotus 1-2-3 bug）。"""
    from datetime import datetime, timedelta
    try:
        dt = datetime(1899, 12, 30) + timedelta(days=int(serial))
        return f"{dt.month}.{dt.day}"
    except Exception:
        return ""


def _normalize_date(raw) -> str:
    """统一日期字符串。钉钉里日期可能是 float (5.16)、int (516 / Excel 序列号 42510)、
    str ('5.16' / '5.21.' / '5月16日')。

    输出规则：保留 '月.日' 形式（如 '5.16'），不补零。空 / 无法解析 → ''
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, float):
        # 5.16 浮点保留原样字符串（避免变成 5.160000001）
        s = f"{raw:g}"
        return s if "." in s else ""
    if isinstance(raw, int):
        # Excel 序列号（> 10000 视为日期）
        if raw > 10000:
            return _excel_serial_to_md(raw)
        return ""  # 单个小整数日期含糊（516？16？），跳过
    s = str(raw).strip().rstrip(".")  # 剥掉用户手输的尾点（"5.21."）
    if not s or s in ("/", "-"):
        return ""
    # '5月16日' / '5月16号' → '5.16'
    m = re.match(r"^(\d{1,2})月(\d{1,2})[日号]?$", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # '5/16'、'5-16'、'5.16' → '5.16'
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # 纯数字字符串可能是 Excel 序列号
    if s.isdigit() and int(s) > 10000:
        return _excel_serial_to_md(int(s))
    return s


def _norm_product(s: str) -> str:
    """产品名归一：去括号备注 + 去空格 + 转小写。
    与前端 _dtNormName() 保持完全一致，用于 mode_map key 构建。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "").strip().lower()
    return re.sub(r"\s+", "", s)


def _date_key(date_str: str) -> str:
    """日期匹配键：把 '5.20' 和 '5.2' 归一为同一个键（钉钉 float 信息丢失补救）。

    规则：'M.D' → 去掉个位日期的尾零。'5.20' → '5.2'，'5.02' → '5.2'，'5.10' → '5.1'。
    用户在 UI 输入 '5.20' 或 '5.2' 都能匹配到原始数据。
    """
    if not date_str:
        return ""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", date_str.strip())
    if not m:
        return date_str.strip()
    month = int(m.group(1))
    day = int(m.group(2).lstrip("0") or "0")
    # 单位数日 + 原字符串以 0 结尾 → 视为整十日（用户写 5.20，钉钉存 5.2）
    raw_day = m.group(2)
    if len(raw_day) == 1 and raw_day != "0":
        # 不能确定是 X日 还是 X0日，归一到去尾零形式
        return f"{month}.{day}"
    return f"{month}.{day}"


# ============================================================
# 汇总表 → 制作模式映射
# ============================================================
def parse_mode_map(content: dict) -> dict:
    """从汇总表读 (组, 品类, 日期) → 制作模式 (1 或 2)。

    汇总表列：日期 / 组 / 品类 / 制作模式 / 需求数量 / ...
    制作模式空 / 非 2 → 默认 1。
    """
    sheets = dingtalk_doc_to_sheets(content)
    rows = sheets.get(SUMMARY_SHEET_TITLE, [])
    if not rows:
        return {}

    # 找表头行：包含 "日期" "组" "品类" "制作模式"
    header_row_idx = -1
    col_date = col_group = col_product = col_mode = -1
    for i, row in enumerate(rows):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if "日期" in cells and "组" in cells and "品类" in cells:
            header_row_idx = i
            col_date = cells.index("日期")
            col_group = cells.index("组")
            col_product = cells.index("品类")
            if "制作模式" in cells:
                col_mode = cells.index("制作模式")
            break
    if header_row_idx < 0:
        return {}

    # 汇总表里"日期"和"组"列有合并单元格，钉钉只在块首行存值，需要 forward-fill
    out = {}
    cur_date = ""
    cur_group = ""
    for row in rows[header_row_idx + 1:]:
        # 日期列
        if col_date < len(row) and row[col_date] is not None:
            d = _normalize_date(row[col_date])
            if d:
                cur_date = d
        # 组列
        if col_group < len(row) and row[col_group] is not None:
            g = str(row[col_group]).strip()
            if g:
                cur_group = g
        # 品类列（不继承，每行必填）
        if col_product >= len(row) or row[col_product] is None:
            continue
        product = str(row[col_product]).strip()
        if not (cur_date and cur_group and product):
            continue
        mode = 1
        if col_mode >= 0 and col_mode < len(row) and row[col_mode] is not None:
            mode_str = str(row[col_mode]).strip()
            if mode_str == "2":
                mode = 2
        # 产品名归一（大小写 / 括号备注），与前端 _dtNormName 保持一致
        out[(cur_group, _norm_product(product), cur_date)] = mode
    return out


def parse_demand_map(content: dict) -> dict:
    """从汇总表读 (组, 品类, 日期) → 需求数量 (int)。

    与 parse_mode_map 共用同一行遍历逻辑，额外解析「需求数量」列。
    找不到该列或值非正整数时该条目不写入 map。
    """
    sheets = dingtalk_doc_to_sheets(content)
    rows = sheets.get(SUMMARY_SHEET_TITLE, [])
    if not rows:
        return {}

    header_row_idx = -1
    col_date = col_group = col_product = col_qty = -1
    for i, row in enumerate(rows):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if "日期" in cells and "组" in cells and "品类" in cells:
            header_row_idx = i
            col_date    = cells.index("日期")
            col_group   = cells.index("组")
            col_product = cells.index("品类")
            # 需求数量列：优先精确匹配，再找包含"需求"的列
            if "需求数量" in cells:
                col_qty = cells.index("需求数量")
            else:
                for j, c in enumerate(cells):
                    if "需求" in c:
                        col_qty = j
                        break
            break
    if header_row_idx < 0 or col_qty < 0:
        return {}

    out = {}
    cur_date = ""
    cur_group = ""
    for row in rows[header_row_idx + 1:]:
        if col_date < len(row) and row[col_date] is not None:
            d = _normalize_date(row[col_date])
            if d:
                cur_date = d
        if col_group < len(row) and row[col_group] is not None:
            g = str(row[col_group]).strip()
            if g:
                cur_group = g
        if col_product >= len(row) or row[col_product] is None:
            continue
        product = str(row[col_product]).strip()
        if not (cur_date and cur_group and product):
            continue
        # 解析需求数量
        qty = 0
        if col_qty < len(row) and row[col_qty] is not None:
            try:
                qty = int(float(str(row[col_qty]).strip()))
            except (ValueError, TypeError):
                qty = 0
        if qty <= 0:
            continue
        out[(cur_group, _norm_product(product), cur_date)] = qty
    return out


# ============================================================
# 段落 / 日期 工具（供 expand-tasks 用）
# ============================================================
def filter_templates_by_date(sec: dict, target_date: str) -> list:
    """从段落里筛出指定日期的模版行（用 _date_key 归一匹配）。

    若段落没有"日期"列，返回全部模版（说明该段落不按日期组织）。
    """
    headers = (sec.get("header_str") or "").split(" // ")
    date_col = None
    for i, h in enumerate(headers):
        if h.strip() == "日期":
            date_col = i
            break
    if date_col is None:
        return list(sec.get("templates", []))
    target_key = _date_key(_normalize_date(target_date))
    if not target_key:
        return []
    out = []
    for tpl in sec.get("templates", []):
        parts = tpl.split(" // ")
        if date_col >= len(parts):
            continue
        d = _date_key(_normalize_date(parts[date_col]))
        if d == target_key:
            out.append(tpl)
    return out


def _date_tuple(d: str):
    """'5.16' → (5, 16)，无法解析返回 None。"""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", (d or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_prev_date(available_dates: list, current_date: str) -> str:
    """从 available_dates 里找比 current_date 最近的前一天，找不到返回 ''。"""
    cur = _date_tuple(_date_key(current_date))
    if not cur:
        return ""
    best = ""
    best_t = None
    for d in available_dates or []:
        dt = _date_tuple(_date_key(d))
        if not dt or dt >= cur:
            continue
        if best_t is None or dt > best_t:
            best_t = dt
            best = d
    return best


def find_recent_dates(available_dates: list, target_date: str, n: int = 2) -> list:
    """从 available_dates 里挑出 ≤ target_date 的最近 n 个日期（按时间降序返回）。

    用于模式 2：取「模板里不晚于任务日」的最近 N 个真实模板日期混用。
    若任务日本身在模板里，也会被纳入（视为最近的一个）。
    可用日期不足 n 个时返回全部。
    """
    cur = _date_tuple(_date_key(target_date))
    if not cur:
        return []
    candidates = []
    for d in available_dates or []:
        dt = _date_tuple(_date_key(d))
        if dt and dt <= cur:
            candidates.append((dt, d))
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    out = []
    for _, d in candidates:
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
        if len(out) >= n:
            break
    return out


def find_section(products: list, sheet: str, product_name: str) -> dict:
    """按 (sheet, product_name) 找段落，匹配规则：sheet 严格相等，product_name 去括号 + 小写。"""
    def norm(s: str) -> str:
        return re.sub(r"[（(][^）)]*[）)]", "", s or "").strip().lower()
    pn = norm(product_name)
    sn = (sheet or "").strip()
    for p in products or []:
        if (p.get("sheet") or "").strip() == sn and norm(p.get("product_name")) == pn:
            return p
    return None


# ============================================================
# URL 解析（从钉钉文档链接抽 nodeId 等）
# ============================================================
def parse_doc_url(url: str) -> dict:
    """从钉钉文档 URL 抽 nodeId 或 dentryKey/docKey。

    支持两种 URL：
      - https://alidocs.dingtalk.com/i/nodes/{nodeId}?...
      - https://alidocs.dingtalk.com/spreadsheetv2/{dentryKey}/edit?docId={docKey}&...
    """
    out = {"node_id": "", "dentry_key": "", "doc_key": ""}
    m = re.search(r"/i/nodes/([A-Za-z0-9]+)", url)
    if m:
        out["node_id"] = m.group(1)
    m = re.search(r"/spreadsheetv2/([A-Za-z0-9]+)", url)
    if m:
        out["dentry_key"] = m.group(1)
    m = re.search(r"[?&]docId=([A-Za-z0-9]+)", url)
    if m:
        out["doc_key"] = m.group(1)
    m = re.search(r"[?&]docKey=([A-Za-z0-9]+)", url)
    if m:
        out["doc_key"] = m.group(1)
    return out
