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

function esc(s){
  return String(s==null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
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
  _populateChannelFilters();
  renderChannels();
  $("#channel-count").textContent = channels.filter(c=>c.active).length + " active";
}

function _populateChannelFilters(){
  const tagSel = $("#channel-tag-filter");
  const allTags = new Set();
  channels.forEach(ch=> (ch.tags||[]).forEach(t=> allTags.add(t)));
  const prevTag = tagSel.value;
  tagSel.innerHTML = '<option value="">All tags (any)</option>' +
    [...allTags].sort().map(t=>`<option value="${t}">${t}</option>`).join("");
  tagSel.value = prevTag || "";

  const primarySel = $("#channel-primary-tag-filter");
  const allPrimary = new Set();
  channels.forEach(ch=> { if(ch.primary_tag) allPrimary.add(ch.primary_tag); });
  const prevPrimary = primarySel.value;
  primarySel.innerHTML = '<option value="">All primary</option>' +
    [...allPrimary].sort().map(t=>`<option value="${t}">${t}</option>`).join("");
  primarySel.value = prevPrimary || "";
}

function _getFilteredChannels(){
  const search = ($("#channel-search").value||"").toLowerCase();
  const filter = $("#channel-filter").value;
  const tagFilter = $("#channel-tag-filter").value;
  const primaryFilter = $("#channel-primary-tag-filter").value;
  const modeFilter = $("#channel-mode-filter").value;
  return channels.filter(ch=>{
    if(search && !ch.title.toLowerCase().includes(search)) return false;
    if(filter==="active" && !ch.active) return false;
    if(filter==="inactive" && ch.active) return false;
    if(filter==="mapped" && !ch.epg_mapped) return false;
    if(filter==="unmapped" && ch.epg_mapped) return false;
    if(tagFilter && !(ch.tags||[]).includes(tagFilter)) return false;
    if(primaryFilter && ch.primary_tag !== primaryFilter) return false;
    if(modeFilter && (ch.activation_mode||"auto") !== modeFilter) return false;
    return true;
  });
}

function _tagClassName(tag){
  return "tag-" + String(tag).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function _tagPills(tags){
  return (tags||[]).map(t=> `<span class="tag-pill ${_tagClassName(t)}">${t}</span>`).join("");
}

function _primaryTagPill(tag){
  if(!tag) return '<span class="tag-pill tag-uncategorized">—</span>';
  return `<span class="tag-pill ${_tagClassName(tag)}">${tag}</span>`;
}

function _modePill(mode){
  const m = mode || "auto";
  if(m === "force_on") return '<span class="mode-pill mode-on" title="Pinned on — ignores tag rules">pinned on</span>';
  if(m === "force_off") return '<span class="mode-pill mode-off" title="Pinned off — ignores tag rules">pinned off</span>';
  return '<span class="mode-pill mode-auto" title="Rule-driven — tag rules decide active state">auto</span>';
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
    body.innerHTML = '<tr><td colspan="9" class="empty-state">No channels found</td></tr>';
    return;
  }

  body.innerHTML = filtered.map(ch=>{
    const tags = _tagPills(ch.tags);
    const primary = _primaryTagPill(ch.primary_tag);
    const mode = _modePill(ch.activation_mode);
    const epg = ch.epg_mapped
      ? `<span class="epg-badge epg-mapped">${ch.epg_channel_id}</span>`
      : `<span class="epg-badge epg-unmapped">None</span>`;
    const logoSrc = ch.logo_cached ? `/logo/${ch.id}` : "";
    const logoHtml = logoSrc
      ? `<img class="ch-logo" src="${logoSrc}" onerror="this.classList.add('no-logo')">`
      : "";

    return `<tr data-id="${ch.id}">
      <td class="td-check"><input type="checkbox" data-bulk-channels="${ch.id}"></td>
      <td class="td-chno">
        <div class="chno-wrap ${ch.channel_number_pinned ? 'chno-pinned' : ''}" title="${ch.channel_number_pinned ? 'Pinned — survives renumber. Clear the number to unpin.' : 'Auto-numbered by tag rules'}">
          <input type="number" class="chno-input" value="${ch.channel_number||''}" data-chno="${ch.id}" placeholder="-" min="1">
          ${ch.channel_number_pinned ? '<span class="chno-pin-ic">📌</span>' : ''}
        </div>
      </td>
      <td class="title-clickable"><span class="ch-title-wrap">${logoHtml}${ch.title_override ? `<span>${ch.title_override}</span><span class="ch-source-title">${ch.title}</span>` : (ch.title||"Untitled")}</span></td>
      <td>${tags}</td>
      <td>${primary}</td>
      <td>${mode}</td>
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
      toast(val !== null ? "Channel number pinned" : "Unpinned — rules will reassign", "success");
      loadChannels();  // refresh so pin indicator updates
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
$("#channel-primary-tag-filter").addEventListener("change", renderChannels);
$("#channel-mode-filter").addEventListener("change", renderChannels);
$("#btn-refresh-channels").addEventListener("click", loadChannels);
$("#btn-auto-number").addEventListener("click", async ()=>{
  const filtered = _getFilteredChannels();
  const hasFilters = ($("#channel-search").value||"").trim() !== ""
    || $("#channel-filter").value !== "all"
    || $("#channel-tag-filter").value !== ""
    || $("#channel-primary-tag-filter").value !== ""
    || $("#channel-mode-filter").value !== "";

  const activeChannels = channels.filter(c=>c.active);
  const filteredActive = filtered.filter(c=>c.active);
  const ranges = await api("/number-ranges");

  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `<div class="modal-content renumber-modal">
    <h3>Renumber Channels</h3>
    <p class="field-hint" style="margin:0 0 14px">Wipes existing numbers in scope and reassigns from per-tag ranges. Manual numbers are overwritten.</p>

    <div class="setting-field" style="margin-bottom:14px">
      <label>Scope</label>
      <div class="toggle-group" id="renumber-scope-group">
        <button class="toggle-btn active" data-scope="all">All active (${activeChannels.length})</button>
        <button class="toggle-btn" data-scope="filtered" ${!hasFilters?"disabled":""}>Current filter (${filteredActive.length})</button>
        <button class="toggle-btn" data-scope="by_tag">By tag</button>
      </div>
    </div>

    <div id="renumber-tag-picker" class="setting-field" style="display:none;margin-bottom:14px">
      <label>Primary tags to renumber</label>
      <div id="renumber-tag-chips" class="chip-row"></div>
    </div>

    <div class="setting-field" style="margin-bottom:14px">
      <label>Range utilization</label>
      <div id="renumber-preview" class="range-preview-box"></div>
    </div>

    <div class="modal-actions">
      <button class="btn" id="renumber-cancel">Cancel</button>
      <button class="btn btn-primary" id="renumber-apply">Renumber</button>
    </div>
  </div>`;

  document.body.appendChild(modal);

  let scope = "all";
  let selectedTags = new Set();

  // Tag chips: every tag that has a configured range OR appears as primary on any active channel.
  const rangeTags = Object.keys(ranges);
  const activePrimaries = [...new Set(activeChannels.map(c=>c.primary_tag).filter(Boolean))];
  const chipTags = [...new Set([...rangeTags, ...activePrimaries])].sort((a,b)=>{
    const ai = rangeTags.indexOf(a), bi = rangeTags.indexOf(b);
    if(ai !== -1 && bi !== -1) return ai - bi;
    if(ai !== -1) return -1;
    if(bi !== -1) return 1;
    return a.localeCompare(b);
  });
  const chipRow = modal.querySelector("#renumber-tag-chips");
  chipRow.innerHTML = chipTags.map(t=>`<button class="chip" data-chip="${t}">${t}</button>`).join("");

  function inScopeChannels(){
    if(scope === "filtered") return filteredActive;
    if(scope === "by_tag") return activeChannels.filter(c=> selectedTags.has(c.primary_tag));
    return activeChannels;
  }

  function renderPreview(){
    const inScope = inScopeChannels();
    const counts = {};
    inScope.forEach(c=>{
      if(c.primary_tag) counts[c.primary_tag] = (counts[c.primary_tag]||0) + 1;
    });

    const tagsToShow = scope === "by_tag"
      ? [...selectedTags]
      : [...new Set([...rangeTags, ...Object.keys(counts)])];

    let anyOver = false;
    const rows = tagsToShow.map(t=>{
      const r = ranges[t];
      const used = counts[t] || 0;
      if(!r){
        return `<div class="range-row range-row-warn"><span class="range-name">${t}</span><span class="range-detail">no range configured — ${used} channel${used===1?"":"s"} will stay unnumbered</span></div>`;
      }
      const slots = (r.end - r.start + 1);
      const pct = slots > 0 ? Math.round(used / slots * 100) : 0;
      const over = used > slots;
      if(over) anyOver = true;
      const detail = over
        ? `${used} / ${slots} slots — OVER by ${used - slots}`
        : `${used} / ${slots} slots (${pct}%)`;
      return `<div class="range-row ${over?"range-row-over":""}">
        <span class="range-name">${t}</span>
        <span class="range-span">${r.start}–${r.end}</span>
        <span class="range-detail">${detail}</span>
      </div>`;
    });

    const container = modal.querySelector("#renumber-preview");
    if(!rows.length){
      container.innerHTML = '<div class="range-row range-row-empty">Nothing in scope</div>';
    } else {
      container.innerHTML = rows.join("");
    }

    const applyBtn = modal.querySelector("#renumber-apply");
    const scopeCount = inScope.length;
    const disable = scopeCount === 0 || anyOver;
    applyBtn.disabled = disable;
    applyBtn.textContent = anyOver ? "Fix range conflicts first" :
                           scopeCount === 0 ? "Nothing to renumber" :
                           `Renumber ${scopeCount} channel${scopeCount===1?"":"s"}`;
  }

  modal.querySelectorAll("[data-scope]").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      if(btn.disabled) return;
      modal.querySelectorAll("[data-scope]").forEach(b=>b.classList.remove("active"));
      btn.classList.add("active");
      scope = btn.dataset.scope;
      modal.querySelector("#renumber-tag-picker").style.display = (scope === "by_tag") ? "" : "none";
      renderPreview();
    });
  });

  chipRow.querySelectorAll("[data-chip]").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const t = btn.dataset.chip;
      if(selectedTags.has(t)){
        selectedTags.delete(t);
        btn.classList.remove("active");
      } else {
        selectedTags.add(t);
        btn.classList.add("active");
      }
      renderPreview();
    });
  });

  modal.querySelector("#renumber-cancel").addEventListener("click", ()=> modal.remove());
  modal.addEventListener("click", e=>{ if(e.target === modal) modal.remove(); });

  modal.querySelector("#renumber-apply").addEventListener("click", async ()=>{
    const payload = {};
    if(scope === "filtered") payload.ids = filteredActive.map(ch=>ch.id);
    if(scope === "by_tag") payload.tags = [...selectedTags];
    const applyBtn = modal.querySelector("#renumber-apply");
    applyBtn.disabled = true;
    applyBtn.textContent = "Numbering...";
    const result = await apiPost("/channels/renumber", payload);
    toast(`Renumbered ${result.assigned} / ${result.scope} channels`, "success");
    modal.remove();
    loadChannels();
  });

  renderPreview();
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
const _MODE_HINTS = {
  auto: "Tag rules decide active state on each ingest.",
  force_on: "Stays active regardless of rules.",
  force_off: "Stays inactive regardless of rules.",
};

function _setEditMode(mode){
  const m = ["auto","force_on","force_off"].includes(mode) ? mode : "auto";
  $$("#edit-mode-group .toggle-btn").forEach(b=>{
    b.classList.toggle("active", b.dataset.mode === m);
  });
  $("#edit-mode-hint").textContent = _MODE_HINTS[m];
}

function openEditModal(id){
  const ch = channels.find(c=>c.id===id);
  if(!ch) return;
  editingId = id;
  $("#edit-chno").value = ch.channel_number || "";
  $("#edit-title").value = ch.title||"";
  $("#edit-title-override").value = ch.title_override || "";
  $("#edit-tags").value = (ch.tags||[]).join(", ");
  $("#edit-primary-tag").innerHTML = _primaryTagPill(ch.primary_tag);
  _setEditMode(ch.activation_mode);
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

$$("#edit-mode-group .toggle-btn").forEach(b=>{
  b.addEventListener("click", ()=> _setEditMode(b.dataset.mode));
});

$("#edit-save").addEventListener("click", async ()=>{
  if(!editingId) return;
  const tags = $("#edit-tags").value.split(",").map(s=>s.trim()).filter(Boolean);
  const selectedMode = $("#edit-mode-group .toggle-btn.active");
  const mode = selectedMode ? selectedMode.dataset.mode : "auto";
  await apiPut(`/channels/${editingId}`, {
    title: $("#edit-title").value,
    title_override: $("#edit-title-override").value || null,
    channel_number: $("#edit-chno").value ? parseInt($("#edit-chno").value) : null,
    tags: tags,
    activation_mode: mode,
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
    const modeClass = mode === "ffmpeg" ? "tag-pill tag-sports" : "tag-pill";
    return `<tr>
      <td class="td-check"><input type="checkbox" data-bulk-sources="${s.id}"></td>
      <td>${s.name}</td>
      <td class="url-cell" title="${s.url}">${urlShort}</td>
      <td><span class="${modeClass}">${mode}</span></td>
      <td>${s.channel_count}</td>
      <td>${fmtDate(s.last_ingested_at)}</td>
      <td class="td-actions"><div class="action-btns">
        <button class="btn-sm" data-ingest-source="${s.id}">Ingest</button>
        <button class="btn-sm" data-edit-source="${s.id}">Edit</button>
        <button class="btn-sm btn-sm-danger" data-delete-source="${s.id}">Delete</button>
      </div></td>
    </tr>`;
  }).join("");

  sourcesBulk.bindRows(body);

  $$("[data-edit-source]", body).forEach(btn=>{
    btn.addEventListener("click", ()=> openEditSourceModal(btn.dataset.editSource));
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
        showIngestWarnings(res.warnings);
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

// Add / Edit source modal
let _editingM3uSourceId = null;
function openAddSourceModal(){
  _editingM3uSourceId = null;
  $("#add-source-modal-title").textContent = "Add M3U Source";
  $("#add-source-save").textContent = "Add";
  $("#add-source-name").value = "";
  $("#add-source-url").value = "";
  $("#add-source-stream-mode").value = "passthrough";
  $("#add-source-auto-activate").checked = false;
  $("#file-browser").classList.add("hidden");
  $("#add-source-modal").classList.remove("hidden");
}
function openEditSourceModal(sourceId){
  const s = m3uSources.find(x => x.id === sourceId);
  if(!s){ toast("Source not found", "error"); return; }
  _editingM3uSourceId = sourceId;
  $("#add-source-modal-title").textContent = "Edit M3U Source";
  $("#add-source-save").textContent = "Save";
  $("#add-source-name").value = s.name || "";
  $("#add-source-url").value = s.url || "";
  $("#add-source-stream-mode").value = s.stream_mode || "passthrough";
  $("#add-source-auto-activate").checked = !!s.auto_activate;
  $("#file-browser").classList.add("hidden");
  $("#add-source-modal").classList.remove("hidden");
}
function closeAddSourceModal(){
  $("#add-source-modal").classList.add("hidden");
  _editingM3uSourceId = null;
}

$("#btn-add-source").addEventListener("click", openAddSourceModal);
$("#add-source-modal-close").addEventListener("click", closeAddSourceModal);
$("#add-source-cancel").addEventListener("click", closeAddSourceModal);
$("#add-source-modal").addEventListener("click", e=>{
  if(e.target.classList.contains("modal-overlay")) closeAddSourceModal();
});

// File browser — reused by M3U + EPG modals
async function loadFileBrowser(path, cfg){
  const res = await api(`/browse?path=${encodeURIComponent(path)}`);
  if(res.error){ toast(res.error, "error"); return; }
  $(cfg.pathSel).textContent = res.path;
  const list = $(cfg.listSel);
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
    el.addEventListener("click", ()=> loadFileBrowser(el.dataset.fbDir, cfg));
  });
  $$("[data-fb-file]", list).forEach(el=>{
    el.addEventListener("click", ()=>{
      $(cfg.urlSel).value = el.dataset.fbFile;
      // Auto-fill name from filename if empty
      const nameEl = $(cfg.nameSel);
      if(nameEl && !nameEl.value.trim()){
        const parts = el.dataset.fbFile.split("/");
        const fname = parts[parts.length-1].replace(/\.[^.]+$/, "");
        nameEl.value = fname;
      }
      $(cfg.containerSel).classList.add("hidden");
    });
  });
}

function toggleFileBrowser(cfg){
  const fb = $(cfg.containerSel);
  if(fb.classList.contains("hidden")){
    loadFileBrowser("/browse", cfg);
    fb.classList.remove("hidden");
  } else {
    fb.classList.add("hidden");
  }
}

const _M3U_FB = {
  containerSel: "#file-browser",
  pathSel: "#fb-path",
  listSel: "#fb-list",
  urlSel: "#add-source-url",
  nameSel: "#add-source-name",
};
const _EPG_FB = {
  containerSel: "#epg-file-browser",
  pathSel: "#epg-fb-path",
  listSel: "#epg-fb-list",
  urlSel: "#add-epg-source-url",
  nameSel: "#add-epg-source-name",
};

$("#add-source-browse").addEventListener("click", ()=> toggleFileBrowser(_M3U_FB));
$("#add-epg-source-browse").addEventListener("click", ()=> toggleFileBrowser(_EPG_FB));

$("#add-source-save").addEventListener("click", async ()=>{
  const name = $("#add-source-name").value.trim();
  const url = $("#add-source-url").value.trim();
  const stream_mode = $("#add-source-stream-mode").value;
  const auto_activate = $("#add-source-auto-activate").checked;
  if(!name || !url){
    toast("Name and URL are required", "error");
    return;
  }
  let res;
  if(_editingM3uSourceId){
    res = await apiPut(`/m3u-sources/${_editingM3uSourceId}`, {name, url, stream_mode, auto_activate});
  } else {
    res = await apiPost("/m3u-sources", {name, url});
    if(!res.error && res.id && (stream_mode !== "passthrough" || auto_activate)){
      await apiPut(`/m3u-sources/${res.id}`, {stream_mode, auto_activate});
    }
  }
  if(res.error){
    toast(res.error, "error");
    return;
  }
  toast(_editingM3uSourceId ? "Source updated" : "Source added", "success");
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
    showIngestWarnings(res.warnings);
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
        <button class="btn-sm" data-edit-epg="${s.id}">Edit</button>
        <button class="btn-sm btn-sm-danger" data-delete-epg="${s.id}">Delete</button>
      </div></td>
    </tr>`;
  }).join("");

  epgsourcesBulk.bindRows(body);

  $$("[data-edit-epg]", body).forEach(btn=>{
    btn.addEventListener("click", ()=> openEditEpgSourceModal(btn.dataset.editEpg));
  });

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

// Add / Edit EPG Source modal
let _editingEpgSourceId = null;
async function _populateEpgM3uDropdown(selectedId){
  const sources = await api("/m3u-sources");
  const sel = $("#add-epg-source-m3u");
  sel.innerHTML = '<option value="">-- Select M3U Source --</option>' +
    sources.map(s=>`<option value="${s.id}" ${s.id===selectedId?"selected":""}>${s.name}</option>`).join("");
}
async function openAddEpgSourceModal(){
  _editingEpgSourceId = null;
  $("#add-epg-source-modal-title").textContent = "Add EPG Source";
  $("#add-epg-source-save").textContent = "Add";
  $("#add-epg-source-name").value = "";
  $("#add-epg-source-url").value = "";
  $("#epg-file-browser").classList.add("hidden");
  await _populateEpgM3uDropdown("");
  $("#add-epg-source-modal").classList.remove("hidden");
}
async function openEditEpgSourceModal(sourceId){
  const s = epgSources.find(x => x.id === sourceId);
  if(!s){ toast("EPG source not found", "error"); return; }
  _editingEpgSourceId = sourceId;
  $("#add-epg-source-modal-title").textContent = "Edit EPG Source";
  $("#add-epg-source-save").textContent = "Save";
  $("#add-epg-source-name").value = s.name || "";
  $("#add-epg-source-url").value = s.url || "";
  $("#epg-file-browser").classList.add("hidden");
  await _populateEpgM3uDropdown(s.m3u_source_id || "");
  $("#add-epg-source-modal").classList.remove("hidden");
}
function closeAddEpgSourceModal(){
  $("#add-epg-source-modal").classList.add("hidden");
  _editingEpgSourceId = null;
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
  const res = _editingEpgSourceId
    ? await apiPut(`/epg-sources/${_editingEpgSourceId}`, {name, url, m3u_source_id})
    : await apiPost("/epg-sources", {name, url, m3u_source_id});
  if(res.error){
    toast(res.error, "error");
    return;
  }
  toast(_editingEpgSourceId ? "EPG source updated" : "EPG source added", "success");
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
const vpnHistory = [];
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

  // VPN / Network latency — same chart, label depends on summary.mode
  try {
    const v = await api("/vpn/history?minutes=60");
    const series = (v.samples || [])
      .map(x => x.rtt_ms)
      .filter(x => x !== null && x !== undefined);
    vpnHistory.length = 0;
    vpnHistory.push(...series.slice(-MAX_POINTS));
    const sum = v.summary || {};
    const mode = sum.mode || "vpn";
    const isVpn = mode === "vpn";

    // Title + footer text + rotate/history visibility key off mode
    $("#vpn-card-title").textContent = isVpn ? "VPN Latency" : "Network Latency";
    $("#vpn-live").textContent = sum.current_rtt_ms != null ? sum.current_rtt_ms.toFixed(1) + "ms" : "--ms";
    if (isVpn) {
      $("#vpn-exit").textContent = sum.current_city ? `${sum.current_city} · ${sum.current_ip || ""}` : "--";
    } else {
      $("#vpn-exit").textContent = "Direct connection";
    }
    $("#vpn-rotate-btn").style.display = isVpn ? "" : "none";
    $("#vpn-history-section").style.display = isVpn ? "" : "none";

    // Auto-scale Y to ~120% of observed max (min 100ms) so spikes are visible
    const observed = vpnHistory.length ? Math.max(...vpnHistory) : 0;
    const max = Math.max(observed * 1.2, 100);
    drawChart("vpn-chart", vpnHistory, "var(--ok)", max);

    // Server history table only makes sense in VPN mode
    if (isVpn) loadVpnHistory();
  } catch(e) {}
}

// VPN Server History modal
function fmtDuration(seconds){
  if(!seconds || seconds < 60) return (seconds||0)+"s";
  const h = Math.floor(seconds/3600);
  const m = Math.floor((seconds%3600)/60);
  if(h > 24){
    const d = Math.floor(h/24);
    return `${d}d ${h%24}h`;
  }
  if(h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function fmtRelative(iso){
  if(!iso) return "--";
  const then = new Date(iso).getTime();
  const sec = Math.floor((Date.now() - then) / 1000);
  if(sec < 60) return sec+"s ago";
  if(sec < 3600) return Math.floor(sec/60)+"m ago";
  if(sec < 86400) return Math.floor(sec/3600)+"h ago";
  return Math.floor(sec/86400)+"d ago";
}

async function loadVpnHistory(sort){
  sort = sort || $("#vpn-history-sort").value || "avg_rtt";
  try {
    const r = await api(`/vpn/servers?sort=${sort}&limit=100`);
    const servers = r.servers || [];
    $("#vpn-history-count").textContent = `${servers.length} server${servers.length === 1 ? "" : "s"}`;
    const body = $("#vpn-history-body");
    if(servers.length === 0){
      body.innerHTML = `<tr><td colspan="10" style="text-align:center;color:var(--text-muted);padding:24px">No server history yet — samples accumulate every minute.</td></tr>`;
      return;
    }
    body.innerHTML = servers.map((s, i) => {
      const cls = [];
      if(s.is_current) cls.push("is-current");
      if(sort === "avg_rtt" && i < 3 && s.avg_rtt_ms != null) cls.push("top-rank");
      const rankMarker = s.is_current
        ? '<span class="rank-marker rank-current" title="Current">●</span>'
        : (sort === "avg_rtt" && i < 3 && s.avg_rtt_ms != null)
          ? `<span class="rank-marker rank-gold" title="Top ${i+1}">${["★","②","③"][i]}</span>`
          : '<span class="rank-marker"></span>';
      return `<tr class="${cls.join(" ")}">
        <td>${rankMarker}${s.city || "?"}${s.country ? ", " + s.country : ""}</td>
        <td class="ip-cell">${s.ip}</td>
        <td>${s.org || "--"}</td>
        <td class="num">${s.avg_rtt_ms != null ? s.avg_rtt_ms.toFixed(1) : "--"}</td>
        <td class="num">${s.min_rtt_ms != null ? s.min_rtt_ms.toFixed(1) : "--"}</td>
        <td class="num">${s.max_rtt_ms != null ? s.max_rtt_ms.toFixed(1) : "--"}</td>
        <td class="num">${(s.success_rate * 100).toFixed(1)}%</td>
        <td class="num">${s.total_samples}</td>
        <td class="num">${fmtDuration(s.total_seconds_connected)}</td>
        <td>${fmtRelative(s.last_seen_at)}</td>
      </tr>`;
    }).join("");
  } catch(e){
    $("#vpn-history-body").innerHTML = `<tr><td colspan="10" style="color:var(--danger);padding:24px">Failed to load: ${e}</td></tr>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const histSort = document.getElementById("vpn-history-sort");
  if (histSort) {
    histSort.addEventListener("change", () => loadVpnHistory());
  }
});

// Manual rotate button (System tab)
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("vpn-rotate-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!confirm("Rotate VPN — picks a new server from your SERVER_CITIES list. ~5-15s of brief connectivity blip while the tunnel reconnects.")) return;
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.textContent = "Rotating...";
    try {
      const r = await fetch(API + "/vpn/rotate", {method: "POST"});
      const j = await r.json();
      if (j.ok) {
        toast(`Rotated to ${j.to.city || j.to.ip || "new exit"}`, "success");
        loadStats();
      } else {
        toast(j.error || "Rotate failed", "error");
      }
    } catch(e) {
      toast("Rotate failed", "error");
    }
    setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 2000);
  });
});

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
  _updateExportLinks();
  $("#sidebar-db").textContent = settings.pg_host + ":" + settings.pg_port + "/" + settings.pg_db;
}

function _bindCopyButtons(root){
  $$("[data-copy-path]", root||document).forEach(btn=>{
    if(btn._copyBound) return;
    btn._copyBound = true;
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
}

function _updateExportLinks(){
  const links = $("#output-links");
  if(!links) return;
  if(settings.export_strategy === "local"){
    const path = (settings.export_local_path || "/app/output").replace(/\/+$/, "");
    links.innerHTML = `
      <span class="output-link output-link-path" title="M3U file path">${esc(path)}/manifold.m3u</span>
      <button class="copy-btn" data-copy-path-literal="${esc(path)}/manifold.m3u" title="Copy path">&#x2398;</button>
      <span class="output-link output-link-path" title="XMLTV file path">${esc(path)}/manifold.xml</span>
      <button class="copy-btn" data-copy-path-literal="${esc(path)}/manifold.xml" title="Copy path">&#x2398;</button>`;
    $$("[data-copy-path-literal]", links).forEach(btn=>{
      btn.addEventListener("click", ()=>{
        const text = btn.dataset.copyPathLiteral;
        copyToClipboard(text);
        btn.classList.add("copied");
        btn.textContent = "\u2713";
        setTimeout(()=>{ btn.classList.remove("copied"); btn.innerHTML = "\u2398"; }, 1500);
        toast("Copied: " + text, "success");
      });
    });
  } else {
    links.innerHTML = `
      <a href="/output/manifold.m3u" target="_blank" class="output-link" title="M3U Playlist URL">M3U</a><button class="copy-btn" data-copy-path="/output/manifold.m3u" title="Copy M3U URL">&#x2398;</button>
      <a href="/output/manifold.xml" target="_blank" class="output-link" title="XMLTV EPG URL">EPG</a><button class="copy-btn" data-copy-path="/output/manifold.xml" title="Copy EPG URL">&#x2398;</button>
      <a href="/discover.json" target="_blank" class="output-link output-link-hdhr" title="HDHomeRun (for Plex)">HDHR</a><button class="copy-btn" data-copy-path="/discover.json" title="Copy HDHR URL">&#x2398;</button>`;
    _bindCopyButtons(links);
  }
}

function renderSettings(){
  const container = $("#settings-container");
  const title = $("#settings-section-title");
  clearInterval(tasksTimer); tasksTimer = null;
  clearInterval(enrichStatusTimer); enrichStatusTimer = null;

  // Tagging + Integrations subtabs use their own in-card Save buttons.
  const globalSave = $("#save-settings");
  if(globalSave){
    const hideGlobal = ["tagging", "integrations"].includes(currentSettingsSub);
    globalSave.style.display = hideGlobal ? "none" : "";
  }

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
    const strategy = settings.export_strategy || "url";
    const localPath = settings.export_local_path || "";
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
        <label>VPN Auto-Rotate Interval (minutes, 0 = disabled)</label>
        <input type="number" id="set-vpn-rotate" value="${settings.vpn_auto_rotate_minutes||0}" min="0">
        <span class="field-hint">Cycles the gluetun WireGuard tunnel through your SERVER_CITIES list on this interval. Brief 5-15s connectivity blip per rotate; manifold's stream proxy and clients recover automatically on next playlist poll.</span>
      </div>
      <div class="setting-field">
        <label>Export Strategy</label>
        <select id="set-export-strategy" class="filter-status" style="width:100%">
          <option value="url" ${strategy==="url"?"selected":""}>URL (HTTP)</option>
          <option value="local" ${strategy==="local"?"selected":""}>Local Path (Shared Storage)</option>
        </select>
        <span class="field-hint">How downstream apps (Jellyfin, etc.) read manifold's outputs. Local path avoids HTTP caching and is preferred when both containers mount the same volume.</span>
      </div>
      <div class="setting-field" id="set-export-path-field" style="${strategy==="local"?"":"display:none"}">
        <label>Local Path</label>
        <input type="text" id="set-export-path" value="${esc(localPath)}" placeholder="/app/output">
        <span class="field-hint">Directory where manifold.m3u and manifold.xml live (as visible to consumers). Header shows this path when Local is selected.</span>
      </div>
    </div>`;
    const strategySel = $("#set-export-strategy");
    if(strategySel){
      strategySel.addEventListener("change", ()=>{
        const field = $("#set-export-path-field");
        if(field) field.style.display = strategySel.value === "local" ? "" : "none";
      });
    }
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
  } else if(currentSettingsSub === "integrations"){
    title.textContent = "Integrations";
    container.innerHTML = '<div id="integrations-container" style="padding:16px 0"><div class="empty-state">Loading...</div></div>';
    loadIntegrations();
  } else if(currentSettingsSub === "tagging"){
    title.textContent = "Tagging";
    container.innerHTML = '<div id="tagging-container" style="padding:16px 0"><div class="empty-state">Loading...</div></div>';
    loadTaggingSettings();
  }
}

// ── Ingest warnings banner ──────────────────────────────────────────────

function _fmtIngestWarning(w){
  if(w.type === "range_exhausted"){
    const [start, end] = w.range || [0, 0];
    return `<strong>${w.tag}</strong> range exhausted (${start}–${end}): ${w.unassigned} channel${w.unassigned===1?"":"s"} unassigned`;
  }
  return JSON.stringify(w);
}

function showIngestWarnings(warnings){
  const banner = $("#ingest-warning-banner");
  if(!banner) return;
  if(!warnings || !warnings.length){
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  const lines = warnings.map(w => `<li>${_fmtIngestWarning(w)}</li>`).join("");
  banner.innerHTML = `
    <div class="ingest-warning-body">
      <strong>Ingest warnings</strong>
      <ul>${lines}</ul>
      <div class="ingest-warning-actions">
        <button class="btn btn-sm btn-primary" id="ingest-warning-fix">Fix ranges</button>
        <button class="btn btn-sm btn-ghost" id="ingest-warning-dismiss">Dismiss</button>
      </div>
    </div>`;
  banner.classList.remove("hidden");
  $("#ingest-warning-fix").addEventListener("click", ()=>{
    // Jump to Settings → Tagging
    const settingsBtn = document.querySelector('[data-view="settings"]');
    if(settingsBtn) settingsBtn.click();
    const taggingBtn = document.querySelector('[data-sub="tagging"]');
    if(taggingBtn) taggingBtn.click();
  });
  $("#ingest-warning-dismiss").addEventListener("click", ()=> showIngestWarnings(null));
}

// ── Tagging ─────────────────────────────────────────────────────────────

let _tagRulesData = null;
let _numberRangesData = null;
let _activationRulesData = null;

async function loadTaggingSettings(){
  try {
    const [rules, ranges, activation] = await Promise.all([
      api("/tag-rules"),
      api("/number-ranges"),
      api("/activation-rules"),
    ]);
    _tagRulesData = rules;
    _numberRangesData = ranges;
    _activationRulesData = activation;
    renderTaggingSettings();
  } catch(e){
    const c = $("#tagging-container");
    if(c) c.innerHTML = '<div class="empty-state">Failed to load tagging settings</div>';
  }
}

function _detectTaggingIssues(){
  const priority = new Set(_tagRulesData?.priority || []);
  const ruleTags = new Set(Object.keys(_tagRulesData || {}).filter(k => k !== "priority"));
  const rangeTags = new Set(Object.keys(_numberRangesData || {}));
  const autoTags = new Set(_activationRulesData?.tags_auto_on || []);
  const issues = [];
  for(const t of rangeTags){
    if(!priority.has(t)){
      issues.push(`<code>${t}</code> has a number range but isn't in priority — channels matching it won't land in that range.`);
    }
  }
  for(const t of ruleTags){
    if(!priority.has(t)){
      issues.push(`<code>${t}</code> is defined as a tag rule but isn't in priority — it will be a secondary tag only, not primary.`);
    }
  }
  // Activation tags without any source (not in rule tags AND not a known
  // structural tag) get flagged more gently since group-title passthroughs
  // and manual tags legitimately appear here.
  return issues;
}

function renderTaggingSettings(){
  const c = $("#tagging-container");
  if(!c) return;
  const issues = _detectTaggingIssues();
  const issuesHtml = issues.length
    ? `<div class="tagging-issues">
        <strong>⚠ ${issues.length} configuration issue${issues.length===1?"":"s"}</strong>
        <ul>${issues.map(i => `<li>${i}</li>`).join("")}</ul>
       </div>`
    : "";
  c.innerHTML = `${issuesHtml}<div class="tagging-grid">
    <div class="tagging-card" id="tag-rules-card">
      <div class="tagging-card-header">
        <h3>Tag Rules</h3>
        <div class="tagging-card-actions">
          <button class="btn btn-sm btn-ghost" id="tag-rules-reset">Reset to Defaults</button>
          <button class="btn btn-sm btn-primary" id="tag-rules-save">Save</button>
        </div>
      </div>
      <p class="field-hint">Title keywords match against the channel title; domain keywords match against the stream URL host. Priority decides the <code>primary_tag</code> when a channel matches multiple categories.</p>
      <div class="setting-field">
        <label>Priority (highest first)</label>
        <input type="text" id="tag-rules-priority" placeholder="event, sports, news, movies, kids, live">
        <span class="field-hint">Comma-separated list of tag names, ordered highest→lowest priority.</span>
      </div>
      <div class="tag-rules-list" id="tag-rules-list"></div>
      <button class="btn btn-sm btn-ghost" id="tag-rules-add">+ Add Tag</button>
    </div>

    <div class="tagging-card" id="number-ranges-card">
      <div class="tagging-card-header">
        <h3>Number Ranges</h3>
        <div class="tagging-card-actions">
          <button class="btn btn-sm btn-ghost" id="number-ranges-reset">Reset to Defaults</button>
          <button class="btn btn-sm btn-primary" id="number-ranges-save">Save</button>
        </div>
      </div>
      <p class="field-hint">Each primary tag maps to a number range. Channels are auto-numbered within their range on ingest (and on renumber). Utilization shows active channels in each range — if it's near or over 100%, widen the range.</p>
      <div class="range-edit-list" id="number-ranges-list"></div>
      <div class="range-edit-add-row">
        <select id="number-ranges-add-tag"></select>
        <button class="btn btn-sm btn-ghost" id="number-ranges-add">+ Add Range</button>
      </div>
      <div id="number-ranges-warnings" class="range-edit-warnings"></div>
    </div>

    <div class="tagging-card" id="activation-rules-card">
      <div class="tagging-card-header">
        <h3>Activation Rules</h3>
        <div class="tagging-card-actions">
          <button class="btn btn-sm btn-ghost" id="activation-rules-reset">Reset to Defaults</button>
          <button class="btn btn-sm btn-primary" id="activation-rules-save">Save</button>
        </div>
      </div>
      <p class="field-hint">Channels with <strong>any</strong> matching tag activate on ingest (unless manually set to Always On/Off). Matches against the full tag list, so you can activate by network/sub-sport, not just primary category.</p>
      <div class="setting-field">
        <label>Tags that activate channels</label>
        <div id="activation-chips" class="chip-row"></div>
      </div>
      <div class="setting-field">
        <label>Add tag not in the list</label>
        <div class="activation-custom-add">
          <input type="text" id="activation-custom-input" placeholder="e.g. espn, ncaaf">
          <button class="btn btn-sm btn-ghost" id="activation-custom-add">Add</button>
        </div>
      </div>
    </div>
  </div>`;

  _renderTagRulesEditor();
  _renderNumberRangesEditor();
  _renderActivationRulesEditor();

  $("#tag-rules-add").addEventListener("click", ()=>{
    const name = (prompt("New tag name (lowercase, no spaces):") || "").trim().toLowerCase();
    if(!name) return;
    if(_tagRulesData[name]){
      toast("Tag already exists", "error");
      return;
    }
    _tagRulesData[name] = {keywords: [], domain_keywords: []};
    _renderTagRulesEditor();
  });

  $("#tag-rules-save").addEventListener("click", _saveTagRules);

  $("#tag-rules-reset").addEventListener("click", async ()=>{
    if(!confirm("Reset tag rules to defaults? All custom changes will be lost.")) return;
    _tagRulesData = await apiPost("/tag-rules/reset-defaults", {});
    _renderTagRulesEditor();
    toast("Tag rules reset to defaults", "success");
  });

  $("#number-ranges-add").addEventListener("click", ()=>{
    const sel = $("#number-ranges-add-tag");
    const name = sel.value;
    if(!name) return;
    // Default new ranges to an unused slot after the current max end.
    const maxEnd = Math.max(0, ...Object.values(_numberRangesData || {})
      .map(r => Number(r.end) || 0));
    const start = Math.max(100, Math.ceil((maxEnd + 100) / 100) * 100);
    _numberRangesData[name] = {start, end: start + 99};
    _renderNumberRangesEditor();
  });

  $("#number-ranges-save").addEventListener("click", _saveNumberRanges);

  $("#number-ranges-reset").addEventListener("click", async ()=>{
    if(!confirm("Reset number ranges to defaults?")) return;
    _numberRangesData = await apiPost("/number-ranges/reset-defaults", {});
    _renderNumberRangesEditor();
    toast("Number ranges reset to defaults", "success");
  });

  $("#activation-rules-save").addEventListener("click", _saveActivationRules);
  $("#activation-rules-reset").addEventListener("click", async ()=>{
    if(!confirm("Reset activation rules to defaults?")) return;
    _activationRulesData = await apiPost("/activation-rules/reset-defaults", {});
    _renderActivationRulesEditor();
    toast("Activation rules reset to defaults", "success");
  });
  $("#activation-custom-add").addEventListener("click", ()=>{
    const inp = $("#activation-custom-input");
    const name = (inp.value || "").trim().toLowerCase();
    if(!name) return;
    const current = _activationRulesData.tags_auto_on || [];
    if(!current.includes(name)){
      _activationRulesData.tags_auto_on = [...current, name];
    }
    inp.value = "";
    _renderActivationRulesEditor();
  });
  $("#activation-custom-input").addEventListener("keydown", (e)=>{
    if(e.key === "Enter"){ e.preventDefault(); $("#activation-custom-add").click(); }
  });
}

function _renderActivationRulesEditor(){
  const chipRow = $("#activation-chips");
  if(!chipRow) return;
  const selected = new Set(_activationRulesData.tags_auto_on || []);

  // Build the chip pool: priority tags first, then all distinct tags seen
  // across channels, then any custom tags the user already selected.
  const priority = (_tagRulesData && _tagRulesData.priority) || [];
  const fromChannels = new Set();
  (channels || []).forEach(c => (c.tags || []).forEach(t => fromChannels.add(t)));
  const merged = [];
  const seen = new Set();
  const push = (t) => { if(!seen.has(t)){ seen.add(t); merged.push(t); } };
  priority.forEach(push);
  [...fromChannels].sort().forEach(push);
  [...selected].sort().forEach(push);

  chipRow.innerHTML = merged.map(t => {
    const active = selected.has(t) ? "active" : "";
    return `<button class="chip ${active}" data-act-chip="${t}">${t}</button>`;
  }).join("");

  $$("[data-act-chip]", chipRow).forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const t = btn.dataset.actChip;
      const current = new Set(_activationRulesData.tags_auto_on || []);
      if(current.has(t)) current.delete(t); else current.add(t);
      _activationRulesData.tags_auto_on = [...current];
      _renderActivationRulesEditor();
    });
  });
}

