/* ============================================================================
   TNAS 应用统一前端运行时 —— 纯原生 JS，零第三方依赖
   ----------------------------------------------------------------------------
   职责：
     1. API 客户端：自动判定平台前缀、携带平台鉴权头、401 跳登录
     2. 统一组件：Toast / 二次确认弹窗 / 空状态 / 任务条 / 目录选择器
     3. 可视化：Squarified Treemap、横向条（自绘，不引 ECharts）

   为什么要自动判定前缀（指引 8.9）：平台可能以 `/<appid>/` 或
   `/v2/proxy/<appid>/` 两种形态把请求转到后端，前端若写死前缀，换形态就 404。
   这里统一解析出 base，其余一律走相对路径。
   ========================================================================== */

/* --------------------------------------------------------------------------
   i18n 兜底。
   app.js 里到处在调 T()，而 T 是 i18n.js 提供的 —— 万一某个页面漏挂
   <script src="./i18n.js">（或升级期间读到旧缓存），裸调会 ReferenceError，
   整个应用白屏。这里兜底成「原样返回中文」：界面退化成纯中文，但能用。
   ⚠️ 必须在任何使用 T 的代码之前执行。
   -------------------------------------------------------------------------- */
if (typeof window.T !== 'function') {
  window.T = function (key) { return key; };
}
if (!window.I18N || typeof window.I18N.t !== 'function') {
  window.I18N = {
    LANGS: ['zh-cn'],
    DEFAULT_LANG: 'zh-cn',
    t: window.T,
    lang: function () { return 'zh-cn'; },
    setLang: function () { return 'zh-cn'; },
    applyStatic: function () {},
    canonical: function () { return 'zh-cn'; },
    nativeName: function (code) { return code; },
  };
}

