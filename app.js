// app.js — router shell. Loads after data.js, triggers.js, overview.js, tables.js.
//
// Routing model: hash-based. Routes:
//   #/overview                → Overview homepage (new)
//   #/corps[?wa=…&illiq=…&email=…&aum=…&focus=CEREF]
//   #/individuals
//   #/pairs
//   #/triggers                → Legacy trigger-card view (preserved)
//
// Data layer: see data.js — fetches the four CSVs via the GitHub Contents API
// with PAT auth (same pattern as triggers.js fetchMetaFile). The CSVs MUST be
// committed under `data/` in this repo by weekly.yml. See DESIGN_NOTES.md
// for the README change Felix needs to push.

(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const PAT_KEY = "krollbd_pat";
  function refreshPatStatus() {
    const has = !!localStorage.getItem(PAT_KEY);
    const el = $("#pat-status");
    el.textContent = has ? "✓ token set" : "no token";
    el.className = has ? "pat-status pat-ok" : "pat-status pat-missing";
  }
  function promptForPat() {
    const backdrop = $("#modal-backdrop");
    const body = $("#modal-body");
    body.innerHTML = `
      <h2 style="margin-top:0">GitHub Personal Access Token</h2>
      <p>Scope needed: <code>repo</code> (the triggers repo is private and also hosts the data CSVs).</p>
      <p>Stored only in this browser's localStorage.</p>
      <input type="password" id="pat-input" placeholder="ghp_..." style="width:100%;padding:8px;font-family:monospace;font-size:14px;margin:8px 0" />
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
        <button class="btn" id="pat-clear">Clear stored token</button>
        <button class="btn" id="pat-cancel">Cancel</button>
        <button class="btn" id="pat-save" style="background:#1f6feb;color:#fff">Save</button>
      </div>
    `;
    backdrop.hidden = false;
    setTimeout(() => $("#pat-input").focus(), 50);
    const close = () => { backdrop.hidden = true; };
    $("#pat-cancel").addEventListener("click", close);
    $("#pat-clear").addEventListener("click", () => {
      localStorage.removeItem(PAT_KEY); refreshPatStatus(); close(); location.reload();
    });
    $("#pat-save").addEventListener("click", () => {
      const t = $("#pat-input").value.trim();
      if (t) { localStorage.setItem(PAT_KEY, t); refreshPatStatus(); close(); location.reload(); }
      else { close(); }
    });
    $("#pat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#pat-save").click();
      if (e.key === "Escape") close();
    });
  }
  $("#pat-btn").addEventListener("click", promptForPat);
  refreshPatStatus();

  // Expose so triggers.js (and any other module) can call the shared modal
  // instead of native window.prompt().
  window.K = window.K || {};
  window.K.promptForPat = promptForPat;

  // ---- route ----
  const ROUTES = ["overview", "corps", "individuals", "pairs", "events", "triggers"];

  function currentRoute() {
    const h = (window.location.hash || "#/overview").replace(/^#\//, "");
    const tab = h.split("?")[0] || "overview";
    return ROUTES.includes(tab) ? tab : "overview";
  }

  function showView(tab) {
    ROUTES.forEach(r => {
      const el = document.getElementById(`view-${r}`);
      if (el) el.hidden = (r !== tab);
    });
    $$("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === tab));
  }

  async function route() {
    const tab = currentRoute();
    showView(tab);
    if (tab === "overview") {
      K.Overview.render();
    } else if (tab === "corps") {
      K.Tables.showCorps();
    } else if (tab === "individuals") {
      K.Tables.showInd();
    } else if (tab === "pairs") {
      K.Tables.showPairs();
    } else if (tab === "events") {
      K.Events.show();
    } else if (tab === "triggers") {
      K.Triggers.init();  // idempotent
    }
  }

  window.addEventListener("hashchange", route);
  if (!window.location.hash) window.location.hash = "#/overview";

  // Kick triggers load eagerly so the nav badge can populate.
  K.Triggers.init();
  // Kick events load eagerly so the Events nav badge can light up red
  // when a must-see (luncheon / breakfast / cocktails / AGM) is in the
  // next 7 days, even before the user visits the tab.
  if (K.Events && K.Events.updateNavBadge) K.Events.updateNavBadge();

  route();
})();
