/* 洛水壁纸工坊 - 插件页面逻辑（原生 JS，全部走 AstrBot 的 bridge 通道）
 *
 * 说明：面板是用 sandbox iframe 加载插件页面的（origin 为 null），
 * 所以页面里不能依赖 cookie、localStorage 或普通的 fetch ——
 * 数据一律通过 window.AstrBotPluginPage 的 postMessage 通道来要，
 * 图片/视频也由后端转成 data URL 返回后再塞进 <img>/<video>。
 */
(function () {
  "use strict";

  var PLUGIN = "astrbot_plugin_wallpaper_luoshui";
  var THUMB_WIDTH = 480;
  var PREVIEW_WIDTH = 1600;

  var STATE = { items: [], collections: [], config: {}, stats: {}, last_apply: null };
  var TAB = "library";
  var SCAN = { results: [], folderText: "", recursive: true, picked: {}, kind: "all" };
  var LIB = { q: "", kind: "all", picked: {} };
  var JOB_TIMER = null;
  var THUMBS = {};

  var panel = document.getElementById("wp-panel");
  var statLine = document.getElementById("wp-stat");
  var toastEl = document.getElementById("wp-toast");
  var modal = document.getElementById("wp-modal");
  var modalTitle = document.getElementById("wp-modal-title");
  var modalBody = document.getElementById("wp-modal-body");

  // ------------------------------------------------------------------ 通道

  function bridge() {
    return window.AstrBotPluginPage || null;
  }

  function bridgeReady(timeoutMs) {
    return new Promise(function (resolve) {
      if (bridge()) return resolve(bridge());
      var start = Date.now();
      var timer = setInterval(function () {
        if (bridge() || Date.now() - start > (timeoutMs || 8000)) {
          clearInterval(timer);
          resolve(bridge());
        }
      }, 120);
    });
  }

  function fallbackUrl(endpoint, params) {
    var url = new URL("/api/plug/" + PLUGIN + "/" + endpoint, location.origin);
    Object.keys(params || {}).forEach(function (k) {
      if (params[k] !== undefined && params[k] !== null) url.searchParams.set(k, params[k]);
    });
    return url.toString();
  }

  async function call(endpoint, params) {
    var b = bridge();
    if (b && b.apiGet) return b.apiGet(endpoint, params || {});
    var res = await fetch(fallbackUrl(endpoint, params), { credentials: "include" });
    return res.json();
  }

  async function post(endpoint, body) {
    var b = bridge();
    if (b && b.apiPost) return b.apiPost(endpoint, body || {});
    var res = await fetch("/api/plug/" + PLUGIN + "/" + endpoint, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return res.json();
  }

  async function upload(endpoint, file) {
    var b = bridge();
    if (b && b.upload) return b.upload(endpoint, file);
    var fd = new FormData();
    fd.append("file", file);
    var res = await fetch("/api/plug/" + PLUGIN + "/" + endpoint, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    return res.json();
  }

  // ------------------------------------------------------------------ 工具

  function esc(text) {
    return String(text === undefined || text === null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hexToRgba(hex, alpha) {
    var m = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.exec(String(hex || "").trim());
    if (!m) return "";
    var h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16);
    var g = parseInt(h.slice(2, 4), 16);
    var b = parseInt(h.slice(4, 6), 16);
    return "rgba(" + r + ", " + g + ", " + b + ", " + (alpha === undefined ? 1 : alpha) + ")";
  }

  function applyUiTheme(cfg) {
    cfg = cfg || {};
    var body = document.body;
    if (!body) return;
    var main = String(cfg.ui_text_color || "").trim();
    var sub = String(cfg.ui_sub_color || "").trim();
    body.style.removeProperty("--wp-text");
    body.style.removeProperty("--wp-sub");
    if (main) body.style.setProperty("--wp-text", main);
    if (sub) {
      body.style.setProperty("--wp-sub", sub);
    } else if (main) {
      var faded = hexToRgba(main, 0.72);
      if (faded) body.style.setProperty("--wp-sub", faded);
    }
  }

  var UI_THEME_KEY = "wp-ui-theme";
  var UI_SAVE_TIMER = null;

  function readUiColorInputs() {
    return {
      ui_text_color: ((document.getElementById("wp-ui-text-text") || {}).value || "").trim(),
      ui_sub_color: ((document.getElementById("wp-ui-sub-text") || {}).value || "").trim(),
    };
  }

  function saveLocalTheme(theme) {
    try {
      window.localStorage.setItem(UI_THEME_KEY, JSON.stringify(theme || {}));
    } catch (err) {}
  }

  function readLocalTheme() {
    try {
      var raw = window.localStorage.getItem(UI_THEME_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      return data && typeof data === "object" ? data : null;
    } catch (err) {
      return null;
    }
  }

  var LIVE_KEY = "astrwp-live";

  function pushLiveToStorage() {
    Promise.resolve(call("live"))
      .then(function (res) {
        if (!res || !res.success || res.pending || !res.hash) return;
        try {
          window.localStorage.setItem(LIVE_KEY, JSON.stringify(res));
        } catch (err) {}
      })
      .catch(function () {});
  }

  function scheduleUiSave() {
    clearTimeout(UI_SAVE_TIMER);
    UI_SAVE_TIMER = setTimeout(function () {
      post("config", { config: readUiColorInputs() }).then(function (res) {
        if (res && res.success) {
          STATE.config = res.config || STATE.config || {};
          applyUiTheme(STATE.config);
        }
      });
    }, 700);
  }

  function toast(message, ms) {    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () {
      toastEl.hidden = true;
    }, ms || 3200);
  }

  function itemById(id) {
    return (STATE.items || []).filter(function (i) {
      return i.id === id;
    })[0];
  }

  // ------------------------------------------------------------------ 数据

  async function loadState() {
    var res;
    try {
      res = await call("state");
    } catch (err) {
      panel.innerHTML =
        '<div class="muted">读取状态失败：' + esc(err && err.message ? err.message : err) + "</div>";
      return;
    }
    if (res && res.success) {
      STATE = {
        items: res.items || [],
        collections: res.collections || [],
        config: res.config || {},
        stats: res.stats || {},
        last_apply: res.last_apply || null,
        pending_deletes: res.pending_deletes || [],
        duplicates: res.duplicates || 0,
      };
      var serverMain = String(STATE.config.ui_text_color || "").trim();
      var serverSub = String(STATE.config.ui_sub_color || "").trim();
      var local = readLocalTheme();
      if (
        !serverMain &&
        !serverSub &&
        local &&
        (String(local.ui_text_color || "").trim() || String(local.ui_sub_color || "").trim())
      ) {
        applyUiTheme(local);
        post("config", { config: local }).then(function (saved) {
          if (saved && saved.success) {
            STATE.config = saved.config || STATE.config;
            applyUiTheme(STATE.config);
          }
        });
      } else {
        saveLocalTheme({ ui_text_color: serverMain, ui_sub_color: serverSub });
        applyUiTheme(STATE.config);
      }
      renderStat();
      renderPanel();
      pushLiveToStorage();
    } else {
      panel.innerHTML =
        '<div class="muted">读取状态失败' +
        (res && res.message ? "：" + esc(res.message) : "，检查插件是否已经启用") +
        "</div>";
    }
  }

  function renderStat() {
    var s = STATE.stats || {};
    var cfg = STATE.config || {};
    var applied = STATE.last_apply;
    var parts = [
      "库 " + (s.total || 0) + " 张（动态 " + (s.videos || 0) + " / 静态 " + (s.images || 0) + "）",
      "模式 " + (cfg.mode === "fixed" ? "固定" : cfg.mode === "sequence" ? "顺序" : "随机"),
      "遮罩 " + Math.round((cfg.veil_opacity || 0) * 100) + "%",
      "模糊 " + (cfg.blur || 0) + "px",
    ];
    if (applied) {
      parts.push(applied.applied ? "已应用 " + applied.count + " 张" : "未应用：" + (applied.reason || ""));
    }
    parts.push(s.ffmpeg ? "ffmpeg 就绪" + (s.ffmpeg_source ? "（" + s.ffmpeg_source + "）" : "") : "缺少 ffmpeg");
    statLine.textContent = parts.join(" · ");
  }

  // ------------------------------------------------------------------ 缩略图

  function loadThumb(img) {
    var id = img.getAttribute("data-id");
    var filePath = img.getAttribute("data-path");
    if (!id && !filePath) return Promise.resolve();
    var key = id ? "id:" + id : "path:" + filePath;
    if (THUMBS[key]) {
      img.src = THUMBS[key];
      img.classList.add("on");
      return Promise.resolve();
    }
    return call("thumb", id ? { id: id, w: THUMB_WIDTH } : { path: filePath, w: THUMB_WIDTH })
      .then(function (res) {
        if (res && res.data_url) {
          THUMBS[key] = res.data_url;
          img.src = res.data_url;
          img.classList.add("on");
        } else {
          img.classList.add("fail");
        }
      })
      .catch(function () {
        img.classList.add("fail");
      });
  }

  function startThumbs(limit) {
    var imgs = Array.prototype.slice.call(panel.querySelectorAll("img.wp-thumb"));
    if (!imgs.length) return;
    if (limit && imgs.length > limit) imgs = imgs.slice(0, limit);
    var HEAD = 16;
    var queue = imgs.length > HEAD ? imgs.slice(0, HEAD) : imgs.slice();
    var lazy = imgs.length > HEAD ? imgs.slice(HEAD) : [];
    var running = 0;

    function pump() {
      while (running < 3 && queue.length) {
        running++;
        var img = queue.shift();
        loadThumb(img).then(function () {
          running--;
          pump();
        });
      }
    }

    if (lazy.length && window.IntersectionObserver) {
      var io = new IntersectionObserver(
        function (entries) {
          var added = false;
          entries.forEach(function (en) {
            if (en.isIntersecting) {
              io.unobserve(en.target);
              queue.push(en.target);
              added = true;
            }
          });
          if (added) pump();
        },
        { rootMargin: "400px 0px" }
      );
      lazy.forEach(function (img) {
        io.observe(img);
      });
    } else {
      queue = queue.concat(lazy);
    }
    pump();
  }

  // ------------------------------------------------------------------ 渲染

  function renderPanel() {
    if (TAB === "library") panel.innerHTML = renderLibrary();
    else if (TAB === "source") panel.innerHTML = renderSource();
    else if (TAB === "upload") panel.innerHTML = renderUpload();
    else if (TAB === "collections") panel.innerHTML = renderCollections();
    else panel.innerHTML = renderAppearance();
    if (TAB === "library") startThumbs();
    else if (TAB === "source") startThumbs();
    if (TAB === "upload") jobUI(null);
  }

  function collectionOptions(selected) {
    return (STATE.collections || [])
      .map(function (c) {
        return (
          '<option value="' + esc(c.id) + '"' + (c.id === selected ? " selected" : "") + ">" + esc(c.name) + "</option>"
        );
      })
      .join("");
  }

  function libraryPickedIds() {
    return Object.keys(LIB.picked).filter(function (k) {
      return LIB.picked[k];
    });
  }

  function filteredItems() {
    var q = String(LIB.q || "").trim().toLowerCase();
    var kind = LIB.kind || "all";
    return (STATE.items || []).filter(function (it) {
      var itemKind = it.kind === "video" ? "video" : "image";
      if (kind !== "all" && itemKind !== kind) return false;
      if (!q) return true;
      var hay = [
        it.name,
        it.file,
        it.source,
        it.note,
        it.origin === "upload" ? "上传" : "扫描",
        itemKind === "video" ? "动态 视频" : "静态 图片",
      ]
        .join(" ")
        .toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  function libraryGrid() {
    var items = filteredItems();
    if (!items.length) {
      return '<div class="muted">没有对上的壁纸，换个关键词，或者把筛选切回「全部」。</div>';
    }
    return items
      .map(function (it) {
        var isVideo = it.kind === "video";
        var meta = [];
        if (it.width) meta.push(it.width + "x" + it.height);
        if (it.duration) meta.push(it.duration + "s");
        if (it.size) meta.push(it.size + "MB");
        meta.push(it.origin === "upload" ? "上传" : "扫描");
        return (
          '<div class="card">' +
          '<div class="thumb" data-act="preview" data-id="' + esc(it.id) + '">' +
          '<img class="wp-thumb" data-id="' + esc(it.id) + '" alt="">' +
          '<span class="badge">' + (isVideo ? "动态" : "静态") + "</span>" +
          (it.sound ? '<span class="badge sound">有声</span>' : "") +
          '<label class="pick" title="勾上这张"><input type="checkbox" data-role="lib-pick" data-id="' +
          esc(it.id) + '"' + (LIB.picked[it.id] ? " checked" : "") + "></label>" +
          "</div>" +
          '<div class="info">' +
          '<div class="name" title="' + esc(it.name) + '">' + esc(it.name) + "</div>" +
          '<div class="meta">' + esc(meta.join(" · ")) + "</div>" +
          "</div>" +
          '<div class="ops">' +
          '<button class="btn small primary" data-act="apply-item" data-id="' + esc(it.id) + '">应用</button>' +
          '<button class="btn small" data-act="rename" data-id="' + esc(it.id) + '">改名</button>' +
          "</div>" +
          '<div class="ops">' +
          '<select data-act="collect" data-id="' + esc(it.id) + '">' +
          '<option value="">加入集合…</option>' +
          collectionOptions(null) +
          "</select>" +
          '<button class="btn small danger" data-act="delete-item" data-id="' + esc(it.id) + '">删除</button>' +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  function updateLibCount() {
    var el = document.getElementById("wp-lib-count");
    if (!el) return;
    var pend = (STATE.pending_deletes || []).length;
    el.textContent =
      "匹配 " + filteredItems().length + " 张，已勾 " + libraryPickedIds().length + " 张 · 批量操作只作用于勾上的" +
      (pend ? " · 有 " + pend + " 个文件被占用，正在自动重删" : "");
  }

  function refreshLibGrid() {
    var grid = document.getElementById("wp-lib-grid");
    if (grid) grid.innerHTML = libraryGrid();
    updateLibCount();
    startThumbs();
  }

  function renderLibrary() {
    var items = STATE.items || [];
    if (!items.length) {
      return '<div class="muted">壁纸库还是空的。先去「来源文件夹」扫描导入，或者到「上传壁纸」传几张。</div>';
    }
    return (
      '<div class="toolbar">' +
      '<input type="text" id="wp-lib-q" class="grow" placeholder="搜壁纸：名字、文件名、来源路径" value="' + esc(LIB.q) + '">' +
      '<select id="wp-lib-kind">' +
      '<option value="all"' + (LIB.kind === "all" ? " selected" : "") + ">全部</option>" +
      '<option value="video"' + (LIB.kind === "video" ? " selected" : "") + ">只看动态</option>" +
      '<option value="image"' + (LIB.kind === "image" ? " selected" : "") + ">只看静态</option>" +
      "</select>" +
      '<button class="btn ghost small" data-act="lib-all">全选</button>' +
      '<button class="btn ghost small" data-act="lib-none">全不选</button>' +
      '<button class="btn ghost small" data-act="lib-invert">反选</button>' +
      '<select data-act="lib-collect"><option value="">批量加入集合…</option>' + collectionOptions(null) + "</select>" +
      '<button class="btn small danger" data-act="lib-delete">批量删除</button>' +
      (Number(STATE.duplicates) > 0
        ? '<button class="btn small" data-act="lib-dedupe">清理重复(' + STATE.duplicates + ")</button>"
        : "") +
      "</div>" +
      '<div class="row" style="margin-bottom:12px">' +
      '<button class="btn primary" data-act="apply">把当前轮换集全部应用到面板</button>' +
      '<span class="muted" id="wp-lib-count"></span>' +
      "</div>" +
      '<div class="grid" id="wp-lib-grid">' + libraryGrid() + "</div>"
    );
  }

  function scanPickedCount() {
    return Object.keys(SCAN.picked).filter(function (k) {
      return SCAN.picked[k];
    }).length;
  }

  function visibleScanRows() {
    var kind = SCAN.kind || "all";
    return (SCAN.results || [])
      .map(function (r, idx) {
        return { r: r, idx: idx };
      })
      .filter(function (x) {
        if (kind === "all") return true;
        return (x.r.kind === "video" ? "video" : "image") === kind;
      });
  }

  function scanPickState(idx) {
    if (SCAN.picked[idx] === undefined) {
      var r = SCAN.results[idx] || {};
      SCAN.picked[idx] = !r.imported;
    }
    return !!SCAN.picked[idx];
  }

  function scanRowsHtml() {
    var rows = visibleScanRows();
    if (!rows.length) {
      return '<div class="muted" style="padding:12px">当前筛选下没有文件，把上面的显示切回「全部」看看。</div>';
    }
    return rows
      .map(function (x) {
        var r = x.r;
        var idx = x.idx;
        return (
          '<div class="list-item">' +
          '<input type="checkbox" data-role="scan-pick" data-idx="' + idx + '"' + (scanPickState(idx) ? " checked" : "") + ">" +
          '<img class="wp-thumb wp-mini" data-act="peek" data-path="' + esc(r.path) + '" alt="">' +
          '<span class="grow">' + esc(r.name) + '<br><span class="tag">' + esc(r.path) + "</span></span>" +
          '<span class="tag">' + (r.kind === "video" ? "动态" : "静态") + " · " + r.size + "MB</span>" +
          (r.imported ? '<span class="tag-pill">已导入</span>' : "") +
          "</div>"
        );
      })
      .join("");
  }

  function scanToolbar() {
    return (
      '<div class="toolbar">' +
      '<span class="muted" id="wp-scan-count">显示 ' +
      visibleScanRows().length +
      " / " +
      (SCAN.results || []).length +
      " 个，已勾 " +
      scanPickedCount() +
      " 个</span>" +
      '<select id="wp-scan-kind">' +
      '<option value="all"' + (SCAN.kind === "all" ? " selected" : "") + ">全部</option>" +
      '<option value="video"' + (SCAN.kind === "video" ? " selected" : "") + ">只看动态</option>" +
      '<option value="image"' + (SCAN.kind === "image" ? " selected" : "") + ">只看静态</option>" +
      "</select>" +
      '<button class="btn ghost small" data-act="scan-all">全选</button>' +
      '<button class="btn ghost small" data-act="scan-none">全不选</button>' +
      '<button class="btn ghost small" data-act="scan-video">只选动态</button>' +
      '<button class="btn ghost small" data-act="scan-image">只选静态</button>' +
      '<label class="switch"><input type="checkbox" id="wp-import-sound"> 导入时保留声音</label>' +
      '<button class="btn primary small" data-act="import">导入选中</button>' +
      "</div>"
    );
  }

  function refreshScanRows() {
    var box = document.getElementById("wp-scan-rows");
    if (box) box.innerHTML = scanRowsHtml();
    var cnt = document.getElementById("wp-scan-count");
    if (cnt) {
      cnt.textContent =
        "显示 " + visibleScanRows().length + " / " + (SCAN.results || []).length + " 个，已勾 " + scanPickedCount() + " 个";
    }
    if (!box) renderPanel();
    startThumbs();
  }

  function renderSource() {
    var cfg = STATE.config || {};
    var folders = Array.isArray(cfg.folders) ? cfg.folders : [];
    var text = SCAN.folderText || folders.join("\n");
    var total = (SCAN.results || []).length;
    var body = total
      ? '<div class="list" id="wp-scan-rows">' + scanRowsHtml() + "</div>"
      : '<div class="muted">点上面的「扫描」看看能识别到什么。视频会在导入时自动转成网页版 mp4。</div>';
    return (
      '<div class="field">' +
      '<label>壁纸数据文件夹（一行一个，支持 Wallpaper Engine 的 steamapps\\workshop\\content\\431960）</label>' +
      '<textarea id="wp-folders" placeholder="D:\\steam\\steamapps\\workshop\\content\\431960">' + esc(text) + "</textarea>" +
      '<div class="row">' +
      '<label class="switch"><input type="checkbox" id="wp-recursive"' + (SCAN.recursive ? " checked" : "") + "> 递归扫描子文件夹</label>" +
      '<button class="btn" data-act="save-folders">保存路径</button>' +
      '<button class="btn primary" data-act="scan">扫描</button>' +
      "</div></div>" +
      '<div class="field"><label>扫描结果（' + total + " 个）</label>" +
      (total ? scanToolbar() : "") +
      body +
      "</div>"
    );
  }

  function renderUpload() {
    return (
      '<div class="split">' +
      '<div class="box"><h3>本地上传</h3>' +
      '<div class="field"><label>选择图片或视频（可多选）</label>' +
      '<input type="file" id="wp-file" multiple accept="image/*,video/*"></div>' +
      '<div class="row">' +
      '<label class="switch"><input type="checkbox" id="wp-upload-sound"> 保留声音（动态壁纸）</label>' +
      "</div>" +
      '<div class="row" style="margin-top:12px">' +
      '<button class="btn primary" data-act="upload">上传并导入</button>' +
      '<button class="btn ghost" data-act="reload">刷新</button>' +
      "</div>" +
      '<div class="progress" id="wp-job-progress"><i style="width:0"></i></div>' +
      '<div class="muted" id="wp-job-log" style="margin-top:8px">转码要花点时间，视频越大越久。</div>' +
      "</div>" +
      '<div class="box"><h3>说明</h3>' +
      ffmpegBox() +
      '<p class="muted">静态图会缩到 2560 宽以内；mp4 / webm / mov 会重新压成网页能播放的视频（勾了保留声音就带 aac 音轨）。</p>' +
      '<p class="muted">浏览器默认会拦截带声音的自动播放，所以开了声音之后需要在面板页面点一下才会响，右下角会出现一个小开关。</p>' +
      "</div></div>"
    );
  }

  function ffmpegBox() {
    var s = STATE.stats || {};
    if (s.ffmpeg) {
      return (
        '<p class="muted">ffmpeg：已就绪（' +
        esc(s.ffmpeg_source || "本机") +
        "）<br>" +
        esc(s.ffmpeg) +
        "</p>"
      );
    }
    return (
      '<p class="muted">没检测到 ffmpeg，动态壁纸就转不了码。点下面的按钮自动装一份到插件包里，' +
      "或者手动把 ffmpeg.exe 丢进插件目录的 bin 文件夹、AstrBot 根目录都行。</p>" +
      '<div class="row" style="margin-bottom:10px">' +
      '<button class="btn small primary" data-act="ffmpeg-install">自动安装 ffmpeg</button>' +
      '<button class="btn small ghost" data-act="ffmpeg-check">重新检测</button>' +
      "</div>"
    );
  }

  function renderCollections() {
    var cfg = STATE.config || {};
    var colls = STATE.collections || [];
    var active = cfg.active_collection || "";
    var rows = colls
      .map(function (c) {
        var isActive = c.name === active || c.id === active;
        return (
          '<div class="list-item">' +
          '<span class="grow">' + esc(c.name) + ' <span class="tag">' + (c.items || []).length + " 张</span>" +
          (isActive ? ' <span class="tag-pill">当前</span>' : "") +
          "</span>" +
          '<button class="btn small" data-act="use-collection" data-key="' + esc(c.id) + '">设为当前</button>' +
          '<button class="btn small" data-act="rename-collection" data-key="' + esc(c.id) + '" data-name="' + esc(c.name) + '">改名</button>' +
          (c.id === "default" ? "" : '<button class="btn small danger" data-act="delete-collection" data-key="' + esc(c.id) + '">删除</button>') +
          "</div>"
        );
      })
      .join("") || '<div class="muted">还没有轮换集。</div>';

    var fixedList = (STATE.items || [])
      .map(function (i) {
        return '<option value="' + esc(i.id) + '"' + (cfg.fixed_item === i.id || cfg.fixed_item === i.name ? " selected" : "") + ">" + esc(i.name) + "</option>";
      })
      .join("");

    return (
      '<div class="split">' +
      '<div class="box"><h3>壁纸集</h3>' +
      '<div class="row" style="margin-bottom:10px">' +
      '<input type="text" id="wp-new-collection" placeholder="新集合名字">' +
      '<button class="btn primary" data-act="add-collection">新建</button>' +
      "</div>" +
      '<div class="list">' + rows + "</div>" +
      '<p class="muted" style="margin-top:10px">在「壁纸库」的卡片上可以把壁纸丢进某个集合。</p>' +
      "</div>" +
      '<div class="box"><h3>轮换设置</h3>' +
      '<div class="field"><label>模式</label><select id="wp-mode">' +
      '<option value="random"' + (cfg.mode === "random" || !cfg.mode ? " selected" : "") + ">每次刷新随机换一张</option>" +
      '<option value="sequence"' + (cfg.mode === "sequence" ? " selected" : "") + ">按顺序轮换</option>" +
      '<option value="fixed"' + (cfg.mode === "fixed" ? " selected" : "") + ">固定一张</option>" +
      "</select></div>" +
      '<div class="field"><label>当前轮换集</label><select id="wp-active-collection">' +
      '<option value="">全部壁纸</option>' + collectionOptions(null) +
      "</select></div>" +
      '<div class="field"><label>固定壁纸（模式选固定时生效）</label><select id="wp-fixed"><option value="">不指定</option>' + fixedList + "</select></div>" +
      '<div class="field"><label>自动切换间隔（秒，0 表示只随刷新换）</label><input type="number" id="wp-interval" min="0" max="3600" value="' + (cfg.switch_interval || 0) + '"></div>' +
      '<button class="btn primary" data-act="save-rotation">保存轮换设置</button>' +
      "</div></div>"
    );
  }

  function renderAppearance() {
    var cfg = STATE.config || {};
    var color = cfg.text_color || "";
    return (
      '<div class="split">' +
      '<div class="box"><h3>界面外观</h3>' +
      '<div class="field"><label>界面字体颜色</label>' +
      '<div class="row"><input type="color" id="wp-text-color" value="' + esc(color || "#e8f0ff") + '">' +
      '<input type="text" id="wp-text-color-text" placeholder="留空表示不改字体颜色" value="' + esc(color) + '"></div>' +
      '<div class="hint">填 #rrggbb，清空就是恢复面板默认文字颜色</div></div>' +
      '<div class="field"><label>插件页面文字颜色</label>' +
      '<div class="row"><input type="color" id="wp-ui-text" value="' + esc(cfg.ui_text_color || "#e6edf3") + '">' +
      '<input type="text" id="wp-ui-text-text" placeholder="留空表示用默认色" value="' + esc(cfg.ui_text_color || "") + '"></div>' +
      '<div class="hint">只改「壁纸工坊」这个页面里的字，不碰面板本身；松手就自动保存</div></div>' +
      '<div class="field"><label>插件页面次要文字颜色</label>' +
      '<div class="row"><input type="color" id="wp-ui-sub" value="' + esc(cfg.ui_sub_color || "#9aa7b4") + '">' +
      '<input type="text" id="wp-ui-sub-text" placeholder="留空则跟着主色自动变淡" value="' + esc(cfg.ui_sub_color || "") + '"></div>' +
      '<div class="hint">路径、说明、提示这些小字；留空就按主色 72% 的透明度走</div></div>' +
      '<label class="switch"><input type="checkbox" id="wp-force-color"' + (cfg.force_text_color ? " checked" : "") + "> 强制覆盖全部文字（含按钮、标签）</label>" +
      '<div class="field" style="margin-top:14px"><label>遮罩透明度：<span id="wp-veil-val">' + (cfg.veil_opacity || 0) + "</span></label>" +
      '<input type="range" id="wp-veil" min="0" max="0.9" step="0.01" value="' + (cfg.veil_opacity || 0) + '">' +
      '<div class="hint">面板上那层白色遮罩，越大字越清楚、壁纸越淡</div></div>' +
      '<div class="field"><label>壁纸模糊程度：<span id="wp-blur-val">' + (cfg.blur || 0) + "px</span></label>" +
      '<input type="range" id="wp-blur" min="0" max="40" step="1" value="' + (cfg.blur || 0) + '"></div>' +
      '<div class="field"><label>切换过渡时长：<span id="wp-fade-val">' + (cfg.fade_ms == null ? 900 : cfg.fade_ms) + "ms</span></label>" +
      '<input type="range" id="wp-fade" min="0" max="2500" step="100" value="' + (cfg.fade_ms == null ? 900 : cfg.fade_ms) + '">' +
      '<div class="hint">换壁纸时两张图交叉淡化的时长，0 表示直接切、不做过渡</div></div>' +
      '<div class="field"><label>页面底色（壁纸加载前的颜色）</label>' +
      '<input type="text" id="wp-bg" value="' + esc(cfg.background_color || "#080a0f") + '"></div>' +
      '<label class="switch"><input type="checkbox" id="wp-shadow-off"' + (cfg.text_shadow_off ? " checked" : "") + "> 去掉文字发光和阴影</label>" +
      "</div>" +
      '<div class="box"><h3>动态壁纸</h3>' +
      '<label class="switch"><input type="checkbox" id="wp-enable-video"' + (cfg.enable_video ? " checked" : "") + "> 启用动态壁纸（mp4）</label>" +
      '<label class="switch" style="margin-top:8px"><input type="checkbox" id="wp-video-sound"' + (cfg.video_sound ? " checked" : "") + "> 动态壁纸开启声音</label>" +
      '<div class="field" style="margin-top:14px"><label>转码宽度</label><input type="number" id="wp-video-width" min="960" max="2560" step="160" value="' + (cfg.video_width || 1920) + '"></div>' +
      '<div class="field"><label>视频质量 CRF（越小越清晰）</label><input type="number" id="wp-video-crf" min="18" max="34" step="1" value="' + (cfg.video_crf || 27) + '"></div>' +
      '<label class="switch"><input type="checkbox" id="wp-keep-audio"' + (cfg.keep_audio ? " checked" : "") + "> 转码时保留音轨</label>" +
      '<div class="hint" style="margin-top:10px">这些改完点保存，面板上会自己同步；万一没动静，Ctrl+F5 一次就好。</div>' +
      "</div>" +
      '<div class="box"><h3>开关</h3>' +
      '<label class="switch"><input type="checkbox" id="wp-enabled"' + (cfg.enabled ? " checked" : "") + "> 启用壁纸注入</label>" +
      '<label class="switch" style="margin-top:8px"><input type="checkbox" id="wp-watch"' + (cfg.watch_config ? " checked" : "") + "> 监听插件设置里的改动并自动应用</label>" +
      '<label class="switch" style="margin-top:8px"><input type="checkbox" id="wp-debug"' + (cfg.debug_log ? " checked" : "") + "> 输出调试日志</label>" +
      '<div class="row" style="margin-top:16px">' +
      '<button class="btn primary" data-act="save-appearance">保存并应用</button>' +
      '<button class="btn ghost" data-act="apply">仅重新应用</button>' +
      "</div></div></div>"
    );
  }

  // ------------------------------------------------------------------ 任务

  function jobUI(job) {
    var bar = document.querySelector("#wp-job-progress > i");
    var log = document.getElementById("wp-job-log");
    if (!job) {
      if (bar) bar.style.width = "0";
      return;
    }
    if (bar) {
      var pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      bar.style.width = Math.min(100, pct) + "%";
    }
    if (log) {
      var lines = job.logs && job.logs.length ? job.logs.slice(-2).join(" ｜ ") : "";
      log.textContent = (job.current ? "正在处理 " + job.current + " " : "") + (job.message || lines);
    }
  }

  function watchJob(jobId, afterTab) {
    if (JOB_TIMER) clearInterval(JOB_TIMER);
    JOB_TIMER = setInterval(async function () {
      var res = await call("job", { id: jobId });
      if (!res || !res.success) {
        clearInterval(JOB_TIMER);
        JOB_TIMER = null;
        return;
      }
      jobUI(res.job);
      if (res.job.state !== "running") {
        clearInterval(JOB_TIMER);
        JOB_TIMER = null;
        toast(res.job.message || (res.job.state === "done" ? "处理完成" : "处理失败"));
        if (afterTab && afterTab !== TAB) {
          TAB = afterTab;
          syncTabs();
        }
        await loadState();
      }
    }, 1500);
  }

  // ------------------------------------------------------------------ 预览

  async function openPreview(id) {
    var item = itemById(id);
    if (!item) return;
    modalTitle.textContent = item.name + "（" + (item.kind === "video" ? "动态" : "静态") + "）";
    modalBody.innerHTML = '<div class="wp-loading">正在加载预览…</div>';
    modal.hidden = false;

    if (item.kind === "video") {
      var res = await call("preview", { id: id, kind: "video" });
      if (res && res.data_url) {
        modalBody.innerHTML =
          '<video src="' + res.data_url + '" controls autoplay loop muted playsinline></video>' +
          '<p class="muted" style="text-align:center;margin-top:8px">预览里的视频是静音的，大小 ' + (res.size_mb || "?") + "MB</p>";
      } else {
        modalBody.innerHTML = '<p class="muted">' + esc((res && res.message) || "预览加载失败") + "</p>";
      }
      return;
    }

    var res2 = await call("preview", { id: id, kind: "still", w: PREVIEW_WIDTH });
    if (res2 && res2.data_url) {
      modalBody.innerHTML = '<img src="' + res2.data_url + '" alt="">';
    } else {
      modalBody.innerHTML = '<p class="muted">' + esc((res2 && res2.message) || "预览加载失败") + "</p>";
    }
  }

  function closeModal() {
    modal.hidden = true;
    modalBody.innerHTML = "";
  }

  // ------------------------------------------------------------------ 动作

  // 浏览器把 window.confirm 拦掉的时候（勾了「阻止此页面的其他对话框」），
  // 删除会变成什么都不发生。改成按钮自己点两次确认，不靠浏览器弹窗。
  function armConfirm(el, word) {
    if (!el) return true;
    if (el.getAttribute("data-armed") === "1") {
      el.removeAttribute("data-armed");
      if (el.getAttribute("data-old-text")) el.textContent = el.getAttribute("data-old-text");
      return true;
    }
    el.setAttribute("data-armed", "1");
    if (!el.getAttribute("data-old-text")) el.setAttribute("data-old-text", el.textContent);
    el.textContent = "再点一次" + word;
    toast("再点一下才" + word + "，防止点错");
    setTimeout(function () {
      if (el.getAttribute("data-armed") === "1") {
        el.removeAttribute("data-armed");
        if (el.getAttribute("data-old-text")) el.textContent = el.getAttribute("data-old-text");
      }
    }, 4000);
    return false;
  }

  async function doAction(act, id, el) {
    if (act === "peek") {
      var peekPath = el ? el.getAttribute("data-path") : "";
      if (!peekPath) return;
      modalTitle.textContent = peekPath;
      modalBody.innerHTML = '<div class="wp-loading">加载预览…</div>';
      modal.hidden = false;
      var peekRes = await call("thumb", { path: peekPath, w: 1600 });
      modalBody.innerHTML =
        peekRes && peekRes.data_url
          ? '<img src="' + peekRes.data_url + '" alt="">'
          : '<p class="muted">' + esc((peekRes && peekRes.message) || "预览加载失败") + "</p>";
      return;
    }

    if (act === "reload") return loadState();

    if (act === "apply") {
      var res1 = await post("apply", {});
      toast(
        res1 && res1.applied && res1.applied.applied
          ? "已应用 " + res1.applied.count + " 张，面板上马上生效"
          : (res1 && res1.applied && res1.applied.reason) || "应用失败"
      );
      return loadState();
    }

    if (act === "apply-item") {
      var res2 = await post("config", { config: { mode: "fixed", fixed_item: id } });
      toast(res2 && res2.success ? "已经把这张设成固定壁纸，面板上马上切换" : "设置失败");
      return loadState();
    }

    if (act === "rename") {
      var item = itemById(id);
      var name = window.prompt("新的名字", item ? item.name : "");
      if (!name) return;
      await post("item/update", { id: id, name: name });
      toast("改好了");
      return loadState();
    }

    if (act === "delete-item") {
      if (!armConfirm(el, "删除")) return;
      var delOne = await post("item/delete", { id: id });
      delete THUMBS["id:" + id];
      if (delOne && delOne.message) toast(delOne.message);
      else toast(delOne && delOne.success ? "删掉了" : "没删掉，刷新一下再试");
      return loadState();
    }

    if (act === "preview") {
      openPreview(id).catch(function (err) {
        modalBody.innerHTML = '<p class="muted">' + esc(err && err.message ? err.message : err) + "</p>";
      });
      return;
    }

    if (act === "save-folders") {
      var box = document.getElementById("wp-folders");
      var folders = (box ? box.value : "")
        .split(/\r?\n/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      SCAN.folderText = folders.join("\n");
      await post("config", { config: { folders: folders } });
      toast("路径已保存");
      return loadState();
    }

    if (act === "scan") {
      var box2 = document.getElementById("wp-folders");
      var folders2 = (box2 ? box2.value : "")
        .split(/\r?\n/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      var rec = document.getElementById("wp-recursive");
      SCAN.folderText = folders2.join("\n");
      SCAN.recursive = rec ? rec.checked : true;
      panel.innerHTML = '<div class="wp-loading">扫描中…</div>';
      var res3 = await post("scan", { folders: folders2, recursive: SCAN.recursive });
      SCAN.results = (res3 && res3.results) || [];
      SCAN.picked = {};
      if (!SCAN.results.length) toast((res3 && res3.message) || "没扫到东西，检查一下路径");
      else toast("扫到 " + SCAN.results.length + " 个文件");
      renderPanel();
      return;
    }

    if (act === "scan-all" || act === "scan-none" || act === "scan-video" || act === "scan-image") {
      var rows = visibleScanRows();
      if (act === "scan-video" || act === "scan-image") {
        // 只选动态 / 只选静态：先全部取消，再把对应类型勾上
        Object.keys(SCAN.picked).forEach(function (k) {
          SCAN.picked[k] = false;
        });
        rows.forEach(function (x) {
          var isVideo = x.r.kind === "video";
          if (act === "scan-video" ? isVideo : !isVideo) SCAN.picked[x.idx] = true;
        });
        toast(act === "scan-video" ? "只勾上动态壁纸了" : "只勾上静态壁纸了");
      } else {
        var want = act === "scan-all";
        rows.forEach(function (x) {
          SCAN.picked[x.idx] = want;
        });
      }
      refreshScanRows();
      return;
    }

    if (act === "import") {
      var picked = [];
      (SCAN.results || []).forEach(function (r, idx) {
        if (SCAN.picked[idx]) picked.push(r.path);
      });
      if (!picked.length) return toast("先勾几条要导入的");
      var sound = (document.getElementById("wp-import-sound") || {}).checked || false;
      var res4 = await post("import", { paths: picked, sound: sound });
      if (res4 && res4.job_id) {
        toast("开始导入 " + picked.length + " 个，完成后自动回壁纸库");
        TAB = "upload";
        syncTabs();
        renderPanel();
        watchJob(res4.job_id, "library");
      } else {
        toast((res4 && res4.message) || "导入失败");
      }
      return;
    }

    if (act === "lib-all" || act === "lib-none" || act === "lib-invert") {
      var list = filteredItems();
      list.forEach(function (it) {
        if (act === "lib-invert") LIB.picked[it.id] = !LIB.picked[it.id];
        else LIB.picked[it.id] = act === "lib-all";
      });
      refreshLibGrid();
      toast(
        act === "lib-invert"
          ? "反选好了"
          : (act === "lib-all" ? "当前筛出来的都勾上了" : "全不选了")
      );
      return;
    }

    if (act === "lib-delete") {
      var delIds = libraryPickedIds();
      if (!delIds.length) return toast("先在壁纸库里勾几张");
      if (!armConfirm(el, "删掉勾选的 " + delIds.length + " 张")) return;
      var delRes = await post("items/batch", { action: "delete", ids: delIds });
      if (delRes && delRes.success) {
        delIds.forEach(function (id) {
          delete THUMBS["id:" + id];
          LIB.picked[id] = false;
        });
        toast(delRes.message || "删掉了");
      } else {
        toast((delRes && delRes.message) || "批量删除失败");
      }
      return loadState();
    }

    if (act === "lib-dedupe") {
      var dupN = Number(STATE.duplicates) || 0;
      if (!dupN) return toast("壁纸库里没有重复的");
      if (!armConfirm(el, "清掉 " + dupN + " 张重复的")) return;
      var dupRes = await post("items/batch", { action: "dedupe", ids: [] });
      toast((dupRes && dupRes.message) || "清理完了");
      return loadState();
    }

    if (act === "ffmpeg-check" || act === "ffmpeg-install") {
      var ffRes = await post("ffmpeg", { action: act === "ffmpeg-install" ? "install" : "check" });
      if (ffRes && ffRes.job_id) {
        toast("开始下载 ffmpeg，装好会告诉你");
        watchJob(ffRes.job_id, TAB);
        return;
      }
      toast((ffRes && ffRes.message) || (ffRes && ffRes.ffmpeg ? "ffmpeg 就绪" : "还没找到 ffmpeg"));
      return loadState();
    }

    if (act === "upload") {
      var input = document.getElementById("wp-file");
      var files = input && input.files ? Array.prototype.slice.call(input.files) : [];
      if (!files.length) return toast("先选几个文件");
      var keepSound = (document.getElementById("wp-upload-sound") || {}).checked || false;
      if (keepSound) await post("config", { config: { keep_audio: true } });
      var okCount = 0;
      for (var i = 0; i < files.length; i++) {
        var up = await upload("upload", files[i]);
        if (up && up.job_id) {
          okCount++;
          watchJob(up.job_id);
        }
      }
      toast(okCount ? "已开始处理 " + okCount + " 个文件" : "上传失败");
      return;
    }

    if (act === "add-collection") {
      var nameBox = document.getElementById("wp-new-collection");
      var name = nameBox ? nameBox.value.trim() : "";
      if (!name) return toast("先起个名字");
      await post("collection/save", { name: name });
      toast("建好了");
      return loadState();
    }

    if (act === "rename-collection") {
      var newName = window.prompt("集合新名字", el.getAttribute("data-name") || "");
      if (!newName) return;
      await post("collection/save", { key: el.dataset.key, name: newName });
      toast("改好了");
      return loadState();
    }

    if (act === "delete-collection") {
      if (!window.confirm("删除这个集合？（壁纸本身不会被删）")) return;
      await post("collection/delete", { key: el.dataset.key });
      toast("删掉了");
      return loadState();
    }

    if (act === "use-collection") {
      var key = el.dataset.key;
      var coll = (STATE.collections || []).filter(function (c) { return c.id === key; })[0];
      await post("config", { config: { active_collection: coll ? coll.name : key } });
      toast("当前轮换集换成 " + (coll ? coll.name : key));
      return loadState();
    }

    if (act === "save-rotation") {
      var mode = (document.getElementById("wp-mode") || {}).value || "random";
      var activeSel = document.getElementById("wp-active-collection") || {};
      var activeColl = (STATE.collections || []).filter(function (c) { return c.id === activeSel.value; })[0];
      var fixed = (document.getElementById("wp-fixed") || {}).value || "";
      var interval = parseInt((document.getElementById("wp-interval") || {}).value || "0", 10) || 0;
      await post("config", {
        config: {
          mode: mode,
          active_collection: activeColl ? activeColl.name : "",
          fixed_item: fixed,
          switch_interval: interval,
        },
      });
      toast("轮换设置已保存");
      return loadState();
    }

    if (act === "save-appearance") {
      var textColor = ((document.getElementById("wp-text-color-text") || {}).value || "").trim();
      var patch = {
        text_color: textColor,
        ui_text_color: ((document.getElementById("wp-ui-text-text") || {}).value || "").trim(),
        ui_sub_color: ((document.getElementById("wp-ui-sub-text") || {}).value || "").trim(),
        force_text_color: !!(document.getElementById("wp-force-color") || {}).checked,
        veil_opacity: parseFloat((document.getElementById("wp-veil") || {}).value || "0.1"),
        blur: parseInt((document.getElementById("wp-blur") || {}).value || "0", 10) || 0,
        fade_ms: (function () {
          var raw = parseInt((document.getElementById("wp-fade") || {}).value, 10);
          return isNaN(raw) ? 900 : Math.max(0, Math.min(raw, 5000));
        })(),
        background_color: ((document.getElementById("wp-bg") || {}).value || "#080a0f").trim(),
        text_shadow_off: !!(document.getElementById("wp-shadow-off") || {}).checked,
        enable_video: !!(document.getElementById("wp-enable-video") || {}).checked,
        video_sound: !!(document.getElementById("wp-video-sound") || {}).checked,
        video_width: parseInt((document.getElementById("wp-video-width") || {}).value || "1920", 10) || 1920,
        video_crf: parseInt((document.getElementById("wp-video-crf") || {}).value || "27", 10) || 27,
        keep_audio: !!(document.getElementById("wp-keep-audio") || {}).checked,
        enabled: !!(document.getElementById("wp-enabled") || {}).checked,
        watch_config: !!(document.getElementById("wp-watch") || {}).checked,
        debug_log: !!(document.getElementById("wp-debug") || {}).checked,
      };
      var saved = await post("config", { config: patch });
      if (saved && saved.success) {
        var applied = saved.applied || {};
        toast(applied.applied ? "保存成功，已应用 " + applied.count + " 张" : "保存成功，" + (applied.reason || "等待壁纸"));
      } else {
        toast("保存失败");
      }
      return loadState();
    }
  }

  function syncTabs() {
    var nav = document.getElementById("wp-tabs");
    Array.prototype.forEach.call(nav.querySelectorAll("button"), function (b) {
      b.classList.toggle("on", b.dataset.tab === TAB);
    });
  }

  // ------------------------------------------------------------------ 事件

  document.getElementById("wp-tabs").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    TAB = btn.dataset.tab;
    syncTabs();
    renderPanel();
  });

  panel.addEventListener("click", function (e) {
    if (e.target.closest("input, label.pick")) return;
    var btn = e.target.closest("[data-act]");
    if (!btn || btn.tagName === "SELECT") return;
    doAction(btn.dataset.act, btn.dataset.id, btn).catch(function (err) {
      toast("操作失败：" + (err && err.message ? err.message : err));
    });
  });

  panel.addEventListener("change", async function (e) {
    if (e.target.closest('[data-role="lib-pick"]')) {
      LIB.picked[e.target.getAttribute("data-id")] = e.target.checked;
      updateLibCount();
      return;
    }
    if (e.target.closest('[data-role="scan-pick"]')) {
      SCAN.picked[e.target.dataset.idx] = e.target.checked;
      var cntEl = document.getElementById("wp-scan-count");
      if (cntEl) {
        cntEl.textContent =
          "显示 " + visibleScanRows().length + " / " + (SCAN.results || []).length + " 个，已勾 " + scanPickedCount() + " 个";
      }
      return;
    }
    if (e.target.id === "wp-lib-kind") {
      LIB.kind = e.target.value;
      refreshLibGrid();
      return;
    }
    if (e.target.id === "wp-scan-kind") {
      SCAN.kind = e.target.value;
      refreshScanRows();
      return;
    }
    var bulkSel = e.target.closest('select[data-act="lib-collect"]');
    if (bulkSel && bulkSel.value) {
      var bulkIds = libraryPickedIds();
      if (!bulkIds.length) {
        bulkSel.value = "";
        return toast("先在壁纸库里勾几张");
      }
      var bulkRes = await post("items/batch", { action: "collect", key: bulkSel.value, ids: bulkIds });
      toast((bulkRes && bulkRes.message) || (bulkRes && bulkRes.success ? "加好了" : "加入集合失败"));
      bulkSel.value = "";
      return loadState();
    }
    var sel = e.target.closest('select[data-act="collect"]');
    if (sel && sel.value) {
      var itemId = sel.dataset.id;
      var coll = (STATE.collections || []).filter(function (c) {
        return c.id === sel.value;
      })[0];
      if (!coll) return;
      var ids = (coll.items || []).slice();
      if (ids.indexOf(itemId) < 0) ids.push(itemId);
      post("collection/items", { key: coll.id, items: ids }).then(function () {
        toast("已加进「" + coll.name + "」");
      });
      sel.value = "";
      return;
    }
    if (e.target.id === "wp-text-color") {
      document.getElementById("wp-text-color-text").value = e.target.value;
    }
    if (e.target.id === "wp-text-color-text") {
      var v = e.target.value.trim();
      if (/^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/.test(v)) {
        document.getElementById("wp-text-color").value = v;
      }
    }
    if (e.target.id === "wp-ui-text") {
      document.getElementById("wp-ui-text-text").value = e.target.value;
    }
    if (e.target.id === "wp-ui-text-text") {
      var uv = e.target.value.trim();
      if (/^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/.test(uv)) {
        document.getElementById("wp-ui-text").value = uv;
      }
    }
    if (e.target.id === "wp-ui-sub") {
      document.getElementById("wp-ui-sub-text").value = e.target.value;
    }
    if (e.target.id === "wp-ui-sub-text") {
      var sv = e.target.value.trim();
      if (/^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/.test(sv)) {
        document.getElementById("wp-ui-sub").value = sv;
      }
    }
  });

  panel.addEventListener("input", function (e) {
    if (e.target.id === "wp-lib-q") {
      LIB.q = e.target.value;
      refreshLibGrid();
      return;
    }
    if (e.target.id === "wp-veil") {
      document.getElementById("wp-veil-val").textContent = e.target.value;
    }
    if (e.target.id === "wp-blur") {
      document.getElementById("wp-blur-val").textContent = e.target.value + "px";
    }
    if (e.target.id === "wp-fade") {
      document.getElementById("wp-fade-val").textContent = e.target.value + "ms";
    }
    if (String(e.target.id || "").indexOf("wp-ui-") === 0) {
      if (e.target.id === "wp-ui-text") {
        document.getElementById("wp-ui-text-text").value = e.target.value;
      }
      if (e.target.id === "wp-ui-sub") {
        document.getElementById("wp-ui-sub-text").value = e.target.value;
      }
      var themeNow = readUiColorInputs();
      applyUiTheme(themeNow);
      saveLocalTheme(themeNow);
      scheduleUiSave();
    }
  });

  document.addEventListener("click", function (e) {
    if (e.target.closest('[data-act="close-modal"]')) return closeModal();
    if (e.target === modal) return closeModal();
    var head = e.target.closest(".wp-head-actions [data-act]");
    if (head) {
      doAction(head.dataset.act, null, head).catch(function (err) {
        toast("操作失败：" + (err && err.message ? err.message : err));
      });
    }
  });

  // ------------------------------------------------------------------ 启动

  async function boot() {
    applyUiTheme(readLocalTheme() || {});
    await bridgeReady(9000);
    var b = bridge();
    if (b && b.onContext) {
      b.onContext(function (ctx) {
        if (ctx && typeof ctx.isDark === "boolean") {
          document.body.dataset.theme = ctx.isDark ? "dark" : "light";
        }
      });
    }
    if (!b) {
      panel.innerHTML =
        '<div class="muted">没有拿到面板的桥接通道，试试从插件详情页重新打开这个页面。</div>';
      return;
    }
    await loadState();
  }

  boot();
})();
