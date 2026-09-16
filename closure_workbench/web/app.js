/* Shield closure workbench — native JS, no dependencies. */
"use strict";

let STATE = null;
let pwTimer = null;

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  setTimeout(() => (t.className = ""), 2600);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

async function loadState(pw = null) {
  const w = pw === null ? parseFloat($("#pw").value) : pw;
  STATE = await api("/api/state?pw=" + w);
  render();
}

/* ------------------------------------------------------------- rendering */
function render() {
  renderBanner();
  renderConflicts();
  renderTopo();
  renderVersions();
  renderAdjust();
  renderObs();
  renderConfirms();
  $("#inputHash").textContent = "输入摘要 " + STATE.input_hash;
  $("#genTime").textContent = "生成 " + STATE.generated_at;
}

function renderBanner() {
  const b = $("#statusBanner");
  if (STATE.blocked) {
    b.className = "banner bad";
    const n = STATE.topology.conflicts.length;
    b.textContent = "⛔ " + STATE.status_text + `（${n} 项阻断）`;
  } else {
    b.className = "banner ok";
    b.textContent = "✓ " + STATE.status_text;
  }
}

const TYPE_LABEL = {
  time_inversion: ["复测时间早于换站", "time"],
  version_cross: ["坐标版本同环交叉", "ring"],
  contradiction_cycle: ["控制点矛盾环", "block"],
  ring_inversion: ["环号倒灌", "block"],
};

function obsRef(id) {
  return `<a class="ref" data-obs="${id}">#${id}</a>`;
}

function renderConflicts() {
  const box = $("#conflictList");
  const cs = STATE.topology.conflicts;
  const cut = new Set(STATE.topology.cut_edges);
  if (!cs.length) {
    box.innerHTML = `<div class="card"><span class="oktext">无阻断冲突。</span></div>`;
  } else {
    box.innerHTML = cs
      .map((c) => {
        const [label, cls] = TYPE_LABEL[c.type] || [c.type, "block"];
        let edges = "";
        if (c.obs_ids) {
          edges =
            "<div>相关观测：" +
            c.obs_ids.map((id) => {
              const isCut = cut.has(id);
              return obsRef(id) + (isCut ? `<span class="cutbadge">最小冲突边</span>` : "");
            }).join(" ") +
            "</div>";
        }
        if (c.obs_id) {
          edges = `<div>相关观测：${obsRef(c.obs_id)}${
            cut.has(c.obs_id) ? '<span class="cutbadge">最小冲突边</span>' : ""
          }</div>`;
        }
        const extra =
          c.misclosure != null
            ? ` <span class="badtext">闭合差 ${(c.misclosure * 1000).toFixed(1)} mm</span>`
            : "";
        return `<div class="card ${cls}">
          <div class="ttl">${label}${extra}</div>
          <div>${c.message}</div>${edges}</div>`;
      })
      .join("");
  }

  const t = STATE.topology;
  const pb = $("#pathBox");
  const cls =
    t.path_status === "unique" ? "oktext" : t.path_status === "none" ? "badtext" : "warnt";
  let html = `<div class="card"><span class="${cls}">${t.path_message}</span></div>`;
  if (t.path_status === "unique") {
    const p = t.paths[0];
    html += `<div class="card">里程顺序：${p.stations.join(" → ")}<br>`;
    html +=
      "接续证据（共享控制点）：" +
      p.links
        .map((l) => `${l.from}→${l.to}@${l.cp}（交出侧最大环 ${l.from_max_ring}，接管侧最小环 ${l.to_min_ring ?? "—"}）`)
        .join("； ") +
      "</div>";
  }
  pb.innerHTML = html;
}