async function _saveActivationRules(){
  const tags_auto_on = [...new Set(_activationRulesData.tags_auto_on || [])];
  try {
    await apiPut("/activation-rules", {tags_auto_on});
    _activationRulesData = await api("/activation-rules");
    _renderActivationRulesEditor();
    toast("Activation rules saved. Re-ingest to apply.", "success");
  } catch(e){
    toast("Failed to save activation rules", "error");
  }
}

async function _saveNumberRanges(){
  const payload = {};
  for(const [name, r] of Object.entries(_numberRangesData || {})){
    const start = parseInt(r.start);
    const end = parseInt(r.end);
    if(isNaN(start) || isNaN(end) || start <= 0 || end < start){
      toast(`Range "${name}" is invalid — fix start/end first`, "error");
      return;
    }
    payload[name] = {start, end};
  }
  try {
    await apiPut("/number-ranges", payload);
    _numberRangesData = await api("/number-ranges");
    _renderNumberRangesEditor();
    toast("Number ranges saved. Re-ingest or click Auto-Number to apply.", "success");
  } catch(e){
    toast("Failed to save number ranges", "error");
  }
}

function _renderTagRulesEditor(){
  const priorityInput = $("#tag-rules-priority");
  priorityInput.value = (_tagRulesData.priority || []).join(", ");

  const listEl = $("#tag-rules-list");
  const tagNames = Object.keys(_tagRulesData).filter(k => k !== "priority").sort();

  if(!tagNames.length){
    listEl.innerHTML = '<div class="empty-state" style="padding:16px">No tags defined. Click "Add Tag" to create one.</div>';
    return;
  }

  listEl.innerHTML = tagNames.map(name => {
    const spec = _tagRulesData[name] || {};
    const keywords = (spec.keywords || []).join("\n");
    const domains = (spec.domain_keywords || []).join("\n");
    return `<div class="tag-rule-card" data-tag-card="${name}">
      <div class="tag-rule-head">
        <strong class="tag-rule-name">${name}</strong>
        <button class="btn-sm btn-ghost" data-tag-delete="${name}">Remove</button>
      </div>
      <div class="tag-rule-grid">
        <div class="setting-field">
          <label>Title keywords (one per line)</label>
          <textarea rows="4" data-tag-keywords="${name}">${keywords}</textarea>
        </div>
        <div class="setting-field">
          <label>Domain keywords</label>
          <textarea rows="4" data-tag-domains="${name}">${domains}</textarea>
        </div>
      </div>
    </div>`;
  }).join("");

  $$("[data-tag-delete]", listEl).forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const name = btn.dataset.tagDelete;
      if(!confirm(`Remove tag "${name}"? Channels will stop matching this category on next ingest.`)) return;
      delete _tagRulesData[name];
      _renderTagRulesEditor();
    });
  });
}

