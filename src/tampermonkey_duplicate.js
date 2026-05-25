// ==UserScript==
// @name         飞书多维表格 - 自动复制副表
// @namespace    http://tampermonkey.net/
// @version      1.1
// @description  自动复制第一个副表并重命名，任务由本地秀悦工具提供（127.0.0.1:5678）
// @author       秀悦
// @match        https://my.feishu.cn/base/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:5678';
  let isRunning = false;

  // ─── 状态指示器 ───────────────────────────────────────────
  function createIndicator() {
    const el = document.createElement('div');
    el.id = 'xiu-auto-copy';
    el.style.cssText = [
      'position:fixed', 'bottom:20px', 'right:20px',
      'background:#4f46e5', 'color:#fff',
      'padding:8px 14px', 'border-radius:8px',
      'font-size:12px', 'z-index:99999',
      'box-shadow:0 2px 8px rgba(0,0,0,.25)',
      'pointer-events:none', 'transition:background .3s'
    ].join(';');
    el.textContent = '🤖 自动复制：待机中';
    document.body.appendChild(el);
    return el;
  }

  function setStatus(msg, color) {
    const el = document.getElementById('xiu-auto-copy');
    if (!el) return;
    el.textContent = '🤖 ' + msg;
    el.style.background = color || '#4f46e5';
  }

  // ─── 轮询任务 ─────────────────────────────────────────────
  function pollTask() {
    if (isRunning) return;
    GM_xmlhttpRequest({
      method: 'GET',
      url: API_BASE + '/api/get-task',
      onload: function (res) {
        try {
          const data = JSON.parse(res.responseText);
          if (data.ok && data.task) {
            isRunning = true;
            setStatus('执行中：' + data.task.name, '#16a34a');
            executeTask(data.task);
          } else {
            setStatus('待机中', '#4f46e5');
          }
        } catch (e) { /* 本地服务未启动时静默忽略 */ }
      },
      onerror: function () { /* 连接失败静默忽略 */ }
    });
  }

  // ─── 主流程 ───────────────────────────────────────────────
  function executeTask(task) {
    const firstTab = document.querySelector('.bitable-new-table-item');
    if (!firstTab) {
      setStatus('❌ 找不到副表标签', '#dc2626');
      isRunning = false;
      return;
    }

    // 1. 先设置 MutationObserver 监听菜单出现（必须在右键之前设置）
    let menuTimer;
    const menuObserver = new MutationObserver(function (mutations) {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          const items = node.querySelectorAll('li.b-menu__item');
          for (const li of items) {
            if ((li.textContent || '').trim() === '复制数据表') {
              clearTimeout(menuTimer);
              menuObserver.disconnect();
              setTimeout(() => {
                li.click();
                watchForDialog(task);
              }, 100);
              return;
            }
          }
        }
      }
    });
    menuObserver.observe(document.body, { childList: true });

    // 2. 计算元素的屏幕坐标（视口坐标 + 浏览器窗口位置）
    const rect = firstTab.getBoundingClientRect();
    const chromeHeight = window.outerHeight - window.innerHeight;
    const screenX = Math.round(window.screenX + rect.left + rect.width / 2);
    const screenY = Math.round(window.screenY + chromeHeight + rect.top + rect.height / 2);

    // 3. 让 Flask/PyAutoGUI 执行真实系统级右键点击（isTrusted = true）
    GM_xmlhttpRequest({
      method: 'POST',
      url: API_BASE + '/api/right-click',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ x: screenX, y: screenY }),
      onload: function (res) {
        try {
          const data = JSON.parse(res.responseText);
          if (!data.ok) {
            console.warn('[秀悦] 右键点击失败:', data.msg);
            setStatus('❌ 右键失败：' + data.msg, '#dc2626');
            menuObserver.disconnect();
            clearTimeout(menuTimer);
            isRunning = false;
          }
        } catch (e) {}
      },
      onerror: function () {
        console.warn('[秀悦] 无法连接本地服务，请确认软件已启动');
        menuObserver.disconnect();
        clearTimeout(menuTimer);
        setStatus('❌ 无法连接本地软件', '#dc2626');
        isRunning = false;
      }
    });

    // 4. 超时保护（5秒内菜单未出现则放弃）
    menuTimer = setTimeout(() => {
      menuObserver.disconnect();
      setStatus('❌ 菜单未出现，超时', '#dc2626');
      isRunning = false;
    }, 5000);
  }

  // ─── 等待对话框 ───────────────────────────────────────────
  function watchForDialog(task) {
    let dialogTimer;
    const dialogObserver = new MutationObserver(function (mutations) {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          // 优先在新增节点里找 input；Feishu 用 ud__portal 做容器
          const input = node.querySelector
            ? node.querySelector('input[placeholder="请输入数据表名称"]')
            : null;
          if (input) {
            clearTimeout(dialogTimer);
            dialogObserver.disconnect();
            setTimeout(() => fillDialog(input, node, task), 300);
            return;
          }
        }
      }
    });
    dialogObserver.observe(document.body, { childList: true, subtree: true });

    dialogTimer = setTimeout(() => {
      dialogObserver.disconnect();
      setStatus('❌ 对话框未出现，超时', '#dc2626');
      isRunning = false;
    }, 8000);
  }

  // ─── 填写并确认 ───────────────────────────────────────────
  function fillDialog(input, portal, task) {
    // 用 React/Vue 原生 setter 触发受控输入框更新
    const nativeSetter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, 'value'
    ).set;
    nativeSetter.call(input, task.name);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));

    setTimeout(() => {
      // 找"复制"确认按钮
      const buttons = portal.querySelectorAll
        ? portal.querySelectorAll('button')
        : document.querySelectorAll('.ud__portal button');
      let confirmBtn = null;
      for (const btn of buttons) {
        if ((btn.textContent || '').trim() === '复制') {
          confirmBtn = btn;
          break;
        }
      }
      if (!confirmBtn) {
        setStatus('❌ 找不到确认按钮', '#dc2626');
        isRunning = false;
        return;
      }
      confirmBtn.click();
      setStatus('✅ 完成：' + task.name, '#16a34a');
      reportDone(task.id);
    }, 500);
  }

  // ─── 回报完成 ─────────────────────────────────────────────
  function reportDone(taskId) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: API_BASE + '/api/complete-task',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ id: taskId }),
      onload: function () {
        setTimeout(() => { isRunning = false; }, 2000);
      },
      onerror: function () { isRunning = false; }
    });
  }

  // ─── 启动 ─────────────────────────────────────────────────
  window.addEventListener('load', function () {
    createIndicator();
    setInterval(pollTask, 2000);
  });

})();