const API = (() => {
  const APP_ID = (document.documentElement.dataset.appId || '').trim();

  // API 基址判定 —— 2026-09-23 在 TOS 7 真机上实测后的结论：
  //
  //   平台把静态页放在 /<appid>/ 下**由自己直接提供**（不经过后端），
  //   只把 /v2/proxy/<appid>/... 反代到应用的 Unix socket。
  //   实测：GET /<appid>/api/app → 404，而 GET /v2/proxy/<appid>/api/app → 200。
  //
  //   所以页面虽然从 /<appid>/ 加载，**请求必须打到 /v2/proxy/<appid>/**。
  //   本地开发（后端自己 serve，路径是 /）时基址为空，走相对路径即可。
  function detectBase() {
    const path = window.location.pathname;
    let m = path.match(/^\/v2\/proxy\/([^/]+)/);
    if (m) return m[0];
    if (APP_ID) {
      const exact = '/' + APP_ID;
      if (path === exact || path.startsWith(exact + '/')) {
        return '/v2/proxy/' + APP_ID;
      }
    }
    m = path.match(/^\/([A-Za-z0-9][A-Za-z0-9._-]*)\//);
    if (m) return '/v2/proxy/' + m[1];
    return '';
  }

  const BASE = detectBase();

  function getCookie(name) {
    const prefix = encodeURIComponent(name) + '=';
    const hit = document.cookie.split(';')
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix));
    return hit ? decodeURIComponent(hit.slice(prefix.length)) : '';
  }

  // 指引 8.10：请求必须带 X-Csrf-Token 与自定义 Cookie 头
  function authHeaders() {
    const session = getCookie('TMSESSNAME');
    const csrf = getCookie('X-Csrf-Token');
    const headers = { 'Content-Type': 'application/json' };
    // 界面语言：前端已经解析好（用户选择 → navigator.language → zh-cn），
    // 直接告诉后端，省得它再读一遍设置、也解决了任务线程拿不到请求上下文的问题。
    if (window.I18N && typeof I18N.lang === 'function') headers['Accept-Language'] = I18N.lang();
    if (csrf) headers['X-Csrf-Token'] = csrf;
    if (session || csrf) {
      // 浏览器会丢弃这个自定义 Cookie 头（它是 fetch 的 forbidden header），
      // 但同源请求下 credentials:'include' 会自动带上真 cookie，
      // 而且 X-Csrf-Token 头能正常送达——后端两者都认。
      headers['Cookie'] = `TMSESSNAME=${session}; X-Csrf-Token=${csrf};`;
    }
    return headers;
  }

  function url(path) {
    const clean = String(path || '').replace(/^\/+/, '');
    return (BASE ? BASE + '/' : '/') + clean;
  }

  async function request(method, path, body, options) {
    const opts = options || {};
    const init = {
      method,
      headers: authHeaders(),
      credentials: 'include',
    };
    if (body !== undefined && body !== null) init.body = JSON.stringify(body);
    const response = await fetch(url(path), init);
    if (response.status === 401) {
      // 指引 8.10：不要试图刷新令牌，回首页触发 TOS 重新认证
      window.location.href = '/';
      throw new Error('会话已失效，正在返回登录页');
    }
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
    if (!response.ok) {
      const message = (payload && (payload.error || payload.message)) || `HTTP ${response.status}`;
      const error = new Error(message);
      error.status = response.status;
      error.hint = payload && payload.hint;
      // 结构化错误码。**判断分支一律读它，不要拿 message 里的中文措辞做正则匹配** ——
      // 那样在非中文界面下会静默失效（后端返回的 message 已经是本地化后的文本）。
      error.code = (payload && payload.code) || '';
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  return {
    base: BASE,
    appId: APP_ID,
    url,
    get: (p, o) => request('GET', p, null, o),
    post: (p, b, o) => request('POST', p, b, o),
    put: (p, b, o) => request('PUT', p, b, o),
    del: (p, o) => request('DELETE', p, null, o),
    downloadUrl: (p) => url(p),
    authHeaders,
  };
})();

/* ------------------------------------------------------------------ 工具 */

/** U.el 里哪些属性值该过一遍词表 —— 只有面向用户的提示类属性 */
const I18N_ATTRS = { title: 1, placeholder: 1, alt: 1 };

const U = {
  esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },

  size(bytes, digits) {
    const n = Number(bytes);
    if (!isFinite(n) || n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let value = n, index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    const dec = digits == null ? (value < 10 && index > 0 ? 1 : 0) : digits;
    return value.toFixed(dec) + ' ' + units[index];
  },

  num(value) {
    const n = Number(value) || 0;
    return n.toLocaleString('en-US');
  },

  pct(value) {
    return (Number(value) || 0).toFixed(1) + '%';
  },

  time(ts) {
    if (!ts) return '—';
    const date = typeof ts === 'string' ? new Date(ts) : new Date(Number(ts) * 1000);
    if (isNaN(date.getTime())) return String(ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },

  date(ts) {
    if (!ts) return '—';
    const date = new Date(Number(ts) * 1000);
    if (isNaN(date.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  },

  duration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return T('{n} 秒', { n: total });
    const m = Math.floor(total / 60), s = total % 60;
    if (m < 60) return T('{m} 分 {s} 秒', { m: m, s: s });
    const h = Math.floor(m / 60);
    return T('{h} 小时 {m} 分', { h: h, m: m % 60 });
  },

  ms(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    if (total < 1) return (total * 1000).toFixed(0) + ' ms';
    if (total < 60) return total.toFixed(1) + ' s';
    return this.duration(total);
  },

  debounce(fn, wait) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait || 250);
    };
  },

  /**
   * 建元素。**这里是界面多语言的第一个收口点**：text / html / 裸字符串子节点 /
   * title / placeholder / alt 都过一遍 T()。词表以中文原文为 key，查不到原样返回，
   * 所以文件路径、文件名、数字这类动态数据穿过 T() 不会被动到。
   */
  el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'html') node.innerHTML = T(value);
        else if (key === 'text') node.textContent = T(value);
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else node.setAttribute(key, I18N_ATTRS[key] ? T(value) : (value === true ? '' : value));
      }
    }
    for (const child of [].concat(children || [])) {
      if (child == null) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(T(child)) : child);
    }
    return node;
  },

  $(selector, root) { return (root || document).querySelector(selector); },
  $$(selector, root) { return Array.from((root || document).querySelectorAll(selector)); },

  byId(id) { return document.getElementById(id); },

  /** 颜色：按 index 稳定取色（用于 Treemap / 分类着色） */
  color(index, lightness) {
    const hue = (index * 137.508) % 360;
    const l = lightness == null ? 46 : lightness;
    return `hsl(${hue.toFixed(0)} 52% ${l}%)`;
  },

  colorFor(key) {
    let hash = 0;
    const text = String(key || '');
    for (let i = 0; i < text.length; i += 1) {
      hash = (hash * 31 + text.charCodeAt(i)) % 100000;
    }
    return this.color(hash % 360, 44);
  },
};