function _renderNumberRangesEditor(){
  const listEl = $("#number-ranges-list");
  const ranges = _numberRangesData || {};
  const tagNames = Object.keys(ranges);

  // Active-channel counts per primary_tag for utilization display.
  const activeCounts = {};
  (channels || []).forEach(c=>{
    if(c.active && c.primary_tag){
      activeCounts[c.primary_tag] = (activeCounts[c.primary_tag] || 0) + 1;
    }
  });

  if(!tagNames.length){
    listEl.innerHTML = '<div class="empty-state" style="padding:16px">No ranges configured. Pick a tag below and click "Add Range".</div>';
  } else {
    listEl.innerHTML = tagNames.map(name => {
      const r = ranges[name] || {};
      const start = r.start ?? "";
      const end = r.end ?? "";
      const slots = (Number(end) - Number(start) + 1) || 0;
      const used = activeCounts[name] || 0;
      const pct = slots > 0 ? Math.round(used / slots * 100) : 0;
      const over = used > slots && slots > 0;
      return `<div class="range-edit-row ${over?"range-edit-row-over":""}" data-range-tag="${name}">
        <span class="range-edit-name">${name}</span>
        <input type="number" class="range-edit-num" data-range-start="${name}" value="${start}" min="1">
        <span class="range-edit-sep">–</span>
        <input type="number" class="range-edit-num" data-range-end="${name}" value="${end}" min="1">
        <span class="range-edit-stats">
          <span class="range-edit-slots">${slots > 0 ? slots : "—"} slots</span>
          <span class="range-edit-util ${over?"range-edit-util-over":""}">${used} active ${slots > 0 ? `(${pct}%)` : ""}</span>
        </span>
        <button class="btn-sm btn-ghost" data-range-delete="${name}" title="Remove">×</button>
      </div>`;
    }).join("");
  }

  // Populate "Add Range" dropdown with eligible tags (in priority but not yet ranged)
  const priority = (_tagRulesData && _tagRulesData.priority) || [];
  const allKnown = new Set([
    ...priority,
    ...Object.keys(_tagRulesData || {}).filter(k => k !== "priority"),
  ]);
  const available = [...allKnown].filter(t => !tagNames.includes(t));
  const addSel = $("#number-ranges-add-tag");
  addSel.innerHTML = available.length
    ? available.map(t => `<option value="${t}">${t}</option>`).join("")
    : '<option value="">(no tags left to add)</option>';
  $("#number-ranges-add").disabled = !available.length;

  _updateRangeOverlapWarnings();

  $$("[data-range-delete]", listEl).forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const name = btn.dataset.rangeDelete;
      if(!confirm(`Remove range for "${name}"? Channels with this primary tag will stop being auto-numbered until you add it back.`)) return;
      delete _numberRangesData[name];
      _renderNumberRangesEditor();
    });
  });

  $$(".range-edit-num", listEl).forEach(inp=>{
    inp.addEventListener("change", ()=>{
      const name = inp.dataset.rangeStart || inp.dataset.rangeEnd;
      const side = inp.dataset.rangeStart ? "start" : "end";
      const val = parseInt(inp.value);
      if(!_numberRangesData[name]) _numberRangesData[name] = {start: 0, end: 0};
      _numberRangesData[name][side] = isNaN(val) ? 0 : val;
      // Re-render to refresh slot count + utilization + overlap
      _renderNumberRangesEditor();
    });
  });
}

