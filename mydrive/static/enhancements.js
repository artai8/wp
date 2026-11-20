/* MyDrive PikPak / HalalCloud Full-Feature Frontend Enhancements */
(function () {
  // ---- Batch Toolbar ----
  function ensureBatchBar() {
    let bar = document.getElementById('batchBar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'batchBar';
      bar.style.cssText = 'display:none;position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1d1d1f;color:#fff;padding:10px 18px;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.25);z-index:260;align-items:center;gap:10px;font-size:13px;';
      bar.innerHTML = `
        <span id="batchCount" style="font-weight:600">已选 0 项</span>
        <button class="btn btn-sm" onclick="batchOp('copy')">复制</button>
        <button class="btn btn-sm" onclick="batchOp('star')">⭐ 收藏</button>
        <button class="btn btn-sm" onclick="batchZipDownload()">打包 ZIP</button>
        <button class="btn btn-sm" style="background:#ff3b30;color:#fff;border:none" onclick="batchOp('trash')">移入回收站</button>
        <button class="btn btn-sm" onclick="clearBatchSelection()">取消</button>
      `;
      document.body.appendChild(bar);
    }
    if (selectedFiles.length > 0) {
      bar.style.display = 'flex';
      document.getElementById('batchCount').textContent = `已选 ${selectedFiles.length} 项`;
    } else {
      bar.style.display = 'none';
    }
  }

  const _origToggleSelect = window.toggleSelect;
  window.toggleSelect = function (identity, checked) {
    _origToggleSelect(identity, checked);
    ensureBatchBar();
  };
  const _origSelectAll = window.selectAll;
  window.selectAll = function (checked) {
    _origSelectAll(checked);
    ensureBatchBar();
  };
  window.clearBatchSelection = function () {
    selectedFiles = [];
    document.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
    ensureBatchBar();
  };

  window.batchOp = async function (operation) {
    if (!selectedFiles.length) return;
    try {
      const res = await apiJSON('/api/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({identities: selectedFiles, operation, dest: currentParent})
      });
      showToast(`批量操作完成：影响 ${res.affected} 项`);
      clearBatchSelection();
      refreshFiles();
    } catch (e) {
      showToast('批量操作失败: ' + e.message);
    }
  };

  window.batchZipDownload = async function () {
    if (!selectedFiles.length) return;
    showToast('正在打包 ZIP...');
    try {
      const resp = await api('/api/batch/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({identities: selectedFiles})
      });
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'mydrive_batch.zip';
      a.click();
      URL.revokeObjectURL(url);
      showToast('ZIP 下载已开始');
    } catch (e) {
      showToast('打包失败: ' + e.message);
    }
  };

  // ---- Real Copy, Star, Version History ----
  window.copyFile = async function (identity) {
    try {
      await apiJSON('/api/copy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({identity, new_parent: currentParent})
      });
      showToast('已创建副本');
      refreshFiles();
    } catch (e) {
      showToast('复制失败: ' + e.message);
    }
  };

  window.toggleStarFile = async function (identity) {
    try {
      const f = await apiJSON(`/api/files/${identity}/star`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})
      });
      showToast(f.starred ? '已加入收藏 ⭐' : '已取消收藏');
      refreshFiles();
    } catch (e) {
      showToast('收藏操作失败');
    }
  };

  window.showVersionsModal = async function (identity) {
    try {
      const data = await apiJSON(`/api/files/${identity}/versions`);
      const rows = (data.versions || []).map(v => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #eee">
          <div>
            <strong>v${v.version}</strong> · ${formatSize(v.size)}
            <div style="font-size:12px;color:#86868b">${formatDate(v.create_ts)} ${escapeHtml(v.note || '')}</div>
          </div>
          <button class="btn btn-sm" onclick="restoreFileVersion('${identity}', ${v.id})">回滚到此版本</button>
        </div>
      `).join('') || '<div style="color:#86868b;padding:12px 0">暂无历史快照，可先手动创建快照。</div>';

      showModal('文件版本历史与回滚', `
        <div style="font-size:13px;margin-bottom:10px">当前版本：<strong>v${data.current_version}</strong></div>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <input id="snapNote" class="modal-input" style="margin:0" placeholder="快照备注（可选）">
          <button class="btn btn-primary btn-sm" onclick="createFileSnapshot('${identity}')">创建快照</button>
        </div>
        <div style="max-height:260px;overflow:auto">${rows}</div>
      `, () => closeModal());
    } catch (e) {
      showToast('获取版本历史失败');
    }
  };

  window.createFileSnapshot = async function (identity) {
    const note = (document.getElementById('snapNote') || {}).value || '';
    await apiJSON(`/api/files/${identity}/versions/snapshot`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({note})
    });
    showToast('已保存版本快照');
    closeModal();
    showVersionsModal(identity);
  };

  window.restoreFileVersion = async function (identity, vid) {
    if (!confirm('确定回滚到该历史版本？系统会自动备份当前版本。')) return;
    await apiJSON(`/api/files/${identity}/versions/${vid}/restore`, {method: 'POST'});
    showToast('已回滚到选定版本');
    closeModal();
    refreshFiles();
  };

  // Enhance context menu with Star & Versions
  const _origContextMenu = window.onFileContextMenu;
  window.onFileContextMenu = function (event, identity) {
    _origContextMenu(event, identity);
    const menu = document.querySelector('.ctx-menu');
    const file = files.find(f => f.identity === identity);
    if (menu && file) {
      const starItem = document.createElement('div');
      starItem.className = 'ctx-item';
      starItem.textContent = file.starred ? '取消收藏' : '⭐ 收藏';
      starItem.onclick = () => toggleStarFile(identity);
      menu.insertBefore(starItem, menu.lastElementChild);

      if (!file.is_dir) {
        const verItem = document.createElement('div');
        verItem.className = 'ctx-item';
        verItem.textContent = '🕒 版本历史';
        verItem.onclick = () => showVersionsModal(identity);
        menu.insertBefore(verItem, menu.lastElementChild);
      }
    }
  };

  // ---- Global Cross-Directory Search ----
  let searchTimer = null;
  window.onSearch = function (query) {
    searchQuery = (query || '').trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      if (!searchQuery) {
        if (currentPage === 'files') renderFiles();
        return;
      }
      try {
        files = await apiJSON('/api/search?q=' + encodeURIComponent(searchQuery));
        renderFileList();
      } catch (e) {
        showToast('搜索失败');
      }
    }, 250);
  };

  // ---- Smart Categories + Tiered Storage + Telegram Pages ----
  const _origNavigate = window.navigate;
  window.navigate = function (page) {
    if (page.startsWith('cat:')) {
      currentPage = page;
      document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));
      renderCategory(page.slice(4));
      return;
    }
    if (page === 'tiered') {
      currentPage = 'tiered';
      document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === 'tiered'));
      renderTieredStorage();
      return;
    }
    if (page === 'telegram') {
      currentPage = 'telegram';
      document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === 'telegram'));
      renderTelegramBot();
      return;
    }
    if (page === 'vip') {
      currentPage = 'vip';
      document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === 'vip'));
      renderVipCenter();
      return;
    }
    _origNavigate(page);
    if (page === 'trash') {
      setTimeout(injectEmptyTrashButton, 150);
    }
    if (page === 'offline') {
      setTimeout(injectOfflineEnhancements, 150);
    }
  };

  async function renderCategory(cat) {
    const names = {starred: '⭐ 收藏夹', video: '🎬 全部视频', image: '🖼️ 图片相册', audio: '🎵 全部音频', doc: '📄 全部文档'};
    document.getElementById('breadcrumb').innerHTML = `<span class="breadcrumb-item">${names[cat] || cat}</span>`;
    try {
      files = await apiJSON('/api/category/' + encodeURIComponent(cat));
      if (cat === 'image' && files.length) {
        // Gallery grid for photos
        const cards = files.map(f => `
          <div style="background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e5e5e7;cursor:pointer" onclick="previewFile(files.find(x=>x.identity==='${f.identity}'))">
            <img src="/api/media/${f.identity}/thumbnail?access_token=${encodeURIComponent(TOKEN)}"
                 onerror="this.src='/api/download/${f.identity}?access_token=${encodeURIComponent(TOKEN)}'"
                 style="width:100%;height:150px;object-fit:cover;display:block">
            <div style="padding:8px 10px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(f.name)}</div>
          </div>
        `).join('');
        document.getElementById('content').innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px">${cards}</div>`;
      } else {
        renderFileList();
      }
    } catch (e) {
      document.getElementById('content').innerHTML = `<div style="padding:24px;color:#86868b">加载分类失败</div>`;
    }
  }

  function injectEmptyTrashButton() {
    const content = document.getElementById('content');
    if (!content || document.getElementById('emptyTrashBtn')) return;
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;justify-content:flex-end;margin-bottom:12px;gap:8px';
    bar.innerHTML = `<button id="emptyTrashBtn" class="btn" style="background:#ff3b30;color:#fff;border:none" onclick="emptyTrashNow()">🧹 一键清空回收站并释放空间</button>`;
    content.insertBefore(bar, content.firstChild);
  }

  window.emptyTrashNow = async function () {
    if (!confirm('确定永久清空回收站并回收孤立分块？此操作不可恢复。')) return;
    const res = await apiJSON('/api/trash/empty', {method: 'POST'});
    showToast(`已清空 ${res.deleted_files} 个文件，释放 ${formatSize(res.reclaimed_bytes)}`);
    renderTrash();
  };

  // ---- PikPak-Style Cloud Video Player ----
  window.showVideoPreview = async function (file) {
    let overlay = document.getElementById('previewOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'previewOverlay';
      overlay.className = 'preview-overlay';
      document.body.appendChild(overlay);
    }
    let probe = {qualities: ['original', '1080p', '720p', '480p'], subtitles: [], progress: {position_sec: 0, playback_rate: 1.0}};
    try {
      probe = await apiJSON(`/api/media/${file.identity}/probe`);
    } catch (e) {}

    const tok = encodeURIComponent(TOKEN);
    const directSrc = `${API}/api/download/${file.identity}?access_token=${tok}`;
    const allSubs = [
      ...(probe.subtitles || []).map(s => ({label: s.name, url: s.url, matched: s.matched})),
      ...(probe.embedded_subtitles || []).map(s => ({label: '[内封] ' + s.title, url: s.url, matched: false})),
    ];
    const subTracks = allSubs.map((s, idx) =>
      `<track kind="subtitles" label="${escapeHtml(s.label)}" src="${s.url}?access_token=${tok}" ${idx === 0 && s.matched ? 'default' : ''}>`
    ).join('');

    overlay.innerHTML = `
      <div class="preview-close" onclick="closeCloudVideo()">✕</div>
      <div class="preview-content" style="flex-direction:column;gap:12px;max-width:92vw">
        <div style="color:#fff;font-size:15px;font-weight:600;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span>🎬 ${escapeHtml(file.name)}</span>
          <select id="vpQuality" onchange="switchVideoQuality('${file.identity}', this.value)" style="padding:4px 8px;border-radius:6px">
            <option value="direct">原画直连</option>
            <option value="original">HLS 原画 (Remux)</option>
            <option value="1080p">HLS 1080P</option>
            <option value="720p">HLS 720P</option>
            <option value="480p">HLS 480P</option>
          </select>
          <select id="vpSpeed" onchange="setVideoSpeed(this.value)" style="padding:4px 8px;border-radius:6px">
            <option value="0.5">0.5x</option>
            <option value="0.75">0.75x</option>
            <option value="1" selected>1.0x</option>
            <option value="1.25">1.25x</option>
            <option value="1.5">1.5x</option>
            <option value="2">2.0x</option>
          </select>
          <button class="btn btn-sm" onclick="toggleSpritePreview('${file.identity}')">🖼️ 逐帧预览</button>
          <span style="font-size:12px;color:#ccc">已自动挂载 ${allSubs.length} 条外挂/内封字幕</span>
        </div>
        <video id="cloudVideoEl" src="${directSrc}" controls autoplay crossorigin="anonymous" style="max-width:90vw;max-height:76vh;border-radius:8px;background:#000">
          ${subTracks}
        </video>
        <div id="spriteBox" style="display:none;background:#111;padding:8px;border-radius:8px">
          <img id="spriteImg" style="max-width:520px;border-radius:6px">
        </div>
      </div>
    `;

    const v = document.getElementById('cloudVideoEl');
    window._activeVideoId = file.identity;
    const savedPos = (probe.progress && probe.progress.position_sec) || 0;
    const savedRate = (probe.progress && probe.progress.playback_rate) || 1.0;
    document.getElementById('vpSpeed').value = String(savedRate);
    v.playbackRate = savedRate;
    v.addEventListener('loadedmetadata', () => {
      if (savedPos > 3 && savedPos < (v.duration || 1e9) - 5) {
        v.currentTime = savedPos;
        showToast(`已从上次进度 ${Math.floor(savedPos)}s 续播`);
      }
    }, {once: true});
    window._videoProgressTimer = setInterval(() => saveVideoProgress(), 5000);
  };

  window.saveVideoProgress = function () {
    const v = document.getElementById('cloudVideoEl');
    if (!v || !window._activeVideoId) return;
    apiJSON(`/api/media/${window._activeVideoId}/progress`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        position_sec: v.currentTime || 0,
        duration_sec: v.duration || 0,
        playback_rate: v.playbackRate || 1.0,
        quality: (document.getElementById('vpQuality') || {}).value || 'direct'
      })
    }).catch(() => {});
  };

  window.closeCloudVideo = function () {
    saveVideoProgress();
    clearInterval(window._videoProgressTimer);
    window._activeVideoId = null;
    const ov = document.getElementById('previewOverlay');
    if (ov) ov.remove();
  };

  window.setVideoSpeed = function (val) {
    const v = document.getElementById('cloudVideoEl');
    if (v) v.playbackRate = parseFloat(val) || 1.0;
    saveVideoProgress();
  };

  window.switchVideoQuality = function (identity, quality) {
    const v = document.getElementById('cloudVideoEl');
    if (!v) return;
    const pos = v.currentTime || 0;
    const tok = encodeURIComponent(TOKEN);
    const url = quality === 'direct'
      ? `${API}/api/download/${identity}?access_token=${tok}`
      : `${API}/api/media/${identity}/hls/${quality}/index.m3u8?access_token=${tok}`;
    v.src = url;
    v.addEventListener('loadedmetadata', () => { v.currentTime = pos; v.play().catch(() => {}); }, {once: true});
    showToast(`已切换画质：${quality}`);
  };

  window.toggleSpritePreview = function (identity) {
    const box = document.getElementById('spriteBox');
    const img = document.getElementById('spriteImg');
    if (!box || !img) return;
    if (box.style.display === 'none') {
      img.src = `/api/media/${identity}/sprite?access_token=${encodeURIComponent(TOKEN)}`;
      box.style.display = 'block';
    } else {
      box.style.display = 'none';
    }
  };

  // ---- Offline Enhancements (Parse + Select Files + Batch + Speed Limit) ----
  function injectOfflineEnhancements() {
    const content = document.getElementById('content');
    if (!content || document.getElementById('offlineProBar')) return;
    const bar = document.createElement('div');
    bar.id = 'offlineProBar';
    bar.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px';
    bar.innerHTML = `
      <button class="btn btn-primary" onclick="showBatchOfflineDialog()">⚡ 批量离线 / 磁力秒存</button>
      <button class="btn" onclick="showParseOfflineDialog()">🔍 解析磁力/种子选文件</button>
      <button class="btn" onclick="showSpeedLimitDialog()">🚀 限速设置</button>
    `;
    content.insertBefore(bar, content.firstChild);
  }

  window.showBatchOfflineDialog = function () {
    showModal('批量离线下载（支持磁力全局秒存）', `
      <textarea id="batchOfflineUrls" class="modal-input" rows="5" placeholder="每行一条 magnet:? 或 HTTP/HTTPS 下载链接"></textarea>
      <input id="batchOfflinePath" class="modal-input" value="/离线下载" placeholder="保存路径">
    `, async () => {
      const urls = (document.getElementById('batchOfflineUrls').value || '').split('\n').map(s => s.trim()).filter(Boolean);
      const save_path = document.getElementById('batchOfflinePath').value.trim() || '/';
      if (!urls.length) return;
      const res = await apiJSON('/api/offline/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({urls, save_path})
      });
      const instantCount = (res.results || []).filter(r => r.instant_saved).length;
      showToast(`已提交 ${res.count} 条任务（其中 ${instantCount} 条命中秒存 ⚡）`);
      closeModal();
      renderOffline();
      setTimeout(injectOfflineEnhancements, 150);
    });
  };

  window.showParseOfflineDialog = function () {
    showModal('解析磁力/种子并选择子文件', `
      <input id="parseUrl" class="modal-input" placeholder="输入 magnet:? 或 .torrent 链接">
      <button class="btn btn-sm" onclick="doParseOffline()" style="margin-bottom:10px">解析文件列表</button>
      <div id="parseResult" style="max-height:220px;overflow:auto;font-size:13px;color:#555"></div>
    `, async () => {
      const url = document.getElementById('parseUrl').value.trim();
      const checked = Array.from(document.querySelectorAll('.parse-file-cb:checked')).map(cb => parseInt(cb.value, 10));
      if (!url) return;
      await apiJSON('/api/offline/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url, save_path: '/离线下载', selected_files: checked})
      });
      showToast('已按所选文件提交离线任务');
      closeModal();
      renderOffline();
      setTimeout(injectOfflineEnhancements, 150);
    });
  };

  window.doParseOffline = async function () {
    const url = document.getElementById('parseUrl').value.trim();
    const box = document.getElementById('parseResult');
    if (!url) return;
    box.textContent = '正在解析元数据...';
    try {
      const d = await apiJSON('/api/offline/parse', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url})
      });
      const list = d.files || [];
      if (!list.length) {
        box.textContent = `名称：${d.name || '未知'}（暂未解析到多文件列表，可直接提交）`;
        return;
      }
      box.innerHTML = list.map((f, i) => `
        <label style="display:block;padding:4px 0">
          <input type="checkbox" class="parse-file-cb" value="${i + 1}" checked>
          ${escapeHtml(f.name || f.path || ('文件 ' + (i + 1)))} (${formatSize(f.size || 0)})
        </label>
      `).join('');
    } catch (e) {
      box.textContent = '解析失败: ' + e.message;
    }
  };

  window.showSpeedLimitDialog = function () {
    showModal('离线下载全局限速', `
      <input id="maxDlLimit" class="modal-input" placeholder="最大下载速度（如 10M，0 表示不限速）" value="0">
      <input id="maxUlLimit" class="modal-input" placeholder="最大上传速度（如 2M，0 表示不限速）" value="0">
    `, async () => {
      const max_download = document.getElementById('maxDlLimit').value.trim() || '0';
      const max_upload = document.getElementById('maxUlLimit').value.trim() || '0';
      await apiJSON('/api/offline/speed-limit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({max_download, max_upload})
      });
      showToast('已更新限速设置');
      closeModal();
    });
  };

  // ---- Share Link points to standalone /s/{share_id} page ----
  window.copyShareLink = function (shareId) {
    const url = `${location.origin}/s/${shareId}`;
    navigator.clipboard.writeText(url).then(() => showToast('已复制公开分享页链接（支持提取码与一键转存）'));
  };

  // ---- Tiered Large-File Storage Dashboard ----
  async function renderTieredStorage() {
    document.getElementById('breadcrumb').innerHTML = `<span class="breadcrumb-item">☁️ 大文件分级存储与省空间策略</span>`;
    const [cfg, st] = await Promise.all([
      apiJSON('/api/storage/tiered/config'),
      apiJSON('/api/storage/tiered/stats')
    ]);
    const t = st.tiered || {};
    document.getElementById('content').innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:20px">
        <div class="stat-card" style="background:#fff;padding:16px;border-radius:12px;border:1px solid #e5e5e7">
          <div style="font-size:12px;color:#86868b">本地热缓存占用</div>
          <div style="font-size:22px;font-weight:700;margin-top:4px">${formatSize(t.local_cache_bytes || 0)}</div>
          <div style="font-size:12px;color:#6e6e73">上限：${t.local_cache_max_bytes ? formatSize(t.local_cache_max_bytes) : '不限制'}</div>
        </div>
        <div class="stat-card" style="background:#fff;padding:16px;border-radius:12px;border:1px solid #e5e5e7">
          <div style="font-size:12px;color:#86868b">热块 / 冷块分布</div>
          <div style="font-size:22px;font-weight:700;margin-top:4px">${t.local_chunks || 0} / ${t.cold_chunks || 0}</div>
          <div style="font-size:12px;color:#6e6e73">冷存储状态：${t.cold_active ? '✅ 已启用 (' + t.cold_type + ')' : '未启用'}</div>
        </div>
        <div class="stat-card" style="background:#fff;padding:16px;border-radius:12px;border:1px solid #e5e5e7">
          <div style="font-size:12px;color:#86868b">全局去重率</div>
          <div style="font-size:22px;font-weight:700;margin-top:4px">${((st.dedup_ratio || 0) * 100).toFixed(1)}%</div>
          <div style="font-size:12px;color:#6e6e73">逻辑总量 ${formatSize(st.logical_bytes || 0)}</div>
        </div>
        <div class="stat-card" style="background:#fff;padding:16px;border-radius:12px;border:1px solid #e5e5e7">
          <div style="font-size:12px;color:#86868b">分块压缩节省</div>
          <div style="font-size:22px;font-weight:700;margin-top:4px">${formatSize(t.compression_saved_bytes || 0)}</div>
          <div style="font-size:12px;color:#6e6e73">压缩块数：${t.compressed_chunks || 0}</div>
        </div>
      </div>

      <div style="background:#fff;padding:20px;border-radius:12px;border:1px solid #e5e5e7;margin-bottom:16px">
        <h3 style="margin:0 0 12px;font-size:16px">冷热分层对象存储配置（兼容 MinIO / Cloudflare R2 / Backblaze B2 / OSS / 挂载 NAS）</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <label>冷存储后端类型
            <select id="tsColdType" class="modal-input">
              <option value="none" ${cfg.cold_type === 'none' ? 'selected' : ''}>仅本地存储</option>
              <option value="s3" ${cfg.cold_type === 's3' ? 'selected' : ''}>S3 兼容对象存储 (R2 / MinIO / B2 / OSS)</option>
              <option value="dir" ${cfg.cold_type === 'dir' ? 'selected' : ''}>外部磁盘 / NAS / Rclone 挂载目录</option>
            </select>
          </label>
          <label>本地热缓存上限（MB，0=不限）
            <input id="tsMaxMb" class="modal-input" type="number" value="${Math.round((cfg.local_cache_max_bytes || 0) / 1048576)}">
          </label>
          <label>外部冷目录路径（dir 模式）
            <input id="tsColdDir" class="modal-input" value="${escapeHtml(cfg.cold_dir || '')}" placeholder="/mnt/nas/mydrive_cold">
          </label>
          <label>S3 Endpoint（s3 模式）
            <input id="tsS3Endpoint" class="modal-input" value="${escapeHtml(cfg.s3_endpoint || '')}" placeholder="https://xxx.r2.cloudflarestorage.com">
          </label>
          <label>S3 Bucket
            <input id="tsS3Bucket" class="modal-input" value="${escapeHtml(cfg.s3_bucket || '')}" placeholder="mydrive-cold">
          </label>
          <label>S3 Region
            <input id="tsS3Region" class="modal-input" value="${escapeHtml(cfg.s3_region || 'auto')}">
          </label>
          <label>S3 Access Key
            <input id="tsS3Ak" class="modal-input" value="${escapeHtml(cfg.s3_access_key || '')}">
          </label>
          <label>S3 Secret Key
            <input id="tsS3Sk" class="modal-input" type="password" value="${cfg.s3_secret_key_set ? '***' : ''}">
          </label>
        </div>
        <div style="display:flex;gap:18px;margin:8px 0 16px;font-size:13px">
          <label><input type="checkbox" id="tsCompress" ${cfg.compression_enabled ? 'checked' : ''}> 启用智能分块压缩（自动跳过已压缩视频/压缩包）</label>
          <label><input type="checkbox" id="tsAutoOffload" ${cfg.auto_offload ? 'checked' : ''}> 超出本地缓存上限时自动 LRU 迁移冷块</label>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="saveTieredConfig()">保存策略</button>
          <button class="btn" onclick="offloadColdNow()">☁️ 立即迁移本地块至冷存储</button>
          <button class="btn" onclick="runGcNow()">🧹 运行分块垃圾回收 (GC)</button>
        </div>
      </div>
    `;
  }

  window.saveTieredConfig = async function () {
    const body = {
      cold_type: document.getElementById('tsColdType').value,
      local_cache_max_bytes: Math.max(0, parseInt(document.getElementById('tsMaxMb').value || '0', 10)) * 1048576,
      cold_dir: document.getElementById('tsColdDir').value.trim(),
      s3_endpoint: document.getElementById('tsS3Endpoint').value.trim(),
      s3_bucket: document.getElementById('tsS3Bucket').value.trim(),
      s3_region: document.getElementById('tsS3Region').value.trim() || 'auto',
      s3_access_key: document.getElementById('tsS3Ak').value.trim(),
      s3_secret_key: document.getElementById('tsS3Sk').value,
      compression_enabled: document.getElementById('tsCompress').checked,
      auto_offload: document.getElementById('tsAutoOffload').checked,
    };
    await apiJSON('/api/storage/tiered/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    showToast('分级存储策略已保存');
    renderTieredStorage();
  };

  window.offloadColdNow = async function () {
    const r = await apiJSON('/api/storage/tiered/offload', {method: 'POST'});
    if (r.error) showToast(r.error);
    else showToast(`已迁移 ${r.offloaded} 个分块至冷存储，释放本地 ${formatSize(r.freed_bytes || 0)}`);
    renderTieredStorage();
  };

  window.runGcNow = async function () {
    const r = await apiJSON('/api/storage/gc', {method: 'POST'});
    showToast(`GC 完成：回收 ${r.reclaimed_chunks} 个孤立块，释放 ${formatSize(r.reclaimed_bytes || 0)}`);
    renderTieredStorage();
  };

  // ---- PikPak-Style Telegram Bot Page ----
  async function renderTelegramBot() {
    document.getElementById('breadcrumb').innerHTML = `<span class="breadcrumb-item">🤖 Telegram 离线机器人（PikPak 风格）</span>`;
    const cfg = await apiJSON('/api/telegram/config');
    document.getElementById('content').innerHTML = `
      <div style="background:#fff;padding:20px;border-radius:12px;border:1px solid #e5e5e7;margin-bottom:16px">
        <h3 style="margin:0 0 10px;font-size:16px">Bot 配置（Webhook: <code>${location.origin}/api/telegram/webhook</code>）</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <label>Bot Token（从 @BotFather 获取）
            <input id="tgToken" class="modal-input" type="password" placeholder="${cfg.bot_token_set ? '已配置（留空保持不变）' : '123456:ABC-DEF...'}">
          </label>
          <label>默认离线保存目录
            <input id="tgSavePath" class="modal-input" value="${escapeHtml(cfg.default_save_path || '/Telegram')}">
          </label>
          <label>Webhook Secret（可选）
            <input id="tgSecret" class="modal-input" placeholder="自定义验证头密钥">
          </label>
        </div>
        <button class="btn btn-primary" onclick="saveTgConfig()">保存 Bot 配置</button>
        <span style="margin-left:12px;font-size:13px;color:#6e6e73">已绑定会话数：${cfg.bound_chats || 0}</span>
      </div>

      <div style="background:#fff;padding:20px;border-radius:12px;border:1px solid #e5e5e7">
        <h3 style="margin:0 0 10px;font-size:16px">内置命令模拟器（无需外网即可测试转发离线）</h3>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input id="tgSimText" class="modal-input" style="margin:0" placeholder="输入 /help、/bind 用户名 密码、/quota 或直接粘贴 magnet:? 链接">
          <button class="btn btn-primary" onclick="runTgSim()">发送</button>
        </div>
        <pre id="tgSimOut" style="background:#f5f5f7;padding:12px;border-radius:8px;white-space:pre-wrap;font-size:13px;min-height:60px">等待测试指令...</pre>
      </div>
    `;
  }

  window.saveTgConfig = async function () {
    const body = {default_save_path: document.getElementById('tgSavePath').value.trim() || '/Telegram'};
    const tok = document.getElementById('tgToken').value.trim();
    const sec = document.getElementById('tgSecret').value.trim();
    if (tok) body.bot_token = tok;
    if (sec) body.webhook_secret = sec;
    await apiJSON('/api/telegram/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    showToast('Telegram Bot 配置已保存');
    renderTelegramBot();
  };

  window.runTgSim = async function () {
    const text = document.getElementById('tgSimText').value.trim();
    if (!text) return;
    const res = await apiJSON('/api/telegram/simulate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chat_id: 'web_sim_chat', text})
    });
    document.getElementById('tgSimOut').textContent = res.reply || JSON.stringify(res);
  };

  // ---- Commercial VIP & Quota Center ----
  async function renderVipCenter() {
    document.getElementById('breadcrumb').innerHTML = `<span class="breadcrumb-item">👑 会员等级、配额与兑换中心</span>`;
    const v = await apiJSON('/api/vip/me');
    const exp = v.expire_ts ? new Date(v.expire_ts * 1000).toLocaleDateString() : '长期有效';
    const pct = v.storage_quota_bytes ? Math.min(100, (v.used_bytes / v.storage_quota_bytes) * 100).toFixed(2) : '0';
    document.getElementById('content').innerHTML = `
      <div style="background:linear-gradient(135deg,#1d1d1f,#3a3a3c);color:#fff;padding:24px;border-radius:14px;margin-bottom:18px">
        <div style="font-size:13px;opacity:.8">当前等级</div>
        <div style="font-size:26px;font-weight:700;margin:6px 0">👑 ${escapeHtml(v.plan_name)} (${escapeHtml(v.plan.toUpperCase())})</div>
        <div style="font-size:13px;opacity:.85">到期时间：${exp} · 每日离线配额：${v.daily_offline_quota} 次 · 最高云播画质：${escapeHtml(v.max_quality)} · 下载限速：${v.download_speed_bps ? formatSize(v.download_speed_bps) + '/s' : '不限速'}</div>
        <div style="margin-top:12px;font-size:12px">空间使用：${formatSize(v.used_bytes)} / ${formatSize(v.storage_quota_bytes)} (${pct}%)</div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div style="background:#fff;padding:20px;border-radius:12px;border:1px solid #e5e5e7">
          <h3 style="margin:0 0 10px;font-size:15px">🎟️ 兑换码开通 VIP / SVIP</h3>
          <div style="display:flex;gap:8px">
            <input id="vipCodeInput" class="modal-input" style="margin:0" placeholder="输入兑换码（如 VIP-XXXX-XXXX）">
            <button class="btn btn-primary" onclick="redeemVipNow()">立即兑换</button>
          </div>
        </div>
        <div style="background:#fff;padding:20px;border-radius:12px;border:1px solid #e5e5e7">
          <h3 style="margin:0 0 10px;font-size:15px">🛠️ 管理员快速发码</h3>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <select id="genPlan" class="modal-input" style="width:auto;margin:0">
              <option value="vip">VIP (1TB / 30天)</option>
              <option value="svip">SVIP (10TB / 30天)</option>
            </select>
            <button class="btn" onclick="genVipCodeNow()">生成兑换码</button>
          </div>
          <div id="genCodeOut" style="margin-top:10px;font-family:monospace;font-size:13px;color:#0071e3"></div>
        </div>
      </div>
    `;
  }

  window.redeemVipNow = async function () {
    const code = (document.getElementById('vipCodeInput') || {}).value || '';
    if (!code.trim()) return;
    try {
      await apiJSON('/api/vip/redeem', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: code.trim()})
      });
      showToast('🎉 兑换成功，会员权益已生效！');
      renderVipCenter();
    } catch (e) {
      showToast('兑换失败: ' + e.message);
    }
  };

  window.genVipCodeNow = async function () {
    const plan = (document.getElementById('genPlan') || {}).value || 'vip';
    const r = await apiJSON('/api/vip/codes/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({plan, duration_days: 30, count: 1})
    });
    const code = (r.codes && r.codes[0] && r.codes[0].code) || '';
    document.getElementById('genCodeOut').textContent = '已生成：' + code;
    const inp = document.getElementById('vipCodeInput');
    if (inp) inp.value = code;
  };

})();