/* ---------------- topology SVG: stations along X, CPs above/below ------ */
function renderTopo() {
  const svg = $("#topoSvg");
  const W = 1180, H = 420, padX = 70;
  const stations = STATE.stations;
  const cps = STATE.cps;
  const xMin = Math.min(...stations.map((s) => s.base_x), ...cps.map((c) => c.base_x));
  const xMax = Math.max(...stations.map((s) => s.base_x), ...cps.map((c) => c.base_x));
  const X = (x) => padX + ((x - xMin) / (xMax - xMin)) * (W - 2 * padX);
  const sy = 300;
  const cut = new Set(STATE.topology.cut_edges);
  const invalidIds = new Set(
    STATE.topology.conflicts
      .filter((c) => c.type === "time_inversion")
      .map((c) => c.obs_id)
  );
  const handoverIds = new Set(
    STATE.topology.links
      .filter((l) => l.declared && l.ring_ok && l.time_ok)
      .flatMap((l) => l.evidence_ids)
  );

  const posS = Object.fromEntries(stations.map((s) => [s.code, [X(s.base_x), sy]]));
  const posC = Object.fromEntries(
    cps.map((c, i) => [c.code, [X(c.base_x), i % 2 ? 90 : 170]])
  );

  let parts = [`<line x1="40" y1="${sy}" x2="${W - 30}" y2="${sy}" stroke="#2b3642" stroke-dasharray="4 4"/>`];
  parts.push(`<text x="40" y="${sy + 22}" class="lbl-muted">里程方向 →</text>`);

  for (const o of STATE.topology.edges) {
    const a = posS[o.station], b = posC[o.cp];
    if (!a || !b) continue;
    let cls = "";
    if (cut.has(o.id)) cls = "edge-cut";
    else if (invalidIds.has(o.id)) cls = "edge-invalid";
    else if (handoverIds.has(o.id)) cls = "edge-handover";
    parts.push(
      `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" class="${cls}" stroke-width="1.6"/>`
    );
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    if (o.cvx != null) {
      parts.push(`<text x="${mx}" y="${my - 3}" class="lbl-muted" text-anchor="middle">${Math.hypot(o.cvx, o.cvy).toFixed(1)}m</text>`);
    }
  }

  for (const [code, [x, y]] of Object.entries(posC)) {
    const hot = STATE.topology.edges.some((e) => e.cp === code && cut.has(e.id));
    parts.push(`<circle cx="${x}" cy="${y}" r="11" class="node-cp${hot ? " node-cut" : ""}"/>`);
    parts.push(`<text x="${x}" y="${y - 16}" text-anchor="middle">${code}</text>`);
  }
  for (const [code, [x, y]] of Object.entries(posS)) {
    parts.push(`<rect x="${x - 16}" y="${y - 14}" width="32" height="28" rx="5" class="node-station"/>`);
    parts.push(`<text x="${x}" y="${y + 5}" text-anchor="middle">${code}</text>`);
  }
  svg.innerHTML = parts.join("");

  $("#linkTable").innerHTML =
    "<tr><th>交出测站</th><th>接管测站</th><th>共享控制点</th><th>是否里程声明</th><th>环号约束</th><th>时间约束</th><th>证据观测</th></tr>" +
    STATE.topology.links
      .map((l) => {
        const ringTxt = l.ring_ok
          ? '<span class="oktext">满足</span>'
          : `<span class="badtext">倒灌 ${l.from_max_ring}→${l.to_min_ring}</span>`;
        const timeTxt = l.time_ok
          ? '<span class="oktext">有效</span>'
          : '<span class="warnt">含早于换站的复测</span>';
        return `<tr class="${l.declared && l.ring_ok && l.time_ok ? "" : ""}">
          <td>${l.from}</td><td>${l.to}</td><td>${l.cp}</td>
          <td>${l.declared ? "✓" : "—"}</td><td>${ringTxt}</td><td>${timeTxt}</td>
          <td>${l.evidence_ids.map(obsRef).join(" ")}</td></tr>`;
      })
      .join("");
}

/* ---------------- versions ---------------------------------------------- */
function renderVersions() {
  $("#versionTable").innerHTML =
    "<tr><th>版本</th><th>名称</th><th>类型</th><th>族</th><th>状态</th><th>到基准的链参数 a,b,tx,ty</th><th>可拟合的确认变换</th></tr>" +
    STATE.versions
      .map((v) => {
        let chain = '<span class="warnt">未链接（观测不可转换）</span>';
        if (v.chain) {
          const c = v.chain;
          chain = `<span class="hash">a=${c.a} b=${c.b}<br>tx=${c.tx} ty=${c.ty}</span>`;
        }
        const status = v.confirmed
          ? '<span class="pill conf">已确认</span>'
          : '<span class="pill unconf">待确认</span>';
        let actions = "";
        if (!v.confirmed && v.fit_preview.length) {
          actions = v.fit_preview
            .slice(0, 3)
            .map(
              (p) => `<button class="primary" data-confirm='${JSON.stringify({
                id: v.id, to: p.to_version, mode: p.mode,
              })}'>确认→${p.to_version}（${
                { cp_abs: "控制点", vector: "矢量", ring: "公共环" }[p.mode]
              }，RMS ${(p.rms * 1000).toFixed(2)}mm，${p.n}点）</button>`
            )
            .join("<br>");
        } else if (!v.confirmed) {
          actions = '<span class="warnt">公共点不足，无法拟合</span>';
        }
        return `<tr><td><b>${v.id}</b></td><td>${v.name}</td><td>${v.kind}</td>
          <td>${v.family ?? "—"}</td><td>${status}</td><td>${chain}</td><td>${actions}</td>`;
      })
      .join("");
}