function _updateRangeOverlapWarnings(){
  const warnEl = $("#number-ranges-warnings");
  if(!warnEl) return;
  const ranges = _numberRangesData || {};
  const entries = Object.entries(ranges)
    .filter(([, r]) => r && Number(r.start) > 0 && Number(r.end) >= Number(r.start));

  const overlaps = [];
  for(let i = 0; i < entries.length; i++){
    for(let j = i + 1; j < entries.length; j++){
      const [a, ar] = entries[i];
      const [b, br] = entries[j];
      if(Number(ar.start) <= Number(br.end) && Number(br.start) <= Number(ar.end)){
        overlaps.push(`${a} (${ar.start}–${ar.end}) overlaps ${b} (${br.start}–${br.end})`);
      }
    }
  }
  const invalid = Object.entries(ranges)
    .filter(([, r]) => !r || !(Number(r.end) >= Number(r.start) && Number(r.start) > 0))
    .map(([name]) => name);

  const msgs = [];
  if(invalid.length) msgs.push(`Invalid ranges: ${invalid.join(", ")} (start must be > 0 and end must be ≥ start)`);
  overlaps.forEach(m => msgs.push(m));
  warnEl.innerHTML = msgs.length
    ? msgs.map(m => `<div class="range-edit-warning">⚠ ${m}</div>`).join("")
    : "";
}

