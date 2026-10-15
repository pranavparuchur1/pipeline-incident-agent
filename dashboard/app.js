(function () {
  "use strict";
  var DATA = window.PIA_DATA || { incidents: [], configs: [], metrics: {}, episodes: [] };
  var root = document.getElementById("main");
  var state = { set: null };

  /* ---------- helpers ---------- */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (attrs[k] == null || attrs[k] === false) continue;
        if (k === "class") el.className = attrs[k];
        else if (k === "text") el.textContent = attrs[k];
        else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), attrs[k]);
        else el.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
      }
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(el, x); }); return; }
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  function has(v) { return v !== null && v !== undefined && v !== ""; }
  function usd(v) { return typeof v === "number" ? "$" + v.toFixed(v < 1 ? 3 : 2) : "–"; }
  function secs(v) { return typeof v === "number" ? v.toFixed(1) + "s" : "–"; }
  function pct(r) { return Math.round(r * 100) + "%"; }

  var incById = {};
  (DATA.incidents || []).forEach(function (i) { incById[i.id] = i; });

  var latest = {}; // incident|config -> episode (last wins)
  (DATA.episodes || []).forEach(function (e) { latest[e.incident + "|" + e.config] = e; });
  function epFor(inc, cfg) { return latest[inc + "|" + cfg]; }

  var configs = (DATA.configs || []).slice();
  (DATA.episodes || []).forEach(function (e) { if (configs.indexOf(e.config) < 0) configs.push(e.config); });

  function setHasEpisodes(s) {
    return (DATA.episodes || []).some(function (e) { return e.set === s; });
  }
  if (!state.set) state.set = setHasEpisodes("held-out") || !setHasEpisodes("dev") ? "held-out" : "dev";

  /* ---------- overview ---------- */
  function rateCell(r) {
    if (!r || !r.n) return h("td", { class: "num muted", text: "–" });
    return h("td", { class: "num" },
      r.k + "/" + r.n + " (" + pct(r.rate) + ")",
      h("div", { class: "mut-mono", text: "95% CI " + pct(r.ci95[0]) + "–" + pct(r.ci95[1]) }));
  }
  function plainCell(main, sub) {
    return h("td", { class: "num" }, main, sub ? h("div", { class: "mut-mono", text: sub }) : null);
  }

  var SB_ROWS = [
    ["Root-cause accuracy (family + cause + target)", function (m) { return rateCell(m.root_cause_accuracy); }],
    ["Decision accuracy (fix vs escalate)", function (m) { return rateCell(m.decision_accuracy); }],
    ["Recovery, first attempt", function (m) { return rateCell(m.recovery_first_attempt); }],
    ["Recovery, with one retry", function (m) { return rateCell(m.recovery_with_retry); }],
    ["Correct escalation", function (m) { return rateCell(m.correct_escalation); }],
    ["False remediation (of applied fixes)", function (m) {
      var f = m.false_remediation; if (!f) return rateCell(null);
      if (!f.applied_fixes) return plainCell("0 applied", null);
      return rateCell({ k: f.count, n: f.applied_fixes, rate: f.rate, ci95: f.ci95 });
    }],
    ["Time to diagnosis, median (p90)", function (m) {
      var t = m.time_to_diagnosis_s || {};
      return plainCell(has(t.median) ? secs(t.median) : "–", has(t.p90) ? "p90 " + secs(t.p90) : null);
    }],
    ["Cost per incident, mean", function (m) {
      var c = m.cost_per_incident_usd || {};
      return plainCell(has(c.mean) ? usd(c.mean) : "–", has(c.worker_share) ? "worker share " + pct(c.worker_share) : null);
    }],
    ["Delegations per incident", function (m) { return plainCell(has(m.delegations_per_incident) ? String(m.delegations_per_incident) : "–"); }],
    ["Citations that resolve", function (m) {
      var c = m.citations_valid; if (!c || !c.total) return plainCell("–");
      return plainCell(c.valid + "/" + c.total + " (" + pct(c.valid / c.total) + ")");
    }]
  ];

  function scoreboard(set) {
    var ms = ((DATA.metrics || {})[set] || []).filter(function (m) { return m && m.n > 0; });
    var wrap = h("div");
    if (!ms.length) {
      wrap.appendChild(h("p", { class: "muted", text: "No episodes graded yet for the " + set + " set." }));
    } else {
      var thead = h("thead", null, h("tr", null, h("th", { scope: "col", text: "Metric" }),
        ms.map(function (m) { return h("th", { scope: "col", class: "mono", text: m.config + " (n=" + m.n + ")" }); })));
      var tbody = h("tbody", null, SB_ROWS.map(function (r) {
        return h("tr", null, h("th", { scope: "row", text: r[0] }), ms.map(function (m) { return r[1](m); }));
      }));
      wrap.appendChild(h("div", { class: "scroll" }, h("table", { class: "sb" }, h("caption", { class: "muted small", style: "text-align:left;caption-side:bottom;padding-top:6px", text: "Scoreboard, " + set + " set" }), thead, tbody)));
    }
    wrap.appendChild(h("p", { class: "muted small", text: "Rates with 95% Wilson intervals; costs are API-equivalent estimates (runs used a subscription)." }));
    return wrap;
  }

  function cellStatus(inc, ep) {
    var g = ep.grade || {};
    var fin = g.final || {};
    var first = g.first || {};
    var rcOk = first.root_cause && first.root_cause.correct;
    var tries = g.attempts || (ep.attempts || []).length || 0;
    var status;
    if (inc.outcome === "escalate") {
      status = fin.decision === "escalate" && !g.false_remediations ? "escalated" : "failed";
    } else {
      status = fin.recovered ? "verified" : "failed";
    }
    var label = status === "verified" ? "recovered" : status === "escalated" ? "escalated" : (fin.decision === "escalate" ? "escalated wrongly" : "not recovered");
    var parts = ["rc " + (rcOk ? "✓" : "✗"), tries + (tries === 1 ? " try" : " tries"), usd(ep.usage && ep.usage.api_equiv_usd)];
    return { status: status, label: label, text: parts.join(" · ") };
  }

  function glyph(s) { return h("span", { class: "glyph g-" + s, "aria-hidden": "true" }); }

  function ledger(set) {
    var incs = (DATA.incidents || []).filter(function (i) { return i.set === set; });
    var wrap = h("div");
    wrap.appendChild(h("div", { class: "legend" },
      h("span", null, glyph("verified"), "recovered, verified by rerun"),
      h("span", null, glyph("escalated"), "correct escalation"),
      h("span", null, glyph("failed"), "not recovered or wrong decision"),
      h("span", null, glyph("na"), "no episode yet")));
    if (!incs.length) { wrap.appendChild(h("p", { class: "muted", text: "No incidents in this set." })); return wrap; }
    var thead = h("thead", null, h("tr", null,
      h("th", { scope: "col", text: "Incident" }),
      configs.map(function (c) { return h("th", { scope: "col", class: "mono", text: c }); })));
    var tbody = h("tbody", null, incs.map(function (inc) {
      return h("tr", null,
        h("th", { scope: "row" },
          h("span", { class: "mono", text: inc.id }), " ",
          h("span", { class: "muted small", text: inc.family }),
          h("div", { text: inc.title }),
          inc.outcome === "escalate" ? h("div", { class: "muted small", text: "escalate-only" }) : null),
        configs.map(function (c) {
          var ep = epFor(inc.id, c);
          if (!ep) return h("td", null, h("div", { class: "cell" }, glyph("na"), h("span", { class: "cellna", text: "no episode" })));
          var st = cellStatus(inc, ep);
          return h("td", null, h("div", { class: "cell" }, glyph(st.status),
            h("a", { href: "#/case/" + encodeURIComponent(inc.id) + "/" + encodeURIComponent(c), "aria-label": inc.id + " " + c + ": " + st.label + ", " + st.text }, st.text)));
        }));
    }));
    wrap.appendChild(h("div", { class: "scroll" }, h("table", { class: "ledger" }, thead, tbody)));
    return wrap;
  }

  function renderOverview() {
    document.title = "Pipeline incident agent";
    root.textContent = "";
    function toggleBtn(s) {
      return h("button", { type: "button", "aria-pressed": String(state.set === s), text: s, onclick: function () { state.set = s; renderOverview(); } });
    }
    root.appendChild(h("h1", { text: "Pipeline incident agent" }));
    root.appendChild(h("p", { class: "lede", text: "Opus diagnoses, Sonnet workers gather evidence, and a fix only counts when the sandbox rerun proves it." }));
    root.appendChild(h("div", { class: "toggle", role: "group", "aria-label": "Incident set" }, toggleBtn("held-out"), toggleBtn("dev")));
    root.appendChild(h("section", null, h("h2", { text: "Scoreboard" }), scoreboard(state.set)));
    root.appendChild(h("section", null, h("h2", { text: "Ledger" }), ledger(state.set)));
  }

  /* ---------- case file ---------- */
  var STAGES = [["landing", "files"], ["raw", "tables"], ["staging", "models"], ["marts", "models"], ["checks", "tests"]];
  var STEP_STAGES = { ingest: [0, 1], transform: [2, 3], quality_checks: [4] };

  function strip(label, cls) { // cls: array of per-stage classes
    var kids = [];
    STAGES.forEach(function (s, i) {
      if (i) kids.push(h("div", { class: "link" + (cls[i] === "failed" && cls[i - 1] === "failed" ? " failed" : "") }));
      kids.push(h("div", { class: "stage " + cls[i] },
        s[0],
        h("span", { class: "w", text: cls[i] === "failed" ? "failed" : cls[i] === "verified" ? "ok" : cls[i] === "na" ? "not rerun" : "" })));
    });
    return h("div", null, h("div", { class: "strip-label", text: label }), h("div", { class: "strip", role: "img", "aria-label": label + ": " + STAGES.map(function (s, i) { return s[0] + " " + cls[i]; }).join(", ") }, kids));
  }
  function classesForFailed(step) {
    var c = ["", "", "", "", ""];
    (STEP_STAGES[step] || []).forEach(function (i) { c[i] = "failed"; });
    return c;
  }

  function lastVerification(ep) {
    var atts = ep.attempts || [];
    for (var i = atts.length - 1; i >= 0; i--) if (atts[i].verification) return atts[i].verification;
    return null;
  }

  function cmpRow(label, truth, got, graded) {
    var t = has(truth) ? String(truth) : null, g = has(got) ? String(got) : null;
    var verdict;
    if (!t) verdict = g ? h("span", { class: "muted", text: "not graded" }) : h("span", { class: "muted", text: "n/a" });
    else if (t === g) verdict = h("span", { class: "ok", text: "match" });
    else verdict = h("span", { class: "mm", text: "mismatch" });
    return h("tr", null, h("th", { scope: "row", text: label }),
      h("td", { class: "v", text: t || "–" }), h("td", { class: "v" + (t && t !== g ? " mm" : ""), text: g || "–" }), h("td", null, verdict));
  }

  function evChip(id, evMap, onToggle) {
    var found = evMap && Object.prototype.hasOwnProperty.call(evMap, id);
    if (!found) return h("span", { class: "chip missing", title: "cited but never returned by any tool", text: id });
    var b = h("button", { type: "button", class: "chip", "aria-expanded": "false", text: id });
    b.addEventListener("click", function () { onToggle(b, id); });
    return b;
  }

  function chipRow(ids, evMap, holder) {
    var row = h("div", { class: "chips" });
    var shown = null;
    (ids || []).forEach(function (id) {
      row.appendChild(evChip(id, evMap, function (btn) {
        var open = btn.getAttribute("aria-expanded") === "true";
        Array.prototype.forEach.call(row.querySelectorAll(".chip[aria-expanded]"), function (x) { x.setAttribute("aria-expanded", "false"); });
        holder.textContent = "";
        if (open) return;
        btn.setAttribute("aria-expanded", "true");
        var e = evMap[id] || {};
        holder.appendChild(h("div", { class: "evbox", tabindex: "0", role: "region", "aria-label": "Evidence " + id },
          h("span", { class: "evh", text: id + (e.kind ? "  [" + e.kind + "]" : "") }),
          typeof e.text === "string" ? e.text : JSON.stringify(e.text, null, 2)));
      }));
    });
    return row;
  }

  function diagnosisSection(inc, ep) {
    var sec = h("section", null, h("h2", { text: "Diagnosis vs ground truth" }));
    var atts = ep.attempts || [];
    if (!atts.length) { sec.appendChild(h("p", { class: "muted", text: "No attempts recorded." })); return sec; }
    var truth = inc.root_cause || {};
    atts.forEach(function (a) {
      var d = a.diagnosis;
      if (atts.length > 1) sec.appendChild(h("h3", { text: "Attempt " + a.n }));
      if (!d) { sec.appendChild(h("p", { class: "muted", text: a.error ? "No diagnosis: " + a.error : "No diagnosis produced." })); return; }
      var rc = d.root_cause || {};
      sec.appendChild(h("div", { class: "scroll" }, h("table", { class: "cmp" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Field" }), h("th", { scope: "col", text: "Ground truth" }), h("th", { scope: "col", text: "Agent diagnosis" }), h("th", { scope: "col", text: "Result" }))),
        h("tbody", null,
          cmpRow("family", truth.family, rc.family),
          cmpRow("cause", truth.cause, rc.cause),
          cmpRow("table", truth.table, rc.table),
          cmpRow("column", truth.column, rc.column),
          cmpRow("model", truth.model, rc.model)))));
      if (rc.explanation) sec.appendChild(h("p", { class: "small muted", text: rc.explanation }));
      sec.appendChild(h("p", null, d.summary || ""));
      sec.appendChild(h("p", { class: "mono small" },
        "decision: " + (d.decision || "–") + " · confidence: " + (typeof d.confidence === "number" ? d.confidence.toFixed(2) : "–") +
        (d.escalation_owner ? " · owner: " + d.escalation_owner : "")));
      if (d.alternatives_considered && d.alternatives_considered.length) {
        sec.appendChild(h("details", { class: "more" }, h("summary", { text: "Alternatives considered (" + d.alternatives_considered.length + ")" }),
          h("div", null, d.alternatives_considered.map(function (x) { return h("p", { text: x }); }))));
      }
    });
    return sec;
  }

  function evidenceSection(ep) {
    var sec = h("section", null, h("h2", { text: "Evidence" }));
    var evMap = ep.evidence || {};
    var any = false;
    (ep.attempts || []).forEach(function (a) {
      var list = a.diagnosis && a.diagnosis.evidence;
      if (!list || !list.length) return;
      any = true;
      if ((ep.attempts || []).length > 1) sec.appendChild(h("h3", { text: "Attempt " + a.n }));
      list.forEach(function (c) {
        var holder = h("div");
        sec.appendChild(h("div", { class: "claim" }, h("div", { text: c.claim || "" }), chipRow(c.evidence_ids || [], evMap, holder), holder));
      });
    });
    if (!any) sec.appendChild(h("p", { class: "muted", text: "No cited evidence." }));
    else sec.appendChild(h("p", { class: "muted small", text: "Click an id to read what the tool returned. Struck-through ids were cited but never returned." }));
    return sec;
  }

  function argsText(a) {
    if (!a || typeof a !== "object") return "";
    var s;
    try { s = JSON.stringify(a); } catch (e) { s = ""; }
    if (s === "{}") return "";
    return s.length > 400 ? s.slice(0, 400) + "…" : s;
  }
  function lane(ev) { return /^master/.test(ev.agent || "") ? "master" : "worker"; }

  function eventNode(ev, ep) {
    var evMap = ep.evidence || {};
    var time = h("span", { class: "time", text: typeof ev.t === "number" ? "+" + ev.t.toFixed(1) + "s" : "" });
    var cls = "ev ", node;
    if (ev.kind === "tool") {
      var l = lane(ev);
      var holder = h("div");
      node = h("div", { class: cls + l },
        h("div", { class: "lane", text: l === "master" ? "master" : "worker: " + (ev.agent || "") }),
        h("div", { class: "top" }, time, h("span", { class: "tool", text: ev.tool || "tool" }), ev.ok === false ? h("span", { class: "t-failed small", text: "error" }) : null),
        argsText(ev.args) ? h("div", { class: "args", text: argsText(ev.args) }) : null,
        ev.summary ? h("div", { class: "sum", text: ev.summary }) : null,
        ev.evidence && ev.evidence.length ? chipRow(ev.evidence, evMap, holder) : null,
        holder,
        (ev.images || []).map(function (src) {
          var img = h("img", { src: src, alt: "Chart returned by " + ev.tool, loading: "lazy" });
          img.addEventListener("error", function () { var p = img.parentNode; if (p) p.replaceChild(h("span", { class: "muted small", text: "image missing: " + src }), img); });
          return h("figure", null, img, h("figcaption", { text: ev.summary || ev.tool || "" }));
        }));
    } else if (ev.kind === "llm") {
      var ll = lane(ev);
      node = h("div", { class: cls + "llm " + ll },
        h("div", { class: "lane", text: ll === "master" ? "master" : "worker: " + (ev.agent || "") }),
        h("div", { class: "top" }, time, h("span", { class: "tool", text: ev.model || "model" }),
          h("span", { class: "mut-mono", text: (ev.turns != null ? ev.turns + " turns · " : "") + secs(ev.seconds) + " · " + usd(ev.usd) })),
        ev.task ? h("details", { class: "more" }, h("summary", { text: "Task" }), h("div", { text: ev.task })) : null,
        ev.answer ? h("details", { class: "more" }, h("summary", { text: "Answer" }), h("div", { text: ev.answer })) : null);
    } else if (ev.kind === "proposal") {
      node = h("div", { class: cls + "proposal span" },
        h("div", { class: "top" }, time, h("span", { class: "tool", text: "proposal" })),
        h("div", { class: "args", text: (ev.actions || []).map(function (a) { return a.action + (a.params ? " " + JSON.stringify(a.params) : ""); }).join("; ") }),
        ev.rationale ? h("div", { class: "sum", text: ev.rationale }) : null);
    } else if (ev.kind === "rollback") {
      node = h("div", { class: cls + "rollback span" },
        h("div", { class: "top" }, time, h("span", { class: "tool t-failed", text: "rollback" })),
        h("div", { class: "sum", text: ev.reason || ev.summary || "Sandbox restored to its pre-fix state." }));
    } else if (ev.kind === "denied") {
      node = h("div", { class: cls + "denied span" },
        h("div", { class: "top" }, time, h("span", { class: "tool t-failed", text: "denied" })),
        h("div", { class: "sum", text: [ev.tool, ev.reason || ev.summary].filter(Boolean).join(": ") }));
    } else if (ev.kind === "verification") {
      node = h("div", { class: cls + "verif span" },
        h("div", { class: "top" }, time, h("span", { class: "tool", text: "verification" }),
          h("span", { class: ev.recovered ? "t-verified small" : "t-failed small", text: ev.recovered ? "rerun succeeded" : "rerun did not recover" })));
    } else if (ev.kind === "attempt_start") {
      node = h("div", { class: cls + "attempt span" }, h("div", { class: "top" }, time, h("span", { class: "tool", text: "attempt " + (ev.attempt != null ? ev.attempt : "") + " starts" })));
    } else {
      node = h("div", { class: cls + "span" }, h("div", { class: "top" }, time, h("span", { class: "tool", text: ev.kind || "event" })));
    }
    return node;
  }

  function timelineSection(ep) {
    var sec = h("section", null, h("h2", { text: "Investigation timeline" }));
    var evs = ep.events || [];
    if (!evs.length) { sec.appendChild(h("p", { class: "muted", text: "No events recorded." })); return sec; }
    sec.appendChild(h("div", { class: "tl" },
      h("div", { class: "tl-head" }, h("div", { text: "Master" }), h("div", { text: "Workers" })),
      h("div", { class: "tl-row" }, evs.map(function (e) { return eventNode(e, ep); }))));
    return sec;
  }

  function outcomeOf(inc, ep) {
    var atts = ep.attempts || [];
    var last = atts[atts.length - 1];
    var anyApplied = atts.some(function (a) { return a.applied && a.applied.length; });
    var d = last && last.diagnosis;
    if (!last) return { cls: "na", word: "NO RESULT" };
    if (last.recovered || (last.verification && last.verification.recovered)) return { cls: "verified", word: "VERIFIED RECOVERED" };
    if (d && d.decision === "escalate" && !anyApplied) return { cls: "escalated", word: "ESCALATED — NO CHANGE MADE" };
    return { cls: "failed", word: "NOT RECOVERED" };
  }

  function dl(rows) {
    var d = h("dl");
    rows.forEach(function (r) { d.appendChild(h("dt", { text: r[0] })); d.appendChild(h("dd", null, r[1])); });
    return d;
  }
  function listText(arr, empty) { return arr && arr.length ? arr.join("\n") : empty; }

  function verificationSection(inc, ep) {
    var o = outcomeOf(inc, ep);
    var box = h("div", { class: "verify " + o.cls });
    var word = h("div", { class: "outcome " + o.cls, role: "status", text: o.word });
    box.appendChild(word);
    var atts = ep.attempts || [];
    atts.forEach(function (a) {
      var v = a.verification;
      var rows = [];
      var applied = a.applied || [];
      rows.push(["applied", h("span", { style: "white-space:pre-wrap", text: applied.length ? applied.join("\n") : "nothing applied" })]);
      if (a.proposal && a.proposal.actions) rows.push(["proposed", a.proposal.actions.map(function (x) { return x.action + (x.params ? " " + JSON.stringify(x.params) : ""); }).join("; ")]);
      if (a.apply_error) rows.push(["apply error", h("span", { class: "t-failed", text: String(a.apply_error) })]);
      if (v) {
        rows.push(["rerun", h("span", { class: v.recovered ? "t-verified" : "t-failed", text: v.recovered ? "success, run recovered" : "failed" + (v.failed_step ? " at step " + v.failed_step : "") })]);
        rows.push(["failed checks", h("span", { style: "white-space:pre-wrap", text: listText(v.failed_checks, "none") })]);
        rows.push(["differs from known-good", h("span", { style: "white-space:pre-wrap", text: listText(v.tables_differing_from_known_good, "no tables differ") })]);
        if (v.run_log_errors && v.run_log_errors.length) rows.push(["rerun log errors", h("span", { style: "white-space:pre-wrap", text: v.run_log_errors.join("\n") })]);
        if (v.error) rows.push(["error", String(v.error)]);
      } else {
        rows.push(["rerun", h("span", { class: "muted", text: "not run (no fix was applied)" })]);
      }
      if (a.error) rows.push(["attempt error", String(a.error)]);
      box.appendChild(h("div", { class: "vatt" }, h("h3", { text: "Attempt " + a.n + (a.diagnosis && a.diagnosis.decision ? " · decision: " + a.diagnosis.decision : "") }), dl(rows)));
    });
    if (!atts.length) box.appendChild(h("p", { class: "muted", text: "No attempts recorded." }));
    var sec = h("section", null, h("h2", { text: "Verification" }), box);
    // the one motion: opacity fade when scrolled into view
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduce && "IntersectionObserver" in window) {
      word.classList.add("pre");
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { if (en.isIntersecting) { word.classList.remove("pre"); io.disconnect(); } });
      }, { threshold: 0.6 });
      io.observe(word);
    }
    return sec;
  }

  function usageSection(ep) {
    var u = ep.usage || {}, br = u.by_role || {}, m = br.master || {}, w = br.worker || {};
    var g = ep.grade || {}, first = g.first || {};
    return h("section", null, h("h2", { text: "Usage" }),
      h("div", { class: "usage" },
        h("div", null, h("div", { class: "muted", text: "Master" }), h("div", { class: "mono", text: usd(m.api_equiv_usd) + " · " + (m.calls != null ? m.calls : 0) + " calls" })),
        h("div", null, h("div", { class: "muted", text: "Workers" }), h("div", { class: "mono", text: usd(w.api_equiv_usd) + " · " + (w.calls != null ? w.calls : 0) + " calls" })),
        h("div", null, h("div", { class: "muted", text: "Total" }), h("div", { class: "mono", text: usd(u.api_equiv_usd) + " · " + (u.tool_calls != null ? u.tool_calls : 0) + " tool calls" })),
        h("div", null, h("div", { class: "muted", text: "Time to diagnosis" }), h("div", { class: "mono", text: secs(first.seconds) }))),
      h("p", { class: "muted small", text: "Costs are API-equivalent estimates (runs used a subscription)." }));
  }

  function renderCase(incId, cfg) {
    root.textContent = "";
    var inc = incById[incId];
    var ep = epFor(incId, cfg);
    document.title = incId + " / " + cfg + " · Pipeline incident agent";
    root.appendChild(h("div", { class: "nav" }, h("a", { href: "#/", text: "← Back to overview" })));
    if (!inc || !ep) {
      root.appendChild(h("h1", { text: incId + " / " + cfg }));
      root.appendChild(h("p", { class: "muted", text: inc ? "No episode has been recorded for this incident and config yet." : "Unknown incident." }));
      return;
    }
    var al = ep.alert || {};
    root.appendChild(h("h1", null, h("span", { class: "mono", text: inc.id }), " " + inc.title));
    root.appendChild(h("p", { class: "mono small muted", text: "config " + cfg + " · " + inc.family + " · " + inc.set + " set" + (inc.outcome === "escalate" ? " · escalate-only" : "") }));
    root.appendChild(h("p", { class: "alert", text: "nightly " + (al.success === false || al.success == null ? "FAILED" : "ok") + " · partition " + (al.partition || "?") + " · run " + String(al.run_id || "?").slice(0, 8) + " · step " + (al.failed_step || "?") }));

    var v = lastVerification(ep);
    var strips = [strip("At alert time", classesForFailed(al.failed_step))];
    if (v) {
      var c2;
      if (v.recovered) c2 = ["verified", "verified", "verified", "verified", "verified"];
      else { c2 = classesForFailed(v.failed_step); for (var i = 0; i < c2.length; i++) if (!c2[i]) c2[i] = "na"; }
      strips.push(strip("After sandbox rerun", c2));
    } else {
      strips.push(strip("After sandbox rerun (not run, no change made)", ["na", "na", "na", "na", "na"]));
    }
    root.appendChild(h("div", { class: "strips" }, strips));

    root.appendChild(diagnosisSection(inc, ep));
    root.appendChild(evidenceSection(ep));
    root.appendChild(timelineSection(ep));
    root.appendChild(verificationSection(inc, ep));
    root.appendChild(usageSection(ep));
    root.appendChild(h("p", null, h("a", { href: "#/", text: "← Back to overview" })));
  }

  /* ---------- router ---------- */
  function route() {
    var hash = location.hash || "#/";
    var m = /^#\/case\/([^/]+)\/([^/]+)\/?$/.exec(hash);
    try {
      if (m) renderCase(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
      else renderOverview();
    } catch (err) {
      root.textContent = "";
      root.appendChild(h("p", { class: "t-failed", text: "Render error: " + err.message }));
      if (window.console) console.error(err);
    }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", function () { route(); root.focus({ preventScroll: true }); });
  route();
})();
