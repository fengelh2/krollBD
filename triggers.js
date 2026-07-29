// triggers.js — legacy trigger-card view, preserved verbatim from app.js v7
// and re-exposed as K.Triggers.{init, getOpenIssues, refresh} so the router
// can show/hide it without re-running fetches.

(function () {
  const REPO = "fengelh2/krollBD";
  const API = `https://api.github.com/repos/${REPO}/issues`;
  const DISPATCH = `https://api.github.com/repos/${REPO}/dispatches`;
  const CSV_URL = `https://raw.githubusercontent.com/${REPO}/main/outreach_log.csv`;
  const PAT_KEY = "krollbd_pat";
  const PENDING_KEY = "krollbd_pending_v1";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let CURRENT_FILTER = "all";
  let ALL_OPEN = [];
  let LOG_ROWS = [];

  function getPat() { return localStorage.getItem(PAT_KEY) || ""; }
  function setPat(t) { if (t) localStorage.setItem(PAT_KEY, t); else localStorage.removeItem(PAT_KEY); refreshPatStatus(); }
  function refreshPatStatus() {
    const has = !!getPat();
    const el = $("#pat-status");
    if (!el) return;
    el.textContent = has ? "✓ token set" : "no token";
    el.className = has ? "pat-status pat-ok" : "pat-status pat-missing";
  }
  function promptForPat() {
    // Prefer the shared in-page modal from app.js. Fall back to alert if
    // the modal hook isn't loaded yet (rare race during cold start).
    if (window.K && typeof window.K.promptForPat === "function") {
      window.K.promptForPat();
      return;
    }
    alert("Click the 'GitHub token' button at the top right to set your PAT, then try again.");
  }

  function getPending() { try { return new Set(JSON.parse(localStorage.getItem(PENDING_KEY) || "[]")); } catch { return new Set(); } }
  function addPending(n) { const s = getPending(); s.add(String(n)); localStorage.setItem(PENDING_KEY, JSON.stringify(Array.from(s))); }
  function removePending(n) { const s = getPending(); s.delete(String(n)); localStorage.setItem(PENDING_KEY, JSON.stringify(Array.from(s))); }

  const _META_CACHE = new Map();
  async function fetchMetaFile(path) {
    if (_META_CACHE.has(path)) return _META_CACHE.get(path);
    const pat = getPat();
    // cache-bust both the GitHub API CDN edge and the browser HTTP cache
    const url = `https://api.github.com/repos/${REPO}/contents/${encodeURI(path)}?_=${Date.now()}`;
    try {
      const r = await fetch(url, {
        cache: "no-store",
        headers: {
          "Accept": "application/vnd.github+json",
          ...(pat ? { "Authorization": `Bearer ${pat}` } : {}),
        },
      });
      if (!r.ok) { _META_CACHE.set(path, null); return null; }
      const data = await r.json();
      const b64 = (data.content || "").replace(/\s/g, "");
      const json = decodeURIComponent(escape(atob(b64)));
      const parsed = JSON.parse(json);
      _META_CACHE.set(path, parsed);
      return parsed;
    } catch (e) {
      _META_CACHE.set(path, null);
      return null;
    }
  }
  function parseMetaSync(body) {
    if (!body) return null;
    const b64 = body.match(/<!--\s*DASH_META_B64:\s*([A-Za-z0-9+/=]+)\s*-->/);
    if (b64) {
      try { return JSON.parse(decodeURIComponent(escape(atob(b64[1])))); } catch {}
    }
    const m = body.match(/<!--\s*DASH_META:\s*(\{[\s\S]*?\})\s*-->/);
    if (m) { try { return JSON.parse(m[1]); } catch {} }
    return null;
  }
  function parseMeta(bodyOrIssue) {
    if (!bodyOrIssue) return null;
    if (typeof bodyOrIssue === "object" && bodyOrIssue._meta !== undefined) {
      return bodyOrIssue._meta;
    }
    const body = typeof bodyOrIssue === "string" ? bodyOrIssue : bodyOrIssue.body;
    return parseMetaSync(body);
  }
  async function fetchAndAttachMetas(issues) {
    await Promise.all(issues.map(async (i) => {
      if (i._meta !== undefined) return;
      const body = i.body || "";
      const fileMatch = body.match(/META_FILE:\s*(\S+)/);
      if (fileMatch) {
        i._meta = await fetchMetaFile(fileMatch[1]);
      } else {
        i._meta = parseMetaSync(body);
      }
    }));
    return issues;
  }

  async function fetchIssues(state) {
    const out = [];
    for (let p = 1; p <= 10; p++) {
      const r = await fetch(`${API}?state=${state}&per_page=100&page=${p}`,
        { headers: { "Accept": "application/vnd.github+json", ...(getPat() ? { "Authorization": `Bearer ${getPat()}` } : {}) } });
      if (!r.ok) throw new Error("GitHub API: " + r.status);
      const batch = await r.json();
      out.push(...batch.filter(i => !i.pull_request));
      if (batch.length < 100) break;
    }
    return out;
  }
  async function fetchLog() {
    try {
      const r = await fetch(CSV_URL + "?t=" + Date.now());
      if (!r.ok) return [];
      const text = await r.text();
      return parseCsv(text);
    } catch { return []; }
  }
  function parseCsv(text) {
    // Quoted CSV fields can span multiple lines (e.g. an email body with
    // embedded newlines). A naive split-by-newline turns each body line
    // into a fake row — which inflated the "reached out" counter (17
    // actual sends were showing as ~105). Walk char by char and treat
    // newlines INSIDE quoted fields as literal content.
    const rows = [];
    let row = [];
    let cur = "";
    let q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) {
        if (c === '"' && text[i+1] === '"') { cur += '"'; i++; }
        else if (c === '"') q = false;
        else cur += c;
      } else {
        if (c === '"') q = true;
        else if (c === ',') { row.push(cur); cur = ""; }
        else if (c === '\n' || c === '\r') {
          if (c === '\r' && text[i+1] === '\n') i++;
          row.push(cur); cur = "";
          // ignore wholly-empty rows (trailing newlines)
          if (row.length > 1 || row[0] !== "") rows.push(row);
          row = [];
        }
        else cur += c;
      }
    }
    if (cur !== "" || row.length > 0) { row.push(cur); rows.push(row); }
    if (rows.length < 2) return [];
    const hdr = rows[0];
    return rows.slice(1)
      .filter(r => r.some(v => (v || "").trim() !== ""))
      .map(r => {
        const o = {};
        hdr.forEach((h, i) => o[h] = r[i] || "");
        return o;
      });
  }
  // Legacy splitter kept for any external callers that might still use it.
  function splitCsvLine(ln) {
    const out = []; let cur = ""; let q = false;
    for (let i = 0; i < ln.length; i++) {
      const c = ln[i];
      if (q) {
        if (c === '"' && ln[i+1] === '"') { cur += '"'; i++; }
        else if (c === '"') q = false;
        else cur += c;
      } else {
        if (c === ',') { out.push(cur); cur = ""; }
        else if (c === '"') q = true;
        else cur += c;
      }
    }
    out.push(cur);
    return out;
  }

  async function fetchEnrichStatus() {
    // Fetch the bulk-enrich status file via Contents API (cache-busted)
    const pat = getPat();
    const url = `https://api.github.com/repos/${REPO}/contents/data/.enrich_status.json?_=${Date.now()}`;
    try {
      const r = await fetch(url, {
        cache: "no-store",
        headers: {
          "Accept": "application/vnd.github+json",
          ...(pat ? { "Authorization": `Bearer ${pat}` } : {}),
        },
      });
      if (!r.ok) return null;
      const j = await r.json();
      const decoded = decodeURIComponent(escape(atob((j.content || "").replace(/\s/g, ""))));
      return JSON.parse(decoded);
    } catch (e) {
      return null;
    }
  }

  async function startEnrichAll() {
    const pat = getPat();
    if (!pat) { promptForPat(); if (!getPat()) return; }
    if (!confirm("Run firecrawl scrape + Hunter+AbstractAPI on every open trigger?\n\nTypical runtime: ~15-60s per trigger × open trigger count. You can leave this tab open or come back later.")) return;
    const btn = document.getElementById("enrich-all-btn");
    const hint = document.getElementById("enrich-all-hint");
    const wrap = document.getElementById("enrich-all-progress");
    const fill = document.getElementById("enrich-all-fill");
    const lbl = document.getElementById("enrich-all-label");
    btn.disabled = true; btn.textContent = "Dispatching…";
    hint.hidden = true;
    wrap.hidden = false;
    fill.style.width = "5%";
    lbl.textContent = "queued — waiting for GitHub Actions to start…";

    // Capture baseline so we know which run is ours
    const baselineStatus = await fetchEnrichStatus();
    const baselineStartedAt = baselineStatus ? baselineStatus.started_at : null;

    const dispatchR = await fetch(DISPATCH, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${getPat()}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ event_type: "enrich_all", client_payload: {} }),
    });
    if (!dispatchR.ok) {
      const t = await dispatchR.text().catch(() => "");
      lbl.textContent = `Dispatch failed (${dispatchR.status}): ${t.slice(0,200)}`;
      lbl.style.color = "#991b1b";
      btn.disabled = false; btn.textContent = "Enrich all open triggers";
      return;
    }
    btn.textContent = "Running…";

    // Poll status file every 5s
    const t0 = Date.now();
    const POLL = 5000;
    const TIMEOUT_S = 60 * 60;
    const tick = setInterval(async () => {
      const elapsed = (Date.now() - t0) / 1000;
      if (elapsed > TIMEOUT_S) {
        clearInterval(tick);
        lbl.textContent = "Timed out after 60 min. Workflow may still be running — refresh later.";
        btn.disabled = false; btn.textContent = "Enrich all open triggers";
        return;
      }
      const s = await fetchEnrichStatus();
      // Wait until status file reflects OUR run (newer started_at than baseline)
      if (!s || (baselineStartedAt && s.started_at === baselineStartedAt && !s.done)) {
        lbl.textContent = `queued — workflow spinning up… (${Math.floor(elapsed)}s elapsed)`;
        return;
      }
      if (s.done) {
        clearInterval(tick);
        fill.style.width = "100%";
        fill.style.background = "#10b981";
        lbl.textContent = `Done — ${s.total} triggers enriched in ${Math.floor((Date.now()-t0)/1000)}s. Refreshing cards…`;
        // Clear in-memory meta cache + refresh
        _META_CACHE.clear();
        ALL_OPEN.forEach(i => delete i._meta);
        setTimeout(async () => {
          await fetchAndAttachMetas(ALL_OPEN);
          refresh();
          btn.disabled = false; btn.textContent = "Enrich all open triggers";
          // Browser notification if permitted
          if ("Notification" in window && Notification.permission === "granted") {
            new Notification("Enrich-all complete", { body: `${s.total} triggers refreshed.` });
          }
        }, 800);
        return;
      }
      const pct = Math.min(95, (s.current_idx / Math.max(1, s.total)) * 100);
      fill.style.width = pct.toFixed(1) + "%";
      lbl.textContent = `Processing ${s.current_idx} of ${s.total}${s.current_firm ? " — " + s.current_firm : ""} (${Math.floor(elapsed)}s elapsed)`;
    }, POLL);

    // Best-effort notification permission
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }

  // Wire the global button once Triggers view is initialized
  function wireEnrichAllButton() {
    const btn = document.getElementById("enrich-all-btn");
    if (btn && !btn._wired) {
      btn.addEventListener("click", startEnrichAll);
      btn._wired = true;
    }
  }

  async function fetchMetaSha(path) {
    // Get the current Git SHA of a meta file. Used to detect when the
    // on-demand enrichment workflow has written a new version.
    const pat = getPat();
    const url = `https://api.github.com/repos/${REPO}/contents/${encodeURI(path)}?_=${Date.now()}`;
    const r = await fetch(url, {
      cache: "no-store",
      headers: {
        "Accept": "application/vnd.github+json",
        ...(pat ? { "Authorization": `Bearer ${pat}` } : {}),
      },
    });
    if (!r.ok) return null;
    const j = await r.json();
    return j.sha || null;
  }

  async function dispatchEnrichment(eventType, issue, meta, btnEl, label) {
    const pat = getPat();
    if (!pat) { promptForPat(); if (!getPat()) return false; }
    const metaFileMatch = (issue.body || "").match(/META_FILE:\s*(\S+)/);
    if (!metaFileMatch) {
      alert("Can't enrich: this issue has no META_FILE pointer in its body.");
      return false;
    }
    const metaPath = metaFileMatch[1];
    const baselineSha = await fetchMetaSha(metaPath);

    // Replace the button with a progress bar
    const wrap = document.createElement("div");
    wrap.className = "enrich-progress";
    wrap.innerHTML = `
      <div class="enrich-bar"><div class="enrich-bar-fill"></div></div>
      <div class="enrich-label">${label} · queued <span class="enrich-elapsed">0s</span></div>
    `;
    btnEl.replaceWith(wrap);
    const fill = wrap.querySelector(".enrich-bar-fill");
    const lbl = wrap.querySelector(".enrich-label");
    const elap = wrap.querySelector(".enrich-elapsed");
    const t0 = Date.now();
    let phase = "queued";
    // Indeterminate-ish progress: fill bar based on elapsed vs expected (~90s)
    const tick = setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000);
      elap.textContent = `${s}s`;
      const pct = Math.min(95, (s / 90) * 100);
      fill.style.width = pct + "%";
      if (s > 5 && phase === "queued") {
        phase = "running"; lbl.firstChild.nodeValue = `${label} · running `;
      }
    }, 500);

    const r = await fetch(DISPATCH, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${getPat()}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        event_type: eventType,
        client_payload: {
          trigger_id: (metaPath.split("/").pop() || "").replace(/\.json$/, ""),
          ceref: meta.ceref,
          issue_number: issue.number,
        },
      }),
    });
    if (!r.ok) {
      clearInterval(tick);
      const msg = await r.text().catch(() => "");
      wrap.innerHTML = `<div class="enrich-label" style="color:#991b1b">Dispatch failed (${r.status}): ${msg.slice(0,200)}</div>`;
      return false;
    }

    // Poll meta-file SHA until it changes — workflow has committed → done
    const POLL_INTERVAL = 5000;
    const TIMEOUT_S = 300;
    return await new Promise((resolve) => {
      const poll = setInterval(async () => {
        const elapsedS = (Date.now() - t0) / 1000;
        if (elapsedS > TIMEOUT_S) {
          clearInterval(poll); clearInterval(tick);
          wrap.innerHTML = `<div class="enrich-label" style="color:#991b1b">Timed out after ${TIMEOUT_S}s — workflow may still be running. Refresh in a minute.</div>`;
          resolve(false);
          return;
        }
        const sha = await fetchMetaSha(metaPath);
        if (sha && sha !== baselineSha) {
          clearInterval(poll); clearInterval(tick);
          fill.style.width = "100%";
          fill.style.background = "#10b981";
          lbl.textContent = `${label} · done in ${Math.floor(elapsedS)}s — refreshing card`;
          // Wipe in-memory cache for this meta file so refresh picks up new
          _META_CACHE.delete(metaPath);
          delete issue._meta;
          setTimeout(() => {
            // Re-fetch issue body + meta, then re-render this card
            fetchAndAttachMetas([issue]).then(() => {
              const fresh = renderCard(issue, { pending: false });
              if (fresh) wrap.closest(".card").replaceWith(fresh);
            });
          }, 600);
          resolve(true);
        }
      }, POLL_INTERVAL);
    });
  }

  async function dropTrigger(issue, meta, reason) {
    const pat = getPat();
    if (!pat) { promptForPat(); if (!getPat()) return false; }
    const url = `https://api.github.com/repos/${REPO}/issues/${issue.number}`;
    // Close as "not_planned" + add a dropped label + comment with the reason
    // so future audit shows why this trigger wasn't pursued.
    const commentR = await fetch(`${url}/comments`, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${getPat()}`,
      },
      body: JSON.stringify({ body: `Dropped (no outreach planned).\n\nReason: ${reason || "(none provided)"}\n\n_Dropped via dashboard._` }),
    });
    if (!commentR.ok) {
      const t = await commentR.text().catch(() => "");
      alert(`Comment failed (${commentR.status}): ${t.slice(0,200)}`);
      return false;
    }
    const closeR = await fetch(url, {
      method: "PATCH",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${getPat()}`,
      },
      body: JSON.stringify({
        state: "closed",
        state_reason: "not_planned",
        labels: [...(issue.labels || []).map(l => l.name || l), "dropped-no-outreach"],
      }),
    });
    if (!closeR.ok) {
      const t = await closeR.text().catch(() => "");
      alert(`Close failed (${closeR.status}): ${t.slice(0,200)}`);
      return false;
    }
    return true;
  }

  // Reverted 2026-07-29: direct Contents API write nuked outreach_log.csv
  // on issue #253 (one flaky/empty GET response → PUT replaced 2125 rows
  // with 2 lines). Back to repository_dispatch → log_outreach workflow;
  // that path is 30-60s but survived after the per-issue concurrency +
  // dedup + 15-try retry fixes earlier this session.
  async function dispatchOutreach(issue, meta) {
    const pat = getPat();
    if (!pat) { promptForPat(); if (!getPat()) return false; }
    const data = {
      issue_number: issue.number,
      sent_at_utc: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      trigger_type: meta.type,
      variant_id: meta.variant_id || (meta.type + "-v1"),
      firm: meta.firm,
      ceref: meta.ceref,
      primary_ro: meta.primary_ro || "",
      email_subject: meta.email_subject || "",
      email_body_hash: meta.email_body_hash || "",
      email_body: meta.email_body || "",
      sent_via: "dashboard",
      notes: "",
    };
    const r = await fetch(DISPATCH, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${getPat()}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ event_type: "outreach_sent", client_payload: { data: JSON.stringify(data) } }),
    });
    if (!r.ok) {
      let msg = await r.text().catch(() => "");
      alert(`Dispatch failed (${r.status}): ${msg.slice(0,200)}\n\nCheck your PAT has 'public_repo' scope.`);
      return false;
    }
    return true;
  }

  function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
      const orig = btn.textContent;
      btn.textContent = "Copied"; btn.classList.add("copied");
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1500);
    });
  }

  function isoWeekKey(d) {
    const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const day = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    const wk = Math.ceil((((t - yearStart) / 86400000) + 1) / 7);
    return `${t.getUTCFullYear()}-W${String(wk).padStart(2, "0")}`;
  }
  function weeksBack(n) {
    const out = []; const now = new Date();
    for (let i = n - 1; i >= 0; i--) {
      const d = new Date(now); d.setUTCDate(d.getUTCDate() - i * 7);
      out.push(isoWeekKey(d));
    }
    return out;
  }
  function drawChart() {
    if (!$("#chart")) return;
    const weeks = weeksBack(12);
    // Per-week tally across ALL trigger types (was C1+C2 only — also missed R1/C5)
    const byWeek = {};
    for (const w of weeks) byWeek[w] = 0;
    for (const r of LOG_ROWS) {
      if (!r.sent_at_utc) continue;
      const wk = isoWeekKey(new Date(r.sent_at_utc));
      if (byWeek[wk] !== undefined) byWeek[wk] += 1;
    }
    const perWeek = weeks.map(w => byWeek[w]);

    const W = 600, H = 100, PAD = 12;
    const max = Math.max(1, ...perWeek);
    const barW = (W - PAD * 2) / weeks.length;
    const y = (v) => H - PAD - ((H - PAD * 2) * v / max);

    const bars = perWeek.map((v, i) => {
      const x = PAD + i * barW + 1;
      const h = (H - PAD) - y(v);
      return `<rect x="${x}" y="${y(v)}" width="${barW - 2}" height="${h}" fill="#1a3554" opacity="0.85"/>
              <text x="${x + barW/2 - 1}" y="${y(v) - 3}" font-size="9" fill="#1a3554" font-family="Inter,sans-serif" text-anchor="middle">${v || ""}</text>`;
    }).join("");

    $("#chart").innerHTML = `
      <line x1="0" y1="${H - PAD}" x2="${W}" y2="${H - PAD}" stroke="#e4e6ea" stroke-width="1"/>
      ${bars}
      <text x="${PAD}" y="${H-1}" font-size="8" fill="#7a818b" font-family="Inter,sans-serif">${weeks[0]}</text>
      <text x="${W-PAD-38}" y="${H-1}" font-size="8" fill="#7a818b" font-family="Inter,sans-serif">${weeks[weeks.length-1]}</text>
    `;
  }

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
  }

  function renderCard(issue, opts = {}) {
    const meta = parseMeta(issue);
    if (!meta) return null;
    const card = document.createElement("article");
    card.className = "card" + (opts.pending ? " pending" : "");
    card.dataset.type = meta.type;
    card.dataset.id = issue.number;
    const departed = meta.ros_departed || [];
    const rosList = meta.ros || meta.ros_current || [];
    const illiq = meta.illiq_likelihood || "";
    const ac = meta.asset_classes || "";
    const aum = meta.aum_raw_string || "";
    const parent = meta.parent_org || "";
    const cls_src = meta.classification_source || "";
    const wa = meta.website_accuracy || "";
    const has_strategy = illiq || ac || aum || parent || wa;
    const waBadge = wa ? `<span class="chip wa wa-${esc(wa)}" title="website accuracy verdict">site: ${esc(wa)}</span>` : "";
    const strategyChips = !has_strategy ? "" : `
      <div class="strategy-chips">
        ${waBadge}
        ${illiq ? `<span class="chip illiq-${illiq}">${esc({high:"illiquids",medium:"mixed",low:"liquids only",none:"no illiquids",unknown:"book unknown"}[illiq]||illiq)}</span>` : ""}
        ${ac ? `<span class="chip ac">${esc(ac)}</span>` : ""}
        ${aum ? `<span class="chip aum">AUM: ${esc(aum.slice(0,40))}</span>` : ""}
        ${parent ? `<span class="chip parent">parent: ${esc(parent)}</span>` : ""}
        ${cls_src ? `<span class="chip src" title="classification source">src: ${esc(cls_src)}</span>` : ""}
      </div>`;
    card.innerHTML = `
      <div class="head">
        <h2 class="firm">${esc(meta.firm)}</h2>
        <span class="tag ${meta.type}">${meta.type} · ${esc(meta.type_label)}${meta.variant_id ? ` · ${esc(meta.variant_id)}` : ""}</span>
      </div>
      <p class="meta">CE <code>${esc(meta.ceref)}</code> · <a href="${meta.sfc_url}" target="_blank" rel="noopener">SFC register →</a> · <a href="${issue.html_url}" target="_blank" rel="noopener">GitHub issue →</a></p>
      ${meta.address ? `<p class="addr">${esc(meta.address)}</p>` : ""}
      ${strategyChips}
      ${rosList.length ? `
        <div class="ros">
          <span class="lbl">${meta.type === 'C2' ? 'ROs still on file' : 'Responsible Officers'}</span>
          <ul>${rosList.map(r => `<li>${esc(r.name)} <span class="name-ceref">${esc(r.ceref)}</span></li>`).join("")}</ul>
        </div>` : ""}
      ${departed.length ? `
        <div class="ros">
          <span class="lbl warm">Departed ROs · warm-lead candidates</span>
          <ul>${departed.map(r => `<li>${esc(r.name)} <a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(r.name)}" target="_blank" rel="noopener" style="font-size:10px;color:var(--muted)">LinkedIn →</a></li>`).join("")}</ul>
        </div>` : ""}
      <div class="lookups">
        <a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(meta.natural)}" target="_blank" rel="noopener">LinkedIn · firm</a>
        ${meta.primary_ro ? `<a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(meta.primary_ro + ' ' + meta.natural)}" target="_blank" rel="noopener">LinkedIn · ${esc(meta.primary_ro)}</a>` : ""}
        <a href="https://www.google.com/search?q=${encodeURIComponent(meta.natural + ' hong kong contact email')}" target="_blank" rel="noopener">Google · contact</a>
        <a href="https://duckduckgo.com/?q=${encodeURIComponent(meta.natural + ' hong kong asset management')}" target="_blank" rel="noopener">Find website</a>
      </div>
      <div class="email-block">
        <div class="email-row">
          <span class="label">Subject</span>
          <span class="value">${esc(meta.email_subject || "")}</span>
          <button class="btn copy" data-copy="subject">Copy subject</button>
        </div>
        <div class="email-row" style="align-items:flex-start">
          <span class="label" style="padding-top:6px">Body</span>
          <div style="flex:1"><pre class="email-body-text">${esc(meta.email_body || "")}</pre></div>
        </div>
        <div class="actions" style="margin-top:6px">
          <button class="btn copy" data-copy="body">Copy body</button>
        </div>
      </div>
      ${(meta.email_candidates && meta.email_candidates.length) ? (() => {
        // Suppress pattern-guess clutter for an RO once we have a verified
        // address for the same person. (Generic firm-level guesses like
        // info@, contact@ stay — those aren't per-RO.)
        const verifiedRos = new Set(
          meta.email_candidates
            .filter(c => ["hunter_verified","verified","very_high","high"].includes((c.confidence||"").toLowerCase()))
            .map(c => (c.ro || "").toLowerCase().trim())
            .filter(Boolean)
        );
        const personKinds = new Set([
          "ro_guess","inferred_pattern","ro_pattern_match","ro_via_aggregator","person"
        ]);
        const showAllOverride = !!issue._showAllCands;
        const genericKinds = new Set(["generic_guess","generic_on_site","generic"]);
        // Any per-RO candidate on this card means the message is personalized
        // — generic inboxes are then the wrong recipient ("Dear Wayne" sent
        // to info@ makes no sense). Only show generics as a fallback when
        // there are zero per-RO candidates at all.
        const hasAnyPersonCand = meta.email_candidates.some(c =>
          personKinds.has(c.kind) || (c.kind === "hunter_io") || (c.kind === "observed_on_site" && c.ro)
        );
        const visibleCands = showAllOverride ? meta.email_candidates : meta.email_candidates.filter(c => {
          const ro = (c.ro || "").toLowerCase().trim();
          const conf = (c.confidence||"").toLowerCase();
          const isVerified = ["hunter_verified","verified","very_high","high"].includes(conf);
          // Rule 1: drop generic-inbox candidates when we have any personal email
          if (hasAnyPersonCand && genericKinds.has(c.kind)) return false;
          // Rule 2: drop lower-confidence per-RO guesses where that RO has a verified email
          if (!isVerified && personKinds.has(c.kind) && ro && verifiedRos.has(ro)) return false;
          return true;
        });
        const hiddenCount = meta.email_candidates.length - visibleCands.length;
        return `
        <div class="candidates">
          <div class="cand-head">
            <span>Email candidates · ordered best→worst · <em>verify before sending</em></span>
            ${hiddenCount ? `<span class="muted-text" style="font-size:11px">· ${hiddenCount} lower-confidence guess${hiddenCount===1?"":"es"} hidden (verified addr found) · <a href="#" data-action="show-all-cands" style="color:var(--muted)">show all</a></span>` : ""}
          </div>
          ${visibleCands.map(c => {
            // Per-RO draft: if a C1 has multiple founding ROs, each gets a
            // personalized salutation. Match candidate.ro to per_ro_drafts so
            // the mailto link opens with "Dear <this RO>" rather than the
            // primary RO's greeting.
            const perRoDrafts = meta.per_ro_drafts || [];
            const matchDraft = c.ro ? perRoDrafts.find(d =>
              (d.ro_name || "").toLowerCase() === (c.ro || "").toLowerCase()) : null;
            const useSubj = matchDraft ? matchDraft.email_subject : (meta.email_subject || "");
            const useBody = matchDraft ? matchDraft.email_body    : (meta.email_body || "");
            const subj = encodeURIComponent(useSubj);
            const body = encodeURIComponent(useBody);
            const mailto = `mailto:${c.email}?subject=${subj}&body=${body}`;
            const conf = (c.confidence || "low").toLowerCase();
            const kindLabel = ({
              "sfc_filed": "filed with SFC by the firm",
              "hunter_io": "hunter.io verified",
              "inferred_pattern": "inferred from firm pattern",
              "observed_on_site": "verified · on firm site",
              "generic_on_site": "verified · generic inbox on site",
              "ro_via_aggregator": "aggregator-declared pattern",
              "ro_pattern_match": "pattern match from observed",
              "ro_guess": "pattern guess",
              "generic_guess": "generic inbox guess",
              "person": c.ro ? `for ${esc(c.ro)}` : "person",
              "generic": "generic",
            })[c.kind] || c.kind || "";
            const roHint = c.ro ? ` · ${esc(c.ro)}` : "";
            const evidence = c.evidence ? ` title="${esc(c.evidence)}"` : "";

            // Inline verdict tag — surface the AbstractAPI / Hunter result
            // so the user doesn't have to hover for the reason a candidate
            // is low/medium/high.
            let verdictTag = "";
            if (c.kind === "sfc_filed") {
              verdictTag = `<span class="verdict-tag verdict-good">SFC filed-of-record ✓</span>`;
            } else if (c.kind === "hunter_io" && (conf === "hunter_verified" || conf === "high")) {
              verdictTag = `<span class="verdict-tag verdict-good">Hunter+SMTP verified ✓</span>`;
            } else if (c.flag === "abstractapi_says_undeliverable" || c.abstract_verdict === "undeliverable") {
              const det = c.abstract_detail ? ` (${esc(c.abstract_detail)})` : "";
              verdictTag = `<span class="verdict-tag verdict-bad">SMTP says undeliverable${det}</span>`;
            } else if (c.flag === "likely_catch_all") {
              verdictTag = `<span class="verdict-tag verdict-warn">catch-all domain — can't confirm</span>`;
            } else if (c.abstract_verdict === "deliverable") {
              verdictTag = `<span class="verdict-tag verdict-good">SMTP deliverable ✓</span>`;
            } else if (c.abstract_verdict === "risky") {
              verdictTag = `<span class="verdict-tag verdict-warn">SMTP risky</span>`;
            }

            return `
              <div class="cand-row conf-${conf}"${evidence}>
                <span class="conf-badge conf-${conf}">${conf}</span>
                <code class="cand-email">${esc(c.email)}</code>
                <span class="cand-kind">${kindLabel}${roHint}</span>
                ${verdictTag}
                <a class="btn small" href="${mailto}">Open in mail</a>
                <button class="btn copy small" data-copy-cand="${esc(c.email)}">Copy</button>
              </div>`;
          }).join("")}
        </div>
      `;
      })() : ""}
      <div class="actions">
        ${opts.pending
          ? `<span class="muted-text">Logging… (Action running, refresh in ~30s)</span>`
          : `<button class="btn primary" data-action="reached-out">Mark as reached out</button>
             <button class="btn ghost" data-action="drop" title="Close this trigger without outreach (e.g. mega-bank, no realistic conversion)">Drop</button>`}
      </div>
    `;
    card.querySelector('[data-copy="subject"]').addEventListener("click", e =>
      copyToClipboard(meta.email_subject || "", e.target));
    card.querySelector('[data-copy="body"]').addEventListener("click", e =>
      copyToClipboard(meta.email_body || "", e.target));
    card.querySelectorAll('[data-copy-cand]').forEach(b => b.addEventListener("click", e => {
      copyToClipboard(e.target.dataset.copyCand, e.target);
    }));
    const showAll = card.querySelector('[data-action="show-all-cands"]');
    if (showAll) showAll.addEventListener("click", (e) => {
      e.preventDefault();
      // Re-render this single card with the suppression bypassed
      issue._showAllCands = true;
      const fresh = renderCard(issue, { pending: opts.pending });
      if (fresh) card.replaceWith(fresh);
    });
    const btn = card.querySelector('[data-action="reached-out"]');
    if (btn) btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Sending…";
      const ok = await dispatchOutreach(issue, meta);
      if (ok) { addPending(issue.number); refresh(); }
      else { btn.disabled = false; btn.textContent = "Mark as reached out"; }
    });
    const dropBtn = card.querySelector('[data-action="drop"]');
    if (dropBtn) dropBtn.addEventListener("click", async () => {
      const reason = window.prompt(
        `Drop "${meta.firm}"?\n\nReason (optional — shown on the closed issue):`,
        "mega-bank — no realistic conversion"
      );
      if (reason === null) return;  // user cancelled
      dropBtn.disabled = true; dropBtn.textContent = "Dropping…";
      const ok = await dropTrigger(issue, meta, reason.trim());
      if (ok) {
        // Optimistically remove from the open list and re-render
        ALL_OPEN = ALL_OPEN.filter(i => i.number !== issue.number);
        refresh();
      } else {
        dropBtn.disabled = false; dropBtn.textContent = "Drop";
      }
    });
    return card;
  }

  function refresh() {
    if (!$("#cards")) return;
    const pending = getPending();
    const loggedIssueNums = new Set(LOG_ROWS.map(r => String(r.issue_number)));
    const openNums = new Set(ALL_OPEN.map(i => String(i.number)));
    for (const p of Array.from(pending)) {
      // Clear pending in two cases:
      //   1. issue was successfully logged (row exists in LOG_ROWS)
      //   2. issue is no longer open on GH (closed/deleted) AND not in
      //      LOG_ROWS — an orphan we can never resolve. Silently drop so
      //      it stops rendering a "Logging…" spinner.
      if (loggedIssueNums.has(p)) removePending(p);
      else if (openNums.size > 0 && !openNums.has(p)) removePending(p);
    }
    pending.clear();
    for (const p of getPending()) pending.add(p);
    const cards = $("#cards"); cards.innerHTML = "";
    const isPending = (i) => pending.has(String(i.number));
    let visible;
    if (CURRENT_FILTER === "done") {
      visible = [];
      const list = LOG_ROWS.slice().sort((a,b) => (b.sent_at_utc||"").localeCompare(a.sent_at_utc||""));
      if (!list.length) {
        cards.innerHTML = `<p class="loading">No outreach logged yet.</p>`;
      } else {
        cards.innerHTML = list.map(r => `
          <article class="card done">
            <div class="head">
              <h2 class="firm">${esc(r.firm || "(no firm)")}</h2>
              <span class="tag ${r.trigger_type}">${esc(r.trigger_type)} · ${esc(r.variant_id||"")}</span>
            </div>
            <p class="meta">Sent ${esc(r.sent_at_utc)} · CE <code>${esc(r.ceref)}</code> · issue <a href="https://github.com/${REPO}/issues/${r.issue_number}" target="_blank">#${esc(r.issue_number)}</a> · body_hash <code>${esc(r.email_body_hash)}</code></p>
            <p class="addr">Subject: ${esc(r.email_subject)}</p>
          </article>
        `).join("");
      }
    } else {
      visible = ALL_OPEN.slice();
      if (CURRENT_FILTER !== "all") {
        const splitFilter = CURRENT_FILTER.match(/^(C1|R1)-(PV|FSCR)$/);
        if (splitFilter) {
          const [_, t, lane] = splitFilter;
          visible = visible.filter(i => {
            const m = parseMeta(i);
            if (!m || m.type !== t) return false;
            const illiq = (m.illiq_likelihood || "").toLowerCase();
            const isPV = illiq === "high" || illiq === "medium";
            return lane === "PV" ? isPV : !isPV;
          });
        } else {
          visible = visible.filter(i => { const m = parseMeta(i); return m && m.type === CURRENT_FILTER; });
        }
      }
      if (visible.length === 0) {
        cards.innerHTML = `<p class="loading">Nothing in this view.</p>`;
      } else {
        visible.forEach(i => {
          const c = renderCard(i, { pending: isPending(i) });
          if (c) cards.appendChild(c);
        });
      }
    }
    const toAction = ALL_OPEN.filter(i => !isPending(i)).length;
    const doneEntries = LOG_ROWS;
    const reachedCount = doneEntries.length;
    const totalCycle = ALL_OPEN.length + reachedCount;
    $("#stat-open").textContent = toAction;

    // Count open triggers per filter bucket and surface as a chip on each
    // filter button so the user can see at-a-glance where work is sitting.
    const tallies = { all: 0, "C1-PV": 0, "C1-FSCR": 0, "R1-PV": 0, "R1-FSCR": 0, C2: 0, C5: 0 };
    for (const i of ALL_OPEN) {
      if (isPending(i)) continue;
      tallies.all += 1;
      const m = parseMeta(i);
      if (!m) continue;
      const t = m.type;
      if (t === "C1" || t === "R1") {
        const illiq = (m.illiq_likelihood || "").toLowerCase();
        const lane = (illiq === "high" || illiq === "medium") ? "PV" : "FSCR";
        tallies[`${t}-${lane}`] = (tallies[`${t}-${lane}`] || 0) + 1;
      } else if (t === "C2" || t === "C5") {
        tallies[t] = (tallies[t] || 0) + 1;
      }
    }
    $$("#filters button").forEach(b => {
      const key = b.dataset.filter;
      const n = (key === "done") ? reachedCount : (tallies[key] ?? null);
      if (n == null) return;
      // Remove any prior chip and re-append
      const oldChip = b.querySelector(".filter-count");
      if (oldChip) oldChip.remove();
      const chip = document.createElement("span");
      chip.className = "filter-count";
      chip.textContent = String(n);
      b.appendChild(chip);
    });
    $("#stat-done").textContent = reachedCount;
    const rate = totalCycle ? Math.round(100 * reachedCount / totalCycle) : 0;
    $("#stat-rate").textContent = rate + "%";
    const c1Done = doneEntries.filter(d => d.trigger_type === "C1").length;
    const c2Done = doneEntries.filter(d => d.trigger_type === "C2").length;
    const r1Done = doneEntries.filter(d => d.trigger_type === "R1").length;
    const c5Done = doneEntries.filter(d => d.trigger_type === "C5").length;
    $("#stat-c1").textContent = c1Done;
    $("#stat-c2").textContent = c2Done;
    if ($("#stat-r1")) $("#stat-r1").textContent = r1Done;
    if ($("#stat-c5")) $("#stat-c5").textContent = c5Done;
    const thisWeek = isoWeekKey(new Date());
    const weekDone = doneEntries.filter(d => d.sent_at_utc && isoWeekKey(new Date(d.sent_at_utc)) === thisWeek).length;
    $("#stat-week").textContent = weekDone;
    drawChart();
  }

  let _initted = false;
  async function init() {
    if (_initted) return; _initted = true;
    // Nuke pending on every page load — it's only meant as ~30s in-tab
    // UI feedback while a workflow runs. If the workflow completed, the
    // log row + closed issue are the durable state. If it didn't (silent
    // fail / cancellation), letting pending persist just glues a
    // permanent "Logging…" spinner to the card.
    localStorage.removeItem(PENDING_KEY);
    $$("#filters button").forEach(b => b.addEventListener("click", () => {
      $$("#filters button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      CURRENT_FILTER = b.dataset.filter;
      refresh();
    }));
    refreshPatStatus();
    wireEnrichAllButton();
    try {
      [ALL_OPEN, LOG_ROWS] = await Promise.all([fetchIssues("open"), fetchLog()]);
      await fetchAndAttachMetas(ALL_OPEN);
      refresh();
      // notify overview that triggers are ready
      window.dispatchEvent(new CustomEvent("triggers-loaded"));
    } catch (e) {
      $("#cards").innerHTML = `<p class="loading">Failed to load: ${esc(e.message)}</p>`;
    }
    setInterval(async () => {
      try { LOG_ROWS = await fetchLog(); refresh(); window.dispatchEvent(new CustomEvent("triggers-loaded")); } catch {}
    }, 20000);
    setInterval(async () => {
      try { ALL_OPEN = await fetchIssues("open"); await fetchAndAttachMetas(ALL_OPEN); refresh(); window.dispatchEvent(new CustomEvent("triggers-loaded")); } catch {}
    }, 60000);
  }

  window.K = window.K || {};
  window.K.Triggers = {
    init,
    promptForPat,
    refreshPatStatus,
    getOpenIssues: () => ALL_OPEN,
    getLogRows: () => LOG_ROWS,
    parseMeta,
    esc,
  };
})();
