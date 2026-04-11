/* Manifold UI */
(function(){
"use strict";

const API = "/api";
let channels = [];
let epgEntries = [];
let m3uSources = [];
let settings = {};
let logPos = 0;
let logTimer = null;
let statsTimer = null;
let currentView = "channels";
let currentSettingsSub = "general";
let editingId = null;
let channelViewMode = "table"; // "table" or "tiles"
let tasksTimer = null;
let enrichStatusTimer = null;

// ── Helpers ──────────────────────────────────────────────────────────────
function $(sel, ctx){ return (ctx||document).querySelector(sel) }
function $$(sel, ctx){ return [...(ctx||document).querySelectorAll(sel)] }

async function api(path, opts){
  const r = await fetch(API + path, opts);
  return r.json();
}
async function apiPost(path, body){
  return api(path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
}
async function apiPut(path, body){
  return api(path, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
}
async function apiDelete(path){
  return api(path, {method:"DELETE"});
}

function toast(msg, type){
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (type||"info");
  clearTimeout(t._tid);
  t._tid = setTimeout(()=>{ t.classList.add("fade-out"); setTimeout(()=>t.classList.add("hidden"),300); }, 3000);
}

function fmtDate(iso){
  if(!iso) return "\u2014";
  const d = new Date(iso);
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
}

// ── Bulk Selection Helper ───────────────────────────────────────────────
function setupBulk(prefix, getSelected, onDelete){
  const selectAll = $(`#${prefix}-select-all`);
  const bar = $(`#${prefix}-bulk-bar`);
  const countEl = $(`#${prefix}-bulk-count`);
  const deleteBtn = $(`#${prefix}-bulk-delete`);

  function updateBar(){
    const checked = getSelected();
    if(checked.length > 0){
      bar.classList.remove("hidden");
      countEl.textContent = checked.length + " selected";
    } else {
      bar.classList.add("hidden");
    }
  }

  function bindRows(bodyEl){
    $$(`input[data-bulk-${prefix}]`, bodyEl).forEach(cb=>{
      cb.addEventListener("change", ()=>{
        const row = cb.closest("tr");
        if(cb.checked) row.classList.add("row-selected");
        else row.classList.remove("row-selected");
        // sync select-all state
        const all = $$(`input[data-bulk-${prefix}]`, bodyEl);
        selectAll.checked = all.length > 0 && all.every(c=>c.checked);
        updateBar();
      });
    });
  }

  selectAll.addEventListener("change", ()=>{
    const bodyEl = selectAll.closest("table").querySelector("tbody");
    $$(`input[data-bulk-${prefix}]`, bodyEl).forEach(cb=>{
      cb.checked = selectAll.checked;
      const row = cb.closest("tr");
      if(selectAll.checked) row.classList.add("row-selected");
      else row.classList.remove("row-selected");
    });
    updateBar();
  });

  deleteBtn.addEventListener("click", async ()=>{
    const ids = getSelected();
    if(!ids.length) return;
    if(!confirm(`Delete ${ids.length} item(s)?`)) return;
    await onDelete(ids);
    selectAll.checked = false;
    updateBar();
  });

  return { updateBar, bindRows, reset(){ selectAll.checked = false; bar.classList.add("hidden"); } };
}

// ── Navigation ───────────────────────────────────────────────────────────
$$(".nav-item").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    const view = btn.dataset.view;
    if(!view) return;

    if(btn.classList.contains("nav-item-parent")){
      btn.classList.toggle("expanded");
      const sub = $("#settings-subnav");
      sub.classList.toggle("expanded");
      if(currentView !== "settings"){
        switchView("settings");
      }
      return;
    }

    switchView(view);
    $$(".nav-item").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
  });
});

$$(".nav-subitem").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    currentSettingsSub = btn.dataset.sub;
    $$(".nav-subitem").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    renderSettings();
  });
});

function switchView(view){
  currentView = view;
  $$(".view").forEach(v=>v.classList.remove("visible"));
  const el = $(`#view-${view}`);
  if(el) el.classList.add("visible");

  clearInterval(logTimer); logTimer = null;
  clearInterval(statsTimer); statsTimer = null;
  clearInterval(tasksTimer); tasksTimer = null;
  clearInterval(enrichStatusTimer); enrichStatusTimer = null;

  if(view === "channels") loadChannels();
  else if(view === "m3u-sources") loadM3uSources();
  else if(view === "epg-sources") loadEpgSources();
  else if(view === "epg") loadEpg();
  else if(view === "guide") loadGuide();
  else if(view === "bumps") loadBumps();
  else if(view === "logs"){ loadLogs(); logTimer = setInterval(loadLogs, 2000); }
  else if(view === "stats"){ loadStats(); statsTimer = setInterval(loadStats, 3000); }
  else if(view === "settings") loadSettings();
}

// ── Channels ─────────────────────────────────────────────────────────────
const channelsBulk = setupBulk("channels",
  ()=> $$("input[data-bulk-channels]:checked").map(cb=>cb.dataset.bulkChannels),
  async (ids)=>{
    await apiPost("/channels/bulk-delete", {ids});
    toast(`Deleted ${ids.length} channels`, "success");
    loadChannels();
  }
);

$("#channels-bulk-activate").addEventListener("click", async ()=>{
  const ids = $$("input[data-bulk-channels]:checked").map(cb=>cb.dataset.bulkChannels);
  if(!ids.length) return;
  const res = await apiPost("/channels/bulk-activate", {ids});
  toast(`Activated ${res.activated} channels`, "success");
  loadChannels();
});

$("#channels-bulk-deactivate").addEventListener("click", async ()=>{
  const ids = $$("input[data-bulk-channels]:checked").map(cb=>cb.dataset.bulkChannels);
  if(!ids.length) return;
  const res = await apiPost("/channels/bulk-deactivate", {ids});
  toast(`Deactivated ${res.deactivated} channels`, "success");
  loadChannels();
});

async function loadChannels(){
  channels = await api("/channels");
  _populateTagFilter();
  renderChannels();
  $("#channel-count").textContent = channels.filter(c=>c.active).length + " active";
}

function _populateTagFilter(){
  const sel = $("#channel-tag-filter");
  const allTags = new Set();
  channels.forEach(ch=> (ch.tags||[]).forEach(t=> allTags.add(t)));
  const prev = sel.value;
  sel.innerHTML = '<option value="">All Tags</option>' +
    [...allTags].sort().map(t=>`<option value="${t}">${t}</option>`).join("");
  sel.value = prev || "";
}

function _getFilteredChannels(){
  const search = ($("#channel-search").value||"").toLowerCase();
  const filter = $("#channel-filter").value;
  const tagFilter = $("#channel-tag-filter").value;
  return channels.filter(ch=>{
    if(search && !ch.title.toLowerCase().includes(search)) return false;
    if(filter==="active" && !ch.active) return false;
    if(filter==="inactive" && ch.active) return false;
    if(filter==="mapped" && !ch.epg_mapped) return false;
    if(filter==="unmapped" && ch.epg_mapped) return false;
    if(tagFilter && !(ch.tags||[]).includes(tagFilter)) return false;
    return true;
  });
}

function _tagPills(tags){
  return (tags||[]).map(t=>{
    let cls = "tag-pill";
    if(t==="live") cls += " tag-live";
    else if(t==="event") cls += " tag-event";
    else if(t==="sports") cls += " tag-sports";
    else if(t==="news") cls += " tag-news";
    return `<span class="${cls}">${t}</span>`;
  }).join("");
}

function renderChannels(){
  if(channelViewMode === "tiles") renderChannelTiles();
  else renderChannelTable();
}

