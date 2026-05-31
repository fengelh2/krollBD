// overview.js — Overview view as BD command center.
// Narrative order, top to bottom:
//   1. This-week action panel  — what needs my attention NOW
//   2. BD bullseye funnel      — 720 → 637 → 201 → 67, drop-offs are unlocks
//   3. Improvement queue       — concrete next moves to widen the funnel
//   4. Database strength       — context: 1 chart + mini coverage funnel
//   5. Recent triggers         — top 10 open, age-sorted, SLA-colored

(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

  const SLA_BUSINESS_DAYS = 5;

  function businessDaysBetween(d1, d2) {
    if (!(d1 instanceof Date) || !(d2 instanceof Date)) return 0;
    let n = 0;
    const step = d1 < d2 ? 1 : -1;
    const cur = new Date(d1);
    while (cur.getTime() !== d2.getTime() && Math.abs(n) < 365) {
      cur.setDate(cur.getDate() + step);
      const day = cur.getDay();
      if (day !== 0 && day !== 6) n += step;
    }
    return Math.abs(n);
  }

  // ---- subsets used across panels (computed once) ----
  function buildSets(data) {
    const C = data.classification.rows;
    const corpsAll = data.corps.rows;
    const corps = corpsAll.filter(r => (r.has_active_licence || "").trim() === "Y");
    const corpsRetired = corpsAll.length - corps.length;

    const bd = C.filter(K.isBdRelevant);
    const bdSite = bd.filter(r => ["verified","probable"].includes(K.waBucket(r)));
    // "+ email captured" stage = ANY contact (verified or inferred-pattern).
    // Breakdown is shown on the funnel box so the confidence tier is visible.
    const bdContact = bdSite.filter(K.hasAnyContact);
    const bdEmailVerified = bdSite.filter(K.hasEmail);
    const bdEmailInferredOnly = bdSite.filter(r => !K.hasEmail(r) && K.hasInferredEmail(r));
    const bdAum = bdContact.filter(K.hasAum);

    const allSite = C.filter(r => ["verified","probable"].includes(K.waBucket(r)));
    const allContact = allSite.filter(K.hasAnyContact);
    const allEmailVerified = allSite.filter(K.hasEmail);
    const allEmailInferredOnly = allSite.filter(r => !K.hasEmail(r) && K.hasInferredEmail(r));
    const allAum = allContact.filter(K.hasAum);

    return {
      C, corps, corpsRetired,
      bd, bdSite,
      // bdEmail kept under the same name for back-compat; now means ANY contact.
      bdEmail: bdContact, bdEmailVerified, bdEmailInferredOnly, bdAum,
      allSite,
      allEmail: allContact, allEmailVerified, allEmailInferredOnly, allAum,
    };
  }

  // =====================================================================
  // BD BULLSEYE FUNNEL
  // =====================================================================
  function renderBdFunnel(sets) {
    // Narrative connector: link BD-relevant cohort back to the parent
    // 2,814 active corps shown in Database Strength.
    const narrative = $("#ov-funnel-narrative");
    if (narrative) {
      const totalCorps = sets.corps.length;
      const bdCount = sets.bd.length;
      const pct = totalCorps ? Math.round(100 * bdCount / totalCorps) : 0;
      narrative.innerHTML = `Starting from <a href="#db-strength"><span class="anchor-num">${totalCorps.toLocaleString()}</span> active T9-licensed corps</a>, narrowing to the <strong>${bdCount.toLocaleString()}</strong> (${pct}%) with illiquid or mixed books — the firms Kroll PortVal can credibly engage. <a href="#db-strength" class="narrative-link">see Database strength &darr;</a>`;
    }

    const stages = [
      { n: sets.bd.length,      l: "BD-relevant",       d: "book type = illiquids or mixed", href: "#/corps?illiq=bd" },
      { n: sets.bdSite.length,  l: "+ website found",   d: "verified or probable",            href: "#/corps?illiq=bd&wa=site" },
      { n: sets.bdEmail.length, l: "+ contact captured",
        d: `${sets.bdEmailVerified.length} verified · ${sets.bdEmailInferredOnly.length} inferred from pattern`,
        href: "#/corps?illiq=bd&wa=site&email=yes" },
      { n: sets.bdAum.length,   l: "+ AUM disclosed",   d: "any AUM extracted",               href: "#/corps?illiq=bd&wa=site&email=yes&aum=yes" },
    ];
    const base = stages[0].n || 1;

    // ---- SVG funnel: alternating rectangles + sloped trapezoids ----
    // The trapezoids ARE the drop-offs (you can see the cliff), no separate
    // text divider needed. Drops annotated inside the trapezoid in muted text.
    const W = 720, BAR_H = 38, TRANS_H = 26;
    const totalH = 4 * BAR_H + 3 * TRANS_H;
    const PAL = ["#1f2937", "#374151", "#4b5563", "#6b7280"];   // charcoal → gray

    const widthFor = n => Math.max(80, W * (n / base));   // floor at 80px so tiny stages still readable
    const bars = stages.map((s, i) => {
      const w = widthFor(s.n);
      const x = (W - w) / 2;
      const y = i * (BAR_H + TRANS_H);
      return { ...s, w, x, y, fill: PAL[i] };
    });

    // Cap bar max width so external labels always fit. Reserve ~200px on the
    // right for the small stage label that sits OUTSIDE the bar.
    const RIGHT_LABEL_PAD = 210;
    const inWidth = W - RIGHT_LABEL_PAD - 20;
    const widthFor2 = n => Math.max(60, inWidth * (n / base));
    bars.forEach((b, i) => {
      const w = widthFor2(b.n);
      b.w = w;
      b.x = 10; // left-align bars (not centered) so the right label rail is consistent
    });

    let svg = `<svg viewBox="0 0 ${W} ${totalH}" preserveAspectRatio="xMidYMid meet" style="display:block;width:100%;max-width:760px;margin:0 auto;height:auto;aspect-ratio:${W}/${totalH}">`;

    // Transitions (drop-off trapezoids) between bars
    bars.forEach((b, i) => {
      if (i === 0) return;
      const prev = bars[i-1];
      const topY = prev.y + BAR_H;
      const botY = b.y;
      const topL = prev.x, topR = prev.x + prev.w;
      const botL = b.x, botR = b.x + b.w;
      const dropN = prev.n - b.n;
      const dropPct = prev.n > 0 ? Math.round(100 * dropN / prev.n) : 0;
      svg += `<polygon points="${topL},${topY} ${topR},${topY} ${botR},${botY} ${botL},${botY}" fill="#e5e7eb" opacity="0.55"/>`;
      svg += `<text x="${(topL+topR)/2}" y="${(topY+botY)/2 + 4}" font-size="11" text-anchor="middle" fill="#374151" font-family="Inter,sans-serif">↓ ${dropN.toLocaleString()} dropped (${dropPct}%)</text>`;
    });

    // Bars + side labels
    bars.forEach((b, i) => {
      const pct = Math.round(100 * b.n / base);
      const cy = b.y + BAR_H/2 + 5;
      svg += `<a href="${b.href}">`;
      svg += `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${BAR_H}" fill="${b.fill}" rx="2" ry="2"/>`;
      // Number on left, % on right, inside the bar
      svg += `<text x="${b.x + 14}" y="${cy}" font-size="18" font-weight="600" fill="#fff" font-family="Inter,sans-serif">${b.n.toLocaleString()}</text>`;
      svg += `<text x="${b.x + b.w - 12}" y="${cy}" font-size="11" text-anchor="end" fill="#fff" opacity="0.7" font-family="Inter,sans-serif">${pct}%</text>`;
      svg += `</a>`;
      // Stage label sits to the right of the bar in the dedicated rail.
      svg += `<text x="${b.x + b.w + 12}" y="${cy - 5}" font-size="13" font-weight="500" fill="#1f2937" font-family="Inter,sans-serif">${esc(b.l)}</text>`;
      svg += `<text x="${b.x + b.w + 12}" y="${cy + 11}" font-size="11" fill="#6b7280" font-family="Inter,sans-serif">${esc(b.d)}</text>`;
    });

    svg += `</svg>`;
    $("#ov-funnel").innerHTML = svg;

    // Biggest-unlock callout hidden per user request 2026-05-29 — the same
    // info is already in the Unlocks queue at the bottom of the page.
    const callout = $("#ov-funnel-callout");
    if (callout) { callout.innerHTML = ""; callout.hidden = true; }
  }

  // =====================================================================
  // 3. IMPROVEMENT QUEUE — reframed as unlocks
  // =====================================================================
  function renderImproveQueue(data, sets) {
    const bd = sets.bd;

    // ---- Hunter cohort: BD + verified/probable site + no NAMED email + no
    // inferred pattern + has website. Excluding inferred-email firms because
    // they already have a candidate — Hunter would only verify, not discover,
    // and Hunter's quota is too tight to burn on verification.
    const hunterAll = bd.filter(r =>
      ["verified", "probable"].includes(K.waBucket(r))
      && !K.hasEmail(r)
      && !K.hasInferredEmail(r)
      && K.hasWebsite(r));
    const hunterZero = hunterAll.filter(r => !K.hasGenericEmail(r));
    const hunterGenOnly = hunterAll.filter(r => K.hasGenericEmail(r));

    const noSite = bd.filter(r => !K.hasWebsite(r) || K.waBucket(r) === "not_found");

    // ---- Thin cohort: BD + suspect/unverified site. Sub-split shows how many
    // already gained ANY email from a prior deep-scrape.
    const thin = bd.filter(r => ["suspect", "unverified"].includes(K.waBucket(r)));
    const thinWithAnyEmail = thin.filter(r => K.hasEmail(r) || K.hasGenericEmail(r));
    const thinStillBare = thin.filter(r => !K.hasEmail(r) && !K.hasGenericEmail(r));

    // ---- AUM
    const noAum = bd.filter(r => !K.hasAum(r));

    // Rank: AUM-disclosed first (so non-misleading top 5), then alpha
    const rank = (rows) => rows.slice().sort((a, b) => {
      const aA = parseFloat(a.aum_usd_m) || 0;
      const bA = parseFloat(b.aum_usd_m) || 0;
      if (bA !== aA) return bA - aA;
      return (a.name_en || "").localeCompare(b.name_en || "");
    });

    const queues = [
      {
        id: "hunter", title: "Named-email lookup — Hunter.io",
        unlock: hunterAll.length, unlockLabel: "still need a named contact",
        desc: "BD-relevant · verified/probable site · no named email captured",
        breakdown: `<strong>${hunterZero.length.toLocaleString()}</strong> have no email at all · <strong>${hunterGenOnly.length.toLocaleString()}</strong> have a generic-only inbox already (info@ / contact@ / etc.)`,
        action: "Run Hunter.io /email-finder for the primary RO (50/mo free; cache-aware)",
        impact: "high", rows: rank(hunterAll),
        href: "#/corps?illiq=bd&wa=site&email=no",
      },
      {
        id: "no-site", title: "Website discovery — SerpAPI rerun",
        unlock: noSite.length, unlockLabel: "would gain a website",
        desc: "BD-relevant · no website found at all",
        action: "Add `website_overrides.csv` row or rerun classifier",
        impact: "high", rows: rank(noSite),
        href: "#/corps?illiq=bd&wa=not_found",
      },
      {
        id: "thin", title: "Suspect/unverified site — re-verify",
        unlock: thin.length, unlockLabel: "have a shaky website match",
        desc: "BD-relevant · website_accuracy flagged suspect or unverified",
        breakdown: `<strong>${thinWithAnyEmail.length.toLocaleString()}</strong> already have at least one email captured (from prior deep-scrape) · <strong>${thinStillBare.length.toLocaleString()}</strong> still have nothing — these are the highest-leverage to re-scrape`,
        action: "Deep-scrape /contact + /team, then re-classify with the new content",
        impact: "medium", rows: rank(thin),
        href: "#/corps?illiq=bd&wa=suspect",
      },
      {
        id: "no-aum", title: "AUM lookup — Form ADV / press",
        unlock: noAum.length, unlockLabel: "would gain a sizing signal",
        desc: "BD-relevant · no AUM extracted",
        action: "Hand-check Form ADV / press releases / 13F filings",
        impact: "low", rows: rank(noAum),
        href: "#/corps?illiq=bd&aum=no",
      },
    ];

    $("#ov-improve").innerHTML = queues.map(q => {
      const aumDisclosed = q.rows.slice(0, 5).filter(r => r.aum_usd_m).length;
      const sumLabel = aumDisclosed === 0
        ? "Top 5 (alphabetical — none have AUM disclosed)"
        : aumDisclosed === 5
          ? "Top 5 (by AUM)"
          : "Top 5 (AUM-disclosed first · ties alphabetical)";
      return `
      <div class="improve-card impact-${q.impact}">
        <div class="improve-head">
          <h3><a href="${q.href}">${esc(q.title)}</a></h3>
          <span class="improve-count">+${q.unlock.toLocaleString()}</span>
        </div>
        <p class="improve-desc">${esc(q.desc)}</p>
        <p class="improve-unlock"><strong>${q.unlock.toLocaleString()}</strong> firms ${esc(q.unlockLabel)} · <a href="${q.href}">see all</a></p>
        <p class="improve-action"><span class="lbl">Action:</span> ${esc(q.action)}</p>
        <details class="improve-details">
          <summary>${esc(sumLabel)}</summary>
          <ol class="improve-list">
            ${q.rows.slice(0, 5).map(r => `
              <li>
                <a href="#/corps?focus=${esc(r.ceref)}">${esc(r.name_en)}</a>
                <span class="meta">CE ${esc(r.ceref)}${r.aum_usd_m ? ` · $${esc(r.aum_usd_m)}m` : " · AUM n/a"}${r.parent_org ? ` · ${esc(r.parent_org)}` : ""}</span>
              </li>
            `).join("") || "<li class='muted-text'>(none)</li>"}
          </ol>
        </details>
      </div>
    `;
    }).join("");
  }

  // =====================================================================
  // 4. DATABASE STRENGTH — WA chart + mini coverage funnel
  // =====================================================================
  function renderWaChart(data) {
    const C = data.classification.rows;
    const tiers = ["high", "medium", "low", ""];
    const tierLabels = { "high": "illiquids", "medium": "mixed", "low": "liquids only", "": "unclassified" };
    const buckets = ["verified", "probable", "unverified", "suspect", "not_found", "unknown"];
    // Corporate monochrome: dark navy → light gray, plus a single warm accent
    // for not_found (which is the meaningful "gap" the eye should pick up).
    const colors = {
      verified: "#1f2937",   // charcoal
      probable: "#4b5563",   // dark gray
      unverified: "#9ca3af", // mid gray
      suspect: "#d1d5db",    // light gray
      not_found: "#c79b9b",  // dusty rose — distinguishable from grays without shouting
      unknown: "#e5e7eb",    // very light gray
    };
    const counts = {};
    for (const t of tiers) {
      counts[t] = {}; for (const b of buckets) counts[t][b] = 0;
    }
    for (const r of C) {
      const t = String(r.illiquid_book_likelihood || "").toLowerCase();
      const tier = tiers.includes(t) ? t : "";
      const b = K.waBucket(r);
      const bucket = buckets.includes(b) ? b : "unknown";
      counts[tier][bucket]++;
    }
    const totals = tiers.map(t => buckets.reduce((s, b) => s + counts[t][b], 0));
    const maxTotal = Math.max(1, ...totals);

    const W = 600, H = 220, PAD_L = 90, PAD_R = 190, PAD_T = 10, PAD_B = 36;
    const barH = (H - PAD_T - PAD_B) / tiers.length - 8;
    const xMax = W - PAD_L - PAD_R;

    // BD-relevant bracket: illiquids (row 0) + mixed (row 1) together = the
    // BD bullseye baseline (694 firms). Draw a thin red bracket on the LEFT
    // spanning these two rows so the eye groups them.
    const rowTop = (i) => PAD_T + i * ((H - PAD_T - PAD_B) / tiers.length);
    const bdN = totals[0] + totals[1];
    const bdTopY = rowTop(0);
    const bdBotY = rowTop(1) + barH;
    let svg = "";
    // Two right-side brackets:
    //   - Inner: illiquids + mixed = BD-relevant (rows 0+1)
    //   - Outer: all four tiers = total classified (matches the 2,814 anchor)
    const brX1 = PAD_L + xMax + 18;
    const brX2 = brX1 + 8;
    const brX3 = brX2 + 56;   // outer bracket sits further right
    const brX4 = brX3 + 8;
    const brYM = (bdTopY + bdBotY) / 2;
    const BR_COLOR = "#1f2937";

    // Inner bracket — BD-relevant
    svg += `<path d="M ${brX1} ${bdTopY} L ${brX2} ${bdTopY} L ${brX2} ${bdBotY} L ${brX1} ${bdBotY}" stroke="${BR_COLOR}" stroke-width="1.5" fill="none"/>`;
    svg += `<text x="${brX2 + 5}" y="${brYM - 3}" font-size="11" fill="${BR_COLOR}" font-weight="700" font-family="Inter,sans-serif">${bdN.toLocaleString()}</text>`;
    svg += `<text x="${brX2 + 5}" y="${brYM + 9}" font-size="9" fill="${BR_COLOR}" font-family="Inter,sans-serif">BD-relevant</text>`;

    // Outer bracket — total classified (anchor: 2,814)
    const totalN = C.length;
    const allTopY = rowTop(0);
    const allBotY = rowTop(tiers.length - 1) + barH;
    const allYM = (allTopY + allBotY) / 2;
    svg += `<path d="M ${brX3} ${allTopY} L ${brX4} ${allTopY} L ${brX4} ${allBotY} L ${brX3} ${allBotY}" stroke="#991b1b" stroke-width="1.5" fill="none"/>`;
    svg += `<text x="${brX4 + 5}" y="${allYM - 3}" font-size="11" fill="#991b1b" font-weight="700" font-family="Inter,sans-serif">${totalN.toLocaleString()}</text>`;
    svg += `<text x="${brX4 + 5}" y="${allYM + 9}" font-size="9" fill="#991b1b" font-family="Inter,sans-serif">total classified</text>`;

    tiers.forEach((t, i) => {
      const yTop = PAD_T + i * ((H - PAD_T - PAD_B) / tiers.length);
      svg += `<text x="${PAD_L - 6}" y="${yTop + barH/2 + 4}" font-size="11" text-anchor="end" fill="#3b424a" font-family="Inter,sans-serif">${tierLabels[t]}</text>`;
      let x = PAD_L;
      const total = totals[i];
      for (const b of buckets) {
        const v = counts[t][b];
        if (!v) continue;
        const w = xMax * (v / maxTotal);
        svg += `<rect x="${x}" y="${yTop}" width="${w}" height="${barH}" fill="${colors[b]}" opacity="0.9">
          <title>${tierLabels[t]} · ${b}: ${v} (${total ? Math.round(100*v/total) : 0}% of ${tierLabels[t]})</title></rect>`;
        if (w > 24) svg += `<text x="${x + w/2}" y="${yTop + barH/2 + 4}" font-size="10" text-anchor="middle" fill="#fff" font-family="Inter,sans-serif">${v}</text>`;
        x += w;
      }
      svg += `<text x="${x + 4}" y="${yTop + barH/2 + 4}" font-size="10" fill="#7a818b" font-family="Inter,sans-serif">n=${total}</text>`;
    });
    svg += `<line x1="${PAD_L}" y1="${H - PAD_B + 8}" x2="${W - PAD_R}" y2="${H - PAD_B + 8}" stroke="#e4e6ea"/>`;
    svg += `<text x="${PAD_L}" y="${H - 12}" font-size="10" fill="#7a818b" font-family="Inter,sans-serif">bar width = count</text>`;
    $("#ov-wa-chart").innerHTML = svg;
    $("#ov-wa-legend").innerHTML = buckets.map(b =>
      `<span class="legend-item"><span class="swatch" style="background:${colors[b]}"></span>${b}</span>`
    ).join("");
  }

  function renderCoverageFunnel(sets) {
    const stages = [
      { n: sets.corps.length, l: "Active T9-licensed corps", retiredSub: `+${sets.corpsRetired.toLocaleString()} retired`, anchor: true },
      { n: sets.allSite.length, l: "Website found" },
      { n: sets.allEmail.length, l: "Contact captured",
        retiredSub: `${sets.allEmailVerified.length.toLocaleString()} verified · ${sets.allEmailInferredOnly.length.toLocaleString()} inferred` },
      { n: sets.allAum.length, l: "AUM disclosed" },
    ];
    const base = stages[0].n || 1;
    $("#ov-coverage").innerHTML = stages.map(s => `
      <div class="cov-step${s.anchor ? " cov-step-anchor" : ""}">
        <div class="cov-n">${s.n.toLocaleString()}</div>
        <div class="cov-l">${esc(s.l)}</div>
        <div class="cov-pct">${Math.round(100 * s.n / base)}% of active</div>
        ${s.retiredSub ? `<div class="cov-sub">${esc(s.retiredSub)}</div>` : ""}
      </div>
    `).join("<div class='cov-arrow'>→</div>");

    // Wire the 2,814 anchor in three spots so it reads as the shared spine
    // of the Database Strength section:
    //   - "Corporate coverage · 2,814 active · 100% baseline" subhead chip
    //   - The thick-bordered first box in the row (already visually anchored)
    //   - Prominent number above the WA chart
    const n = sets.corps.length;
    const fmt = (label) => `· <span class="anchor-num">${n.toLocaleString()}</span> ${label}`;
    const anchor = $("#ov-anchor-corps");
    if (anchor) anchor.innerHTML = fmt("active corps · 100% baseline");
    const chartAnchor = $("#ov-chart-anchor");
    if (chartAnchor) chartAnchor.innerHTML = "";  // 2,814 now shown as a bracket on the chart
  }

  function renderPeopleRow(data, sets) {
    const inds = data.individuals.rows;
    const pairs = data.pairs.rows;
    const C = data.classification.rows;
    const activeInds = inds.filter(r => (r.has_active_licence || "Y") === "Y").length;
    const uniqueRos = new Set(pairs.map(p => p.ro_ceref).filter(Boolean)).size;
    const totalAssignments = pairs.length;

    // RO-level email coverage (two granularities):
    //  - strict: RO whose primary-corp has a Hunter-verified named email
    //    captured (hunter_email column populated)
    //  - broad: RO at a firm with any named email (emails_on_site OR ir_email
    //    OR hunter_email). The firm-level signal projected onto its ROs.
    const firmHunterEmail = new Set();
    const firmAnyNamedEmail = new Set();
    for (const r of C) {
      const hunter = (r.hunter_email || "").trim();
      const onSite = (r.emails_on_site || "").trim();
      const ir = (r.ir_email || "").trim();
      if (hunter) firmHunterEmail.add(r.ceref);
      if (hunter || onSite || ir) firmAnyNamedEmail.add(r.ceref);
    }
    const rosWithHunter = new Set();
    const rosWithAnyNamed = new Set();
    for (const p of pairs) {
      if (firmHunterEmail.has(p.corp_ceref)) rosWithHunter.add(p.ro_ceref);
      if (firmAnyNamedEmail.has(p.corp_ceref)) rosWithAnyNamed.add(p.ro_ceref);
    }

    const multiAffil = totalAssignments - uniqueRos;
    $("#ov-people").innerHTML = `
      <div class="cov-row">
        <div class="cov-step">
          <div class="cov-n">${inds.length.toLocaleString()}</div>
          <div class="cov-l">Individuals on register</div>
          <div class="cov-sub">all-time</div>
        </div>
        <div class="cov-arrow">&rarr;</div>
        <div class="cov-step">
          <div class="cov-n">${activeInds.toLocaleString()}</div>
          <div class="cov-l">Currently active license-holders</div>
        </div>
        <div class="cov-arrow">&rarr;</div>
        <div class="cov-step cov-step-linked">
          <div class="cov-connector-tag">&uarr; employed by the <span class="anchor-num">${sets.corps.length.toLocaleString()}</span> corps above</div>
          <div class="cov-n">${uniqueRos.toLocaleString()}</div>
          <div class="cov-l">Serve as RO on an active T9 corp</div>
          <div class="cov-sub">${totalAssignments.toLocaleString()} assignments &middot; ${multiAffil.toLocaleString()} ROs serve &gt;1 firm</div>
        </div>
        <div class="cov-arrow">&rarr;</div>
        <div class="cov-step">
          <div class="cov-n">${rosWithAnyNamed.size.toLocaleString()}</div>
          <div class="cov-l">At firm with named email</div>
          <div class="cov-sub">of which <strong>${rosWithHunter.size.toLocaleString()}</strong> Hunter-verified</div>
        </div>
      </div>
    `;
  }

  // =====================================================================
  // Nav badge — show count of "new" triggers (created in last 7 days).
  // Lives on the Triggers tab regardless of which view is active.
  // =====================================================================
  function updateNavBadge() {
    const badge = document.getElementById("nav-triggers-badge");
    if (!badge) return;
    const issues = (window.K.Triggers && window.K.Triggers.getOpenIssues()) || [];
    const open = issues.length;
    if (open === 0) {
      badge.hidden = true;
      return;
    }
    // Red if any open trigger has aged past the SLA (5 business days).
    // Otherwise muted gray badge with count.
    const now = new Date();
    let overdue = 0;
    let freshThisWeek = 0;
    const sevenDaysMs = 7 * 24 * 3600 * 1000;
    for (const i of issues) {
      const created = i.created_at ? new Date(i.created_at) : null;
      if (!created) continue;
      if (businessDaysBetween(created, now) > SLA_BUSINESS_DAYS) overdue++;
      if ((now - created) < sevenDaysMs) freshThisWeek++;
    }
    badge.hidden = false;
    badge.textContent = String(open);
    badge.classList.toggle("nav-badge-overdue", overdue > 0);
    const parts = [`${open} open trigger${open === 1 ? "" : "s"}`];
    if (overdue) parts.push(`${overdue} past the 5-day window`);
    if (freshThisWeek) parts.push(`${freshThisWeek} new this week`);
    badge.setAttribute("title", parts.join(" · "));
  }

  // (Recent triggers strip removed from Overview 2026-05-29. The Triggers
  // tab's nav badge carries the equivalent signal — count + red on SLA-overdue.
  // SLA_BUSINESS_DAYS + businessDaysBetween are still used by updateNavBadge.)

  // =====================================================================
  // RENDER
  // =====================================================================
  async function render() {
    try {
      const data = await K.loadAll();
      window.__data = data;
      const sets = buildSets(data);
      renderBdFunnel(sets);
      renderImproveQueue(data, sets);
      renderWaChart(data);
      renderCoverageFunnel(sets);
      renderPeopleRow(data, sets);
    } catch (e) {
      // Consolidated empty-state: blank out every Overview section so the
      // 'Loading…' placeholders don't sit there indefinitely.
      const msg = `<p class="loading">Failed to load: ${esc(e.message)}<br><br>` +
        `Set a GitHub PAT (top right) with <code>repo</code> scope, then refresh.</p>`;
      for (const id of ["ov-funnel", "ov-funnel-callout",
                        "ov-improve", "ov-coverage", "ov-people",
                        "ov-wa-chart", "ov-wa-legend",
                        "ov-anchor-corps", "ov-chart-anchor"]) {
        const el = document.getElementById(id);
        if (el && id === "ov-funnel") el.innerHTML = msg;
        else if (el) el.innerHTML = "";
      }
      console.error(e);
    }
  }

  window.K = window.K || {};
  window.K.Overview = { render };
  // Refresh action panel + triggers strip when issues load
  // The nav badge is global — always update on triggers-loaded regardless of tab.
  window.addEventListener("triggers-loaded", () => {
    updateNavBadge();
  });
})();
