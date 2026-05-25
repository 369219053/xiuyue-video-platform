// ==UserScript==
// @name         飞书多维表格视频下载器
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  一键下载飞书多维表格中的视频，自动命名为"副表名称_行号.mp4"
// @author       小牛马
// @match        https://my.feishu.cn/base/*
// @match        https://*.feishu.cn/base/*
// @match        https://*.larksuite.com/base/*
// @grant        GM_download
// @grant        GM_notification
// ==/UserScript==

(function () {
  'use strict';

  // ============================================================
  // 配置
  // ============================================================
  const VIDEO_FIELD_ID = 'fldksNMEZ4'; // "视频剪辑.输出结果1" 字段ID

  // ============================================================
  // 工具函数
  // ============================================================
  function getBaseToken() {
    const m = location.pathname.match(/\/base\/([^/?]+)/);
    return m ? m[1] : null;
  }

  function getTableId() {
    const m = location.search.match(/[?&]table=([^&]+)/);
    return m ? m[1] : null;
  }

  async function decodeGzip(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const ds = new DecompressionStream('gzip');
    const writer = ds.writable.getWriter();
    writer.write(bytes);
    writer.close();
    const reader = ds.readable.getReader();
    const chunks = [];
    while (true) {
      const r = await reader.read();
      if (r.done) break;
      chunks.push(r.value);
    }
    const total = chunks.reduce((a, b) => a + b.length, 0);
    const combined = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { combined.set(chunk, offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder().decode(combined));
  }

  async function fetchTableData(baseToken, tableId) {
    const url = `/space/api/v1/bitable/${baseToken}/clientvars?tableID=${tableId}&recordLimit=2000&ondemandLimit=2000&needBase=false&viewLazyLoad=false&ondemandVer=2&openType=0&noMissCS=true&optimizationFlag=1&removeFmlExtra=false`;
    const resp = await fetch(url, { credentials: 'include' });
    return resp.json();
  }

  async function fetchTableName(baseToken, tableId) {
    const url = `/space/api/v1/bitable/${baseToken}/clientvars?tableID=${tableId}&recordLimit=1&needBase=true&viewLazyLoad=false&ondemandVer=2&openType=0`;
    const resp = await fetch(url, { credentials: 'include' });
    const data = await resp.json();
    const baseJson = await decodeGzip(data.data.base);
    const blockInfos = baseJson.blockInfos || {};
    const info = blockInfos[tableId];
    return info ? (info.name || tableId) : tableId;
  }

  function extractVideosSorted(tableJson) {
    const recordMap = tableJson.recordMap || {};
    const rankMap = tableJson.rankInfo?.rankMap || {};
    const records = [];
    for (const [recId, rec] of Object.entries(recordMap)) {
      const val = rec[VIDEO_FIELD_ID]?.value;
      const url = Array.isArray(val) ? val[0]?.text : '';
      if (url && url.startsWith('http')) {
        records.push({ rank: rankMap[recId] || 'z999', url });
      }
    }
    return records.sort((a, b) => a.rank < b.rank ? -1 : 1);
  }

  function safeName(name) {
    return name.replace(/[<>:"/\\|?*]/g, '_');
  }

  // ============================================================
  // 下载逻辑
  // ============================================================
  async function downloadAll(btn) {
    const baseToken = getBaseToken();
    const tableId = getTableId();
    if (!baseToken || !tableId) {
      alert('❌ 无法识别飞书 Base 信息，请确认当前页面是多维表格');
      return;
    }

    btn.textContent = '⏳ 获取副表信息...';
    btn.disabled = true;

    try {
      const tableName = await fetchTableName(baseToken, tableId);
      btn.textContent = `⏳ 读取数据: ${tableName}`;

      const rawData = await fetchTableData(baseToken, tableId);
      if (rawData.code !== 0) throw new Error(`API 错误: ${JSON.stringify(rawData)}`);

      const tableJson = await decodeGzip(rawData.data.table);
      const records = extractVideosSorted(tableJson);

      if (records.length === 0) {
        alert('⚠️ 当前副表没有找到视频URL，请确认字段ID是否正确');
        return;
      }

      const prefix = safeName(tableName);
      btn.textContent = `⬇️ 下载中 0/${records.length}`;

      for (let i = 0; i < records.length; i++) {
        const filename = `${prefix}_${i + 1}.mp4`;
        GM_download({ url: records[i].url, name: filename });
        btn.textContent = `⬇️ 下载中 ${i + 1}/${records.length}`;
        // 每次下载间隔300ms，避免触发浏览器拦截
        await new Promise(r => setTimeout(r, 300));
      }

      btn.textContent = `✅ 完成！共 ${records.length} 个`;
      GM_notification({ title: '下载完成', text: `${tableName} 共 ${records.length} 个视频已触发下载`, timeout: 4000 });
    } catch (e) {
      console.error(e);
      alert(`❌ 出错了：${e.message}`);
    } finally {
      setTimeout(() => {
        btn.textContent = '⬇️ 下载本表视频';
        btn.disabled = false;
      }, 5000);
    }
  }

  // ============================================================
  // 注入浮动按钮
  // ============================================================
  function injectButton() {
    if (document.getElementById('fs-video-dl-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'fs-video-dl-btn';
    btn.textContent = '⬇️ 下载本表视频';
    Object.assign(btn.style, {
      position: 'fixed', bottom: '80px', right: '24px', zIndex: '99999',
      padding: '10px 18px', borderRadius: '8px', border: 'none',
      background: '#1664FF', color: '#fff', fontSize: '14px',
      fontWeight: 'bold', cursor: 'pointer', boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
      transition: 'opacity 0.2s'
    });
    btn.onmouseenter = () => btn.style.opacity = '0.85';
    btn.onmouseleave = () => btn.style.opacity = '1';
    btn.onclick = () => downloadAll(btn);

    document.body.appendChild(btn);
  }

  // 等页面加载完再注入（飞书是SPA，需要轮询）
  const timer = setInterval(() => {
    if (document.body) {
      injectButton();
      clearInterval(timer);
    }
  }, 1000);

  // 监听 URL 变化（切换副表时按钮保持存在）
  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      setTimeout(injectButton, 1500);
    }
  }).observe(document, { subtree: true, childList: true });

})();
