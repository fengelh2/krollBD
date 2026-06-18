// events.js — Events tab. Reads data/events.csv via the same GitHub Contents
// API pattern as the other CSVs. Filters: host, topic, time window, free-text.

(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const STATE = {
    rows: [],
    filters: { search: "", host: "", topic: "", window: "future", mode: "in_person" },
  };

  function withinWindow(dateStr, window) {
    if (!dateStr) return window === "all";
    const d = new Date(dateStr);
    if (isNaN(d)) return window === "all";
    const now = new Date();
    now.setHours(0, 0, 0, 0);
    if (window === "all") return true;
    if (window === "future") return d >= now;
    const days = window === "week" ? 7 : 30;
    const cutoff = new Date(now);
    cutoff.setDate(cutoff.getDate() + days);
    return d >= now && d <= cutoff;
  }

  function syncEventsHash() {
    const f = STATE.filters;
    const parts = [];
    for (const k of ["search", "host", "topic", "window", "mode"]) {
      // skip defaults so the URL stays clean
      const defaultVal = (k === "window") ? "future" : (k === "mode") ? "in_person" : "";
      if (f[k] && f[k] !== defaultVal) parts.push(`${k}=${encodeURIComponent(f[k])}`);
    }
    const newHash = "#/events" + (parts.length ? "?" + parts.join("&") : "");
    if (window.location.hash !== newHash) {
      history.replaceState(null, "", newHash);
    }
  }

  function renderTable() {
    const rows = STATE.rows.filter(r => {
      const f = STATE.filters;
      if (f.host && r.host !== f.host) return false;
      if (f.topic && !((r.topic || "").includes(f.topic))) return false;
      if (!withinWindow(r.date_start, f.window)) return false;
      const isVirtual = (r.is_virtual || "").toString().toLowerCase() === "true";
      if (f.mode === "in_person" && isVirtual) return false;
      if (f.mode === "virtual" && !isVirtual) return false;
      if (f.search) {
        const s = f.search.toLowerCase();
        const hay = (r.title + " " + r.speakers + " " + r.venue + " " + r.host).toLowerCase();
        if (!hay.includes(s)) return false;
      }
      return true;
    });
    // Sort by date ascending (future first), undated last
    rows.sort((a, b) => {
      const ad = a.date_start || "9999";
      const bd = b.date_start || "9999";
      return ad < bd ? -1 : ad > bd ? 1 : 0;
    });
    $("#events-count").textContent = `— ${rows.length.toLocaleString()} matching · ${STATE.rows.length.toLocaleString()} total`;
    // "shouldn't miss" = informal networking event (luncheon / breakfast /
    // cocktails / drinks / networking / AGM). These are where Felix actually
    // builds the relationships that drive referrals.
    const mustSeeRe = /\b(luncheon|brownbag|breakfast|cocktails?|drinks|networking|agm|reception)\b/i;

    // Topic relevance: title mentions PE, private credit/debt, or related
    // alts wording. Case-insensitive for full words; abbreviations require
    // uppercase to dodge false positives (e.g. 'lp' in 'completely').
    const peTopicRe = /\b(private[\s\-]?(equity|credit|debt)|venture[\s\-]?capital|buyout|leveraged[\s\-]?buyout|LBO|secondaries|direct[\s\-]?lending|fund[\s\-]?of[\s\-]?funds|carried[\s\-]?interest|alternative[\s\-]?investments?|alts?)\b/i;
    const peAbbrevRe = /\b(PE|VC|PD|PC|GP|LP)\b/;   // case-sensitive: uppercase only
    const head = `
      <thead><tr>
        <th>Date</th><th>Title</th><th>Host</th><th>Topic</th>
        <th>Venue / City</th><th>Link</th>
      </tr></thead>`;
    const body = "<tbody>" + rows.map(r => {
      const isVirtual = (r.is_virtual || "").toString().toLowerCase() === "true";
      const cityChip = isVirtual ? "<span class='evt-virtual'>virtual</span>" : esc(r.city || "");
      const linkHtml = r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">register &rarr;</a>` : "";
      const title = r.title || "";
      const mustSee = mustSeeRe.test(title);
      const isPETopic = peTopicRe.test(title) || peAbbrevRe.test(title);
      const star = mustSee ? `<span class="evt-must-see" title="High-value networking event">★</span> ` : "";
      const peTag = isPETopic ? ` <span class="evt-pe-tag" title="PE / private credit / alts topic">PE/PD</span>` : "";
      const rowClasses = [];
      if (mustSee) rowClasses.push("evt-row-must-see");
      if (isPETopic) rowClasses.push("evt-row-pe");
      const trClass = rowClasses.length ? ` class="${rowClasses.join(" ")}"` : "";
      return `<tr${trClass}>
        <td class="evt-date"><code>${esc(r.date_start || "—")}</code>${r.date_end ? `<br><small>&rarr; ${esc(r.date_end)}</small>` : ""}${r.time ? `<br><small class="muted-text">${esc(r.time)}</small>` : ""}</td>
        <td>${star}<strong>${esc(title)}</strong>${peTag}${r.audience ? `<br><small class="muted-text">${esc(r.audience)}</small>` : ""}</td>
        <td><span class="evt-host">${esc(r.host)}</span></td>
        <td>${esc(r.topic || "")}</td>
        <td>${esc(r.venue || "")}${r.venue && (r.city || isVirtual) ? "<br>" : ""}<small>${cityChip}</small></td>
        <td>${linkHtml}</td>
      </tr>`;
    }).join("") + "</tbody>";
    $("#events-table").innerHTML = head + body;
  }

  function applyHashParams() {
    const h = window.location.hash || "";
    const q = (h.split("?")[1] || "").split("&");
    for (const p of q) {
      const [k, v] = p.split("=");
      if (!k) continue;
      const val = decodeURIComponent(v || "");
      if (k in STATE.filters) {
        STATE.filters[k] = val;
        const el = document.getElementById(`events-${k}`);
        if (el) el.value = val;
      }
    }
  }

  function wireFilters() {
    const onChange = (key) => (e) => {
      STATE.filters[key] = e.target.value;
      renderTable();
      syncEventsHash();
    };
    $("#events-search").addEventListener("input", onChange("search"));
    $("#events-host").addEventListener("change", onChange("host"));
    $("#events-topic").addEventListener("change", onChange("topic"));
    $("#events-window").addEventListener("change", onChange("window"));
    $("#events-mode").addEventListener("change", onChange("mode"));
  }

  function populateFilterOptions() {
    const hosts = new Set(STATE.rows.map(r => r.host).filter(Boolean));
    const topics = new Set();
    for (const r of STATE.rows) {
      for (const t of (r.topic || "").split(/[+,]/)) {
        const tt = t.trim();
        if (tt) topics.add(tt);
      }
    }
    const hostSel = $("#events-host");
    [...hosts].sort().forEach(h => {
      const opt = document.createElement("option");
      opt.value = h; opt.textContent = h;
      hostSel.appendChild(opt);
    });
    const topicSel = $("#events-topic");
    [...topics].sort().forEach(t => {
      const opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      topicSel.appendChild(opt);
    });
  }

  let _wired = false;
  async function show() {
    if (!STATE.rows.length) {
      try {
        const text = await K.fetchCsvText("data/events.csv");
        const parsed = K.parseCsv(text);
        STATE.rows = parsed.rows;
      } catch (e) {
        $("#events-table").innerHTML = `<tbody><tr><td><p class="loading">No events file yet, or fetch failed: ${esc(e.message)}</p></td></tr></tbody>`;
        return;
      }
      populateFilterOptions();
    }
    if (!_wired) {
      wireFilters();
      _wired = true;
    }
    applyHashParams();
    renderTable();
  }

  // Nav badge: red dot with count when one or more starred (must-see)
  // events fall within the next 7 days. Same regex as the row highlight.
  async function updateNavBadge() {
    const badge = document.getElementById("nav-events-badge");
    if (!badge) return;
    try {
      if (!STATE.rows.length) {
        const text = await K.fetchCsvText("data/events.csv");
        STATE.rows = K.parseCsv(text).rows;
      }
    } catch {
      badge.hidden = true;
      return;
    }
    const mustSeeRe = /\b(luncheon|brownbag|breakfast|cocktails?|drinks|networking|agm|reception)\b/i;
    const now = new Date();
    now.setHours(0, 0, 0, 0);
    const cutoff = new Date(now);
    cutoff.setDate(cutoff.getDate() + 7);
    const soon = STATE.rows.filter(r => {
      if (!mustSeeRe.test(r.title || "")) return false;
      const d = r.date_start ? new Date(r.date_start) : null;
      return d && !isNaN(d) && d >= now && d <= cutoff;
    });
    if (soon.length) {
      badge.hidden = false;
      badge.textContent = String(soon.length);
      badge.classList.add("nav-badge-overdue");
      const titles = soon.slice(0, 3).map(r => `${r.date_start} ${r.title.slice(0,60)}`).join(" · ");
      const more = soon.length > 3 ? ` (+${soon.length - 3} more)` : "";
      badge.setAttribute("title", `${soon.length} starred event${soon.length === 1 ? "" : "s"} in next 7 days: ${titles}${more}`);
    } else {
      badge.hidden = true;
    }
  }

  window.K = window.K || {};
  window.K.Events = { show, updateNavBadge };
})();