function renderChannelTable(){
  channelsBulk.reset();
  $("#channels-table-view").classList.remove("hidden");
  $("#channels-tile-view").classList.add("hidden");

  const filtered = _getFilteredChannels();
  const body = $("#channels-body");
  if(!filtered.length){
    body.innerHTML = '<tr><td colspan="7" class="empty-state">No channels found</td></tr>';
    return;
  }

  body.innerHTML = filtered.map(ch=>{
    const tags = _tagPills(ch.tags);
    const epg = ch.epg_mapped
      ? `<span class="epg-badge epg-mapped">${ch.epg_channel_id}</span>`
      : `<span class="epg-badge epg-unmapped">None</span>`;
    const logoSrc = ch.logo_cached ? `/logo/${ch.id}` : "";
    const logoHtml = logoSrc
      ? `<img class="ch-logo" src="${logoSrc}" onerror="this.classList.add('no-logo')">`
      : "";

    return `<tr data-id="${ch.id}">
      <td class="td-check"><input type="checkbox" data-bulk-channels="${ch.id}"></td>
      <td class="td-chno"><input type="number" class="chno-input" value="${ch.channel_number||''}" data-chno="${ch.id}" placeholder="-" min="1"></td>
      <td class="title-clickable"><span class="ch-title-wrap">${logoHtml}${ch.title_override ? `<span>${ch.title_override}</span><span class="ch-source-title">${ch.title}</span>` : (ch.title||"Untitled")}</span></td>
      <td>${tags}</td>
      <td>${epg}</td>
      <td>
        <label class="toggle">
          <input type="checkbox" ${ch.active?"checked":""} data-toggle="${ch.id}">
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td class="td-actions"><div class="action-btns">
        <button class="btn-sm btn-sm-watch" data-watch="${ch.id}" data-watch-title="${(ch.title||'').replace(/"/g,'&quot;')}">Watch</button>
        <button class="btn-sm" data-edit="${ch.id}">Edit</button>
      </div></td>
    </tr>`;
  }).join("");

  channelsBulk.bindRows(body);

  $$(".chno-input", body).forEach(inp=>{
    inp.addEventListener("change", async ()=>{
      const val = inp.value ? parseInt(inp.value) : null;
      await apiPut(`/channels/${inp.dataset.chno}`, {channel_number: val});
      toast("Channel number updated", "success");
    });
  });
  $$("[data-toggle]", body).forEach(cb=>{
    cb.addEventListener("change", async ()=>{
      await apiPost(`/channels/${cb.dataset.toggle}/toggle`, {active:cb.checked});
      toast(cb.checked ? "Channel activated" : "Channel deactivated", "success");
    });
  });
  $$("[data-watch]", body).forEach(btn=>{
    btn.addEventListener("click", ()=> watchChannel(btn.dataset.watch, btn.dataset.watchTitle));
  });
  $$("[data-edit]", body).forEach(btn=>{
    btn.addEventListener("click", ()=> openEditModal(btn.dataset.edit));
  });
  $$(".title-clickable", body).forEach(td=>{
    td.addEventListener("click", ()=> openEditModal(td.parentElement.dataset.id));
  });
}

function renderChannelTiles(){
  $("#channels-table-view").classList.add("hidden");
  const grid = $("#channels-tile-view");
  grid.classList.remove("hidden");

  // Tile view only shows active channels (filtered)
  const filtered = _getFilteredChannels().filter(ch=>ch.active);
  if(!filtered.length){
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">No active channels match your filters</div>';
    return;
  }

  grid.innerHTML = filtered.map(ch=>{
    const logoSrc = ch.logo_cached ? `/logo/${ch.id}` : "";
    const tags = _tagPills(ch.tags);

    return `<div class="channel-card" data-card-id="${ch.id}">
      <div class="channel-card-logo-row" style="position:relative">
        ${ch.channel_number ? `<span class="tile-chno">${ch.channel_number}</span>` : ''}
        <img class="channel-card-logo" src="${logoSrc}" alt="" onerror="this.classList.add('no-logo')">
      </div>
      <div class="channel-card-body">
        <div class="channel-card-header">
          <h3 title="${(ch.title_override||ch.title||'').replace(/"/g,'&quot;')}">${ch.title_override||ch.title||"Untitled"}</h3>
          <span class="badge status-live">Active</span>
        </div>
        <div class="channel-card-meta">${tags}</div>
        <div class="channel-card-actions">
          <button class="btn-sm btn-sm-watch" data-watch="${ch.id}" data-watch-title="${(ch.title||'').replace(/"/g,'&quot;')}">Watch</button>
          <button class="btn-sm" data-edit="${ch.id}">Edit</button>
        </div>
      </div>
    </div>`;
  }).join("");

  $$("[data-watch]", grid).forEach(btn=>{
    btn.addEventListener("click", (e)=>{ e.stopPropagation(); watchChannel(btn.dataset.watch, btn.dataset.watchTitle); });
  });
  $$("[data-edit]", grid).forEach(btn=>{
    btn.addEventListener("click", (e)=>{ e.stopPropagation(); openEditModal(btn.dataset.edit); });
  });
  $$(".channel-card", grid).forEach(card=>{
    card.addEventListener("click", ()=> openEditModal(card.dataset.cardId));
  });
}

$("#channel-search").addEventListener("input", renderChannels);
$("#channel-filter").addEventListener("change", renderChannels);
$("#channel-tag-filter").addEventListener("change", renderChannels);
$("#btn-refresh-channels").addEventListener("click", loadChannels);
$("#btn-auto-number").addEventListener("click", ()=>{
  const filtered = _getFilteredChannels();
  const hasFilters = ($("#channel-search").value||"").trim() !== ""
    || $("#channel-filter").value !== "all"
    || $("#channel-tag-filter").value !== "";
  const scopeLabel = hasFilters ? `${filtered.length} filtered channels` : `all ${channels.length} channels`;

  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `<div class="modal-content renumber-modal">
    <h3>Auto-Number Channels</h3>
    <div class="setting-field" style="margin-bottom:16px">
      <label>Start at</label>
      <input type="number" id="renumber-start" value="1" min="1" style="width:100px">
    </div>
    <div class="setting-field" style="margin-bottom:16px">
      <label>Scope</label>
      <div class="toggle-group">
        <button class="toggle-btn ${hasFilters?"":"active"}" data-scope="all">All channels (${channels.length})</button>
        <button class="toggle-btn ${hasFilters?"active":""}" data-scope="filtered" ${!hasFilters?"disabled":""}>Current filter (${filtered.length})</button>
      </div>
      ${hasFilters?`<span class="field-hint" style="margin-top:6px">Current filter will number only the ${filtered.length} channels visible in the table</span>`:""}
    </div>
    <div class="modal-actions">
      <button class="btn" id="renumber-cancel">Cancel</button>
      <button class="btn btn-primary" id="renumber-apply">Number ${scopeLabel}</button>
    </div>
  </div>`;

  document.body.appendChild(modal);

  let scope = hasFilters ? "filtered" : "all";

  modal.querySelectorAll("[data-scope]").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      if(btn.disabled) return;
      modal.querySelectorAll("[data-scope]").forEach(b=>b.classList.remove("active"));
      btn.classList.add("active");
      scope = btn.dataset.scope;
      const count = scope === "filtered" ? filtered.length : channels.length;
      modal.querySelector("#renumber-apply").textContent = `Number ${count} channels`;
    });
  });

  modal.querySelector("#renumber-cancel").addEventListener("click", ()=> modal.remove());
  modal.addEventListener("click", e=>{ if(e.target === modal) modal.remove(); });

  modal.querySelector("#renumber-apply").addEventListener("click", async ()=>{
    const start = parseInt(modal.querySelector("#renumber-start").value) || 1;
    const payload = {start};
    if(scope === "filtered"){
      payload.ids = filtered.map(ch=>ch.id);
    }
    const applyBtn = modal.querySelector("#renumber-apply");
    applyBtn.disabled = true;
    applyBtn.textContent = "Numbering...";
    const result = await apiPost("/channels/renumber", payload);
    toast(`Numbered ${result.updated} channels starting at ${start}`, "success");
    modal.remove();
    loadChannels();
  });
});

// View toggle
$("#btn-view-table").addEventListener("click", ()=>{
  channelViewMode = "table";
  $("#btn-view-table").classList.add("active");
  $("#btn-view-tiles").classList.remove("active");
  renderChannels();
});
$("#btn-view-tiles").addEventListener("click", ()=>{
  channelViewMode = "tiles";
  $("#btn-view-tiles").classList.add("active");
  $("#btn-view-table").classList.remove("active");
  renderChannels();
});

