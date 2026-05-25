#!/usr/bin/env python3
"""
飞书多维表格自动监控 - 独立版
运行: python3 src/bitable_monitor.py
访问: http://127.0.0.1:5679
"""
import concurrent.futures as _futures
import json
import os
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template_string, request, Response

# ── 加载 .env ──────────────────────────────────────────────────
def _load_env_file(path: str = ".env"):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass

_load_env_file()

app = Flask(__name__)

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# ── 全局状态 ───────────────────────────────────────────────────
_FEISHU_HOST = "https://open.feishu.cn"
_monitor_running = False
_monitor_thread  = None
_monitor_log     = []
_monitor_lock    = threading.Lock()
_monitor_stats   = {"stage2": 0, "stage3": 0, "stage4": 0, "errors": 0}
_monitor_configs = []
_GROUPS_FILE     = Path("data/bitable_monitor_groups.json")

def _log(msg: str):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    with _monitor_lock:
        _monitor_log.append(f"[{ts}] {msg}")
        if len(_monitor_log) > 500:
            _monitor_log.pop(0)
    print(f"[monitor] {msg}")

# ── 飞书 API ───────────────────────────────────────────────────
def _get_feishu_token() -> str:
    resp = requests.post(
        f"{_FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": os.getenv("FEISHU_APP_ID",""), "app_secret": os.getenv("FEISHU_APP_SECRET","")},
        timeout=10
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书token失败: {data}")
    return data["tenant_access_token"]

