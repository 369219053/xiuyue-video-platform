"""钉钉扫码登录窗口

职责：
  - 在主桌面窗口运行时，弹出第二个 pywebview 窗口加载钉钉文档 URL
  - 钉钉自动跳到登录页 → 用户扫码 → 跳回文档页
  - 检测到文档页 URL（含 spreadsheetv2）后捕获 cookie 存盘 + 提取 dentryKey/docKey
  - 自动关闭窗口

写死的目标文档：
  https://alidocs.dingtalk.com/i/nodes/9bN7RYPWdEdx2zZpckZmKvM6VZd1wyK0
"""
import threading
import time
import re
from typing import Optional

import webview

from dingtalk_client import DingTalkSession


# 写死的目标文档 URL（带 nodeId，钉钉会自动重定向到 spreadsheetv2/{dentryKey}）
TARGET_DOC_URL = "https://alidocs.dingtalk.com/i/nodes/9bN7RYPWdEdx2zZpckZmKvM6VZd1wyK0"

# 全局状态（多线程安全用于回传结果）
_login_lock = threading.Lock()
_login_state = {
    "in_progress": False,
    "result": None,  # {"success": bool, "message": str, "dentry_key": str, "doc_key": str}
}


def _cookies_to_playwright(http_cookies) -> list:
    """pywebview window.get_cookies() 返回 http.cookiejar.Cookie 列表，转 Playwright 格式。"""
    out = []
    for c in http_cookies:
        # http.cookiejar.Cookie 有 name/value/domain/path/expires 等属性
        out.append({
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain or "",
            "path": c.path or "/",
            "expires": float(c.expires) if c.expires else -1,
            "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly", False)),
            "secure": bool(c.secure),
            "sameSite": "Lax",
        })
    return out


def _extract_keys_from_url(url: str) -> tuple:
    """从 spreadsheetv2 URL 抽 dentryKey 和 docKey。"""
    dentry = ""
    doc = ""
    m = re.search(r"/spreadsheetv2/([A-Za-z0-9]+)", url)
    if m:
        dentry = m.group(1)
    m = re.search(r"[?&]docKey=([A-Za-z0-9]+)", url)
    if m:
        doc = m.group(1)
    else:
        m = re.search(r"[?&]docId=([A-Za-z0-9]+)", url)
        if m:
            doc = m.group(1)
    return dentry, doc


def open_login_window(session: Optional[DingTalkSession] = None) -> dict:
    """打开扫码登录窗口（阻塞直到登录完成或窗口关闭）。

    返回：{"success": bool, "message": str, "dentry_key": str, "doc_key": str}
    """
    with _login_lock:
        if _login_state["in_progress"]:
            return {"success": False, "message": "登录窗口已打开，请先完成或关闭", "dentry_key": "", "doc_key": ""}
        _login_state["in_progress"] = True
        _login_state["result"] = None

    sess = session or DingTalkSession()
    done_event = threading.Event()
    captured = {"success": False, "message": "用户关闭了窗口", "dentry_key": "", "doc_key": ""}

    def on_loaded(window):
        """每次页面加载（包括跳转后）都触发。一旦 URL 含 spreadsheetv2 就抓 cookie。"""
        try:
            current_url = window.get_current_url() or ""
        except Exception:
            return
        if "alidocs.dingtalk.com/spreadsheetv2/" not in current_url:
            return
        # 等 1.5 秒让 cookie 完全落盘
        time.sleep(1.5)
        try:
            http_cookies = window.get_cookies()
        except Exception as e:
            captured["message"] = f"抓 cookie 失败：{e}"
            done_event.set()
            return
        # 多次 get_cookies 拿到的是分 domain 的列表，扁平化
        all_cookies = []
        for jar in http_cookies:
            try:
                for c in jar:
                    all_cookies.append(c)
            except TypeError:
                all_cookies.append(jar)
        pl_cookies = _cookies_to_playwright(all_cookies)
        # 验证关键 cookie 存在
        names = {c["name"] for c in pl_cookies if "dingtalk" in c["domain"]}
        if "doc_atoken" not in names or "account" not in names:
            captured["message"] = f"关键 cookie 缺失（doc_atoken/account），当前 cookie 数={len(pl_cookies)}"
            done_event.set()
            return
        dentry, doc = _extract_keys_from_url(current_url)
        sess.save_cookies(pl_cookies, dentry_key=dentry, doc_key=doc)
        captured.update({
            "success": True,
            "message": f"登录成功（{len(pl_cookies)} 个 cookie）",
            "dentry_key": dentry,
            "doc_key": doc,
        })
        done_event.set()
        # 延迟关窗口，让用户看到"登录成功"反馈
        try:
            time.sleep(0.5)
            window.destroy()
        except Exception:
            pass

    # 创建新窗口（pywebview 支持多窗口；主窗口已 webview.start()）
    win = webview.create_window(
        title="钉钉扫码登录",
        url=TARGET_DOC_URL,
        width=900,
        height=720,
        resizable=True,
    )
    win.events.loaded += lambda: on_loaded(win)

    # 等回调完成（最多 5 分钟）
    done_event.wait(timeout=300)

    with _login_lock:
        _login_state["in_progress"] = False
        _login_state["result"] = captured
    return captured