// ── Edit Modal ───────────────────────────────────────────────────────────
function openEditModal(id){
  const ch = channels.find(c=>c.id===id);
  if(!ch) return;
  editingId = id;
  $("#edit-chno").value = ch.channel_number || "";
  $("#edit-title").value = ch.title||"";
  $("#edit-title-override").value = ch.title_override || "";
  $("#edit-tags").value = (ch.tags||[]).join(", ");
  $("#edit-active").checked = ch.active;
  $("#edit-modal-title").textContent = "Edit: " + (ch.title||"Untitled");

  // Logo preview
  const logoImg = $("#edit-logo-img");
  const logoStatus = $("#edit-logo-status");
  logoStatus.textContent = "";
  if(ch.logo_cached){
    logoImg.src = `/logo/${ch.id}?t=${Date.now()}`;
    logoImg.classList.remove("no-logo");
  } else {
    logoImg.src = "";
    logoImg.classList.add("no-logo");
    logoStatus.textContent = "No logo cached";
  }
  logoImg.onerror = ()=>{ logoImg.classList.add("no-logo"); };

  $("#edit-modal").classList.remove("hidden");
}

function closeEditModal(){
  $("#edit-modal").classList.add("hidden");
  editingId = null;
}

// Logo upload
$("#edit-logo-file").addEventListener("change", async (e)=>{
  const file = e.target.files[0];
  if(!file || !editingId) return;
  const logoStatus = $("#edit-logo-status");
  logoStatus.textContent = "Uploading...";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const r = await fetch(`/logo/${editingId}`, {method:"POST", body:formData});
    const d = await r.json();
    if(d.ok){
      logoStatus.textContent = "Logo saved";
      const logoImg = $("#edit-logo-img");
      logoImg.src = `/logo/${editingId}?t=${Date.now()}`;
      logoImg.classList.remove("no-logo");
      // Mark as cached in local data
      const ch = channels.find(c=>c.id===editingId);
      if(ch) ch.logo_cached = true;
      toast("Logo uploaded", "success");
    } else {
      logoStatus.textContent = d.error || "Upload failed";
    }
  } catch(err){
    logoStatus.textContent = "Upload failed";
  }
  e.target.value = "";  // reset file input
});

$("#edit-modal-close").addEventListener("click", closeEditModal);
$("#edit-cancel").addEventListener("click", closeEditModal);
$("#edit-modal").addEventListener("click", e=>{
  if(e.target.classList.contains("modal-overlay")) closeEditModal();
});

$("#edit-save").addEventListener("click", async ()=>{
  if(!editingId) return;
  const tags = $("#edit-tags").value.split(",").map(s=>s.trim()).filter(Boolean);
  await apiPut(`/channels/${editingId}`, {
    title: $("#edit-title").value,
    title_override: $("#edit-title-override").value || null,
    channel_number: $("#edit-chno").value ? parseInt($("#edit-chno").value) : null,
    tags: tags,
    active: $("#edit-active").checked,
  });
  toast("Channel updated", "success");
  closeEditModal();
  loadChannels();
});

$("#edit-delete").addEventListener("click", async ()=>{
  if(!editingId) return;
  if(!confirm("Delete this channel?")) return;
  await apiDelete(`/channels/${editingId}`);
  toast("Channel deleted", "success");
  closeEditModal();
  loadChannels();
});

// ── Generate ─────────────────────────────────────────────────────────────
$("#btn-generate").addEventListener("click", async ()=>{
  toast("Regenerating...", "info");
  const res = await apiPost("/generate");
  const epgMsg = res.real_epg !== undefined
    ? `XMLTV (${res.xmltv_channels} ch: ${res.real_epg} real, ${res.dummy_epg} dummy)`
    : `XMLTV (${res.xmltv_channels} ch)`;
  toast(`Generated M3U (${res.m3u_channels} ch) + ${epgMsg}`, "success");
  loadChannels();
});

// ── M3U Sources ─────────────────────────────────────────────────────────
const sourcesBulk = setupBulk("sources",
  ()=> $$("input[data-bulk-sources]:checked").map(cb=>cb.dataset.bulkSources),
  async (ids)=>{
    await apiPost("/m3u-sources/bulk-delete", {ids});
    toast(`Deleted ${ids.length} sources`, "success");
    loadM3uSources();
  }
);

async function loadM3uSources(){
  m3uSources = await api("/m3u-sources");
  renderM3uSources();
}

function renderM3uSources(){
  sourcesBulk.reset();
  const body = $("#m3u-sources-body");
  if(!m3uSources.length){
    body.innerHTML = '<tr><td colspan="6" class="empty-state">No M3U sources configured. Click "Add Source" to get started.</td></tr>';
    return;
  }
  body.innerHTML = m3uSources.map(s=>{
    const urlShort = s.url.length > 60 ? s.url.substring(0,57) + "..." : s.url;
    const mode = s.stream_mode || "passthrough";
    return `<tr>
      <td class="td-check"><input type="checkbox" data-bulk-sources="${s.id}"></td>
      <td>${s.name}</td>
      <td class="url-cell" title="${s.url}">${urlShort}</td>
      <td>
        <select class="filter-status stream-mode-select" data-source-mode="${s.id}" style="font-size:12px;padding:4px 8px">
          <option value="passthrough" ${mode==="passthrough"?"selected":""}>Passthrough</option>
          <option value="ffmpeg" ${mode==="ffmpeg"?"selected":""}>FFmpeg</option>
        </select>
      </td>
      <td>${s.channel_count}</td>
      <td>${fmtDate(s.last_ingested_at)}</td>
      <td class="td-actions"><div class="action-btns">
        <button class="btn-sm" data-ingest-source="${s.id}">Ingest</button>
        <button class="btn-sm btn-sm-danger" data-delete-source="${s.id}">Delete</button>
      </div></td>
    </tr>`;
  }).join("");

  sourcesBulk.bindRows(body);

  // Stream mode change
  $$("[data-source-mode]", body).forEach(sel=>{
    sel.addEventListener("change", async ()=>{
      await apiPut(`/m3u-sources/${sel.dataset.sourceMode}`, {stream_mode: sel.value});
      toast(`Stream mode set to ${sel.value}`, "success");
    });
  });

  $$("[data-ingest-source]", body).forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      btn.disabled = true;
      btn.textContent = "Ingesting...";
      toast("Ingesting playlist...", "info");
      const res = await apiPost(`/m3u-sources/${btn.dataset.ingestSource}/ingest`);
      btn.disabled = false;
      btn.textContent = "Ingest";
      if(res.error){
        toast("Ingest failed: " + res.error, "error");
      } else {
        toast(`Ingested ${res.channels} channels`, "success");
        loadM3uSources();
      }
    });
  });

  $$("[data-delete-source]", body).forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      if(!confirm("Delete this M3U source?")) return;
      await apiDelete(`/m3u-sources/${btn.dataset.deleteSource}`);
      toast("Source deleted", "success");
      loadM3uSources();
    });
  });
}

// Add source modal
function openAddSourceModal(){
  $("#add-source-name").value = "";
  $("#add-source-url").value = "";
  $("#add-source-modal").classList.remove("hidden");
}
function closeAddSourceModal(){
  $("#add-source-modal").classList.add("hidden");
}

$("#btn-add-source").addEventListener("click", openAddSourceModal);
$("#add-source-modal-close").addEventListener("click", closeAddSourceModal);
$("#add-source-cancel").addEventListener("click", closeAddSourceModal);
$("#add-source-modal").addEventListener("click", e=>{
  if(e.target.classList.contains("modal-overlay")) closeAddSourceModal();
});

// File browser for M3U source
$("#add-source-browse").addEventListener("click", ()=>{
  const fb = $("#file-browser");
  if(fb.classList.contains("hidden")){
    loadFileBrowser("/browse");
    fb.classList.remove("hidden");
  } else {
    fb.classList.add("hidden");
  }
});

async function loadFileBrowser(path){
  const res = await api(`/browse?path=${encodeURIComponent(path)}`);
  if(res.error){ toast(res.error, "error"); return; }
  $("#fb-path").textContent = res.path;
  const list = $("#fb-list");
  let html = "";
  if(res.parent !== null){
    html += `<div class="fb-entry fb-up" data-fb-dir="${res.parent}"><span class="fb-icon">&#x2B06;</span><span class="fb-name">..</span></div>`;
  }
  for(const e of res.entries){
    if(e.type === "dir"){
      html += `<div class="fb-entry fb-dir" data-fb-dir="${e.path}"><span class="fb-icon">&#x1F4C1;</span><span class="fb-name">${e.name}</span></div>`;
    } else {
      html += `<div class="fb-entry fb-file" data-fb-file="${e.path}"><span class="fb-icon">&#x1F4C4;</span><span class="fb-name">${e.name}</span></div>`;
    }
  }
  if(!res.entries.length && res.parent === null) html = '<div style="padding:12px;color:var(--muted)">Empty</div>';
  list.innerHTML = html;

  $$("[data-fb-dir]", list).forEach(el=>{
    el.addEventListener("click", ()=> loadFileBrowser(el.dataset.fbDir));
  });
  $$("[data-fb-file]", list).forEach(el=>{
    el.addEventListener("click", ()=>{
      $("#add-source-url").value = el.dataset.fbFile;
      // Auto-fill name from filename if empty
      if(!$("#add-source-name").value.trim()){
        const parts = el.dataset.fbFile.split("/");
        const fname = parts[parts.length-1].replace(/\.[^.]+$/, "");
        $("#add-source-name").value = fname;
      }
      $("#file-browser").classList.add("hidden");
    });
  });
}