async function _saveTagRules(){
  // Collect state from DOM
  const priorityRaw = $("#tag-rules-priority").value || "";
  const priority = priorityRaw.split(",").map(s=>s.trim()).filter(Boolean);

  const payload = {priority};
  $$("[data-tag-card]").forEach(card=>{
    const name = card.dataset.tagCard;
    const kwEl = $(`[data-tag-keywords="${name}"]`, card);
    const domEl = $(`[data-tag-domains="${name}"]`, card);
    const keywords = (kwEl.value || "").split("\n").map(s=>s.trim().toLowerCase()).filter(Boolean);
    const domain_keywords = (domEl.value || "").split("\n").map(s=>s.trim().toLowerCase()).filter(Boolean);
    payload[name] = {keywords, domain_keywords};
  });

  // Warn on priority tags that aren't defined
  const undefinedPriority = priority.filter(p => !payload[p]);
  if(undefinedPriority.length){
    if(!confirm(`Priority references undefined tags: ${undefinedPriority.join(", ")}. Save anyway?`)) return;
  }

  try {
    await apiPut("/tag-rules", payload);
    _tagRulesData = await api("/tag-rules");
    _renderTagRulesEditor();
    toast("Tag rules saved. Re-ingest to apply.", "success");
  } catch(e){
    toast("Failed to save tag rules", "error");
  }
}