/* ---------------- adjustment / residuals -------------------------------- */
function renderAdjust() {
  const a = STATE.adjustment;
  if (a.status !== "ok") {
    $("#adjSummary").innerHTML = `<span class="warnt">${a.status}</span>`;
    $("#resSvg").innerHTML = "";
    $("#resTable").innerHTML = "";
    return;
  }
  const used = a.obs.filter((o) => o.status === "ok");
  const unconverted = a.obs.filter((o) => o.status === "unconverted");
  $("#adjSummary").innerHTML = `
    <div class="w2"><div class="card">
      <div class="ttl">全线 RMS：<span class="${a.rms > STATE.tolerance ? "badtext" : "oktext"}">${(a.rms * 1000).toFixed(2)} mm</span></div>
      参与平差观测 ${used.length} 条；过程边权 ${a.params.process_weight}；
      设计环宽 1.5 m。
    </div><div class="card">
      <div class="ttl">最大残差：<span class="badtext">${(a.max_residual * 1000).toFixed(2)} mm</span> @ ${obsRef(a.max_obs_id)}</div>
      ${unconverted.length ? `<span class="warnt">${unconverted.length} 条观测因版本未确认而未参与（见版本页）。</span>` : "全部观测均已转换到基准版本。"}
    </div></div>`;

  // residual stem chart
  const W = 1180, H = 320, pad = 46;
  const maxR = Math.max(a.max_residual, STATE.tolerance) * 1000;
  const x = (i) => pad + (i / Math.max(used.length - 1, 1)) * (W - 2 * pad);
  const y = (r) => H - pad - (r / (maxR * 1.15)) * (H - 2 * pad);
  let p = [`<line x1="${pad}" y1="${y(STATE.tolerance * 1000)}" x2="${W - pad}" y2="${y(STATE.tolerance * 1000)}" stroke="#e8a33d" stroke-dasharray="6 4"/>`];
  p.push(`<text x="${W - pad}" y="${y(STATE.tolerance * 1000) - 5}" text-anchor="end" class="lbl-muted">限差 ${STATE.tolerance * 1000} mm</text>`);
  p.push(`<line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="#44505c"/><text x="${pad}" y="${H - pad + 18}" class="lbl-muted">观测序号（按环）</text>`);
  used.forEach((o, i) => {
    const r = o.residual * 1000;
    const hot = o.id === a.max_obs_id;
    p.push(`<line x1="${x(i)}" y1="${H - pad}" x2="${x(i)}" y2="${y(r)}" stroke="${hot ? "#ff5d5d" : "#4ea1ff"}" stroke-width="${hot ? 3 : 1.4}"/>`);
    if (hot) p.push(`<text x="${x(i)}" y="${y(r) - 6}" text-anchor="middle" fill="#ff5d5d">#${o.id}</text>`);
  });
  $("#resSvg").innerHTML = p.join("");

  $("#resTable").innerHTML =
    "<tr><th>观测</th><th>类型</th><th>环</th><th>版本</th><th class='num'>权</th><th class='num'>dx(mm)</th><th class='num'>dy(mm)</th><th class='num'>残差(mm)</th></tr>" +
    used
      .map(
        (o) => `<tr class="${o.id === a.max_obs_id ? "cut" : ""}">
        <td>${obsRef(o.id)}</td><td>${o.kind}</td><td>${o.ring}</td><td>${o.version}</td>
        <td class="num">${o.weight}</td>
        <td class="num">${(o.dx * 1000).toFixed(2)}</td>
        <td class="num">${(o.dy * 1000).toFixed(2)}</td>
        <td class="num"><b>${(o.residual * 1000).toFixed(2)}</b></td></tr>`
      )
      .join("");
}