/* ------------------------------------------------------------------ 通知 */

const UI = {
  toast(message, kind, detail) {
    let host = U.byId('toasts');
    if (!host) {
      host = U.el('div', { id: 'toasts', class: 'toasts' });
      document.body.appendChild(host);
    }
    const node = U.el('div', { class: 'toast ' + (kind || '') }, [
      U.el('div', { class: 'tt', text: message }),
      detail ? U.el('div', { class: 'td', text: detail }) : null,
    ]);
    host.appendChild(node);
    const ttl = kind === 'error' ? 8000 : 3800;
    setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 220); }, ttl);
    return node;
  },

  ok(message, detail) { return this.toast(message, 'ok', detail); },
  warn(message, detail) { return this.toast(message, 'warn', detail); },
  err(error, fallback) {
    const message = (error && error.message) || fallback || '操作失败';
    const hint = error && error.hint;
    return this.toast(message, 'error', hint);
  },

  /** 统一横幅（设计文档 §52：错误要给可读原因与下一步） */
  banner(kind, title, bodyHtml) {
    const iconName = kind === 'error' ? 'alert' : kind === 'warn' ? 'alert' : kind === 'ok' ? 'check' : 'info';
    return U.el('div', { class: 'banner ' + kind }, [
      U.el('span', { html: Icons.svg(iconName, { size: 16 }) }),
      U.el('div', {}, [
        U.el('div', { class: 'bt', text: title }),
        bodyHtml ? U.el('div', { class: 'bd', html: bodyHtml }) : null,
      ]),
    ]);
  },

  /**
   * 二次确认。设计文档 §8「删除/移动等破坏性操作必须二次确认」——
   * 所有破坏性动作都必须过这个函数。
   */
  confirm(options) {
    const opts = Object.assign({
      title: '请确认',
      body: '',
      confirmText: '确认',
      cancelText: '取消',
      danger: false,
      requireText: null,
    }, options || {});

    return new Promise((resolve) => {
      const backdrop = U.el('div', { class: 'modal-backdrop' });
      const bodyParts = [U.el('div', { class: 'prewrap', text: opts.body })];
      let input = null;
      // requireText 是「让用户手输的确认词」。它必须跟着界面语言走：提示里显示译词、
      // 校验也比对译词，否则英文界面下会要求用户手打一个中文词才能继续。
      const needText = opts.requireText ? T(opts.requireText) : '';
      if (opts.requireText) {
        input = U.el('input', {
          type: 'text',
          placeholder: needText,
          style: 'margin-top:10px',
        });
        bodyParts.push(U.el('div', { class: 'small muted', text: T('请输入 {x} 以确认', { x: needText }) }));
        bodyParts.push(input);
      }
      const confirmBtn = U.el('button', {
        class: 'btn ' + (opts.danger ? 'danger' : 'primary'),
        text: opts.confirmText,
        disabled: !!opts.requireText,
      });
      const close = (value) => { backdrop.remove(); resolve(value); };

      confirmBtn.addEventListener('click', () => close(true));
      U.el(backdrop, {}, [
        U.el('div', { class: 'modal' }, [
          U.el('header', {}, [
            U.el('span', { html: Icons.svg(opts.danger ? 'alert' : 'info', { size: 17 }) }),
            U.el('span', { text: opts.title }),
          ]),
          U.el('div', { class: 'body' }, bodyParts),
          U.el('footer', {}, [
            U.el('button', { class: 'btn', text: opts.cancelText, onclick: () => close(false) }),
            confirmBtn,
          ]),
        ]),
      ]);
      if (input) {
        input.addEventListener('input', () => {
          confirmBtn.disabled = input.value.trim() !== needText;
        });
        setTimeout(() => input.focus(), 30);
      }
      backdrop.addEventListener('click', (event) => {
        if (event.target === backdrop) close(false);
      });
      document.addEventListener('keydown', function onKey(event) {
        if (event.key === 'Escape') { document.removeEventListener('keydown', onKey); close(false); }
      });
      document.body.appendChild(backdrop);
    });
  },

  /** 通用弹窗；`buttons` 是 [{text, kind, onClick(close)}] */
  modal(options) {
    const opts = Object.assign({ title: '', bodyHtml: '', buttons: [], wide: false }, options || {});
    const backdrop = U.el('div', { class: 'modal-backdrop' });
    const close = () => backdrop.remove();
    const footer = U.el('footer', {});
    opts.buttons.forEach((spec) => {
      footer.appendChild(U.el('button', {
        class: 'btn ' + (spec.kind || ''),
        text: spec.text,
        onclick: () => (spec.onClick ? spec.onClick(close) : close()),
      }));
    });
    const modal = U.el('div', { class: 'modal', style: opts.wide ? 'width:min(860px,100%)' : null }, [
      U.el('header', {}, [
        U.el('span', { html: Icons.svg(opts.icon || 'info', { size: 17 }) }),
        U.el('span', { text: opts.title }),
      ]),
      U.el('div', { class: 'body', html: opts.bodyHtml }),
      opts.buttons.length ? footer : null,
    ]);
    backdrop.appendChild(modal);
    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
    document.body.appendChild(backdrop);
    return { close, node: modal, body: modal.querySelector('.body') };
  },

  empty(iconName, title, description, actionNode) {
    return U.el('div', { class: 'empty' }, [
      U.el('span', { html: Icons.svg(iconName || 'inbox', { size: 40 }) }),
      U.el('div', { class: 'et', text: title }),
      description ? U.el('div', { class: 'ed', html: description }) : null,
      actionNode || null,
    ]);
  },

  progress(value, indeterminate) {
    const bar = U.el('div', { class: 'progress' + (indeterminate ? ' indeterminate' : '') },
      [U.el('i', { style: `width:${Math.max(0, Math.min(100, Number(value) || 0))}%` })]);
    return bar;
  },

  badge(text, kind) {
    return U.el('span', { class: 'badge ' + (kind || 'neutral'), text: text });
  },

  /**
   * 语言选择器（放在各应用「设置」页）。语言自称用它自己那套写法，不翻译。
   * 选中后：落 localStorage → 套用静态文案 → 广播 tnas-langchange（应用据此重渲染）
   * → 尽力同步到后端 settings.ui_language（存不上也不影响本次会话）。
   */
  langSelect(options) {
    const opts = Object.assign({ persist: true, onChange: null }, options || {});
    const select = U.el('select', {}, I18N.LANGS.map((code) =>
      U.el('option', { value: code, text: I18N.nativeName(code) })));
    select.value = I18N.lang();
    select.addEventListener('change', async () => {
      const code = I18N.setLang(select.value);
      if (opts.persist) {
        try { await API.post('api/settings', { ui_language: code }); } catch (error) { /* 忽略 */ }
      }
      if (opts.onChange) opts.onChange(code);
    });
    return select;
  },

  /** 目录/文件选择器：走 /api/fs 系列接口 */
  pickDir(options) {
    const opts = Object.assign({ start: '', onPick: null, title: '选择目录', pickFile: false }, options || {});
    const listNode = U.el('div', { class: 'list' });
    const crumbNode = U.el('div', { class: 'crumb' });
    const chosen = { path: opts.start || '' };

    const dialog = this.modal({
      title: opts.title,
      icon: 'folderOpen',
      wide: true,
      bodyHtml: '',
      buttons: [
        { text: '取消' },
        {
          text: '选定此目录',
          kind: 'primary',
          onClick: (close) => {
            if (!chosen.path) { UI.warn('请先进入一个目录'); return; }
            if (opts.onPick) opts.onPick(chosen.path);
            close();
          },
        },
      ],
    });
    dialog.body.innerHTML = '';
    dialog.body.appendChild(crumbNode);
    dialog.body.appendChild(U.el('div', { class: 'picker' }, [listNode]));
    dialog.body.appendChild(U.el('div', { class: 'small muted mt1' },
      [U.el('span', { id: 'picker-chosen' })]));

    async function load(path) {
      listNode.innerHTML = '';
      listNode.appendChild(U.el('div', { class: 'empty' }, [U.el('div', { class: 'ed', text: '加载中…' })]));
      let data;
      try {
        data = await API.get('api/fs/list?path=' + encodeURIComponent(path || ''));
      } catch (error) {
        listNode.innerHTML = '';
        listNode.appendChild(UI.empty('alert', '无法读取此目录', U.esc(error.message)));
        return;
      }
      chosen.path = data.path || path || '';
      const label = U.byId('picker-chosen');
      if (label) label.textContent = T('当前目录：') + (chosen.path || T('(根)'));

      crumbNode.innerHTML = '';
      const rootsBtn = U.el('button', { class: 'btn sm ghost', text: '起始位置' });
      rootsBtn.addEventListener('click', () => load(''));
      crumbNode.appendChild(rootsBtn);

      listNode.innerHTML = '';
      const entries = (data.entries || []).filter((e) => (opts.pickFile ? true : e.is_dir));
      if (!entries.length) {
        listNode.appendChild(UI.empty('folder', '这里是空的', '没有可选的子目录'));
      }
      entries.forEach((entry) => {
        const button = U.el('button', { class: 'entry' }, [
          U.el('span', { html: Icons.svg(entry.is_dir ? 'folder' : (entry.icon || 'fileText'), { size: 15 }) }),
          U.el('span', { text: entry.name }),
          entry.size != null ? U.el('span', { class: 'sz', text: entry.is_dir ? '' : U.size(entry.size) }) : null,
        ]);
        button.addEventListener('click', () => {
          if (entry.is_dir) load(entry.path);
          else if (opts.onPick) { opts.onPick(entry.path); dialog.close(); }
        });
        listNode.appendChild(button);
      });
      if (data.parent) {
        const up = U.el('button', { class: 'entry' }, [
          U.el('span', { html: Icons.svg('arrowLeft', { size: 15 }) }),
          U.el('span', { text: '上一级' }),
        ]);
        up.addEventListener('click', () => load(data.parent));
        listNode.insertBefore(up, listNode.firstChild);
      }
    }

    load(opts.start || '');
    return dialog;
  },
};