$("#add-source-save").addEventListener("click", async ()=>{
  const name = $("#add-source-name").value.trim();
  const url = $("#add-source-url").value.trim();
  if(!name || !url){
    toast("Name and URL are required", "error");
    return;
  }
  const res = await apiPost("/m3u-sources", {name, url});
  if(res.error){
    toast(res.error, "error");
    return;
  }
  toast("Source added", "success");
  closeAddSourceModal();
  loadM3uSources();
});

// Ingest All
$("#btn-ingest-all").addEventListener("click", async ()=>{
  if(!m3uSources.length){
    toast("No sources to ingest", "error");
    return;
  }
  const btn = $("#btn-ingest-all");
  btn.disabled = true;
  btn.textContent = "Ingesting...";
  toast("Ingesting all sources...", "info");
  const res = await apiPost("/m3u-sources/ingest");
  btn.disabled = false;
  btn.textContent = "Ingest All";
  if(res.error){
    toast("Ingest failed: " + res.error, "error");
  } else {
    toast(`Ingested ${res.channels} channels from ${res.ingested} sources`, "success");
    loadM3uSources();
    loadChannels();
  }
});

// ── EPG Sources ─────────────────────────────────────────────────────────
let epgSources = [];

const epgsourcesBulk = setupBulk("epgsources",
  ()=> $$("input[data-bulk-epgsources]:checked").map(cb=>cb.dataset.bulkEpgsources),
  async (ids)=>{
    await apiPost("/epg-sources/bulk-delete", {ids});
    toast(`Deleted ${ids.length} EPG sources`, "success");
    loadEpgSources();
  }
);

async function loadEpgSources(){
  epgSources = await api("/epg-sources");
  renderEpgSources();
}

function renderEpgSources(){
  epgsourcesBulk.reset();
  const body = $("#epg-sources-body");
  if(!epgSources.length){
    body.innerHTML = '<tr><td colspan="7" class="empty-state">No EPG sources configured. Click "Add EPG Source" to get started.</td></tr>';
    return;
  }
  body.innerHTML = epgSources.map(s=>{
    const urlShort = s.url.length > 50 ? s.url.substring(0,47) + "..." : s.url;
    return `<tr>
      <td class="td-check"><input type="checkbox" data-bulk-epgsources="${s.id}"></td>
      <td>${s.name}</td>
      <td><span class="tag-pill tag-sports">${s.m3u_source_name}</span></td>
      <td class="url-cell" title="${s.url}">${urlShort}</td>
      <td>${s.channel_count}</td>
      <td>${fmtDate(s.last_ingested_at)}</td>
      <td class="td-actions"><div class="action-btns">
        <button class="btn-sm" data-ingest-epg="${s.id}">Ingest</button>
        <button class="btn-sm btn-sm-danger" data-delete-epg="${s.id}">Delete</button>
      </div></td>
    </tr>`;
  }).join("");

  epgsourcesBulk.bindRows(body);

  $$("[data-ingest-epg]", body).forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      btn.disabled = true;
      btn.textContent = "Ingesting...";
      toast("Ingesting EPG...", "info");
      const res = await apiPost(`/epg-sources/${btn.dataset.ingestEpg}/ingest`);
      btn.disabled = false;
      btn.textContent = "Ingest";
      if(res.error){
        toast("EPG ingest failed: " + res.error, "error");
      } else {
        toast(`Matched ${res.channels} of ${res.total_xmltv||"?"} EPG channels`, "success");
        loadEpgSources();
      }
    });
  });

  $$("[data-delete-epg]", body).forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      if(!confirm("Delete this EPG source and its data?")) return;
      await apiDelete(`/epg-sources/${btn.dataset.deleteEpg}`);
      toast("EPG source deleted", "success");
      loadEpgSources();
    });
  });
}

// Add EPG Source modal
async function openAddEpgSourceModal(){
  $("#add-epg-source-name").value = "";
  $("#add-epg-source-url").value = "";
  // Populate M3U source dropdown
  const sources = await api("/m3u-sources");
  const sel = $("#add-epg-source-m3u");
  sel.innerHTML = '<option value="">-- Select M3U Source --</option>' +
    sources.map(s=>`<option value="${s.id}">${s.name}</option>`).join("");
  $("#add-epg-source-modal").classList.remove("hidden");
}
function closeAddEpgSourceModal(){
  $("#add-epg-source-modal").classList.add("hidden");
}

$("#btn-add-epg-source").addEventListener("click", openAddEpgSourceModal);
$("#add-epg-source-modal-close").addEventListener("click", closeAddEpgSourceModal);
$("#add-epg-source-cancel").addEventListener("click", closeAddEpgSourceModal);
$("#add-epg-source-modal").addEventListener("click", e=>{
  if(e.target.classList.contains("modal-overlay")) closeAddEpgSourceModal();
});

$("#add-epg-source-save").addEventListener("click", async ()=>{
  const name = $("#add-epg-source-name").value.trim();
  const url = $("#add-epg-source-url").value.trim();
  const m3u_source_id = $("#add-epg-source-m3u").value;
  if(!name || !url || !m3u_source_id){
    toast("Name, URL, and linked M3U source are required", "error");
    return;
  }
  const res = await apiPost("/epg-sources", {name, url, m3u_source_id});
  if(res.error){
    toast(res.error, "error");
    return;
  }
  toast("EPG source added", "success");
  closeAddEpgSourceModal();
  loadEpgSources();
});

// Ingest All EPG
$("#btn-ingest-all-epg").addEventListener("click", async ()=>{
  if(!epgSources.length){
    toast("No EPG sources to ingest", "error");
    return;
  }
  const btn = $("#btn-ingest-all-epg");
  btn.disabled = true;
  btn.textContent = "Ingesting...";
  toast("Ingesting all EPG sources...", "info");
  const res = await apiPost("/epg-sources/ingest");
  btn.disabled = false;
  btn.textContent = "Ingest All";
  if(res.error){
    toast("EPG ingest failed: " + res.error, "error");
  } else {
    toast(`Ingested ${res.channels} channels from ${res.ingested} EPG sources`, "success");
    loadEpgSources();
  }
});

// ── EPG Data ─────────────────────────────────────────────────────────────
const epgBulk = setupBulk("epg",
  ()=> $$("input[data-bulk-epg]:checked").map(cb=>cb.dataset.bulkEpg),
  async (ids)=>{
    await apiPost("/epg/bulk-delete", {ids});
    toast(`Deleted ${ids.length} EPG entries`, "success");
    loadEpg();
  }
);

async function loadEpg(){
  epgEntries = await api("/epg");
  renderEpg();
}

function renderEpg(){
  epgBulk.reset();
  const search = ($("#epg-search").value||"").toLowerCase();
  let filtered = epgEntries.filter(e=>{
    if(search && !e.channel_name.toLowerCase().includes(search) && !e.channel_id.toLowerCase().includes(search)) return false;
    return true;
  });

  const body = $("#epg-body");
  if(!filtered.length){
    body.innerHTML = '<tr><td colspan="4" class="empty-state">No EPG entries</td></tr>';
    return;
  }
  body.innerHTML = filtered.map(e=>`<tr>
    <td class="td-check"><input type="checkbox" data-bulk-epg="${e.id}"></td>
    <td>${e.channel_id}</td>
    <td>${e.channel_name}</td>
    <td>${fmtDate(e.last_updated)}</td>
  </tr>`).join("");

  epgBulk.bindRows(body);
}

$("#epg-search").addEventListener("input", renderEpg);