/* ---------------- observations table ------------------------------------ */
function renderObs() {
  const cut = new Set(STATE.topology.cut_edges);
  const filter = $("#obsFilter").value.trim().toLowerCase();
  const onlyConflict = $("#onlyConflict").checked;
  let rows = STATE.observations;
  $("#obsCount").textContent = rows.length + " 条";
  if (onlyConflict) rows = rows.filter((o) => cut.has(o.id));
  if (filter)
    rows = rows.filter((o) =>
      [o.id, o.ring, o.station, o.target_cp, o.version, o.kind]
        .filter(Boolean).join(" ").toLowerCase().includes(filter)
    );
  rows = rows.slice(0, 400);
  $("#obsTable").innerHTML =
    "<tr><th>#</th><th>类型</th><th>版本</th><th>环</th><th>测站</th><th>控制点</th>" +
    "<th class='num'>x/vx</th><th class='num'>y/vy</th><th class='num'>基准x</th><th class='num'>基准y</th>" +
    "<th>时刻</th><th class='num'>权</th><th>忽略</th><th>内容哈希</th></tr>" +
    rows
      .map((o) => {
        const cx = o.cx == null ? "—" : o.cx.toFixed(4);
        const cy = o.cy == null ? "—" : o.cy.toFixed(4);
        const cvx = o.cvx == null ? "" : ` / ${o.cvx.toFixed(4)}`;
        const cvy = o.cvy == null ? "" : ` / ${o.cvy.toFixed(4)}`;
        return `<tr class="${cut.has(o.id) ? "cut" : ""} ${o.ignored ? "ignored" : ""} ${
          !o.convertible && o.kind === "resurvey" ? "unconverted" : ""
        }" data-rowid="${o.id}">
        <td>${o.id}${cut.has(o.id) ? '<span class="cutbadge">冲突</span>' : ""}</td>
        <td>${o.kind}</td><td>${o.version}</td><td>${o.ring ?? ""}</td>
        <td>${o.station ?? ""}</td><td>${o.target_cp ?? ""}</td>
        <td class="num">${o.x ?? o.vx ?? ""}</td><td class="num">${o.y ?? o.vy ?? ""}</td>
        <td class="num">${cx}${cvx}</td><td class="num">${cy}${cvy}</td>
        <td>${o.measured_at.replace("T", " ")}</td>
        <td class="num"><input class="w-in" data-wid="${o.id}" value="${o.weight}" size="4"></td>
        <td><input type="checkbox" class="ig-in" data-iid="${o.id}" ${o.ignored ? "checked" : ""}></td>
        <td class="hash">${o.content_hash}</td></tr>`;
      })
      .join("");
}

/* ---------------- confirmations & runs ---------------------------------- */
function renderConfirms() {
  return api("/api/runs").then((runs) => {
    // confirmations are embedded in the latest run snapshot; also show runs
    const confRows = runs.length
      ? runs[0].snapshot.confirmations
      : [];
    $("#confirmTable").innerHTML =
      "<tr><th>版本</th><th>配准到</th><th>确认时刻</th><th>变换参数</th><th>冻结旧坐标(条数)</th><th>点集摘要</th></tr>" +
      (confRows.length
        ? confRows
            .map((c) => {
              const p = JSON.parse(c.params_json);
              return `<tr><td><b>${c.version_id}</b></td><td>${c.to_version}</td>
              <td>${c.confirmed_at}</td>
              <td class="hash">a=${p.a.toFixed(7)} b=${p.b.toFixed(7)}<br>tx=${p.tx.toFixed(4)} ty=${p.ty.toFixed(4)}<br>rms=${(p.rms * 1000).toFixed(2)}mm（${p.mode}，${p.n}点）</td>
              <td>${JSON.parse(c.old_coords_json).length}</td>
              <td class="hash">${(c.input_hash || "").slice(0, 12)}</td></tr>`;
            })
            .join("")
        : '<tr><td colspan="6" class="hint">尚无确认记录</td></tr>');

    $("#runList").innerHTML = runs
      .map(
        (r) => `<details class="run">
        <summary><b>#${r.id}</b> ${r.created_at} · 输入 ${r.input_hash} ·
          ${r.result.blocked ? '<span class="badtext">阻断态快照</span>' : '<span class="oktext">闭合态快照</span>'}
          · RMS ${(r.result.adjustment.rms * 1000).toFixed(2)} mm</summary>
        <h4>计算摘要</h4><pre>${JSON.stringify(
          { params: r.params, result: {
            blocked: r.result.blocked, status: r.result.status_text,
            path: r.result.path, conflicts: r.result.conflicts,
            cut_edges: r.result.cut_edges, adjustment: r.result.adjustment,
          } },
          null, 1
        )}</pre>
        <h4>冻结的旧坐标与变换参数（版本快照）</h4><pre>${JSON.stringify(
          r.snapshot, null, 1
        )}</pre></details>`
      )
      .join("") || '<p class="hint">尚无快照。点击顶部「冻结本次计算」生成。</p>';
  });
}