// ── Integrations ────────────────────────────────────────────────────────
let _integData = {};

async function loadIntegrations(){
  try {
    _integData = await api("/integrations/status");
    renderIntegrations();
  } catch(e){
    const c = $("#integrations-container");
    if(c) c.innerHTML = '<div class="empty-state">Failed to load integrations</div>';
  }
}

function renderIntegrations(){
  const c = $("#integrations-container");
  if(!c) return;
  const jf = _integData.jellyfin || {};
  function badge(configured){
    if(!configured) return `<span class="integ-badge integ-not-configured">Not Configured</span>`;
    return `<span class="integ-badge integ-configured">Configured</span>`;
  }
  c.innerHTML = `<div class="integ-grid">
    <div class="integ-card" data-integ="jellyfin">
      <div class="integ-card-header">
        <span class="integ-card-name">Jellyfin</span>
        ${badge(jf.configured)}
      </div>
      <div class="integ-card-desc">Trigger Jellyfin's guide refresh after M3U/XMLTV regen so it picks up new data without waiting for its cache TTL.</div>
    </div>
  </div>`;
  $$(".integ-card", c).forEach(card=>{
    card.addEventListener("click", ()=> openIntegModal(card.dataset.integ));
  });
}

function closeIntegModal(){
  $("#integ-modal-overlay").classList.add("hidden");
}