// ── Logs ─────────────────────────────────────────────────────────────────
async function loadLogs(){
  const res = await api(`/logs/tail?pos=${logPos}`);
  if(res.lines){
    const out = $("#log-output");
    out.textContent += res.lines;
    logPos = res.pos;
    const container = out.parentElement;
    container.scrollTop = container.scrollHeight;
  }
}

$("#clear-log").addEventListener("click", ()=>{
  $("#log-output").textContent = "";
});

// ── System Stats ─────────────────────────────────────────────────────────
const cpuHistory = [];
const ramHistory = [];
const MAX_POINTS = 60;

async function loadStats(){
  const s = await api("/system/stats");

  $("#cpu-live").textContent = s.cpu_percent + "%";
  $("#ram-live").textContent = s.ram_percent + "%";
  $("#disk-live").textContent = s.disk_used_gb + " / " + s.disk_total_gb + " GB";

  cpuHistory.push(s.cpu_percent);
  ramHistory.push(s.ram_percent);
  if(cpuHistory.length > MAX_POINTS) cpuHistory.shift();
  if(ramHistory.length > MAX_POINTS) ramHistory.shift();

  drawChart("cpu-chart", cpuHistory, "var(--accent)", 100);
  drawChart("ram-chart", ramHistory, "var(--accent-2)", 100);
  drawDisk("disk-chart", s.disk_percent, s.disk_used_gb, s.disk_total_gb);
}

