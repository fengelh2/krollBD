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
    const lines = text.split(/\r?\n/).filter(Boolean);
    if (lines.length < 2) return [];
    const hdr = splitCsvLine(lines[0]);
    return lines.slice(1).map(ln => {
      const vals = splitCsvLine(ln);
      const o = {};
      hdr.forEach((h, i) => o[h] = vals[i] || "");
      return o;
    });
  }
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
    const byWeek = {};
    for (const w of weeks) byWeek[w] = { C1: 0, C2: 0 };
    for (const r of LOG_ROWS) {
      if (!r.sent_at_utc) continue;
      const wk = isoWeekKey(new Date(r.sent_at_utc));
      if (byWeek[wk]) byWeek[wk][r.trigger_type] = (byWeek[wk][r.trigger_type] || 0) + 1;
    }
    const c1 = weeks.map(w => byWeek[w].C1);
    const c2 = weeks.map(w => byWeek[w].C2);
    const max = Math.max(1, ...c1, ...c2);
    const W = 600, H = 100, PAD = 6;
    const xStep = (W - PAD * 2) / Math.max(1, weeks.length - 1);
    const y = (v) => H - PAD - ((H - PAD * 2) * v / max);
    const line = (arr) => arr.map((v, i) => `${PAD + i * xStep},${y(v)}`).join(" ");
    $("#chart").innerHTML = `
      <line x1="0" y1="${H - PAD}" x2="${W}" y2="${H - PAD}" stroke="#e4e6ea" stroke-width="1"/>
      <polyline fill="none" stroke="#1a3554" stroke-width="1.6" points="${line(c1)}"/>
      <polyline fill="none" stroke="#9b1d23" stroke-width="1.6" points="${line(c2)}"/>
      ${c1.map((v,i) => v ? `<circle cx="${PAD+i*xStep}" cy="${y(v)}" r="2.5" fill="#1a3554"/>` : "").join("")}
      ${c2.map((v,i) => v ? `<circle cx="${PAD+i*xStep}" cy="${y(v)}" r="2.5" fill="#9b1d23"/>` : "").join("")}
      <text x="${PAD}" y="${H-1}" font-size="8" fill="#7a818b" font-family="Inter,sans-serif">${weeks[0]}</text>
      <text x="${W-PAD-38}" y="${H-1}" font-size="8" fill="#7a818b" font-family="Inter,sans-serif">${weeks[weeks.length-1]}</text>
      <text x="${PAD}" y="10" font-size="8" fill="#7a818b" font-family="Inter,sans-serif">max ${max}</text>
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
      ${(meta.email_candidates && meta.email_candidates.length) ? `
        <div class="candidates">
          <div class="cand-head"><span>Email candidates · ordered best→worst · <em>verify before sending</em></span></div>
          ${meta.email_candidates.map(c => {
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
            if (c.kind === "hunter_io" && (conf === "hunter_verified" || conf === "high")) {
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
      ` : ""}
      <div class="actions">
        ${opts.pending
          ? `<span class="muted-text">Logging… (Action running, refresh in ~30s)</span>`
          : `<button class="btn primary" data-action="reached-out">Mark as reached out</button>`}
      </div>
    `;
    card.querySelector('[data-copy="subject"]').addEventListener("click", e =>
      copyToClipboard(meta.email_subject || "", e.target));
    card.querySelector('[data-copy="body"]').addEventListener("click", e =>
      copyToClipboard(meta.email_body || "", e.target));
    card.querySelectorAll('[data-copy-cand]').forEach(b => b.addEventListener("click", e => {
      copyToClipboard(e.target.dataset.copyCand, e.target);
    }));
    const btn = card.querySelector('[data-action="reached-out"]');
    if (btn) btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Sending…";
      const ok = await dispatchOutreach(issue, meta);
      if (ok) { addPending(issue.number); refresh(); }
      else { btn.disabled = false; btn.textContent = "Mark as reached out"; }
    });
    return card;
  }

  function refresh() {
    if (!$("#cards")) return;
    const pending = getPending();
    const loggedIssueNums = new Set(LOG_ROWS.map(r => String(r.issue_number)));
    for (const p of Array.from(pending)) if (loggedIssueNums.has(p)) removePending(p);
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
        visible = visible.filter(i => { const m = parseMeta(i); return m && m.type === CURRENT_FILTER; });
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
    $$("#filters button").forEach(b => b.addEventListener("click", () => {
      $$("#filters button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      CURRENT_FILTER = b.dataset.filter;
      refresh();
    }));
    refreshPatStatus();
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
