/* ============================================================================
   MCP Hub —— 前端逻辑
   ----------------------------------------------------------------------------
   界面的核心不是「展示工具清单」，而是让管理员**清楚地看到并控制**
   「AI 现在被允许做什么」：权限级别、目录白名单、以及每一次调用的审计。
   ========================================================================== */

(function () {
  'use strict';

  const State = {
    status: null,
    tools: { groups: {}, count: 0, level: 'READ_ONLY' },
    allowedRoots: [],
    audit: [],
    recycle: [],
    config: null,
  };

  const LEVEL_TEXT = {
    READ_ONLY: T('只读。AI 只能看，不能改。适合绝大多数场景。'),
    READ_WRITE: T('可写。允许 AI 生成新文件、移动文件（都可逆）。'),
    DANGEROUS: T('危险。允许把文件移入回收站等破坏性操作。请谨慎开启。'),
  };

  /* ------------------------------------------------------------ 概览 */

  async function renderOverview(host) {
    host.innerHTML = '';
    host.appendChild(UI.banner('info', T('正在加载…'), ''));

    let status;
    try {
      status = await API.get('api/mcp/status');
    } catch (error) {
      host.innerHTML = '';
      host.appendChild(UI.banner('error', T('无法读取状态'),
        U.esc(error.message) + T('<ul><li>服务可能正在重启，稍后重试</li>') +
        T('<li>或查看日志：journalctl -u shh13-mcp-hub</li></ul>')));
      return;
    }
    State.status = status;
    State.allowedRoots = status.allowed_roots || [];
    host.innerHTML = '';

    if (!State.allowedRoots.length) {
      const action = U.el('button', { class: 'btn primary', text: T('去添加可访问目录') });
      action.addEventListener('click', () => Shell.show('settings'));
      const banner = UI.banner('warn', T('目录白名单还是空的 —— AI 现在什么都读不到'),
        T('这是**有意的默认值**：必须先由你显式指定 AI 可以看哪些目录。'));
      banner.querySelector('.bd').appendChild(U.el('div', { class: 'mt1' }, [action]));
      host.appendChild(banner);
    }
    if (status.level !== 'READ_ONLY') {
      host.appendChild(UI.banner('warn', T('当前权限级别是「') + status.level_label + '」',
        LEVEL_TEXT[status.level] + T(' 建议只在需要时临时提升，用完改回只读。')));
    }

    host.appendChild(U.el('div', { class: 'grid cols-4 mb2' }, [
      tile(T('权限级别'), status.level_label, T('默认只读')),
      tile(T('可用工具'), U.num(status.tool_count), T('四组')),
      tile(T('调用次数'), U.num(status.audit_count), T('被拒 ') + (status.denied_count || 0) + T(' 次')),
      tile(T('回收站'), U.num(status.recycle_count), T('个文件')),
    ]));

    host.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('shield', { size: 17 }) }),
        U.el('span', { text: T('这个应用给 AI 的边界') }),
      ]),
      U.el('div', { class: 'small muted prewrap', text:
        T('· **没有 shell 执行入口**：工具表是固定命名的具体能力，不存在 shell_exec / run_command\n') +
        T('· **目录白名单**：每次调用都先 realpath 再比对白名单，目录穿越与逃出白名单的软链接一律拒绝\n') +
        T('· **三级权限**：默认只读；破坏性工具（移入回收站）默认不可用\n') +
        T('· **全量审计**：每次调用（含被拒的）都落库，可在「审计」页翻查\n') +
        T('· **内容即数据**：工具返回的文件内容不会被当作指令执行') }),
    ]));

    const card = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('link', { size: 17 }) }),
        U.el('span', { text: T('接入 AI 客户端') }),
      ]),
      U.el('div', { class: 'card-hint', text: T('在「客户端配置」页可以复制现成的配置片段。') }),
    ]);
    const go = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('copy', { size: 15 }) }),
      U.el('span', { text: T('查看配置片段') }),
    ]);
    go.addEventListener('click', () => Shell.show('client'));
    card.appendChild(U.el('div', { class: 'btn-row' }, [go]));
    host.appendChild(card);
  }

  function tile(label, value, hint) {
    return U.el('div', { class: 'stat-tile' }, [
      U.el('div', { class: 'label', text: label }),
      U.el('div', { class: 'value' }, [
        U.el('span', { text: value }),
        hint ? U.el('small', { text: hint }) : null,
      ]),
    ]);
  }

  /* ------------------------------------------------------------ 能力（工具） */

  async function renderTools(host) {
    host.innerHTML = '';
    let data;
    try {
      data = await API.get('api/mcp/tools');
    } catch (error) {
      host.appendChild(UI.banner('error', T('无法读取工具清单'), U.esc(error.message)));
      return;
    }
    State.tools = data;

    const card = U.el('div', { class: 'card' });
    card.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('grid', { size: 17 }) }),
      U.el('span', { text: T('AI 现在能做什么（') + data.count + T(' 个工具）') }),
    ]));
    card.appendChild(U.el('div', { class: 'card-hint',
      text: T('灰掉的是当前权限级别下不可用的工具。要点开使用，请到「设置」提升级别。') }));

    const groupLabel = { files: T('文件'), storage: T('存储'), media: T('媒体'), jobs: T('任务') };
    Object.keys(data.groups).sort().forEach((group) => {
      const section = U.el('div', { class: 'mt2' });
      section.appendChild(U.el('h3', { text: (groupLabel[group] || group) +
        '（' + data.groups[group].length + '）' }));
      data.groups[group].forEach((tool) => {
        const item = U.el('div', { class: 'tool-item' + (tool.available ? '' : ' disabled') });
        const header = U.el('header', {}, [
          U.el('span', { html: Icons.svg(tool.available ? 'check' : 'lock', { size: 14 }) }),
          U.el('span', { class: 'nm', text: tool.name }),
          U.el('span', { class: 'grow' }),
          UI.badge(tool.level_label.replace(/（.*）/, ''),
                   tool.level === 'READ_ONLY' ? 'ok'
                     : (tool.level === 'READ_WRITE' ? 'warn' : 'danger')),
        ]);
        const desc = U.el('div', { class: 'desc', text: tool.description });
        const schema = U.el('div', { class: 'schema hidden', text:
          JSON.stringify(tool.schema || tool.inputSchema || {}, null, 2) });
        header.addEventListener('click', () => schema.classList.toggle('hidden'));
        item.appendChild(header);
        item.appendChild(desc);
        item.appendChild(schema);
        section.appendChild(item);
      });
      card.appendChild(section);
    });
    host.appendChild(card);
  }

  /* ------------------------------------------------------------ 审计 */

  async function renderAudit(host) {
    host.innerHTML = '';
    let data;
    try {
      data = await API.get('api/mcp/audit?limit=300');
    } catch (error) {
      host.appendChild(UI.banner('error', T('无法读取审计日志'), U.esc(error.message)));
      return;
    }
    State.audit = data.entries || [];

    const card = U.el('div', { class: 'card flush' });
    const clear = U.el('button', { class: 'btn sm' }, [
      U.el('span', { html: Icons.svg('trash', { size: 13 }) }),
      U.el('span', { text: T('清空审计日志') }),
    ]);
    clear.addEventListener('click', async () => {
      const ok = await UI.confirm({
        title: T('清空审计日志'),
        body: T('将删除全部审计记录。审计日志是「AI 做过什么」的唯一凭据，') +
              T('建议先确认不再需要追溯。\n（清空动作本身也会被记一条）'),
        confirmText: T('清空'), danger: true, requireText: '清空',
      });
      if (!ok) return;
      try {
        const result = await API.post('api/mcp/audit/clear');
        UI.ok(T('已清空 ') + result.removed + T(' 条记录'));
        Shell.show('audit');
      } catch (error) { UI.err(error); }
    });
    card.appendChild(U.el('div', { class: 'card-head spread' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('list', { size: 17 }) }),
        U.el('span', { text: T('审计日志') }),
      ]),
      clear,
    ]));

    const body = U.el('div', { class: 'card-body' });
    card.appendChild(body);
    host.appendChild(card);

    if (!State.audit.length) {
      body.appendChild(UI.empty('shield', T('还没有任何调用记录'),
        T('AI 客户端连接并调用工具后，这里会逐条记录（含被权限拦下的）。')));
      return;
    }

    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: T('时间') }), U.el('th', { text: T('工具') }),
        U.el('th', { text: T('级别') }), U.el('th', { text: T('参数') }),
        U.el('th', { text: T('结果') }), U.el('th', { class: 'num', text: T('耗时') }),
        U.el('th', { text: T('来源') }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    State.audit.forEach((row) => {
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { class: 'small nowrap mono', text: row.time }),
        U.el('td', { class: 'mono small', text: row.tool }),
        U.el('td', { class: 'small', text: row.level }),
        U.el('td', { class: 'path-cell mono small', text: row.params || '' }),
        U.el('td', { class: row.allowed ? 'audit-ok small' : 'audit-deny small',
                     text: row.allowed ? T('放行') : (T('拒绝：') + (row.error || '')) }),
        U.el('td', { class: 'num small', text: Math.round(row.duration_ms || 0) + ' ms' }),
        U.el('td', { class: 'small faint', text: row.caller || '' }),
      ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }

  /* ------------------------------------------------------------ 客户端配置 */

  async function renderClient(host) {
    host.innerHTML = '';
    let cfg;
    try {
      cfg = await API.get('api/mcp/config');
    } catch (error) {
      host.appendChild(UI.banner('error', T('无法读取配置'), U.esc(error.message)));
      return;
    }
    State.config = cfg;

    const httpCard = U.el('div', { class: 'card cfg' });
    httpCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
      U.el('span', { text: T('HTTP 传输（推荐）') }),
    ]));
    httpCard.appendChild(UI.banner('info', T('经 TOS 平台代理，访问受登录会话约束'),
      U.esc(cfg.http.note)));
    httpCard.appendChild(U.el('pre', { text: JSON.stringify(cfg.http.sample, null, 2) }));
    httpCard.appendChild(copyRow(cfg.http.sample));
    host.appendChild(httpCard);

    const stdioCard = U.el('div', { class: 'card cfg' });
    stdioCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('terminal', { size: 17 }) }),
      U.el('span', { text: T('stdio 传输（本机 / SSH）') }),
    ]));
    stdioCard.appendChild(U.el('div', { class: 'card-hint', text: cfg.stdio.note }));
    stdioCard.appendChild(U.el('pre', { text: JSON.stringify(cfg.stdio.sample, null, 2) }));
    stdioCard.appendChild(copyRow(cfg.stdio.sample));
    host.appendChild(stdioCard);

    const toolsCard = U.el('div', { class: 'card' });
    toolsCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('info', { size: 17 }) }),
      U.el('span', { text: T('当前工具清单（') + cfg.tools.length + '）' }),
    ]));
    toolsCard.appendChild(U.el('pre', { class: 'logview',
      text: cfg.tools.map((t) => t.name).join('\n') }));
    host.appendChild(toolsCard);

    function copyRow(sample) {
      const row = U.el('div', { class: 'btn-row' });
      const button = U.el('button', { class: 'btn sm' }, [
        U.el('span', { html: Icons.svg('copy', { size: 13 }) }),
        U.el('span', { text: T('复制配置') }),
      ]);
      button.addEventListener('click', async () => {
        const text = JSON.stringify(sample, null, 2);
        try {
          await navigator.clipboard.writeText(text);
          UI.ok(T('已复制到剪贴板'));
        } catch (error) {
          UI.modal({ title: T('手动复制'), icon: 'copy', wide: true,
                     bodyHtml: '<pre class="logview">' + U.esc(text) + '</pre>',
                     buttons: [{ text: T('关闭') }] });
        }
      });
      row.appendChild(button);
      return row;
    }
  }

  /* ------------------------------------------------------------ 回收站 */

  async function renderRecycle(host) {
    host.innerHTML = '';
    let data;
    try {
      data = await API.get('api/mcp/recycle');
    } catch (error) {
      host.appendChild(UI.banner('error', T('无法读取回收站'), U.esc(error.message)));
      return;
    }
    State.recycle = data.items || [];

    const card = U.el('div', { class: 'card flush' });
    card.appendChild(U.el('div', { class: 'card-head' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('archive', { size: 17 }) }),
        U.el('span', { text: T('回收站') }),
      ]),
    ]));
    const body = U.el('div', { class: 'card-body' });
    card.appendChild(body);
    host.appendChild(card);

    if (!State.recycle.length) {
      body.appendChild(UI.empty('archive', T('回收站是空的'),
        T('只有权限级别为「危险」时 AI 才能调用 delete_file，且它只是把文件移到这里，') +
        T('不是永久删除。')));
      return;
    }

    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: T('时间') }), U.el('th', { text: T('原位置') }),
        U.el('th', { class: 'num', text: T('大小') }), U.el('th', { text: T('原因') }),
        U.el('th', { text: T('状态') }), U.el('th', { text: '' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    State.recycle.forEach((item) => {
      const restore = U.el('button', { class: 'btn sm ghost', text: T('还原'),
                                       disabled: !!item.restored || !item.exists });
      restore.addEventListener('click', async () => {
        try {
          const result = await API.post('api/mcp/recycle/restore', { item_ids: [item.id] });
          UI.ok(T('已还原 ') + result.restored + T(' 个') + (result.skipped ? T('，跳过 ') + result.skipped : ''));
          Shell.show('recycle');
        } catch (error) { UI.err(error); }
      });
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { class: 'small mono nowrap', text: item.time }),
        U.el('td', { class: 'path-cell mono small', text: item.original }),
        U.el('td', { class: 'num', text: U.size(item.size) }),
        U.el('td', { class: 'small', text: item.reason || '' }),
        U.el('td', {}, [UI.badge(item.restored ? T('已还原') : (item.exists ? T('在回收站') : T('文件缺失')),
                                item.restored ? 'ok' : (item.exists ? 'warn' : 'danger'))]),
        U.el('td', {}, [restore]),
      ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }

  /* ------------------------------------------------------------ 设置 */

  async function renderSettings(host) {
    host.innerHTML = '';
    // 界面语言（放最前：非中文用户进来第一眼就该看到它）
    // UI.langSelect() 内部已处理「落 localStorage + 套用 + 同步到后端 settings.ui_language」。
    %(host)s.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
        U.el('span', { text: T('界面语言') }),
      ]),
      U.el('div', { class: 'card-hint',
        text: T('选择本应用界面的语言。首次打开时会跟随浏览器语言。') }),
      UI.langSelect(),
    ]));

    // 权限级别
    const levelCard = U.el('div', { class: 'card' });
    levelCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('key', { size: 17 }) }),
      U.el('span', { text: T('权限级别') }),
    ]));
    levelCard.appendChild(U.el('div', { class: 'card-hint',
      text: T('决定 AI 被允许做什么。默认只读。每次变更都会记入审计日志。') }));
    const cards = U.el('div', { class: 'level-cards' });
    (State.status && State.status.levels || []).forEach((level) => {
      const active = State.status && State.status.level === level.id;
      const node = U.el('div', {
        class: 'level-card' + (active ? ' active' : '') +
               (level.id === 'DANGEROUS' ? ' danger' : ''),
      }, [
        U.el('div', { class: 't' }, [
          U.el('span', { html: Icons.svg(active ? 'check' : 'lock', { size: 15 }) }),
          U.el('span', { text: level.label }),
        ]),
        U.el('div', { class: 'd', text: LEVEL_TEXT[level.id] || '' }),
      ]);
      node.addEventListener('click', async () => {
        if (active) return;
        const danger = level.id !== 'READ_ONLY';
        const ok = await UI.confirm({
          title: T('把权限级别改为「') + level.label + '」？',
          body: LEVEL_TEXT[level.id] + '\n\n' +
                (danger ? T('提升权限意味着 AI 可以修改你的文件。建议用完改回只读。') : ''),
          confirmText: T('确认修改'),
          danger: level.id === 'DANGEROUS',
          requireText: level.id === 'DANGEROUS' ? T('危险') : null,
        });
        if (!ok) return;
        try {
          await API.post('api/mcp/level', { level: level.id });
          UI.ok(T('权限级别已改为「') + level.label + '」');
          Shell.show('settings');
        } catch (error) { UI.err(error); }
      });
      cards.appendChild(node);
    });
    levelCard.appendChild(cards);
    host.appendChild(levelCard);

    // 目录白名单
    const rootsCard = U.el('div', { class: 'card' });
    rootsCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('folderOpen', { size: 17 }) }),
      U.el('span', { text: T('AI 可以访问的目录（白名单）') }),
    ]));
    rootsCard.appendChild(U.el('div', { class: 'card-hint',
      text: T('默认是空的 —— 不加目录，AI 什么都读不到。路径校验：先 realpath 再比对白名单，') +
            T('目录穿越（../）与指向白名单之外的软链接都会被拒绝。') }));

    const chipBox = U.el('div', { class: 'chips mb1' });
    function renderRoots() {
      chipBox.innerHTML = '';
      if (!State.allowedRoots.length) {
        chipBox.appendChild(U.el('span', { class: 'small faint', text: T('（当前为空）') }));
        return;
      }
      State.allowedRoots.forEach((root) => {
        const remove = U.el('button', { title: T('移除'), text: '×' });
        remove.addEventListener('click', async () => {
          const ok = await UI.confirm({
            title: T('移除可访问目录'),
            body: T('移除后 AI 将无法再读取：\n') + root + T('\n\n（不会删除任何文件）'),
            confirmText: T('移除'), danger: true,
          });
          if (!ok) return;
          State.allowedRoots = State.allowedRoots.filter((item) => item !== root);
          await save();
          renderRoots();
        });
        chipBox.appendChild(U.el('span', { class: 'chip' }, [
          U.el('span', { text: root, title: root }), remove,
        ]));
      });
    }
    renderRoots();

    const addBtn = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('plus', { size: 15 }) }),
      U.el('span', { text: T('添加目录') }),
    ]);
    addBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择允许 AI 访问的目录'),
        start: State.allowedRoots[0] || '',
        onPick: async (path) => {
          if (State.allowedRoots.includes(path)) { UI.warn(T('已在列表中')); return; }
          State.allowedRoots.push(path);
          await save();
          renderRoots();
        },
      });
    });
    rootsCard.appendChild(U.el('div', { class: 'btn-row' }, [addBtn]));
    host.appendChild(rootsCard);

    async function save() {
      try { await API.post('api/settings', { allowed_roots: State.allowedRoots }); }
      catch (error) { UI.err(error, T('保存失败')); }
    }

    // 关于
    const aboutCard = U.el('div', { class: 'card' });
    aboutCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('info', { size: 17 }) }),
      U.el('span', { text: T('关于与隐私') }),
    ]));
    const about = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('info', { size: 15 }) }),
      U.el('span', { text: T('查看详情') }),
    ]);
    about.addEventListener('click', async () => {
      let info = {};
      try { info = await API.get('api/app'); } catch (error) { /* 忽略 */ }
      UI.modal({
        title: T('关于'), icon: 'info', wide: true,
        bodyHtml: `
          <p><b>MCP Hub</b> v${U.esc(info.version || '')}</p>
          <p class="small muted">${T('让 AI 通过 MCP 使用 TNAS。全部处理在本机完成，不联网、不上传。')}</p>
          <h3 class="mt2">${T('安全边界')}</h3>
          <ul class="small">
            <li>${T('无 shell 执行入口；工具是固定命名的具体能力')}</li>
            <li>${T('目录白名单 + 三级权限，默认只读')}</li>
            <li>${T('每次调用都记入审计日志（含被拒的）')}</li>
            <li>${T('AI 看不到任何凭据；日志与审计里的敏感字段已打码')}</li>
          </ul>
          <h3 class="mt2">${T('运行时写入位置')}</h3>
          <pre class="logview small">${U.esc(info.paths ? JSON.stringify(info.paths, null, 2) : '')}</pre>`,
        buttons: [{ text: T('关闭') }],
      });
    });
    aboutCard.appendChild(U.el('div', { class: 'btn-row' }, [about]));
    host.appendChild(aboutCard);
  }

  /* ------------------------------------------------------------ 启动 */

  async function boot() {
    Jobs.mountTaskbar(U.byId('taskbar'));
    Jobs.start(4000);

    const shell = Shell.init({
      overview: { label: T('概览'), icon: 'home', render: renderOverview },
      tools: { label: T('能力清单'), icon: 'grid', render: renderTools },
      audit: { label: T('审计'), icon: 'list', render: renderAudit },
      client: { label: T('客户端配置'), icon: 'link', render: renderClient },
      recycle: { label: T('回收站'), icon: 'archive', render: renderRecycle },
      settings: { label: T('设置'), icon: 'settings', render: renderSettings },
    }, { defaultView: 'overview' });
    // 切语言后重渲染当前视图 —— 框架只换静态文案，动态渲染的部分要靠这个事件
    Shell.bindLanguage(shell);
    window.Shell = shell;

    try {
      const [status, app] = await Promise.all([API.get('api/mcp/status'), Shell.loadAppInfo()]);
      State.status = status;
      State.allowedRoots = status.allowed_roots || [];
      U.byId('level-meta').textContent = status.level_label;
      if (app) {
        const node = U.byId('app-version');
        if (node) node.textContent = 'v' + app.version;
      }
    } catch (error) {
      U.byId('level-meta').textContent = T('服务未就绪');
    }

    shell.show(shell.current() || 'overview');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