/* ------------------------------------------------------------- events ---- */
$$("nav.tabs button").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$("nav.tabs button").forEach((b) => b.classList.remove("active"));
    $$(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
  })
);

$("#pw").addEventListener("input", () => {
  $("#pwVal").textContent = parseFloat($("#pw").value).toFixed(1);
  clearTimeout(pwTimer);
  pwTimer = setTimeout(() => loadState(), 160);
});

$("#obsFilter").addEventListener("input", renderObs);
$("#onlyConflict").addEventListener("change", renderObs);

document.addEventListener("click", async (e) => {
  const ref = e.target.closest("a.ref");
  if (ref) {
    $$("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === "obs"));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-obs"));
    $("#obsFilter").value = "#" + ref.dataset.obs;
    renderObs();
    const row = $(`tr[data-rowid="${ref.dataset.obs}"]`);
    row?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  const cfm = e.target.closest("button[data-confirm]");
  if (cfm) {
    const { id, to, mode } = JSON.parse(cfm.dataset.confirm);
    if (!confirm(`确认将版本 ${id} 通过${to}配准并加入变换链？旧坐标与参数将被冻结留痕。`)) return;
    try {
      await api(`/api/versions/${id}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to_version: to, mode }),
      });
      toast(`版本 ${id} 已确认（→${to}）`);
      await loadState();
    } catch (err) {
      toast(err.message, true);
    }
  }
});

document.addEventListener("change", async (e) => {
  if (e.target.classList.contains("w-in")) {
    const id = e.target.dataset.wid;
    try {
      await api("/api/observations/" + id, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weight: parseFloat(e.target.value) }),
      });
      await loadState();
    } catch (err) { toast(err.message, true); }
  }
  if (e.target.classList.contains("ig-in")) {
    const id = e.target.dataset.iid;
    try {
      await api("/api/observations/" + id, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ignored: e.target.checked }),
      });
      toast(e.target.checked ? `已隔离 #${id}` : `已恢复 #${id}`);
      await loadState();
    } catch (err) { toast(err.message, true); }
  }
});

$("#btnImport").addEventListener("click", async () => {
  let records;
  try {
    records = JSON.parse($("#importText").value);
  } catch {
    return toast("JSON 解析失败", true);
  }
  try {
    const r = await api("/api/observations", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(records),
    });
    $("#importResult").innerHTML =
      `<span class="oktext">新增 ${r.added} 条</span>，重新绑定权 ${r.rebound} 条，` +
      `<span class="warnt">重复忽略 ${r.duplicates} 条（不增加观测）</span>`;
    await loadState();
  } catch (err) {
    $("#importResult").innerHTML = `<span class="badtext">${err.message}</span>`;
  }
});

$("#btnRun").addEventListener("click", async () => {
  try {
    const r = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ process_weight: parseFloat($("#pw").value) }),
    });
    toast(r.deduplicated ? `输入未变化，复用快照 #${r.run_id}` : `已冻结快照 #${r.run_id}`);
    await loadState();
  } catch (err) { toast(err.message, true); }
});

$("#btnReseed").addEventListener("click", async () => {
  if (!confirm("将删除当前数据库并重置为演示数据，确定？")) return;
  await api("/api/reseed", { method: "POST" });
  toast("已重置演示数据");
  await loadState();
});

loadState();
