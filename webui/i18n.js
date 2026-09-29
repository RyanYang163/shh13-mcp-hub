/* ============================================================================
   界面多语言运行时（公共框架层）
   ----------------------------------------------------------------------------
   设计（对齐 test-center-guide-docker 的既有做法）：
     · **中文原文就是 key** —— 代码里写 T('运行中')，词典里 '运行中' → 'Running'。
       查不到就原样返回中文，永不渲染出空白或键名，缺失是优雅降级。
     · 插值用 {占位符}：T('任务 #{id} 日志', {id: 7})。
     · 词条分两份：公共框架的在本文件（随动文件，gen.py 分发到 10 个应用）；
       应用自己的放 webui/<app>.i18n.js（人工维护，gen.py 不碰）。

   语言判定顺序：用户显式选择（localStorage，后端 settings.ui_language 由应用层同步）
                 → navigator.language → zh-cn

   ⚠️ 改本文件时注意（门禁会拦）：
     · 必须 LF 行尾、UTF-8 无 BOM（verify.py 的 check_text_hygiene）
     · 键名与值里不能出现「敏感词后跟冒号再跟一个 6 字符以上的引号串」这种形态 ——
       verify.py 的 check_credentials 会扫所有 .js 并误判成硬编码凭据。
       （连注释里的示范都会命中，本文件第一版就是这么被自己的说明文字绊倒的。）
   ========================================================================== */

const I18N = (function () {
  // TOS 应用中心支持的 14 种语言（指引 8.5.1），与 <appid>.lang 同一套语言码
  const LANGS = [
    'zh-cn', 'zh-hk', 'en-us', 'fr-fr', 'de-de', 'it-it', 'es-es',
    'hu-hu', 'ja-jp', 'ko-kr', 'pl-pl', 'ru-ru', 'tr-tr', 'pt-pt',
  ];

  // 各语言的自称（语言选择器用；这些名字本身不翻译）
  const NATIVE = {
    'zh-cn': '简体中文', 'zh-hk': '繁體中文', 'en-us': 'English', 'fr-fr': 'Français',
    'de-de': 'Deutsch', 'it-it': 'Italiano', 'es-es': 'Español', 'hu-hu': 'Magyar',
    'ja-jp': '日本語', 'ko-kr': '한국어', 'pl-pl': 'Polski', 'ru-ru': 'Русский',
    'tr-tr': 'Türkçe', 'pt-pt': 'Português',
  };

  const DEFAULT_LANG = 'zh-cn';
  const STORE_KEY = 'tnas-ui-lang';

  // 浏览器语言标签 → TOS 语言码。只做「第一位子标签 + 地区」两级匹配。
  function canonical(tag) {
    const raw = String(tag || '').trim().toLowerCase().replace(/_/g, '-');
    if (!raw) return '';
    if (LANGS.indexOf(raw) >= 0) return raw;
    const parts = raw.split('-');
    const primary = parts[0];
    const region = parts[1] || '';
    if (primary === 'zh') {
      // 繁体：tw / hk / mo / 显式 hant
      if (/^(tw|hk|mo)$/.test(region) || /hant/.test(raw)) return 'zh-hk';
      return 'zh-cn';
    }
    if (primary === 'pt') return 'pt-pt';
    const hit = LANGS.find((code) => code.split('-')[0] === primary);
    return hit || '';
  }

  function detect() {
    // 1) 用户在本应用里显式选过
    try {
      const saved = canonical(window.localStorage.getItem(STORE_KEY));
      if (saved) return saved;
    } catch (e) { /* 隐私模式等：忽略 */ }
    // 2) 浏览器语言（平台不向 iframe 传系统语言，这是唯一可得的信号）
    const list = (navigator.languages && navigator.languages.length)
      ? navigator.languages : [navigator.language];
    for (let i = 0; i < list.length; i += 1) {
      const hit = canonical(list[i]);
      if (hit) return hit;
    }
    // 3) 兜底
    return DEFAULT_LANG;
  }

  let lang = detect();

  function lookup(key, code) {
    const app = (window.TNAS_I18N_APP || {})[code];
    if (app && Object.prototype.hasOwnProperty.call(app, key)) return app[key];
    const common = (window.TNAS_I18N_COMMON || {})[code];
    if (common && Object.prototype.hasOwnProperty.call(common, key)) return common[key];
    return null;
  }

  function fill(text, params) {
    if (!params) return text;
    return text.replace(/\{(\w+)\}/g, (whole, name) => (
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole
    ));
  }

  // 词条的 key 一律是中文原文，所以不含汉字的字符串不可能是 key。
  // 这条快路径把绝大多数动态数据（文件名、数字、SVG 标记）挡在查表之前。
  const CJK = /[㐀-䶿一-鿿豈-﫿]/;

  /**
   * 回退链：目标语言 → en-us → 中文原文。
   *
   * 为什么要回退到英文而不是直接回中文：没翻到的词若回中文，非中文用户会在
   * 一整屏英文里突然看到一句中文，比全英文更糟。回退到英文则至少是「同一门语言」，
   * 未完成的翻译是个**受控的降级**，而不是破洞。
   *
   * ``zh-hk`` 例外：它缺词时回中文原文 —— 繁体读者看简体远比看英文顺。
   */
  function lookupOrder(code) {
    return code === 'zh-hk' ? ['zh-hk'] : [code, 'en-us'];
  }

  /** 取值。查不到返回中文原文。 */
  function t(key, params) {
    if (typeof key !== 'string' || !key) return key;
    if (!CJK.test(key)) return key;
    if (lang === DEFAULT_LANG) return fill(key, params);
    for (const code of lookupOrder(lang)) {
      const hit = lookup(key, code);
      if (hit !== null) return fill(hit, params);
    }
    return fill(key, params);
  }

  /** 把静态 DOM 上的 data-i18n* 套用一遍（HTML 里的中文就是天然的中文词典） */
  function applyStatic(root) {
    const scope = root || document;
    const code = lang === DEFAULT_LANG ? 'zh-cn' : lang;

    scope.querySelectorAll('[data-i18n]').forEach((node) => {
      if (node.__i18nZh === undefined) node.__i18nZh = node.textContent;
      node.textContent = t(node.__i18nZh);
    });
    scope.querySelectorAll('[data-i18n-html]').forEach((node) => {
      if (node.__i18nZhHtml === undefined) node.__i18nZhHtml = node.innerHTML;
      node.innerHTML = t(node.__i18nZhHtml);
    });
    scope.querySelectorAll('[data-i18n-ph]').forEach((node) => {
      if (node.__i18nZhPh === undefined) node.__i18nZhPh = node.getAttribute('data-i18n-ph');
      node.setAttribute('placeholder', t(node.__i18nZhPh));
    });

    document.documentElement.lang = code;
    const titleNode = scope.querySelector('title');
    if (titleNode) {
      if (titleNode.__i18nZh === undefined) titleNode.__i18nZh = titleNode.textContent;
      titleNode.textContent = t(titleNode.__i18nZh);
    }
  }

  function setLang(code, options) {
    const hit = canonical(code) || DEFAULT_LANG;
    lang = hit;
    try { window.localStorage.setItem(STORE_KEY, hit); } catch (e) { /* 忽略 */ }
    applyStatic();
    if (!options || options.rerender !== false) {
      window.dispatchEvent(new CustomEvent('tnas-langchange', { detail: { lang: hit } }));
    }
    return hit;
  }

  return {
    LANGS,
    NATIVE,
    DEFAULT_LANG,
    t,
    lang: () => lang,
    setLang,
    applyStatic,
    canonical,
    nativeName: (code) => NATIVE[code] || code,
  };
})();