function drawChart(id, data, color, max){
  const el = document.getElementById(id);
  if(!data.length){ el.innerHTML = ""; return; }
  const w = el.clientWidth, h = el.clientHeight;
  if(!w||!h) return;
  const pad = 4;
  const points = data.map((v,i)=>{
    const x = pad + (i/(Math.max(data.length-1,1)))*(w-pad*2);
    const y = h - pad - (v/max)*(h-pad*2);
    return `${x},${y}`;
  });
  const line = points.join(" ");
  const area = `${pad},${h-pad} ${line} ${pad + ((data.length-1)/Math.max(data.length-1,1))*(w-pad*2)},${h-pad}`;

  el.innerHTML = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polygon points="${area}" fill="${color}" class="chart-area"/>
    <polyline points="${line}" stroke="${color}" class="chart-line"/>
  </svg>`;
}

function drawDisk(id, pct, used, total){
  const el = document.getElementById(id);
  const color = pct > 90 ? "var(--danger)" : pct > 70 ? "var(--warn)" : "var(--accent)";
  el.innerHTML = `<div class="disk-bar-wrap">
    <div class="disk-pct" style="color:${color}">${pct}%</div>
    <div class="disk-bar"><div class="disk-bar-fill" style="width:${pct}%;background:${color}"></div></div>
    <div class="disk-info"><span>${used} GB used</span><span>${total} GB total</span></div>
  </div>`;
}

// ── Settings ─────────────────────────────────────────────────────────────
async function loadSettings(){
  settings = await api("/settings");
  renderSettings();
  $("#sidebar-db").textContent = settings.pg_host + ":" + settings.pg_port + "/" + settings.pg_db;
}

function renderSettings(){
  const container = $("#settings-container");
  const title = $("#settings-section-title");
  clearInterval(tasksTimer); tasksTimer = null;
  clearInterval(enrichStatusTimer); enrichStatusTimer = null;

  if(currentSettingsSub === "general"){
    title.textContent = "General";
    container.innerHTML = `<div class="settings-fields">
      <div class="setting-field"><label>PostgreSQL Host</label><input type="text" value="${settings.pg_host||""}" disabled></div>
      <div class="setting-field"><label>PostgreSQL Port</label><input type="text" value="${settings.pg_port||""}" disabled></div>
      <div class="setting-field"><label>Database</label><input type="text" value="${settings.pg_db||""}" disabled></div>
    </div>`;
  } else if(currentSettingsSub === "tasks"){
    title.textContent = "Tasks";
    container.innerHTML = '<div id="tasks-container"><div class="empty-state">Loading tasks...</div></div>';
    renderTasksView();
  } else if(currentSettingsSub === "scheduler"){
    title.textContent = "Scheduler";
    container.innerHTML = `<div class="settings-fields">
      <div class="setting-field">
        <label>M3U/XMLTV Regen Interval (minutes)</label>
        <input type="number" id="set-regen" value="${settings.scheduler_regen_minutes||5}" min="1">
      </div>
      <div class="setting-field">
        <label>Event Cleanup Interval (hours)</label>
        <input type="number" id="set-cleanup" value="${settings.scheduler_cleanup_hours||1}" min="1">
      </div>
      <div class="setting-field">
        <label>Auto-Activation Interval (hours)</label>
        <input type="number" id="set-activation" value="${settings.scheduler_activation_hours||4}" min="1">
      </div>
    </div>`;
  } else if(currentSettingsSub === "images"){
    title.textContent = "Images";
    container.innerHTML = '<div id="images-container"><div class="empty-state">Loading...</div></div>';
    renderImagesView();
  } else if(currentSettingsSub === "epg-settings"){
    title.textContent = "EPG Settings";
    container.innerHTML = `<div class="settings-fields">
      <div class="setting-field">
        <label>Dummy EPG Duration (days)</label>
        <input type="number" id="set-dummy-days" value="${settings.dummy_epg_days||7}" min="1" max="14">
        <span class="field-hint">Channels without real EPG data get dummy programme blocks for this many days</span>
      </div>
      <div class="setting-field">
        <label>Dummy EPG Block Length (minutes)</label>
        <select id="set-dummy-block" class="filter-status" style="width:100%">
          <option value="30" ${(settings.dummy_epg_block_minutes||"30")==="30"?"selected":""}>30 minutes</option>
          <option value="60" ${settings.dummy_epg_block_minutes==="60"?"selected":""}>1 hour</option>
          <option value="120" ${settings.dummy_epg_block_minutes==="120"?"selected":""}>2 hours</option>
          <option value="180" ${settings.dummy_epg_block_minutes==="180"?"selected":""}>3 hours</option>
        </select>
      </div>
    </div>`;
  } else if(currentSettingsSub === "stream"){
    title.textContent = "Stream Bridge";
    container.innerHTML = `<div class="settings-fields">
      <div class="setting-field">
        <label>Bridge Host</label>
        <input type="text" id="set-bridge-host" value="${settings.bridge_host||""}" placeholder="192.168.20.34">
      </div>
      <div class="setting-field">
        <label>Bridge Port</label>
        <input type="text" id="set-bridge-port" value="${settings.bridge_port||""}" placeholder="8080">
      </div>
    </div>`;
  }
}

// ── Tasks View ──────────────────────────────────────────────────────────
const TASK_INTERVAL_OPTIONS = {
  m3u_xmltv_regen: [
    {label:"1 minute", seconds:60},
    {label:"2 minutes", seconds:120},
    {label:"5 minutes", seconds:300},
    {label:"10 minutes", seconds:600},
    {label:"15 minutes", seconds:900},
    {label:"30 minutes", seconds:1800},
    {label:"1 hour", seconds:3600},
  ],
  image_enrichment: [
    {label:"1 hour", seconds:3600},
    {label:"2 hours", seconds:7200},
    {label:"4 hours", seconds:14400},
    {label:"6 hours", seconds:21600},
    {label:"12 hours", seconds:43200},
    {label:"24 hours", seconds:86400},
  ],
  event_cleanup: [
    {label:"30 minutes", seconds:1800},
    {label:"1 hour", seconds:3600},
    {label:"2 hours", seconds:7200},
    {label:"4 hours", seconds:14400},
  ],
  logo_sync: [
    {label:"15 minutes", seconds:900},
    {label:"30 minutes", seconds:1800},
    {label:"1 hour", seconds:3600},
    {label:"2 hours", seconds:7200},
  ],
  stream_cleanup: [
    {label:"30 seconds", seconds:30},
    {label:"60 seconds", seconds:60},
    {label:"120 seconds", seconds:120},
  ],
  m3u_refresh: [
    {label:"1 hour", seconds:3600},
    {label:"2 hours", seconds:7200},
    {label:"4 hours", seconds:14400},
    {label:"6 hours", seconds:21600},
    {label:"12 hours", seconds:43200},
    {label:"24 hours", seconds:86400},
  ],
  epg_refresh: [
    {label:"4 hours", seconds:14400},
    {label:"6 hours", seconds:21600},
    {label:"12 hours", seconds:43200},
    {label:"24 hours", seconds:86400},
  ],
};

const TASK_DISPLAY_NAMES = {
  m3u_xmltv_regen: "M3U + XMLTV Regeneration",
  m3u_refresh: "M3U Playlist Refresh",
  epg_refresh: "EPG Data Refresh",
  image_enrichment: "Programme Image Enrichment",
  event_cleanup: "Event Cleanup",
  logo_sync: "Logo Sync",
  stream_cleanup: "Stream Cleanup",
};

function fmtRelative(isoStr){
  if(!isoStr) return "unknown";
  const diff = new Date(isoStr).getTime() - Date.now();
  if(diff < 0) return "now";
  const secs = Math.floor(diff / 1000);
  if(secs < 60) return `in ${secs}s`;
  const mins = Math.floor(secs / 60);
  if(mins < 60) return `in ${mins} min`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  if(remMins === 0) return `in ${hrs}h`;
  return `in ${hrs}h ${remMins}m`;
}

let _enrichStatus = null;

async function renderTasksView(){
  const container = $("#tasks-container");
  if(!container) return;

  let jobs = [];
  try { jobs = await api("/scheduler"); } catch(e){ container.innerHTML = '<div class="empty-state">Failed to load scheduler data</div>'; return; }

  // Try to get enrichment status
  try { _enrichStatus = await api("/images/status"); } catch(e){ _enrichStatus = null; }

  container.innerHTML = jobs.map(job => {
    const name = TASK_DISPLAY_NAMES[job.id] || job.name || job.id;
    const options = TASK_INTERVAL_OPTIONS[job.id] || [];
    const isEnrichment = job.id === "image_enrichment";
    const isRunning = isEnrichment && _enrichStatus && _enrichStatus.running;

    let selectHtml = "";
    if(options.length){
      selectHtml = `<select class="task-select" data-task-interval="${job.id}">` +
        options.map(o => `<option value="${o.seconds}" ${Math.abs(job.interval_seconds - o.seconds) < 5 ? "selected" : ""}>${o.label}</option>`).join("") +
        `</select>`;
    }

    let actionsHtml = `<button class="btn-sm" data-task-run="${job.id}">Run Now</button>`;
    if(isEnrichment && isRunning){
      actionsHtml += ` <button class="btn-sm btn-sm-danger" data-task-stop="image_enrichment">Stop</button>`;
    }

    let extraHtml = "";
    if(isEnrichment && _enrichStatus){
      if(isRunning){
        const pct = _enrichStatus.total > 0 ? Math.round((_enrichStatus.processed / _enrichStatus.total) * 100) : 0;
        extraHtml = `<div class="task-progress">
          <div class="task-status">${_enrichStatus.processed}/${_enrichStatus.total} processed (${_enrichStatus.cached||0} cached, ${_enrichStatus.downloaded||0} new, ${_enrichStatus.failed||0} failed)</div>
          <div class="task-progress-bar"><div class="task-progress-fill" style="width:${pct}%"></div></div>
          <div class="task-status" style="margin-top:2px">${pct}%</div>
        </div>`;
      }
    }

    return `<div class="task-card" data-task-id="${job.id}">
      <div class="task-header">
        <span class="task-name">${name}</span>
        <div class="task-actions">${actionsHtml}</div>
      </div>
      <div class="task-meta">
        <span>Every: ${selectHtml}</span>
        <span class="task-next">Next: ${fmtRelative(job.next_run)}</span>
      </div>
      ${extraHtml}
    </div>`;
  }).join("");

  // Bind interval change handlers
  $$("[data-task-interval]", container).forEach(sel => {
    sel.addEventListener("change", async () => {
      const jobId = sel.dataset.taskInterval;
      const seconds = parseInt(sel.value);
      try {
        await apiPut(`/scheduler/${jobId}`, {interval_seconds: seconds});
        toast("Interval updated", "success");
      } catch(e){ toast("Failed to update interval", "error"); }
    });
  });

  // Bind run now handlers
  $$("[data-task-run]", container).forEach(btn => {
    btn.addEventListener("click", async () => {
      const jobId = btn.dataset.taskRun;
      btn.disabled = true;
      btn.textContent = "Running...";
      try {
        await apiPost(`/scheduler/${jobId}/run`);
        toast("Task triggered", "success");
      } catch(e){ toast("Failed to trigger task", "error"); }
      btn.disabled = false;
      btn.textContent = "Run Now";
      if(jobId === "image_enrichment") startEnrichPoll();
      setTimeout(()=> renderTasksView(), 1000);
    });
  });

  // Bind stop handler
  $$("[data-task-stop]", container).forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await apiPost("/images/stop");
        toast("Enrichment stopped", "success");
      } catch(e){ toast("Failed to stop enrichment", "error"); }
      btn.disabled = false;
      setTimeout(()=> renderTasksView(), 500);
    });
  });

  // Start polling
  clearInterval(tasksTimer);
  tasksTimer = setInterval(()=> renderTasksView(), 10000);

  // If enrichment is running, poll faster for progress
  if(_enrichStatus && _enrichStatus.running){
    startEnrichPoll();
  } else {
    clearInterval(enrichStatusTimer); enrichStatusTimer = null;
  }
}

function startEnrichPoll(){
  clearInterval(enrichStatusTimer);
  enrichStatusTimer = setInterval(async () => {
    if(currentSettingsSub !== "tasks"){ clearInterval(enrichStatusTimer); enrichStatusTimer = null; return; }
    try {
      _enrichStatus = await api("/images/status");
      // Update progress in-place if the card exists
      const card = $('[data-task-id="image_enrichment"]');
      if(!card) return;
      if(_enrichStatus && _enrichStatus.running){
        const pct = _enrichStatus.total > 0 ? Math.round((_enrichStatus.processed / _enrichStatus.total) * 100) : 0;
        let progEl = $(".task-progress", card);
        if(!progEl){
          progEl = document.createElement("div");
          progEl.className = "task-progress";
          card.appendChild(progEl);
        }
        progEl.innerHTML = `<div class="task-status">${_enrichStatus.processed}/${_enrichStatus.total} processed (${_enrichStatus.cached||0} cached, ${_enrichStatus.downloaded||0} new, ${_enrichStatus.failed||0} failed)</div>
          <div class="task-progress-bar"><div class="task-progress-fill" style="width:${pct}%"></div></div>
          <div class="task-status" style="margin-top:2px">${pct}%</div>`;
        // Ensure stop button exists
        const actions = $(".task-actions", card);
        if(actions && !$("[data-task-stop]", actions)){
          const stopBtn = document.createElement("button");
          stopBtn.className = "btn-sm btn-sm-danger";
          stopBtn.dataset.taskStop = "image_enrichment";
          stopBtn.textContent = "Stop";
          stopBtn.addEventListener("click", async () => {
            stopBtn.disabled = true;
            try { await apiPost("/images/stop"); toast("Enrichment stopped", "success"); } catch(e){ toast("Failed to stop", "error"); }
            stopBtn.disabled = false;
            setTimeout(()=> renderTasksView(), 500);
          });
          actions.appendChild(stopBtn);
        }
      } else {
        // Enrichment finished, do a full re-render and stop fast polling
        clearInterval(enrichStatusTimer); enrichStatusTimer = null;
        renderTasksView();
      }
    } catch(e){}
  }, 2000);
}

// ── Images View ─────────────────────────────────────────────────────────
async function renderImagesView(){
  const container = $("#images-container");
  if(!container) return;

  let stats = null;
  try { stats = await api("/images/stats"); } catch(e){}

  const tmdbKey = settings.tmdb_api_key || "";
  const fanartKey = settings.fanart_api_key || "";

  let statsHtml = "";
  if(stats){
    const pct = stats.total_programs > 0 ? Math.round((stats.cached_images / stats.total_programs) * 100) : 0;
    statsHtml = `<div class="image-stats">
      <h4 style="margin:0 0 12px;font-size:15px;color:var(--muted)">Programme Image Stats</h4>
      <div class="image-stat-row"><span class="image-stat-label">Total programmes seen</span><span class="image-stat-value">${stats.total_programs}</span></div>
      <div class="image-stat-row"><span class="image-stat-label">With images</span><span class="image-stat-value">${stats.cached_images} (${pct}%)</span></div>
      <div class="image-stat-row"><span class="image-stat-label">Missing images</span><span class="image-stat-value">${stats.missing_images}</span></div>
    </div>
    <div class="image-actions">
      <button class="btn btn-primary" id="btn-enrich-now">Enrich Now</button>
    </div>`;
  }

  container.innerHTML = `<div class="settings-fields">
    <div class="setting-field">
      <label>TMDB API Key</label>
      <input type="text" id="set-tmdb-key" value="${tmdbKey}" placeholder="Enter your TMDB API key">
      <span class="field-hint">Get a free key at <a href="https://www.themoviedb.org/settings/api" target="_blank" style="color:var(--accent)">themoviedb.org</a></span>
    </div>
    <div class="setting-field">
      <label>Fanart.tv API Key <span style="color:var(--muted);font-weight:400">(optional)</span></label>
      <input type="text" id="set-fanart-key" value="${fanartKey}" placeholder="Enter your Fanart.tv API key">
      <span class="field-hint">Get a free key at <a href="https://fanart.tv/get-an-api-key/" target="_blank" style="color:var(--accent)">fanart.tv</a> — adds high-quality fan art for shows TMDB misses</span>
    </div>
  </div>
  <p style="font-size:12px;color:var(--muted);margin:12px 0 0">Search chain: TMDB → TVMaze → Fanart.tv → Google Images</p>
  ${statsHtml}`;

  const enrichBtn = $("#btn-enrich-now");
  if(enrichBtn){
    enrichBtn.addEventListener("click", async () => {
      enrichBtn.disabled = true;
      enrichBtn.textContent = "Starting...";
      try {
        await apiPost("/images/enrich");
        toast("Image enrichment started", "success");
      } catch(e){ toast("Failed to start enrichment", "error"); }
      enrichBtn.disabled = false;
      enrichBtn.textContent = "Enrich Now";
    });
  }
}

$("#save-settings").addEventListener("click", async ()=>{
  const payload = {};
  const regen = $("#set-regen");
  const cleanup = $("#set-cleanup");
  const activation = $("#set-activation");
  const host = $("#set-bridge-host");
  const port = $("#set-bridge-port");
  const tmdbKey = $("#set-tmdb-key");

  if(regen) payload.scheduler_regen_minutes = regen.value;
  if(cleanup) payload.scheduler_cleanup_hours = cleanup.value;
  if(activation) payload.scheduler_activation_hours = activation.value;
  if(host) payload.bridge_host = host.value;
  if(port) payload.bridge_port = port.value;
  if(tmdbKey) payload.tmdb_api_key = tmdbKey.value;
  const fanartKey = $("#set-fanart-key");
  if(fanartKey) payload.fanart_api_key = fanartKey.value;
  const dummyDays = $("#set-dummy-days");
  const dummyBlock = $("#set-dummy-block");
  if(dummyDays) payload.dummy_epg_days = dummyDays.value;
  if(dummyBlock) payload.dummy_epg_block_minutes = dummyBlock.value;

  await apiPost("/settings", payload);
  const status = $("#settings-status");
  status.textContent = "Saved";
  status.className = "settings-status success";
  setTimeout(()=>{ status.textContent = ""; status.className = "settings-status"; }, 2000);
  toast("Settings saved", "success");
});

// ── Guide ────────────────────────────────────────────────────────────────
let guideHours = 6;
let guideOffset = 0; // days offset from today
let guideTimer = null;

async function loadGuide(){
  const now = new Date();
  now.setHours(now.getHours() - 1); // start 1 hour ago
  now.setDate(now.getDate() + guideOffset);
  const startIso = now.toISOString();

  $("#guide-date").textContent = now.toLocaleDateString(undefined, {weekday:"short", month:"short", day:"numeric"});

  const res = await api(`/guide?hours=${guideHours}&start=${startIso}`);
  renderGuide(res);

  clearInterval(guideTimer);
  guideTimer = setInterval(()=> updateNowLine(res), 60000);
}

function renderGuide(data){
  const grid = $("#guide-grid");
  if(!data.channels || !data.channels.length){
    grid.innerHTML = '<div class="guide-empty">No guide data. Activate channels and regenerate.</div>';
    return;
  }

  const wStart = new Date(data.start).getTime();
  const wEnd = new Date(data.end).getTime();
  const wDur = wEnd - wStart;
  const pxPerMs = 4000 / wDur; // 4000px total timeline width

  // Build channels column
  let chHtml = '<div class="guide-channels"><div class="guide-ch-row" style="height:36px"></div>';
  data.channels.forEach(ch=>{
    const logo = ch.logo ? `<img class="guide-ch-logo" src="${ch.logo}" onerror="this.classList.add('no-logo')">` : "";
    chHtml += `<div class="guide-ch-row">${logo}<span class="guide-ch-name" title="${ch.name}">${ch.name}</span></div>`;
  });
  chHtml += '</div>';

  // Build timeline
  let tlHtml = '<div class="guide-timeline" id="guide-tl"><div class="guide-time-header">';
  const hourMs = 3600000;
  const firstHour = new Date(Math.ceil(wStart / hourMs) * hourMs);
  for(let t = firstHour.getTime(); t < wEnd; t += hourMs){
    const w = Math.min(hourMs, wEnd - t) * pxPerMs;
    const label = new Date(t).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    tlHtml += `<div class="guide-time-mark" style="width:${w}px">${label}</div>`;
  }
  tlHtml += '</div><div class="guide-rows" id="guide-rows" style="position:relative">';

  data.channels.forEach(ch=>{
    tlHtml += '<div class="guide-row">';
    ch.programmes.forEach(p=>{
      const pStart = Math.max(new Date(p.start).getTime(), wStart);
      const pStop = Math.min(new Date(p.stop).getTime(), wEnd);
      const left = (pStart - wStart) * pxPerMs;
      const width = Math.max(2, (pStop - pStart) * pxPerMs);
      const cat = (p.category||"").toLowerCase();
      let cls = "gp-default";
      if(cat.includes("movie")) cls = "gp-movie";
      else if(cat.includes("tv")) cls = "gp-tv";
      else if(cat.includes("sport")) cls = "gp-sports";
      else if(cat.includes("news")) cls = "gp-news";
      else if(cat.includes("general") || !p.desc || p.title === p.desc) cls = "gp-dummy";

      const startT = new Date(p.start).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});
      const stopT = new Date(p.stop).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});
      const tip = `${p.title}\n${startT} - ${stopT}\n${p.desc||""}`.replace(/"/g,"&quot;");
      const icon = (p.icon||"").replace(/"/g,"&quot;");
      const imgTag = icon && width > 44 ? `<img class="gp-icon" src="${icon}" onerror="this.remove()">` : "";
      const titleSpan = width > (icon && width > 44 ? 80 : 60) ? `<span class="gp-title">${p.title}</span>` : "";

      tlHtml += `<div class="guide-prog ${cls}" style="position:absolute;left:${left}px;width:${width}px" title="${tip}" data-icon="${icon}" data-desc="${(p.desc||"").replace(/"/g,"&quot;")}" data-time="${startT} - ${stopT}" data-prog-title="${p.title.replace(/"/g,"&quot;")}">${imgTag}${titleSpan}</div>`;
    });
    tlHtml += '</div>';
  });

  // Now line
  const nowPos = (Date.now() - wStart) * pxPerMs;
  tlHtml += `<div class="guide-now-line" id="guide-now" style="left:${nowPos}px"></div>`;
  tlHtml += '</div></div>';

  grid.innerHTML = chHtml + tlHtml;

  // Programme detail popover on click
  document.querySelectorAll(".guide-prog").forEach(el => {
    el.style.cursor = "pointer";
    el.addEventListener("click", e => {
      e.stopPropagation();
      const old = document.getElementById("guide-prog-detail");
      if(old) old.remove();
      const t = el.getAttribute("data-prog-title") || "";
      if(!t) return;
      const d = el.getAttribute("data-desc") || "";
      const tm = el.getAttribute("data-time") || "";
      const ic = el.getAttribute("data-icon") || "";
      const pop = document.createElement("div");
      pop.id = "guide-prog-detail";
      pop.innerHTML = `<div class="gpd-inner">`
        + (ic ? `<img class="gpd-poster" src="${ic}" onerror="this.remove()">` : "")
        + `<div class="gpd-info"><div class="gpd-title">${t}</div>`
        + `<div class="gpd-time">${tm}</div>`
        + (d ? `<div class="gpd-desc">${d}</div>` : `<div class="gpd-desc gpd-nodesc">No description</div>`)
        + `</div></div>`;
      document.body.appendChild(pop);
      const rect = el.getBoundingClientRect();
      pop.style.top = Math.min(rect.bottom + 6, window.innerHeight - pop.offsetHeight - 10) + "px";
      pop.style.left = Math.min(rect.left, window.innerWidth - pop.offsetWidth - 10) + "px";
    });
  });
  document.addEventListener("click", () => { const p = document.getElementById("guide-prog-detail"); if(p) p.remove(); });

  // Scroll to now
  const tl = $("#guide-tl");
  if(tl) tl.scrollLeft = Math.max(0, nowPos - 200);
}

function updateNowLine(data){
  const nowEl = $("#guide-now");
  if(!nowEl || !data.start) return;
  const wStart = new Date(data.start).getTime();
  const wEnd = new Date(data.end).getTime();
  const pxPerMs = 2400 / (wEnd - wStart);
  nowEl.style.left = ((Date.now() - wStart) * pxPerMs) + "px";
}

// Guide controls
$("#guide-prev").addEventListener("click", ()=>{ guideOffset--; loadGuide(); });
$("#guide-next").addEventListener("click", ()=>{ guideOffset++; loadGuide(); });
$$(".guide-range").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    guideHours = parseInt(btn.dataset.hours);
    $$(".guide-range").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    loadGuide();
  });
});

// ── Bumps ────────────────────────────────────────────────────────────────
let bumpData = {};

async function loadBumps(){
  const r = await api("/bumps");
  bumpData = r;
  renderBumps();
}

function renderBumps(){
  const grid = $("#bumps-grid");
  const clips = bumpData.clips || {};
  const folders = bumpData.folders || {};
  if(!Object.keys(folders).length){
    grid.innerHTML = '<div class="empty-state">No bump clips found. Download some from YouTube or add videos to the bumps folder.</div>';
    return;
  }
  grid.innerHTML = Object.entries(clips).map(([name, items])=>`
    <div class="bump-folder" onclick="toggleBumpFolder(this)">
      <h4>${name}</h4>
      <div class="bump-count">${items.length} clip${items.length!==1?"s":""}</div>
      <div class="bump-clips hidden">
        ${items.map(clip=>`
          <div class="bump-clip">
            <img class="bump-thumb" src="/api/bumps/thumbnail?path=${encodeURIComponent(clip.path)}" alt="" onerror="this.classList.add('no-thumb')"/>
            <span class="bump-clip-name" title="${clip.path}">${clip.name}</span>
            <button class="btn-sm btn-sm-watch" onclick="event.stopPropagation(); previewBump('${clip.path.replace(/'/g,"\\'")}','${clip.name.replace(/'/g,"\\'")}')">Preview</button>
            <button class="btn-sm btn-sm-danger" onclick="event.stopPropagation(); deleteBump('${clip.path.replace(/'/g,"\\'")}','${clip.name.replace(/'/g,"\\'")}')">Delete</button>
          </div>
        `).join("")}
      </div>
    </div>
  `).join("");
}

function toggleBumpFolder(el){
  const clips = el.querySelector(".bump-clips");
  if(clips) clips.classList.toggle("hidden");
}

function previewBump(path, name){
  const url = `/api/bumps/preview?path=${encodeURIComponent(path)}`;
  $("#player-title").textContent = name || "Preview";
  playerOverlay.classList.remove("hidden");
  if(activeHls){ activeHls.destroy(); activeHls = null; }
  playerVideo.src = url;
  playerVideo.play().catch(()=>{});
}

async function deleteBump(path, name){
  if(!confirm(`Delete bump clip "${name}"?`)) return;
  const r = await api("/bumps/clip", {method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({path})});
  if(r.ok){ toast("Clip deleted","success"); loadBumps(); }
  else toast(r.error||"Delete failed","error");
}

$("#rescan-bumps").addEventListener("click", async ()=>{
  const r = await apiPost("/bumps/scan");
  bumpData = r;
  renderBumps();
  toast(`Scan complete: ${r.total||0} clips`,"success");
});

$("#bump-dl-btn").addEventListener("click", async ()=>{
  const url = $("#bump-dl-url").value.trim();
  const folder = $("#bump-dl-folder").value.trim();
  const resolution = $("#bump-dl-res").value;
  if(!url){ toast("Enter a YouTube URL","error"); return; }
  if(!folder){ toast("Enter a folder name","error"); return; }
  const r = await apiPost("/bumps/download", {url, folder, resolution});
  if(r.ok||r.message){
    toast(r.message||"Downloading...","success");
    $("#bump-dl-url").value = "";
  } else toast(r.error||"Download failed","error");
});

// ── Player ───────────────────────────────────────────────────────────────
let activeHls = null;
const playerOverlay = $("#player-overlay");
const playerVideo = $("#player-video");

$("#player-close").addEventListener("click", closePlayer);
playerOverlay.addEventListener("click", e=>{
  if(e.target.classList.contains("modal-overlay")) closePlayer();
});

function watchChannel(id, name){
  const url = `/stream/${id}.m3u8`;
  $("#player-title").textContent = name || "Watch";
  playerOverlay.classList.remove("hidden");

  if(activeHls){ activeHls.destroy(); activeHls = null; }

  if(typeof Hls !== "undefined" && Hls.isSupported()){
    const hls = new Hls({
      liveSyncDurationCount: 3,
      liveMaxLatencyDurationCount: 10,
      liveDurationInfinity: true,
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 0,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
    });
    hls.loadSource(url);
    hls.attachMedia(playerVideo);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=>{ playerVideo.play(); });
    hls.on(Hls.Events.ERROR, (_, data)=>{
      if(data.fatal){
        if(data.type === Hls.ErrorTypes.NETWORK_ERROR){
          toast("Stream not available yet — retrying...", "info");
          setTimeout(()=> hls.startLoad(), 2000);
        } else if(data.type === Hls.ErrorTypes.MEDIA_ERROR){
          hls.recoverMediaError();
        } else {
          toast("Playback error", "error");
          hls.destroy();
        }
      }
    });
    activeHls = hls;
  } else if(playerVideo.canPlayType("application/vnd.apple.mpegurl")){
    playerVideo.src = url;
    playerVideo.play();
  } else {
    toast("HLS not supported in this browser", "error");
  }
}

function closePlayer(){
  playerOverlay.classList.add("hidden");
  if(activeHls){ activeHls.destroy(); activeHls = null; }
  playerVideo.pause();
  playerVideo.removeAttribute("src");
  playerVideo.load();
}

// ── VPN Status ───────────────────────────────────────────────────────────
async function loadVpnStatus(){
  try {
    const v = await api("/vpn/status");
    const badge = $("#vpn-badge");
    if(!v.enabled){
      badge.classList.add("hidden");
      return;
    }
    badge.classList.remove("hidden");
    if(v.status === "running"){
      badge.className = "badge vpn-badge vpn-connected";
      const loc = [v.city, v.country].filter(Boolean).join(", ");
      badge.textContent = `VPN: ${v.ip}${loc ? " ("+loc+")" : ""}`;
      badge.title = `VPN Connected — ${v.ip} ${loc}`;
    } else if(v.status === "unreachable"){
      badge.className = "badge vpn-badge vpn-unconfigured";
      badge.textContent = "VPN: offline";
      badge.title = "Gluetun unreachable";
    } else {
      badge.className = "badge vpn-badge vpn-disconnected";
      badge.textContent = `VPN: ${v.status}`;
      badge.title = `VPN ${v.status}`;
    }
  } catch(e){
    // VPN not configured, hide badge
    $("#vpn-badge").classList.add("hidden");
  }
}

// ── Copy Buttons ─────────────────────────────────────────────────────────
function copyToClipboard(text){
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch(e){}
  document.body.removeChild(ta);
}

$$("[data-copy-path]").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    const path = btn.dataset.copyPath;
    const url = window.location.protocol + "//" + window.location.host + path;
    copyToClipboard(url);
    btn.classList.add("copied");
    btn.textContent = "\u2713";
    setTimeout(()=>{ btn.classList.remove("copied"); btn.innerHTML = "\u2398"; }, 1500);
    toast("Copied: " + url, "success");
  });
});

// ── Init ─────────────────────────────────────────────────────────────────
loadChannels();
loadSettings();
loadVpnStatus();
setInterval(loadVpnStatus, 30000);

})();