/* ------------------------------------------------------------------ 任务条 */

const Jobs = {
  timer: null,
  counts: {},

  /** 底部任务栏。每个应用在 index.html 放一个 `<div id="taskbar" class="taskbar">`。 */
  mountTaskbar(node) {
    if (!node) return;
    node.innerHTML = '';
    const spec = [
      ['running', '运行中'],
      ['queued', '排队'],
      ['paused', '已暂停'],
      ['completed', '已完成'],
      ['failed', '失败'],
    ];
    const stats = {};
    spec.forEach(([key, label]) => {
      const stat = U.el('span', { class: 'stat' }, [
        U.el('span', { class: 'dot ' + key }),
        U.el('span', { text: label }),
        U.el('b', { text: '0' }),
      ]);
      stats[key] = stat.querySelector('b');
      stat.addEventListener('click', () => { window.location.hash = '#jobs'; });
      node.appendChild(stat);
    });
    node.appendChild(U.el('span', { class: 'spacer' }));
    const refresh = U.el('button', { class: 'btn sm ghost', html: Icons.svg('refresh', { size: 13 }) + '<span>' + T('刷新') + '</span>' });
    refresh.addEventListener('click', () => Jobs.tick(true));
    node.appendChild(refresh);
    Jobs.stats = stats;
  },

  async tick(force) {
    try {
      const data = await API.get('api/jobs?limit=1');
      Jobs.counts = data.counts || {};
      const stats = Jobs.stats || {};
      Object.keys(stats).forEach((key) => {
        stats[key].textContent = String(Jobs.counts[key] || 0);
      });
      if (typeof Jobs.onTick === 'function') Jobs.onTick(Jobs.counts);
    } catch (error) {
      if (force) UI.err(error, '获取任务状态失败');
    }
  },

  start(intervalMs) {
    Jobs.stop();
    Jobs.tick();
    Jobs.timer = setInterval(() => Jobs.tick(), intervalMs || 2500);
  },

  stop() {
    if (Jobs.timer) { clearInterval(Jobs.timer); Jobs.timer = null; }
  },

  async submit(type, params, title) {
    // 标题在**提交前**翻译 —— 它会写进 jobs 表，之后前端读到的是历史数据，
    // 那时再翻就晚了（库里存的是中文，英文界面下会一直显示中文）。
    const data = await API.post('api/jobs', { type, params: params || {}, title: T(title || '') });
    UI.ok('任务已提交', '可在底部任务栏查看进度');
    Jobs.tick();
    return data.job;
  },

  async cancel(id) {
    await API.post(`api/jobs/${id}/cancel`);
    Jobs.tick();
  },

  async pause(id) { await API.post(`api/jobs/${id}/pause`); Jobs.tick(); },
  async resume(id) { await API.post(`api/jobs/${id}/resume`); Jobs.tick(); },

  async retry(id) {
    const data = await API.post(`api/jobs/${id}/retry`);
    Jobs.tick();
    return data.job;
  },

  async remove(id) {
    await API.del(`api/jobs/${id}`);
    Jobs.tick();
  },

  async logs(id, offset) {
    const data = await API.get(`api/jobs/${id}/logs?offset=${offset || 0}&limit=2000`);
    return data.logs || [];
  },

  stateBadge(state) {
    const map = {
      queued: ['排队中', 'neutral'],
      running: ['运行中', 'info'],
      paused: ['已暂停', 'warn'],
      canceling: ['取消中', 'warn'],
      canceled: ['已取消', 'neutral'],
      completed: ['已完成', 'ok'],
      failed: ['失败', 'danger'],
    };
    const [text, kind] = map[state] || [state, 'neutral'];
    return UI.badge(text, kind);
  },

  /** 任务列表表格；`host` 是容器元素。`filterType` 限定只显示某类任务。 */
  renderTable(host, jobs, options) {
    const opts = options || {};
    host.innerHTML = '';
    if (!jobs.length) {
      host.appendChild(UI.empty('list', '还没有任务', opts.emptyHint || '在上方提交一个任务试试'));
      return;
    }
    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: '#' }),
        U.el('th', { text: '任务' }),
        U.el('th', { text: '状态' }),
        U.el('th', { text: '进度' }),
        U.el('th', { text: '开始时间' }),
        U.el('th', { text: '操作' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    jobs.forEach((job) => {
      const row = U.el('tr', {}, [
        U.el('td', { class: 'mono', text: String(job.id) }),
        U.el('td', {}, [
          U.el('div', { text: job.title || job.type }),
          job.message ? U.el('div', { class: 'small faint', text: job.message }) : null,
        ]),
        U.el('td', {}, [Jobs.stateBadge(job.state)]),
        U.el('td', { style: 'min-width:130px' }, [
          UI.progress(job.progress, job.state === 'running' && !job.progress),
          U.el('div', { class: 'small faint', text: U.pct(job.progress) }),
        ]),
        U.el('td', { class: 'small nowrap', text: job.started_at_text || job.created_at_text || '—' }),
        U.el('td', {}, [Jobs.actions(job)]),
      ]);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  },

  actions(job) {
    const box = U.el('div', { class: 'btn-row' });
    const add = (text, iconName, kind, handler) => {
      const button = U.el('button', { class: 'btn sm ' + (kind || 'ghost'), title: text },
        [U.el('span', { html: Icons.svg(iconName, { size: 13 }) })]);
      button.addEventListener('click', async () => {
        try { await handler(); if (Jobs.reload) Jobs.reload(); }
        catch (error) { UI.err(error); }
      });
      box.appendChild(button);
    };
    if (job.is_active) {
      add('查看日志', 'terminal', 'ghost', async () => {
        const logs = await Jobs.logs(job.id);
        UI.modal({
          title: T('任务 #{id} 日志', { id: job.id }),
          icon: 'terminal',
          wide: true,
          bodyHtml: Jobs.logsHtml(logs),
        });
      });
      if (job.state === 'paused') add('继续', 'play', 'ghost', () => Jobs.resume(job.id));
      else add('暂停', 'pause', 'ghost', () => Jobs.pause(job.id));
      add('取消', 'stop', 'ghost', () => Jobs.cancel(job.id));
    } else {
      if (job.error) {
        add('错误', 'alert', 'ghost', () => {
          UI.modal({ title: '失败原因', icon: 'alert', bodyHtml: `<pre class="logview">${U.esc(job.error)}</pre>` });
        });
      }
      add('日志', 'terminal', 'ghost', async () => {
        const logs = await Jobs.logs(job.id);
        UI.modal({ title: T('任务 #{id} 日志', { id: job.id }), icon: 'terminal', wide: true, bodyHtml: Jobs.logsHtml(logs) });
      });
      add('重试', 'refresh', 'ghost', () => Jobs.retry(job.id));
      add('删除', 'trash', 'ghost', () => Jobs.remove(job.id));
    }
    return box;
  },

  logsHtml(logs) {
    if (!logs.length) return '<div class="empty"><div class="ed">' + T('暂无日志') + '</div></div>';
    return '<pre class="logview">' + logs.map((entry) =>
      `<span class="lv-${U.esc(entry.level)}">${U.esc(entry.time)} [${U.esc(entry.level)}]</span> ${U.esc(entry.message)}`
    ).join('\n') + '</pre>';
  },
};

/* --------------------------------------------------------------- Treemap */

const Treemap = {
  /**
   * Squarified treemap。`nodes` = [{name, size, key, color, meta}]。
   * 不引 ECharts —— 省一份第三方许可证，且几十行就够。
   */
  render(host, nodes, options) {
    const opts = Object.assign({ onClick: null, minLabelPx: 34 }, options || {});
    host.innerHTML = '';
    const box = U.el('div', { class: 'treemap' });
    host.appendChild(box);

    const total = nodes.reduce((sum, node) => sum + Math.max(0, Number(node.size) || 0), 0);
    if (!total) {
      host.innerHTML = '';
      host.appendChild(UI.empty('chart', '没有可展示的数据', '先扫描一个目录'));
      return;
    }

    const width = box.clientWidth || 800;
    const height = box.clientHeight || 460;
    const sorted = nodes.slice().sort((a, b) => (b.size || 0) - (a.size || 0));

    const rects = squarify(sorted, { x: 0, y: 0, w: width, h: height }, total);

    rects.forEach((rect, index) => {
      const node = rect.node;
      const cell = U.el('div', {
        class: 'cell',
        style: `left:${rect.x}px;top:${rect.y}px;width:${rect.w}px;height:${rect.h}px;` +
          `background:${node.color || U.colorFor(node.key || node.name)}`,
        title: `${node.name}\n${U.size(node.size)}（${U.pct((node.size / total) * 100)}）`,
      });
      if (rect.w >= opts.minLabelPx && rect.h >= 18) {
        cell.appendChild(U.el('span', { class: 'nm', text: node.name }));
        if (rect.h >= 34) cell.appendChild(U.el('span', { class: 'sz', text: U.size(node.size) }));
      }
      if (opts.onClick) cell.addEventListener('click', () => opts.onClick(node));
      box.appendChild(cell);
    });
  },
};

function squarify(items, bounds, total) {
  const out = [];
  const area = bounds.w * bounds.h;
  let remaining = items.map((node) => Object.assign({}, node, {
    _area: Math.max(0, (Number(node.size) || 0)) / total * area,
  }));
  let box = Object.assign({}, bounds);

  while (remaining.length) {
    box = layoutRow(remaining, box, out);
  }
  return out;
}

function layoutRow(items, box, out) {
  const shortSide = Math.min(box.w, box.h);
  if (shortSide <= 0) { items.length = 0; return box; }

  const row = [];
  let rowArea = 0;
  let best = Infinity;
  let index = 0;

  while (index < items.length) {
    const candidate = items[index];
    const nextArea = rowArea + candidate._area;
    const ratio = worstRatio(row.map((r) => r._area).concat([candidate._area]), nextArea, shortSide);
    if (row.length && ratio > best) break;
    row.push(candidate);
    rowArea = nextArea;
    best = ratio;
    index += 1;
  }

  const isWide = box.w >= box.h;
  const thickness = rowArea > 0 ? rowArea / shortSide : 0;

  let offset = 0;
  row.forEach((node) => {
    const length = rowArea > 0 ? (node._area / rowArea) * shortSide : 0;
    if (isWide) {
      out.push({ node, x: box.x, y: box.y + offset, w: thickness, h: length });
    } else {
      out.push({ node, x: box.x + offset, y: box.y, w: length, h: thickness });
    }
    offset += length;
  });

  items.splice(0, row.length);

  if (isWide) {
    return { x: box.x + thickness, y: box.y, w: Math.max(0, box.w - thickness), h: box.h };
  }
  return { x: box.x, y: box.y + thickness, w: box.w, h: Math.max(0, box.h - thickness) };
}

function worstRatio(areas, totalArea, shortSide) {
  if (!totalArea || !shortSide) return Infinity;
  const thickness = totalArea / shortSide;
  let worst = 0;
  areas.forEach((a) => {
    const length = totalArea > 0 ? (a / totalArea) * shortSide : 0;
    if (length <= 0) return;
    const ratio = Math.max(thickness / length, length / thickness);
    if (ratio > worst) worst = ratio;
  });
  return worst;
}

/* ------------------------------------------------------------- 横向条 */

const Bars = {
  render(host, rows, options) {
    const opts = Object.assign({ valueFormat: (v) => U.size(v), max: null }, options || {});
    host.innerHTML = '';
    const max = opts.max || rows.reduce((m, r) => Math.max(m, Number(r.value) || 0), 0) || 1;
    const wrap = U.el('div', { class: 'bars' });
    rows.forEach((row) => {
      const value = Number(row.value) || 0;
      wrap.appendChild(U.el('div', { class: 'bar-row' }, [
        U.el('div', { class: 'bl', title: row.label, text: row.label }),
        U.el('div', { class: 'bt' }, [
          U.el('i', { style: `width:${(value / max) * 100}%;background:${row.color || 'var(--app-accent)'}` }),
        ]),
        U.el('div', { class: 'bv', text: row.text || opts.valueFormat(value) }),
      ]));
    });
    if (!rows.length) {
      host.appendChild(UI.empty('chart', '暂无数据', ''));
    } else {
      host.appendChild(wrap);
    }
  },
};

/* ------------------------------------------------------------------ 外壳 */

const Shell = {
  /**
   * 绑定左侧导航与视图切换。
   * `views` = { key: {label, icon, render(container)} }
   */
  init(views, options) {
    const opts = Object.assign({ defaultView: null, onView: null }, options || {});
    const nav = U.byId('nav');
    const main = U.byId('main');
    if (!nav || !main) return null;

    const keys = Object.keys(views);
    let current = null;

    function show(key) {
      if (!views[key]) key = keys[0];
      current = key;
      U.$$('.nav-item', nav).forEach((node) => {
        node.classList.toggle('active', node.dataset.view === key);
      });
      main.innerHTML = '';
      views[key].render(main);
      if (window.location.hash !== '#' + key) {
        history.replaceState(null, '', '#' + key);
      }
      if (opts.onView) opts.onView(key, main);
    }

    nav.innerHTML = '';
    keys.forEach((key) => {
      const view = views[key];
      if (view.separator) {
        nav.appendChild(U.el('div', { class: 'nav-sep' }));
        return;
      }
      const item = U.el('button', { class: 'nav-item', dataset: { view: key } }, [
        U.el('span', { html: Icons.svg(view.icon || 'dots', { size: 16 }) }),
        U.el('span', { text: view.label }),
        view.countBadge ? U.el('span', { class: 'count', text: '0' }) : null,
      ]);
      item.addEventListener('click', () => show(key));
      nav.appendChild(item);
    });

    const initial = (window.location.hash || '').replace(/^#/, '');
    show(views[initial] ? initial : (opts.defaultView || keys[0]));
    window.addEventListener('hashchange', () => {
      const key = window.location.hash.replace(/^#/, '');
      if (views[key] && key !== current) show(key);
    });

    return { show, current: () => current };
  },

  /** 语言切换后重渲染当前视图。各应用在 Shell.init 之后调一次即可。 */
  bindLanguage(shell) {
    window.addEventListener('tnas-langchange', () => {
      if (shell && shell.current()) shell.show(shell.current());
    });
  },

  async loadAppInfo() {
    try {
      const info = await API.get('api/app');
      const nameNode = U.byId('app-version');
      if (nameNode) nameNode.textContent = 'v' + info.version;
      return info;
    } catch (error) {
      return null;
    }
  },
};