def _list_tables(token: str, app_token: str) -> list:
    resp = requests.get(
        f"{_FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables",
        headers={"Authorization": f"Bearer {token}"}, timeout=15
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取子表失败: {data}")
    return data.get("data", {}).get("items", [])

def _list_records(token: str, app_token: str, table_id: str) -> list:
    records, page_token = [], ""
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"{_FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=headers, params=params, timeout=15
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拉取记录失败: {data}")
        records.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token", "")
    return records

def _update_record(token: str, app_token: str, table_id: str, record_id: str, fields: dict):
    """写回字段，限流时无限重试直到成功"""
    import time as _t
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{_FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    attempt = 0
    while True:
        resp = requests.put(url, headers=headers, json={"fields": fields}, timeout=15)
        data = resp.json()
        code = data.get("code", 0)
        if code == 0:
            if attempt > 0:
                _log(f"  ✅ 写回成功（第{attempt+1}次重试后）")
            return
        if code in (429, 1254003) or resp.status_code == 429:
            wait = min(2 ** attempt, 60)
            _log(f"  ⚠️ 飞书限流，{wait}秒后第{attempt+1}次重试…")
            _t.sleep(wait)
            attempt += 1
            continue
        raise RuntimeError(f"写回失败: {data}")

def _call_coze(workflow_id: str, params: dict, coze_token: str = "") -> dict:
    token   = coze_token or os.getenv("COZE_TOKEN_1", "")
    base_url = os.getenv("COZE_BASE_URL", "https://api.coze.cn")
    _log(f"  [Coze] token={token[:12]}… wf={workflow_id} params={list(params.keys())}")
    resp = requests.post(
        f"{base_url}/v1/workflow/run",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"workflow_id": workflow_id, "parameters": params},
        timeout=3600
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"工作流调用失败: {data}")
    output = data.get("data", {})
    if isinstance(output, str):
        try: output = json.loads(output)
        except: pass
    return output

def _get_field(record: dict, field_name: str) -> str:
    val = record.get("fields", {}).get(field_name, "")
    if isinstance(val, list):
        return "".join(v.get("text", "") for v in val if isinstance(v, dict))
    return str(val) if val is not None else ""


# ── 监控核心逻辑 ───────────────────────────────────────────────
def _scan_group(group: dict, executor, in_progress: set, tk_cache: dict):
    app_token  = (group.get("appToken") or "").strip() or os.getenv("BITABLE_APP_TOKEN", "")
    wf2_id     = (group.get("wf2")      or "").strip() or os.getenv("WORKFLOW2_ID", "")
    wf3_id     = (group.get("wf3")      or "").strip() or os.getenv("WORKFLOW3_ID", "")
    wf4_id     = (group.get("wf4")      or "").strip() or os.getenv("WORKFLOW4_ID", "")
    coze_token = (group.get("cozeToken") or "").strip() or os.getenv("COZE_TOKEN_1", "")
    label      = group.get("name", "") or app_token[:8]

    if not app_token:
        _log(f"⚠️ [{label}] 未配置 Base Token，跳过"); return

    import time
    if time.time() > tk_cache.get("expire", 0) - 60:
        tk_cache["val"]    = _get_feishu_token()
        tk_cache["expire"] = time.time() + 7000
    tk = tk_cache["val"]

    test_mode = not any([wf2_id, wf3_id, wf4_id])
    try:
        tables = _list_tables(tk, app_token)
    except Exception as e:
        _log(f"❌ [{label}] 获取子表失败: {e}"); return

    if not tables:
        _log(f"⚠️ [{label}] 该 Base 下没有子表"); return

    if test_mode and not group.get("_diag_done"):
        group["_diag_done"] = True
        _log(f"🔍 [{label}] 诊断模式，共发现 {len(tables)} 个子表：")
        for t in tables:
            _log(f"  📋 {t.get('name','?')}  (id: {t.get('table_id','?')})")
        try:
            first_id = tables[0]["table_id"]
            records  = _list_records(tk, app_token, first_id)
            _log(f"  [预览] 共 {len(records)} 条记录")
            WATCH = ["生图提示词", "图片url", "启动视频", "视频提示词", "比例", "字幕", "视频生成"]
            for i, rec in enumerate(records[:3]):
                _log(f"  记录{i+1} [{rec['record_id'][:8]}]:")
                for fn in WATCH:
                    fval = _get_field(rec, fn)
                    _log(f"    {'✅' if fval else '○'} {fn}: {(fval[:35]+'…') if len(fval)>35 else fval or '(空)'}")
        except Exception as e:
            _log(f"  ⚠️ 字段预览失败: {e}")
        _log(f"  ✅ [{label}] 诊断完成，可填入工作流ID后重启监控")
        return

    for table in tables:
        table_id, table_name = table["table_id"], table.get("name", table["table_id"])
        try:
            records = _list_records(tk, app_token, table_id)
        except Exception as e:
            _log(f"❌ [{label}][{table_name}] 拉取记录失败: {e}"); continue

        tasks2, tasks3, tasks4 = [], [], []
        for rec in records:
            rid = rec["record_id"]
            if rid in in_progress: continue
            f = lambda n, r=rec: _get_field(r, n)
            if wf2_id and f("生图提示词") and f("比例") and not f("图片url"):
                tasks2.append(rec)
            elif wf3_id and f("启动视频") and f("图片url") and f("视频提示词") and f("比例") and not f("视频生成"):
                tasks3.append(rec)
            elif wf4_id and f("视频生成") and f("字幕") and not f("视频剪辑"):
                tasks4.append(rec)

        def run2(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]; in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _log(f"🎨 [{label}][{tname}] 生图 {rid[:8]}")
                result = _call_coze(wf2_id, {"input": f("生图提示词"), "ratio": f("比例")}, coze_token)
                _log(f"  工作流返回: {str(result)[:200]}")
                raw = result.get("图片url") or result.get("image_url") or result.get("url") or result.get("output") or ""
                url = str(raw[0] if isinstance(raw, list) and raw else raw).strip()
                if url:
                    _update_record(tk, app_token, tid, rid, {"图片url": url})
                    _log(f"✅ [{label}][{tname}] 生图完成 {rid[:8]} → {url[:60]}")
                    with _monitor_lock: _monitor_stats["stage2"] += 1
                else:
                    _log(f"⚠️ [{label}][{tname}] 生图无url {rid[:8]} 完整返回: {result}")
            except Exception as e:
                _log(f"❌ [{label}][{tname}] 生图失败 {rid[:8]}: {e}")
                with _monitor_lock: _monitor_stats["errors"] += 1
            finally: in_progress.discard(rid)

        def run3(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]; in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _log(f"🎬 [{label}][{tname}] 生视频 {rid[:8]} ratio={f('比例')}")
                result = _call_coze(wf3_id, {"url": f("图片url"), "ratio": f("比例"), "prompt": f("视频提示词"), "qidong": f("启动视频")}, coze_token)
                _log(f"  工作流返回: {str(result)[:200]}")
                raw = result.get("视频url") or result.get("video_url") or result.get("url") or result.get("output") or ""
                url = str(raw[0] if isinstance(raw, list) and raw else raw).strip()
                if url:
                    _update_record(tk, app_token, tid, rid, {"视频生成": url})
                    _log(f"✅ [{label}][{tname}] 生视频完成 {rid[:8]} → {url[:60]}")
                    with _monitor_lock: _monitor_stats["stage3"] += 1
                else:
                    _log(f"⚠️ [{label}][{tname}] 生视频无url {rid[:8]} 完整返回: {result}")
            except Exception as e:
                _log(f"❌ [{label}][{tname}] 生视频失败 {rid[:8]}: {e}")
                with _monitor_lock: _monitor_stats["errors"] += 1
            finally: in_progress.discard(rid)

        def run4(rec, tid=table_id, tname=table_name):
            rid = rec["record_id"]; in_progress.add(rid)
            try:
                f = lambda n: _get_field(rec, n)
                _log(f"📝 [{label}][{tname}] 加字幕 {rid[:8]} ratio={f('比例')}")
                result = _call_coze(wf4_id, {"input": f("视频生成"), "ratio": f("比例"), "zimu": f("字幕")}, coze_token)
                _log(f"  工作流返回: {str(result)[:200]}")
                raw = result.get("视频url") or result.get("video_url") or result.get("url") or result.get("output") or ""
                url = str(raw[0] if isinstance(raw, list) and raw else raw).strip()
                if url:
                    _update_record(tk, app_token, tid, rid, {"视频剪辑": url})
                    _log(f"✅ [{label}][{tname}] 加字幕完成 {rid[:8]} → {url[:60]}")
                    with _monitor_lock: _monitor_stats["stage4"] += 1
                else:
                    _log(f"⚠️ [{label}][{tname}] 加字幕无url {rid[:8]} 完整返回: {result}")
            except Exception as e:
                _log(f"❌ [{label}][{tname}] 加字幕失败 {rid[:8]}: {e}")
                with _monitor_lock: _monitor_stats["errors"] += 1
            finally: in_progress.discard(rid)

        for rec in tasks2: executor.submit(run2, rec)
        for rec in tasks3: executor.submit(run3, rec)
        for rec in tasks4: executor.submit(run4, rec)
        if tasks2 or tasks3 or tasks4:
            _log(f"📊 [{label}][{table_name}] 生图{len(tasks2)} 生视频{len(tasks3)} 字幕{len(tasks4)}")


def _monitor_loop():
    global _monitor_stats
    import time
    _log(f"✅ 监控已启动，共 {len(_monitor_configs)} 个监控组")
    executor    = _futures.ThreadPoolExecutor(max_workers=500)
    in_progress = set()
    tk_cache    = {"val": "", "expire": 0}
    while _monitor_running:
        for group in _monitor_configs:
            try:
                _scan_group(group, executor, in_progress, tk_cache)
            except Exception as e:
                _log(f"❌ [{group.get('name','?')}] 出错: {e}")
        time.sleep(2)
    _log("🛑 监控已停止")


# ── API 路由 ───────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def api_start():
    global _monitor_running, _monitor_thread, _monitor_configs
    if _monitor_running:
        return jsonify({"ok": False, "msg": "监控已在运行中"})
    body   = request.get_json(silent=True) or {}
    groups = body.get("groups", [])
    if not groups:
        return jsonify({"ok": False, "msg": "未提供监控组配置"})
    _monitor_configs = groups
    _monitor_running = True
    _monitor_thread  = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()
    return jsonify({"ok": True, "msg": f"监控已启动，共 {len(groups)} 个监控组"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _monitor_running
    _monitor_running = False
    return jsonify({"ok": True, "msg": "监控已停止"})

@app.route("/api/status", methods=["GET"])
def api_status():
    with _monitor_lock:
        logs  = list(_monitor_log[-50:])
        stats = dict(_monitor_stats)
    return jsonify({"running": _monitor_running, "logs": logs, "stats": stats})

@app.route("/api/save-groups", methods=["POST"])
def api_save_groups():
    body = request.get_json(silent=True) or {}
    _GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GROUPS_FILE.write_text(json.dumps(body.get("groups", []), ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/load-groups", methods=["GET"])
def api_load_groups():
    try:
        groups = json.loads(_GROUPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        groups = []
    return jsonify({"groups": groups})

# ── HTML 页面 ──────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>🤖 飞书监控</title>
<style>
  body{font-family:system-ui,sans-serif;background:#f0f9ff;margin:0;padding:20px}
  h1{color:#0369a1;margin-bottom:20px}
  .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px #0001;margin-bottom:16px}
  input{width:100%;box-sizing:border-box;padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:13px;margin-top:4px}
  label{font-size:12px;color:#64748b;font-weight:600}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px}
  button{padding:8px 18px;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px}
  .btn-start{background:#0ea5e9;color:#fff} .btn-stop{background:#ef4444;color:#fff}
  .btn-add{background:#f1f5f9;color:#334155} .btn-del{background:#fee2e2;color:#dc2626;padding:4px 10px}
  #log{background:#0f172a;color:#4ade80;font-family:monospace;font-size:12px;padding:12px;
       border-radius:8px;height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
  .stats{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
  .stat{background:#f1f5f9;border-radius:8px;padding:6px 14px;font-size:13px}
  .group-card{border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:12px;position:relative}
  .group-title{font-weight:700;color:#0369a1;margin-bottom:10px}
</style>
</head>
<body>
<h1>🤖 飞书多维表格自动监控</h1>
<div class="card">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
    <span id="badge" class="badge" style="background:#f1f5f9;color:#64748b">⚪ 未运行</span>
    <button class="btn-start" onclick="startAll()">▶ 启动监控</button>
    <button class="btn-stop" onclick="stopAll()">⏹ 停止监控</button>
    <button class="btn-add" onclick="addGroup()">➕ 添加监控组</button>
  </div>
  <div class="stats">
    <div class="stat">🎨 生图: <b id="s2">0</b></div>
    <div class="stat">🎬 生视频: <b id="s3">0</b></div>
    <div class="stat">📝 加字幕: <b id="s4">0</b></div>
    <div class="stat">❌ 错误: <b id="se">0</b></div>
  </div>
  <div id="groups"></div>
</div>
<div class="card">
  <h3 style="margin:0 0 8px;color:#0369a1">📋 实时日志</h3>
  <div id="log">等待启动…</div>
</div>
<script>
let _groups = [], _poll = null;

function saveGroups() {
  fetch('/api/save-groups', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:_groups})});
}
function val(id) { return (document.getElementById(id)||{}).value || ''; }
function readGroups() {
  return _groups.map((_,i) => ({
    name:       val('g_name_'+i),
    appToken:   val('g_token_'+i),
    cozeToken:  val('g_coze_'+i),
    wf2:        val('g_wf2_'+i),
    wf3:        val('g_wf3_'+i),
    wf4:        val('g_wf4_'+i),
  }));
}
function renderGroups() {
  const el = document.getElementById('groups');
  el.innerHTML = _groups.map((g,i) => `
    <div class="group-card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div class="group-title">监控组 ${i+1}</div>
        <button class="btn-del" onclick="delGroup(${i})">✕ 删除</button>
      </div>
      <div class="row">
        <div><label>别名</label><input id="g_name_${i}" value="${g.name||''}" placeholder="随便填，如表格1"></div>
        <div><label>Base Token</label><input id="g_token_${i}" value="${g.appToken||''}" placeholder="NMwjb..."></div>
      </div>
      <div class="row">
        <div><label>Coze Token</label><input id="g_coze_${i}" value="${g.cozeToken||''}" placeholder="sat_..."></div>
        <div><label>生图 Workflow ID</label><input id="g_wf2_${i}" value="${g.wf2||''}"></div>
      </div>
      <div class="row">
        <div><label>生视频 Workflow ID</label><input id="g_wf3_${i}" value="${g.wf3||''}"></div>
        <div><label>加字幕 Workflow ID</label><input id="g_wf4_${i}" value="${g.wf4||''}"></div>
      </div>
    </div>`).join('');
}
function addGroup() { _groups.push({}); renderGroups(); }
function delGroup(i) { _groups.splice(i,1); renderGroups(); }
function startAll() {
  const groups = readGroups().filter(g => g.appToken);
  if (!groups.length) { alert('请至少填写一个 Base Token'); return; }
  saveGroups();
  fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups})})
    .then(r=>r.json()).then(d=>{ if(d.ok) startPoll(); else alert(d.msg); });
}
function stopAll() {
  fetch('/api/stop', {method:'POST'}).then(()=>{ setBadge(false); if(_poll){clearInterval(_poll);_poll=null;} });
}
function setBadge(running) {
  const b = document.getElementById('badge');
  b.textContent = running ? '🟢 运行中' : '⚪ 未运行';
  b.style.background = running ? '#dcfce7' : '#f1f5f9';
  b.style.color      = running ? '#15803d' : '#64748b';
}
function startPoll() {
  if (_poll) clearInterval(_poll);
  _poll = setInterval(() => {
    fetch('/api/status').then(r=>r.json()).then(d=>{
      setBadge(d.running);
      if (!d.running) { clearInterval(_poll); _poll=null; }
      const s = d.stats||{};
      document.getElementById('s2').textContent = s.stage2||0;
      document.getElementById('s3').textContent = s.stage3||0;
      document.getElementById('s4').textContent = s.stage4||0;
      document.getElementById('se').textContent = s.errors||0;
      const logs = d.logs||[];
      if (logs.length) { const el=document.getElementById('log'); el.textContent=logs.join('\\n'); el.scrollTop=el.scrollHeight; }
    });
  }, 2000);
}
window.addEventListener('DOMContentLoaded', () => {
  fetch('/api/load-groups').then(r=>r.json()).then(d=>{
    _groups = d.groups && d.groups.length ? d.groups : [{}];
    renderGroups();
  }).catch(()=>{ _groups=[{}]; renderGroups(); });
  fetch('/api/status').then(r=>r.json()).then(d=>{ if(d.running){setBadge(true);startPoll();} });
});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    import webbrowser, threading as _t, time as _time
    port = 5679
    print(f"🤖 飞书监控启动中… http://127.0.0.1:{port}")
    _t.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