function openIntegModal(type){
  const overlay = $("#integ-modal-overlay");
  const body = $("#integ-modal-body");
  const titleEl = $("#integ-modal-title");
  overlay.classList.remove("hidden");
  if(type === "jellyfin"){
    const jf = _integData.jellyfin || {};
    titleEl.textContent = "Jellyfin Integration";
    body.innerHTML = `
      <div class="integ-modal-fields">
        <label>Server URL<input type="text" id="integ-jf-url" value="${esc(jf.url||"")}" placeholder="http://192.168.20.34:8096"></label>
        <label>API Key<input type="text" id="integ-jf-key" value="${esc(jf.api_key||"")}" placeholder="Jellyfin API key"></label>
        <div class="integ-toggle-row">
          <label class="scraper-toggle"><input type="checkbox" id="integ-jf-auto" ${jf.auto_refresh?"checked":""}><span class="slider"></span></label>
          <span>Auto-refresh Jellyfin after M3U/XMLTV regeneration</span>
        </div>
        <div class="integ-toggle-row">
          <label class="scraper-toggle"><input type="checkbox" id="integ-jf-rebind" ${jf.rebind_mode?"checked":""}><span class="slider"></span></label>
          <span>Force rebind on every refresh (drops stale channel bindings — use while adding/removing channels)</span>
        </div>
      </div>
      <p style="font-size:11px;color:var(--text-muted);margin-bottom:12px">Refresh triggers Jellyfin's guide data task. Rebind additionally deletes + re-adds the XMLTV listings provider so Jellyfin rediscovers every channel from scratch. Neither modifies tuner host URLs — configure those once in Jellyfin's Live TV settings.</p>
      <div class="integ-modal-actions">
        <button class="btn btn-primary btn-sm" id="integ-jf-save">Save</button>
        <button class="btn btn-sm" id="integ-jf-test">Test Connection</button>
        <button class="btn btn-sm" id="integ-jf-refresh">Force Refresh</button>
      </div>
      <div id="integ-jf-result" style="margin-top:12px;font-size:12px"></div>`;

    async function saveConfig(){
      return apiPut("/integrations/jellyfin/config", {
        url: $("#integ-jf-url").value.trim(),
        api_key: $("#integ-jf-key").value.trim(),
        auto_refresh: $("#integ-jf-auto").checked,
        rebind_mode: $("#integ-jf-rebind").checked,
      });
    }

    $("#integ-jf-save").addEventListener("click", async ()=>{
      try {
        const r = await saveConfig();
        if(r.ok){ toast("Jellyfin config saved", "success"); loadIntegrations(); }
        else toast("Save failed", "error");
      } catch(e){ toast("Save failed", "error"); }
    });

    $("#integ-jf-test").addEventListener("click", async ()=>{
      const res = $("#integ-jf-result");
      res.textContent = "Testing...";
      try {
        await saveConfig();
        const d = await apiPost("/integrations/jellyfin/test");
        if(d.ok){
          res.innerHTML = `<span style="color:var(--ok)">Connected: ${esc(d.server_name)} v${esc(d.version)}</span>`;
          loadIntegrations();
        } else {
          res.innerHTML = `<span style="color:var(--danger)">${esc(d.error)}</span>`;
        }
      } catch(e){ res.innerHTML = '<span style="color:var(--danger)">Connection failed</span>'; }
    });

    $("#integ-jf-refresh").addEventListener("click", async ()=>{
      const res = $("#integ-jf-result");
      const willRebind = $("#integ-jf-rebind").checked;
      res.textContent = willRebind ? "Saving + rebinding provider..." : "Saving + triggering guide refresh...";
      try {
        await saveConfig();  // persist checkbox state first so backend dispatches correctly
        const d = await apiPost("/integrations/jellyfin/refresh");
        if(d.ok){
          const msg = d.mode === "rebind" ? "Rebind + guide refresh triggered" : "Guide refresh triggered";
          res.innerHTML = `<span style="color:var(--ok)">${msg}</span>`;
        } else {
          res.innerHTML = `<span style="color:var(--danger)">${esc(d.error)}</span>`;
        }
      } catch(e){ res.innerHTML = '<span style="color:var(--danger)">Refresh failed</span>'; }
    });
  }
}