/** 全局取词函数。`T('运行中')` → 当前语言的 'Running'。 */
function T(key, params) { return I18N.t(key, params); }

window.I18N = I18N;
window.T = T;

// 启动即套用一次静态文案（<title> 与带 data-i18n 的节点）。
// 动态渲染的部分由 U.el 内的 T() 负责，不需要在这里管。
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => I18N.applyStatic());
} else {
  I18N.applyStatic();
}

/* --------------------------------------------------------------------------
   公共框架词条：汉字原文 → 各语言译文。zh-cn 不需要条目（原文即译文）。
   -------------------------------------------------------------------------- */
window.TNAS_I18N_COMMON = {
  'zh-hk': {
    '会话已失效，正在返回登录页': '工作階段已失效，正在返回登入頁',
    '操作失败': '操作失敗',
    '请确认': '請確認',
    '确认': '確認',
    '取消': '取消',
    '请输入 {x} 以确认': '請輸入 {x} 以確認',
    '选择目录': '選擇目錄',
    '选定此目录': '選定此目錄',
    '请先进入一个目录': '請先進入一個目錄',
    '加载中…': '載入中…',
    '无法读取此目录': '無法讀取此目錄',
    '当前目录：': '目前目錄：',
    '(根)': '(根)',
    '起始位置': '起始位置',
    '这里是空的': '這裡是空的',
    '没有可选的子目录': '沒有可選的子目錄',
    '上一级': '上一層',
    '运行中': '執行中',
    '排队': '排隊',
    '已暂停': '已暫停',
    '已完成': '已完成',
    '失败': '失敗',
    '刷新': '重新整理',
    '获取任务状态失败': '取得任務狀態失敗',
    '任务已提交': '任務已提交',
    '可在底部任务栏查看进度': '可在底部任務列查看進度',
    '排队中': '排隊中',
    '取消中': '取消中',
    '已取消': '已取消',
    '还没有任务': '還沒有任務',
    '在上方提交一个任务试试': '在上方提交一個任務試試',
    '任务': '任務',
    '状态': '狀態',
    '进度': '進度',
    '开始时间': '開始時間',
    '操作': '操作',
    '查看日志': '檢視日誌',
    '任务 #{id} 日志': '任務 #{id} 日誌',
    '继续': '繼續',
    '暂停': '暫停',
    '错误': '錯誤',
    '失败原因': '失敗原因',
    '日志': '日誌',
    '重试': '重試',
    '删除': '刪除',
    '暂无日志': '暫無日誌',
    '没有可展示的数据': '沒有可顯示的資料',
    '先扫描一个目录': '先掃描一個目錄',
    '暂无数据': '暫無資料',
    '{n} 秒': '{n} 秒',
    '{m} 分 {s} 秒': '{m} 分 {s} 秒',
    '{h} 小时 {m} 分': '{h} 小時 {m} 分',
  },

  'en-us': {
    '会话已失效，正在返回登录页': 'Session expired, returning to the login page',
    '操作失败': 'Operation failed',
    '请确认': 'Please confirm',
    '确认': 'Confirm',
    '取消': 'Cancel',
    '请输入 {x} 以确认': 'Type {x} to confirm',
    '选择目录': 'Choose a folder',
    '选定此目录': 'Use this folder',
    '请先进入一个目录': 'Open a folder first',
    '加载中…': 'Loading…',
    '无法读取此目录': 'Cannot read this folder',
    '当前目录：': 'Current folder:',
    '(根)': '(root)',
    '起始位置': 'Starting points',
    '这里是空的': 'This folder is empty',
    '没有可选的子目录': 'No subfolders available',
    '上一级': 'Up one level',
    '运行中': 'Running',
    '排队': 'Queued',
    '已暂停': 'Paused',
    '已完成': 'Done',
    '失败': 'Failed',
    '刷新': 'Refresh',
    '获取任务状态失败': 'Could not fetch job status',
    '任务已提交': 'Job submitted',
    '可在底部任务栏查看进度': 'Track its progress in the task bar below',
    '排队中': 'Queued',
    '取消中': 'Canceling',
    '已取消': 'Canceled',
    '还没有任务': 'No jobs yet',
    '在上方提交一个任务试试': 'Submit one above to get started',
    '任务': 'Job',
    '状态': 'Status',
    '进度': 'Progress',
    '开始时间': 'Started',
    '操作': 'Actions',
    '查看日志': 'View logs',
    '任务 #{id} 日志': 'Job #{id} logs',
    '继续': 'Resume',
    '暂停': 'Pause',
    '错误': 'Error',
    '失败原因': 'Failure reason',
    '日志': 'Logs',
    '重试': 'Retry',
    '删除': 'Delete',
    '暂无日志': 'No logs yet',
    '没有可展示的数据': 'Nothing to display',
    '先扫描一个目录': 'Scan a folder first',
    '暂无数据': 'No data',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} h {m} min',
  },

  'fr-fr': {
    '会话已失效，正在返回登录页': 'Session expirée, retour à la page de connexion',
    '操作失败': 'Échec de l’opération',
    '请确认': 'Veuillez confirmer',
    '确认': 'Confirmer',
    '取消': 'Annuler',
    '请输入 {x} 以确认': 'Saisissez {x} pour confirmer',
    '选择目录': 'Choisir un dossier',
    '选定此目录': 'Utiliser ce dossier',
    '请先进入一个目录': 'Ouvrez d’abord un dossier',
    '加载中…': 'Chargement…',
    '无法读取此目录': 'Impossible de lire ce dossier',
    '当前目录：': 'Dossier actuel :',
    '(根)': '(racine)',
    '起始位置': 'Points de départ',
    '这里是空的': 'Ce dossier est vide',
    '没有可选的子目录': 'Aucun sous-dossier disponible',
    '上一级': 'Niveau supérieur',
    '运行中': 'En cours',
    '排队': 'En attente',
    '已暂停': 'En pause',
    '已完成': 'Terminé',
    '失败': 'Échec',
    '刷新': 'Actualiser',
    '获取任务状态失败': 'Impossible d’obtenir l’état des tâches',
    '任务已提交': 'Tâche envoyée',
    '可在底部任务栏查看进度': 'Suivez sa progression dans la barre des tâches ci-dessous',
    '排队中': 'En attente',
    '取消中': 'Annulation',
    '已取消': 'Annulé',
    '还没有任务': 'Aucune tâche',
    '在上方提交一个任务试试': 'Lancez-en une ci-dessus pour commencer',
    '任务': 'Tâche',
    '状态': 'État',
    '进度': 'Progression',
    '开始时间': 'Début',
    '操作': 'Actions',
    '查看日志': 'Voir les journaux',
    '任务 #{id} 日志': 'Journaux de la tâche #{id}',
    '继续': 'Reprendre',
    '暂停': 'Pause',
    '错误': 'Erreur',
    '失败原因': 'Cause de l’échec',
    '日志': 'Journaux',
    '重试': 'Réessayer',
    '删除': 'Supprimer',
    '暂无日志': 'Aucun journal',
    '没有可展示的数据': 'Rien à afficher',
    '先扫描一个目录': 'Analysez d’abord un dossier',
    '暂无数据': 'Aucune donnée',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} h {m} min',
  },

  'de-de': {
    '会话已失效，正在返回登录页': 'Sitzung abgelaufen, zurück zur Anmeldeseite',
    '操作失败': 'Vorgang fehlgeschlagen',
    '请确认': 'Bitte bestätigen',
    '确认': 'Bestätigen',
    '取消': 'Abbrechen',
    '请输入 {x} 以确认': 'Zur Bestätigung {x} eingeben',
    '选择目录': 'Ordner auswählen',
    '选定此目录': 'Diesen Ordner verwenden',
    '请先进入一个目录': 'Zuerst einen Ordner öffnen',
    '加载中…': 'Wird geladen…',
    '无法读取此目录': 'Dieser Ordner kann nicht gelesen werden',
    '当前目录：': 'Aktueller Ordner:',
    '(根)': '(Stamm)',
    '起始位置': 'Startpunkte',
    '这里是空的': 'Dieser Ordner ist leer',
    '没有可选的子目录': 'Keine Unterordner verfügbar',
    '上一级': 'Eine Ebene höher',
    '运行中': 'Läuft',
    '排队': 'In Warteschlange',
    '已暂停': 'Angehalten',
    '已完成': 'Fertig',
    '失败': 'Fehlgeschlagen',
    '刷新': 'Aktualisieren',
    '获取任务状态失败': 'Aufgabenstatus konnte nicht geladen werden',
    '任务已提交': 'Aufgabe übermittelt',
    '可在底部任务栏查看进度': 'Fortschritt in der Taskleiste unten verfolgen',
    '排队中': 'In Warteschlange',
    '取消中': 'Wird abgebrochen',
    '已取消': 'Abgebrochen',
    '还没有任务': 'Noch keine Aufgaben',
    '在上方提交一个任务试试': 'Oben eine Aufgabe starten',
    '任务': 'Aufgabe',
    '状态': 'Status',
    '进度': 'Fortschritt',
    '开始时间': 'Gestartet',
    '操作': 'Aktionen',
    '查看日志': 'Protokolle ansehen',
    '任务 #{id} 日志': 'Protokolle der Aufgabe #{id}',
    '继续': 'Fortsetzen',
    '暂停': 'Pausieren',
    '错误': 'Fehler',
    '失败原因': 'Fehlerursache',
    '日志': 'Protokolle',
    '重试': 'Wiederholen',
    '删除': 'Löschen',
    '暂无日志': 'Noch keine Protokolle',
    '没有可展示的数据': 'Nichts anzuzeigen',
    '先扫描一个目录': 'Zuerst einen Ordner scannen',
    '暂无数据': 'Keine Daten',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} Min. {s} s',
    '{h} 小时 {m} 分': '{h} Std. {m} Min.',
  },

  'it-it': {
    '会话已失效，正在返回登录页': 'Sessione scaduta, ritorno alla pagina di accesso',
    '操作失败': 'Operazione non riuscita',
    '请确认': 'Confermare',
    '确认': 'Conferma',
    '取消': 'Annulla',
    '请输入 {x} 以确认': 'Digita {x} per confermare',
    '选择目录': 'Scegli una cartella',
    '选定此目录': 'Usa questa cartella',
    '请先进入一个目录': 'Apri prima una cartella',
    '加载中…': 'Caricamento…',
    '无法读取此目录': 'Impossibile leggere questa cartella',
    '当前目录：': 'Cartella corrente:',
    '(根)': '(radice)',
    '起始位置': 'Punti di partenza',
    '这里是空的': 'Questa cartella è vuota',
    '没有可选的子目录': 'Nessuna sottocartella disponibile',
    '上一级': 'Livello superiore',
    '运行中': 'In esecuzione',
    '排队': 'In coda',
    '已暂停': 'In pausa',
    '已完成': 'Completata',
    '失败': 'Non riuscita',
    '刷新': 'Aggiorna',
    '获取任务状态失败': 'Impossibile ottenere lo stato delle attività',
    '任务已提交': 'Attività inviata',
    '可在底部任务栏查看进度': 'Segui l’avanzamento nella barra delle attività in basso',
    '排队中': 'In coda',
    '取消中': 'Annullamento',
    '已取消': 'Annullata',
    '还没有任务': 'Nessuna attività',
    '在上方提交一个任务试试': 'Avviane una qui sopra per iniziare',
    '任务': 'Attività',
    '状态': 'Stato',
    '进度': 'Avanzamento',
    '开始时间': 'Avviata',
    '操作': 'Azioni',
    '查看日志': 'Vedi i log',
    '任务 #{id} 日志': 'Log dell’attività #{id}',
    '继续': 'Riprendi',
    '暂停': 'Pausa',
    '错误': 'Errore',
    '失败原因': 'Causa dell’errore',
    '日志': 'Log',
    '重试': 'Riprova',
    '删除': 'Elimina',
    '暂无日志': 'Nessun log',
    '没有可展示的数据': 'Niente da mostrare',
    '先扫描一个目录': 'Analizza prima una cartella',
    '暂无数据': 'Nessun dato',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} h {m} min',
  },

  'es-es': {
    '会话已失效，正在返回登录页': 'Sesión caducada, volviendo a la página de acceso',
    '操作失败': 'La operación ha fallado',
    '请确认': 'Confirma',
    '确认': 'Confirmar',
    '取消': 'Cancelar',
    '请输入 {x} 以确认': 'Escribe {x} para confirmar',
    '选择目录': 'Elegir una carpeta',
    '选定此目录': 'Usar esta carpeta',
    '请先进入一个目录': 'Abre antes una carpeta',
    '加载中…': 'Cargando…',
    '无法读取此目录': 'No se puede leer esta carpeta',
    '当前目录：': 'Carpeta actual:',
    '(根)': '(raíz)',
    '起始位置': 'Puntos de inicio',
    '这里是空的': 'Esta carpeta está vacía',
    '没有可选的子目录': 'No hay subcarpetas disponibles',
    '上一级': 'Subir un nivel',
    '运行中': 'En ejecución',
    '排队': 'En cola',
    '已暂停': 'En pausa',
    '已完成': 'Completada',
    '失败': 'Fallida',
    '刷新': 'Actualizar',
    '获取任务状态失败': 'No se pudo obtener el estado de las tareas',
    '任务已提交': 'Tarea enviada',
    '可在底部任务栏查看进度': 'Sigue su progreso en la barra de tareas inferior',
    '排队中': 'En cola',
    '取消中': 'Cancelando',
    '已取消': 'Cancelada',
    '还没有任务': 'Aún no hay tareas',
    '在上方提交一个任务试试': 'Envía una arriba para empezar',
    '任务': 'Tarea',
    '状态': 'Estado',
    '进度': 'Progreso',
    '开始时间': 'Inicio',
    '操作': 'Acciones',
    '查看日志': 'Ver registros',
    '任务 #{id} 日志': 'Registros de la tarea #{id}',
    '继续': 'Reanudar',
    '暂停': 'Pausar',
    '错误': 'Error',
    '失败原因': 'Motivo del fallo',
    '日志': 'Registros',
    '重试': 'Reintentar',
    '删除': 'Eliminar',
    '暂无日志': 'Sin registros',
    '没有可展示的数据': 'Nada que mostrar',
    '先扫描一个目录': 'Analiza antes una carpeta',
    '暂无数据': 'Sin datos',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} h {m} min',
  },

  'hu-hu': {
    '会话已失效，正在返回登录页': 'A munkamenet lejárt, visszatérés a bejelentkező oldalra',
    '操作失败': 'A művelet nem sikerült',
    '请确认': 'Kérjük, erősítse meg',
    '确认': 'Megerősítés',
    '取消': 'Mégse',
    '请输入 {x} 以确认': 'A megerősítéshez írja be: {x}',
    '选择目录': 'Mappa kiválasztása',
    '选定此目录': 'E mappa használata',
    '请先进入一个目录': 'Először nyisson meg egy mappát',
    '加载中…': 'Betöltés…',
    '无法读取此目录': 'Ez a mappa nem olvasható',
    '当前目录：': 'Jelenlegi mappa:',
    '(根)': '(gyökér)',
    '起始位置': 'Kiindulási helyek',
    '这里是空的': 'Ez a mappa üres',
    '没有可选的子目录': 'Nincs elérhető almappa',
    '上一级': 'Egy szinttel feljebb',
    '运行中': 'Fut',
    '排队': 'Sorban',
    '已暂停': 'Szüneteltetve',
    '已完成': 'Kész',
    '失败': 'Sikertelen',
    '刷新': 'Frissítés',
    '获取任务状态失败': 'Nem sikerült lekérni a feladatok állapotát',
    '任务已提交': 'Feladat elküldve',
    '可在底部任务栏查看进度': 'A haladást az alsó felsorban követheti',
    '排队中': 'Sorban',
    '取消中': 'Megszakítás',
    '已取消': 'Megszakítva',
    '还没有任务': 'Még nincsenek feladatok',
    '在上方提交一个任务试试': 'Indítson el egyet fent',
    '任务': 'Feladat',
    '状态': 'Állapot',
    '进度': 'Haladás',
    '开始时间': 'Kezdés',
    '操作': 'Műveletek',
    '查看日志': 'Naplók megtekintése',
    '任务 #{id} 日志': 'A(z) #{id} feladat naplói',
    '继续': 'Folytatás',
    '暂停': 'Szüneteltetés',
    '错误': 'Hiba',
    '失败原因': 'Hiba oka',
    '日志': 'Naplók',
    '重试': 'Újra',
    '删除': 'Törlés',
    '暂无日志': 'Még nincs napló',
    '没有可展示的数据': 'Nincs megjeleníthető adat',
    '先扫描一个目录': 'Először vizsgáljon át egy mappát',
    '暂无数据': 'Nincs adat',
    '{n} 秒': '{n} mp',
    '{m} 分 {s} 秒': '{m} perc {s} mp',
    '{h} 小时 {m} 分': '{h} óra {m} perc',
  },

  'ja-jp': {
    '会话已失效，正在返回登录页': 'セッションが切れました。ログインページに戻ります',
    '操作失败': '操作に失敗しました',
    '请确认': '確認してください',
    '确认': '確認',
    '取消': 'キャンセル',
    '请输入 {x} 以确认': '確認のため {x} を入力してください',
    '选择目录': 'フォルダーを選択',
    '选定此目录': 'このフォルダーを選択',
    '请先进入一个目录': '先にフォルダーを開いてください',
    '加载中…': '読み込み中…',
    '无法读取此目录': 'このフォルダーを読み取れません',
    '当前目录：': '現在のフォルダー：',
    '(根)': '(ルート)',
    '起始位置': '開始位置',
    '这里是空的': 'このフォルダーは空です',
    '没有可选的子目录': '選択できるサブフォルダーがありません',
    '上一级': 'ひとつ上へ',
    '运行中': '実行中',
    '排队': '待機中',
    '已暂停': '一時停止中',
    '已完成': '完了',
    '失败': '失敗',
    '刷新': '更新',
    '获取任务状态失败': 'タスクの状態を取得できませんでした',
    '任务已提交': 'タスクを送信しました',
    '可在底部任务栏查看进度': '下のタスクバーで進捗を確認できます',
    '排队中': '待機中',
    '取消中': 'キャンセル中',
    '已取消': 'キャンセル済み',
    '还没有任务': 'タスクはまだありません',
    '在上方提交一个任务试试': '上のフォームから送信してください',
    '任务': 'タスク',
    '状态': '状態',
    '进度': '進捗',
    '开始时间': '開始時刻',
    '操作': '操作',
    '查看日志': 'ログを見る',
    '任务 #{id} 日志': 'タスク #{id} のログ',
    '继续': '再開',
    '暂停': '一時停止',
    '错误': 'エラー',
    '失败原因': '失敗の原因',
    '日志': 'ログ',
    '重试': '再試行',
    '删除': '削除',
    '暂无日志': 'ログはまだありません',
    '没有可展示的数据': '表示するデータがありません',
    '先扫描一个目录': '先にフォルダーをスキャンしてください',
    '暂无数据': 'データがありません',
    '{n} 秒': '{n} 秒',
    '{m} 分 {s} 秒': '{m} 分 {s} 秒',
    '{h} 小时 {m} 分': '{h} 時間 {m} 分',
  },

  'ko-kr': {
    '会话已失效，正在返回登录页': '세션이 만료되어 로그인 페이지로 돌아갑니다',
    '操作失败': '작업에 실패했습니다',
    '请确认': '확인해 주세요',
    '确认': '확인',
    '取消': '취소',
    '请输入 {x} 以确认': '확인하려면 {x}을(를) 입력하세요',
    '选择目录': '폴더 선택',
    '选定此目录': '이 폴더 사용',
    '请先进入一个目录': '먼저 폴더를 열어 주세요',
    '加载中…': '불러오는 중…',
    '无法读取此目录': '이 폴더를 읽을 수 없습니다',
    '当前目录：': '현재 폴더:',
    '(根)': '(루트)',
    '起始位置': '시작 위치',
    '这里是空的': '이 폴더는 비어 있습니다',
    '没有可选的子目录': '선택할 하위 폴더가 없습니다',
    '上一级': '상위로',
    '运行中': '실행 중',
    '排队': '대기 중',
    '已暂停': '일시 중지됨',
    '已完成': '완료됨',
    '失败': '실패',
    '刷新': '새로 고침',
    '获取任务状态失败': '작업 상태를 가져오지 못했습니다',
    '任务已提交': '작업을 제출했습니다',
    '可在底部任务栏查看进度': '아래 작업 표시줄에서 진행 상황을 볼 수 있습니다',
    '排队中': '대기 중',
    '取消中': '취소 중',
    '已取消': '취소됨',
    '还没有任务': '아직 작업이 없습니다',
    '在上方提交一个任务试试': '위에서 작업을 제출해 보세요',
    '任务': '작업',
    '状态': '상태',
    '进度': '진행률',
    '开始时间': '시작 시간',
    '操作': '작업',
    '查看日志': '로그 보기',
    '任务 #{id} 日志': '작업 #{id} 로그',
    '继续': '계속',
    '暂停': '일시 중지',
    '错误': '오류',
    '失败原因': '실패 원인',
    '日志': '로그',
    '重试': '다시 시도',
    '删除': '삭제',
    '暂无日志': '아직 로그가 없습니다',
    '没有可展示的数据': '표시할 데이터가 없습니다',
    '先扫描一个目录': '먼저 폴더를 검사하세요',
    '暂无数据': '데이터 없음',
    '{n} 秒': '{n}초',
    '{m} 分 {s} 秒': '{m}분 {s}초',
    '{h} 小时 {m} 分': '{h}시간 {m}분',
  },

  'pl-pl': {
    '会话已失效，正在返回登录页': 'Sesja wygasła, powrót do strony logowania',
    '操作失败': 'Operacja nie powiodła się',
    '请确认': 'Potwierdź',
    '确认': 'Potwierdź',
    '取消': 'Anuluj',
    '请输入 {x} 以确认': 'Wpisz {x}, aby potwierdzić',
    '选择目录': 'Wybierz folder',
    '选定此目录': 'Użyj tego folderu',
    '请先进入一个目录': 'Najpierw otwórz folder',
    '加载中…': 'Wczytywanie…',
    '无法读取此目录': 'Nie można odczytać tego folderu',
    '当前目录：': 'Bieżący folder:',
    '(根)': '(katalog główny)',
    '起始位置': 'Punkty startowe',
    '这里是空的': 'Ten folder jest pusty',
    '没有可选的子目录': 'Brak dostępnych podfolderów',
    '上一级': 'Poziom wyżej',
    '运行中': 'W toku',
    '排队': 'W kolejce',
    '已暂停': 'Wstrzymane',
    '已完成': 'Zakończone',
    '失败': 'Niepowodzenie',
    '刷新': 'Odśwież',
    '获取任务状态失败': 'Nie udało się pobrać stanu zadań',
    '任务已提交': 'Zadanie wysłane',
    '可在底部任务栏查看进度': 'Postęp śledź na pasku zadań poniżej',
    '排队中': 'W kolejce',
    '取消中': 'Anulowanie',
    '已取消': 'Anulowane',
    '还没有任务': 'Brak zadań',
    '在上方提交一个任务试试': 'Wyślij jedno powyżej, aby zacząć',
    '任务': 'Zadanie',
    '状态': 'Stan',
    '进度': 'Postęp',
    '开始时间': 'Rozpoczęto',
    '操作': 'Działania',
    '查看日志': 'Pokaż dzienniki',
    '任务 #{id} 日志': 'Dzienniki zadania #{id}',
    '继续': 'Wznów',
    '暂停': 'Wstrzymaj',
    '错误': 'Błąd',
    '失败原因': 'Przyczyna niepowodzenia',
    '日志': 'Dzienniki',
    '重试': 'Ponów',
    '删除': 'Usuń',
    '暂无日志': 'Brak dzienników',
    '没有可展示的数据': 'Brak danych do wyświetlenia',
    '先扫描一个目录': 'Najpierw przeskanuj folder',
    '暂无数据': 'Brak danych',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} godz. {m} min',
  },

  'ru-ru': {
    '会话已失效，正在返回登录页': 'Сеанс истёк, возврат на страницу входа',
    '操作失败': 'Не удалось выполнить операцию',
    '请确认': 'Подтвердите',
    '确认': 'Подтвердить',
    '取消': 'Отмена',
    '请输入 {x} 以确认': 'Введите {x} для подтверждения',
    '选择目录': 'Выбрать папку',
    '选定此目录': 'Использовать эту папку',
    '请先进入一个目录': 'Сначала откройте папку',
    '加载中…': 'Загрузка…',
    '无法读取此目录': 'Не удалось прочитать эту папку',
    '当前目录：': 'Текущая папка:',
    '(根)': '(корень)',
    '起始位置': 'Начальные расположения',
    '这里是空的': 'Эта папка пуста',
    '没有可选的子目录': 'Нет доступных подпапок',
    '上一级': 'На уровень выше',
    '运行中': 'Выполняется',
    '排队': 'В очереди',
    '已暂停': 'Приостановлено',
    '已完成': 'Завершено',
    '失败': 'Ошибка',
    '刷新': 'Обновить',
    '获取任务状态失败': 'Не удалось получить состояние задач',
    '任务已提交': 'Задача отправлена',
    '可在底部任务栏查看进度': 'Следите за ходом на панели задач ниже',
    '排队中': 'В очереди',
    '取消中': 'Отмена',
    '已取消': 'Отменено',
    '还没有任务': 'Задач пока нет',
    '在上方提交一个任务试试': 'Отправьте задачу выше',
    '任务': 'Задача',
    '状态': 'Состояние',
    '进度': 'Прогресс',
    '开始时间': 'Начало',
    '操作': 'Действия',
    '查看日志': 'Показать журнал',
    '任务 #{id} 日志': 'Журнал задачи #{id}',
    '继续': 'Продолжить',
    '暂停': 'Пауза',
    '错误': 'Ошибка',
    '失败原因': 'Причина сбоя',
    '日志': 'Журнал',
    '重试': 'Повторить',
    '删除': 'Удалить',
    '暂无日志': 'Журнала пока нет',
    '没有可展示的数据': 'Нечего показать',
    '先扫描一个目录': 'Сначала просканируйте папку',
    '暂无数据': 'Нет данных',
    '{n} 秒': '{n} с',
    '{m} 分 {s} 秒': '{m} мин {s} с',
    '{h} 小时 {m} 分': '{h} ч {m} мин',
  },

  'tr-tr': {
    '会话已失效，正在返回登录页': 'Oturum süresi doldu, giriş sayfasına dönülüyor',
    '操作失败': 'İşlem başarısız',
    '请确认': 'Lütfen onaylayın',
    '确认': 'Onayla',
    '取消': 'İptal',
    '请输入 {x} 以确认': 'Onaylamak için {x} yazın',
    '选择目录': 'Klasör seçin',
    '选定此目录': 'Bu klasörü kullan',
    '请先进入一个目录': 'Önce bir klasör açın',
    '加载中…': 'Yükleniyor…',
    '无法读取此目录': 'Bu klasör okunamıyor',
    '当前目录：': 'Geçerli klasör:',
    '(根)': '(kök)',
    '起始位置': 'Başlangıç konumları',
    '这里是空的': 'Bu klasör boş',
    '没有可选的子目录': 'Kullanılabilir alt klasör yok',
    '上一级': 'Bir üst düzey',
    '运行中': 'Çalışıyor',
    '排队': 'Kuyrukta',
    '已暂停': 'Duraklatıldı',
    '已完成': 'Tamamlandı',
    '失败': 'Başarısız',
    '刷新': 'Yenile',
    '获取任务状态失败': 'Görev durumu alınamadı',
    '任务已提交': 'Görev gönderildi',
    '可在底部任务栏查看进度': 'İlerlemeyi alttaki görev çubuğundan izleyin',
    '排队中': 'Kuyrukta',
    '取消中': 'İptal ediliyor',
    '已取消': 'İptal edildi',
    '还没有任务': 'Henüz görev yok',
    '在上方提交一个任务试试': 'Başlamak için yukarıdan bir görev gönderin',
    '任务': 'Görev',
    '状态': 'Durum',
    '进度': 'İlerleme',
    '开始时间': 'Başlangıç',
    '操作': 'İşlemler',
    '查看日志': 'Günlükleri gör',
    '任务 #{id} 日志': 'Görev #{id} günlükleri',
    '继续': 'Sürdür',
    '暂停': 'Duraklat',
    '错误': 'Hata',
    '失败原因': 'Hata nedeni',
    '日志': 'Günlükler',
    '重试': 'Yeniden dene',
    '删除': 'Sil',
    '暂无日志': 'Henüz günlük yok',
    '没有可展示的数据': 'Gösterilecek veri yok',
    '先扫描一个目录': 'Önce bir klasör tarayın',
    '暂无数据': 'Veri yok',
    '{n} 秒': '{n} sn',
    '{m} 分 {s} 秒': '{m} dk {s} sn',
    '{h} 小时 {m} 分': '{h} sa {m} dk',
  },

  'pt-pt': {
    '会话已失效，正在返回登录页': 'Sessão expirada, a regressar à página de início de sessão',
    '操作失败': 'A operação falhou',
    '请确认': 'Confirme',
    '确认': 'Confirmar',
    '取消': 'Cancelar',
    '请输入 {x} 以确认': 'Escreva {x} para confirmar',
    '选择目录': 'Escolher uma pasta',
    '选定此目录': 'Usar esta pasta',
    '请先进入一个目录': 'Abra primeiro uma pasta',
    '加载中…': 'A carregar…',
    '无法读取此目录': 'Não é possível ler esta pasta',
    '当前目录：': 'Pasta atual:',
    '(根)': '(raiz)',
    '起始位置': 'Pontos de partida',
    '这里是空的': 'Esta pasta está vazia',
    '没有可选的子目录': 'Não há subpastas disponíveis',
    '上一级': 'Subir um nível',
    '运行中': 'Em execução',
    '排队': 'Em fila',
    '已暂停': 'Em pausa',
    '已完成': 'Concluída',
    '失败': 'Falhou',
    '刷新': 'Atualizar',
    '获取任务状态失败': 'Não foi possível obter o estado das tarefas',
    '任务已提交': 'Tarefa enviada',
    '可在底部任务栏查看进度': 'Acompanhe o progresso na barra de tarefas abaixo',
    '排队中': 'Em fila',
    '取消中': 'A cancelar',
    '已取消': 'Cancelada',
    '还没有任务': 'Ainda não há tarefas',
    '在上方提交一个任务试试': 'Envie uma acima para começar',
    '任务': 'Tarefa',
    '状态': 'Estado',
    '进度': 'Progresso',
    '开始时间': 'Início',
    '操作': 'Ações',
    '查看日志': 'Ver registos',
    '任务 #{id} 日志': 'Registos da tarefa #{id}',
    '继续': 'Retomar',
    '暂停': 'Pausar',
    '错误': 'Erro',
    '失败原因': 'Motivo da falha',
    '日志': 'Registos',
    '重试': 'Repetir',
    '删除': 'Eliminar',
    '暂无日志': 'Ainda sem registos',
    '没有可展示的数据': 'Nada a mostrar',
    '先扫描一个目录': 'Analise primeiro uma pasta',
    '暂无数据': 'Sem dados',
    '{n} 秒': '{n} s',
    '{m} 分 {s} 秒': '{m} min {s} s',
    '{h} 小时 {m} 分': '{h} h {m} min',
  },
};