$("#integ-modal-close").addEventListener("click", closeIntegModal);
$("#integ-modal-overlay").addEventListener("click", (e)=>{
  if(e.target.classList.contains("modal-overlay")) closeIntegModal();
});

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
  vpn_sample: "VPN Latency Sampler",
  vpn_rotate: "VPN Auto-Rotate Check",
  vpn_scheduled_rotate: "Scheduled VPN Rotate",
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
    const isCron = job.trigger_type === "cron";

    let selectHtml = "";
    if(isCron){
      // Time-of-day picker for cron jobs (vpn_scheduled_rotate). Triggers a
      // PUT with {time} on change instead of {interval_seconds}.
      const timeVal = job.cron_time || "04:00";
      selectHtml = `<input type="time" class="task-time" data-task-cron="${job.id}" value="${timeVal}">`;
    } else if(options.length){
      selectHtml = `<select class="task-select" data-task-interval="${job.id}">` +
        options.map(o => `<option value="${o.seconds}" ${Math.abs(job.interval_seconds - o.seconds) < 5 ? "selected" : ""}>${o.label}</option>`).join("") +
        `</select>`;
    }
    const labelText = isCron ? "At:" : "Every:";

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
        <span>${labelText} ${selectHtml}</span>
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

  // Bind cron time change handlers (for vpn_scheduled_rotate)
  $$("[data-task-cron]", container).forEach(inp => {
    inp.addEventListener("change", async () => {
      const jobId = inp.dataset.taskCron;
      const time = inp.value;
      if(!time){ return; }
      try {
        await apiPut(`/scheduler/${jobId}`, {time});
        toast(`Scheduled at ${time} daily`, "success");
        setTimeout(()=> renderTasksView(), 500);
      } catch(e){ toast("Failed to update schedule", "error"); }
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
  const host = $("#set-bridge-host");
  const port = $("#set-bridge-port");
  const tmdbKey = $("#set-tmdb-key");

  if(regen) payload.scheduler_regen_minutes = regen.value;
  if(cleanup) payload.scheduler_cleanup_hours = cleanup.value;
  const vpnRotate = $("#set-vpn-rotate");
  if(vpnRotate) payload.vpn_auto_rotate_minutes = vpnRotate.value;
  if(host) payload.bridge_host = host.value;
  if(port) payload.bridge_port = port.value;
  if(tmdbKey) payload.tmdb_api_key = tmdbKey.value;
  const fanartKey = $("#set-fanart-key");
  if(fanartKey) payload.fanart_api_key = fanartKey.value;
  const dummyDays = $("#set-dummy-days");
  const dummyBlock = $("#set-dummy-block");
  if(dummyDays) payload.dummy_epg_days = dummyDays.value;
  if(dummyBlock) payload.dummy_epg_block_minutes = dummyBlock.value;
  const exportStrategy = $("#set-export-strategy");
  if(exportStrategy) payload.export_strategy = exportStrategy.value;
  const exportPath = $("#set-export-path");
  if(exportPath) payload.export_local_path = exportPath.value;

  await apiPost("/settings", payload);
  settings = await api("/settings");
  if(typeof _updateExportLinks === "function") _updateExportLinks();
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
