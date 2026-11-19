// Entry Pilot window. Exchanges the one-time launch code (?c=...) once for an in-memory API token. No framework.
"use strict";
let TOKEN = "";
let sessionReady = null;
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = s => {
  try {
    const u = new URL(String(s ?? "").trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : "";
  } catch { return ""; }
};
let S = null;            // last /api/state
let route = "home";
let step = 0;            // wizard step index
let todoState = "owner";
let xlsxShown = [];      // displayed xlsx_pending rows ({id, version, hash})
const SESSION_KEY = "ep_token";
const SESSION_RETRY_MS = 30000;

function readStoredToken() {
  try { return sessionStorage.getItem(SESSION_KEY) || ""; } catch { return ""; }
}
function writeStoredToken(tok) {
  try {
    if (tok) sessionStorage.setItem(SESSION_KEY, tok);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {}
}
function showLaunchFailure(msg) {
  const v = $("#view");
  if (!v) return;
  const hasCode = !!(new URLSearchParams(location.search).get("c") || "");
  v.innerHTML = `<section class="hero small"><h1>창을 열지 못했습니다</h1>
    <p class="lead">${esc(msg || "앱 서버와 연결하지 못했거나 창 연결 코드가 만료됐습니다.")}</p>
    <div class="card soft" style="max-width:560px;margin:22px auto 0;text-align:left">
      <b>다시 여는 방법</b>
      <p class="sub" style="margin-top:6px">보안을 위해 이 화면에서는 실행 중인 앱에 새 창 열기를 직접 요청할 수 없습니다. 바탕화면·시작 메뉴의 <b>Entry Pilot</b> 아이콘(또는 <code>EntryPilot.exe</code>)을 다시 실행하면 새 창이 열립니다.</p>
      <div class="row" style="margin-top:16px">
        <button class="btn primary" id="reopen-window">다시 열기</button>
        <span class="sub">${hasCode ? "서버가 준비됐다면 다시 연결을 시도합니다." : "Entry Pilot을 다시 실행해 주세요."}</span>
      </div>
    </div></section>`;
}

async function ensureSession() {
  if (TOKEN) return;
  const code = new URLSearchParams(location.search).get("c") || "";
  if (!code) {
    const saved = readStoredToken();
    if (saved) { TOKEN = saved; return; }
    throw new Error("이 창은 만료됐습니다. Entry Pilot을 다시 실행하세요.");
  }
  if (!sessionReady) {
    sessionReady = (async () => {
      const deadline = Date.now() + SESSION_RETRY_MS;
      let delay = 250;
      let lastErr = null;
      for (;;) {
        let fatal = false;
        try {
          const r = await fetch("/api/session", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code }),
          });
          if (r.status === 403) {
            fatal = true;
            throw new Error("이 창은 만료됐습니다. Entry Pilot을 다시 실행하세요.");
          }
          if (!r.ok) throw new Error("앱 서버가 아직 준비되지 않았습니다. 잠시 후 다시 시도하세요.");
          const j = await r.json();
          if (!j.ok || !j.token) {
            fatal = true;
            throw new Error("이 창은 만료됐습니다. Entry Pilot을 다시 실행하세요.");
          }
          TOKEN = j.token;
          writeStoredToken(TOKEN);
          const u = new URL(location.href);
          if (u.searchParams.has("c")) {
            u.searchParams.delete("c");
            history.replaceState(null, "", u.pathname + u.search + u.hash);
          }
          return;
        } catch (e) {
          lastErr = e;
          if (fatal) break;
          const rem = deadline - Date.now();
          if (rem <= 0) break;
          await new Promise(res => setTimeout(res, Math.min(delay, rem)));
          delay = Math.min(3000, delay * 2);
        }
      }
      throw lastErr || new Error("앱 서버와 연결하지 못했습니다. Entry Pilot을 다시 실행하세요.");
    })().finally(() => {
      sessionReady = null;
    });
  }
  return sessionReady;
}

async function api(path, body) {
  await ensureSession();
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "X-EP-Token": TOKEN } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 403) {
    TOKEN = "";
    writeStoredToken("");
    showLaunchFailure("이 창은 만료됐습니다. Entry Pilot을 다시 실행하세요.");
    throw new Error("이 창은 만료됐습니다. Entry Pilot을 다시 실행하세요.");
  }
  return r.json();
}
function toast(msg, bad) {
  const el = $("#t-toast").content.firstElementChild.cloneNode(true);
  el.textContent = msg; if (bad) el.classList.add("bad");
  document.body.append(el); setTimeout(() => el.remove(), 3200);
}
async function refresh() { S = await api("/api/state"); paintChrome(); return S; }

// ---------- header ----------
function paintChrome() {
  const e = S.engine || {}, ce = S.contest_engine || {}, pill = $("#engine-pill");
  const run = e.running || (S.settings.modes.contests && ce.running);
  pill.className = "pill status " + (run ? "run" : (S.settings.setup_done ? "" : "warn"));
  pill.lastElementChild.textContent = run ? "실행 중" : (S.settings.setup_done ? "멈춤" : "설정 필요");
  $("#tabs").hidden = !S.settings.setup_done;
  const n = ((e.states || {}).owner || 0) + ((e.states || {}).manual || 0) + (e.xlsx_pending || 0);
  const c = $("#todo-count"); c.hidden = !n; c.textContent = n;
  const cs = ce.states || {}, cn = (cs.proposal || 0) + (cs.review || 0) + (cs.owner || 0) + (cs.manual || 0);
  $("#tab-contests").hidden = !S.settings.modes.contests;
  const cc = $("#contest-count"); cc.hidden = !cn; cc.textContent = cn;
  $("#foot-ver").textContent = `Entry Pilot ${S.version}`;
  document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.go === route));
}

// ---------- wizard ----------
const AGENTS = [
  { id: "claude", name: "Claude", by: "Anthropic", mark: "Cl", desc: "Claude Pro 이상 구독 필요", tag: "추천" },
  { id: "codex", name: "Codex", by: "OpenAI", mark: "Cx", desc: "ChatGPT Plus 이상 구독 필요", tag: "추천" },
  { id: "gemini", name: "Gemini", by: "Google", mark: "Ge", desc: "무료 API 키로 사용. 설문 같은 간단한 작업용", tag: "선택" },
];
const STEPS = ["AI 고르기", "자동 설치", "AI 로그인", "알림", "내 정보", "사이트 로그인", "공모전 고르기", "시작"];

function stepDone(i) {
  const st = S.settings, L = S.logins || {};
  return [AGENTS.some(a => st.agents[a.id]), st.installed === S.version, AGENTS.some(a => st.agents[a.id]) && AGENTS.every(a => !st.agents[a.id] || (L[a.id] || {}).all_in),
          notifyReady(), !!S.profile_filled, i < step, !!st.contest_chosen, st.setup_done][i];
}
function wizard() {
  const done = i => i !== step && stepDone(i);
  return `
  <section class="hero small">
    <h1>처음 설정</h1>
  </section>
  <div class="wizard">
    <ol class="steps">${STEPS.map((s, i) => `<li class="${i === step ? "on" : ""} ${done(i) ? "done" : ""}" data-step="${i}"><span class="k">${done(i) ? "✓" : i + 1}</span>${s}</li>`).join("")}</ol>
    <div class="card" id="step-body">${stepBody()}</div>
  </div>`;
}

function stepBody() {
  const st = S.settings, L = S.logins || {}, D = S.deps || {}, T = (S.tasks || {}).install;
  const picked = AGENTS.filter(a => st.agents[a.id]);
  switch (step) {
    case 0: return `
      <div class="eyebrow">Step 1</div><h2>사용할 AI</h2>
      <p class="sub">가지고 있는 구독 계정으로 씁니다. 계정이 여러 개면 한 계정의 사용량이 다 찼을 때 다음 계정으로 넘어갑니다.</p>
      <div class="choice">${AGENTS.map(a => `
        <div class="opt ${st.agents[a.id] ? "on" : ""}" data-agent="${a.id}">
          <span class="tag">${a.tag}</span><h3>${a.name}</h3><div class="sub">${a.by}</div><p>${a.desc}</p>
          ${st.agents[a.id] && a.id !== "gemini" ? `<div class="stepper"><span>계정</span><button class="btn sm ghost" data-acct="${a.id}" data-d="-1" ${acctN(a.id) <= 1 ? "disabled" : ""}>−</button><b>${acctN(a.id)}</b><button class="btn sm ghost" data-acct="${a.id}" data-d="1" ${acctN(a.id) >= 5 ? "disabled" : ""}>+</button></div>` : ""}
          <span class="tick"></span>
        </div>`).join("")}</div>
      <div class="eyebrow" style="margin-top:8px">응모 종류</div>
      <div class="choice" style="grid-template-columns:1fr 1fr">
        <div class="opt ${st.modes.events ? "on" : ""}" data-mode="events"><h3>경품 이벤트 응모</h3><p>설문, 댓글, 구독 같은 온라인 이벤트</p><span class="tick"></span></div>
        <div class="opt ${st.modes.contests ? "on" : ""}" data-mode="contests"><h3>공모전 접수</h3><p>요강 확인, 서류 작성, 내가 통과를 눌러야 제출</p><span class="tick"></span></div>
      </div>
      <div class="row"><span class="spacer"></span><button class="btn primary" data-next ${picked.length ? "" : "disabled"}>다음 <span class="arrow">→</span></button></div>`;
    case 1: {
      const run = T && T.state === "running", ok = st.installed === S.version && D.node && picked.every(a => (D.cli || {})[a.id]) && D.python;
      return `
      <div class="eyebrow">Step 2</div><h2>프로그램 설치</h2>
      <p class="sub">Entry Pilot 폴더 안에 설치합니다. 관리자 권한은 필요 없습니다. 3분에서 10분 걸립니다(인터넷 속도에 따라 다름).</p>
      <ul class="checks">
        <li class="${D.node ? "ok" : ""}"><span class="d"></span>Node.js <span class="v">AI 도구 실행용</span></li>
        ${st.agents.claude ? `<li class="${D.git ? "ok" : ""}"><span class="d"></span>Git <span class="v">Claude Code에 필요</span></li>` : ""}
        ${picked.map(a => `<li class="${(D.cli || {})[a.id] ? "ok" : ""}"><span class="d"></span>${a.name} 프로그램 <span class="v">${a.by}</span></li>`).join("")}
        <li class="${D.python ? "ok" : ""}"><span class="d"></span>자동화 엔진 <span class="v">내장</span></li>
        <li class="${D.browser ? "ok" : ""}"><span class="d"></span>브라우저 <span class="v">${esc(D.browser ? D.browser.split("\\").slice(-1)[0] : "없음")}</span></li>
      </ul>
      ${T ? `<div class="progress"><i style="width:${T.pct}%"></i></div><div class="sub mono">${esc(T.text)}</div>` : ""}
      ${T && T.state === "error" ? `<div class="err">${esc(T.error)}</div>` : ""}
      <div class="row" style="margin-top:22px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span>
        ${ok ? `<button class="btn primary" data-next>다음 <span class="arrow">→</span></button>`
              : `<button class="btn primary" id="install" ${run ? "disabled" : ""}>${run ? "설치 중" : (T && T.state === "error" ? "다시 시도" : "설치 시작")}</button>`}
      </div>`;
    }
    case 2: {
      const Q = (S.tasks || {}).logins, qrun = Q && Q.state === "running";
      const need = picked.some(a => a.id !== "gemini" && !(L[a.id] || {}).all_in);
      return `
      <div class="eyebrow">Step 3</div><h2>AI 로그인</h2>
      <p class="sub">로그인 창이 차례로 열립니다. 브라우저에서 로그인하면 창이 닫히고 다음 계정 창이 열립니다. 로그인 정보는 각 AI 프로그램이 저장하고, Entry Pilot은 읽지 않습니다.</p>
      <p class="note">Claude: 로그인 후 브라우저에 코드가 나오면 복사해서 작은 창에 붙여 넣고 Enter<br>
        Codex: 작은 창에 코드가 나오면 브라우저에 그 코드를 입력<br>
        계정이 둘 이상이면 두 번째부터는 브라우저에서 앞 계정을 로그아웃하고 다른 계정으로 로그인</p>
      <div style="margin-top:6px">${picked.map(a => loginRow(a, L[a.id] || {})).join("")}</div>
      ${qrun && Q.device ? codexBox(Q.device) : ""}
      ${Q && (qrun || Q.state === "error") ? `<div class="sub mono" style="margin-top:12px">${esc(qrun ? Q.text : "")}</div>` : ""}
      ${Q && Q.state === "error" ? `<div class="err">${esc(Q.error)}</div>` : ""}
      <div class="row" style="margin-top:18px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span>
        ${need ? `<button class="btn primary" id="login-start" ${qrun ? "disabled" : ""}>${qrun ? "로그인 진행 중" : (Q && Q.state === "error" ? "다시 시도" : "로그인 시작")}</button>`
               : `<button class="btn primary" data-next ${picked.some(a => (L[a.id] || {}).logged_in) ? "" : "disabled"}>다음 <span class="arrow">→</span></button>`}</div>`;
    }
    case 3: {
      const ch = st.notify || (st.telegram_chat_id ? "telegram" : "");
      const opts = [["telegram", "텔레그램", "휴대폰으로 받고, 메시지 버튼으로 바로 답합니다."],
                    ["email", "이메일", "내 메일로 받습니다. 답은 Entry Pilot 창의 확인할 일 탭에서 합니다."],
                    ["none", "받지 않음", "따로 알리지 않습니다. Entry Pilot 창의 확인할 일 탭에서 봅니다."]];
      return `
      <div class="eyebrow">Step 4</div><h2>알림 받을 곳</h2>
      <p class="sub">본인인증, 결제처럼 직접 해야 하는 일이나 AI가 묻는 것이 생기면 알립니다.</p>
      <div class="choice">${opts.map(([k, n, d]) => `<div class="opt ${ch === k ? "on" : ""}" data-notify="${k}">${k === "telegram" ? `<span class="tag">추천</span>` : ""}<h3>${n}</h3><p>${d}</p><span class="tick"></span></div>`).join("")}</div>
      ${ch === "telegram" ? tgPanel(st) : ch === "email" ? mailPanel(st) : ""}
      <div class="row" style="margin-top:22px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span>
        <button class="btn primary" data-next ${notifyReady() ? "" : "disabled"}>다음 <span class="arrow">→</span></button></div>`;
    }
    case 4: return `
      <div class="eyebrow">Step 5</div><h2>내 정보</h2>
      <p class="sub">응모 양식에 넣을 정보입니다. 이 PC에만 저장하고, 응모할 때 양식이 묻는 항목만 AI에게 넘깁니다.</p>
      <form id="pf" class="form2" style="margin-top:18px">${profileFields()}</form>
      <div class="row" style="margin-top:8px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span><button class="btn primary" id="pf-save">저장하고 다음 <span class="arrow">→</span></button></div>`;
    case 5: return `
      <div class="eyebrow">Step 6</div><h2>사이트 로그인</h2>
      <p class="sub">AI는 응모 전용 브라우저로 응모합니다. 아래 사이트에 한 번씩 로그인해 두면 로그인이 유지됩니다. 비밀번호는 브라우저에만 저장되고 AI에게 넘어가지 않습니다.</p>
      <div class="grid g3" style="margin:20px 0">${[["google", "Google (Gmail, 설문)"], ["naver", "네이버"], ["kakao", "카카오"], ["instagram", "인스타그램"], ["x", "X (트위터)"], ["youtube", "유튜브"]]
        .map(([k, n]) => `<button class="btn" data-site="${k}">${n}</button>`).join("")}</div>
      ${st.modes.contests ? `<div class="eyebrow" style="margin-top:24px">공모전용 브라우저</div><p class="sub">공모전은 별도의 브라우저 창을 씁니다. 여기에도 따로 로그인합니다.</p>
        <div class="grid g3" style="margin:12px 0">${[["google", "Google (Gmail, 설문)"], ["naver", "네이버"], ["kakao", "카카오"]].map(([k, n]) => `<button class="btn" data-site="${k}" data-engine="contests">${n}</button>`).join("")}</div>` : ""}
      <p class="note">로그인한 창은 닫아도 됩니다. 건너뛰어도 되고, 나중에 설정에서 할 수 있습니다.</p>
      <div class="row"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span><button class="btn primary" data-next>다음 <span class="arrow">→</span></button></div>`;
    case 6: return contestStep();
    case 7: return `
      <div class="eyebrow">Step 8</div><h2>설정 확인</h2>
      <p class="sub">시작하면 AI가 ${[st.modes.events ? "이벤트" : "", st.modes.contests ? "공모전" : ""].filter(Boolean).join("과 ")}을 찾아 조건을 확인하고 응모합니다. 직접 해야 하는 일은 "확인할 일" 탭과 고른 알림으로 옵니다.${st.modes.contests ? " 공모전은 내가 통과를 눌러야 제출합니다(공모전 탭)." : ""}</p>
      <ul class="checks">
        ${picked.map(a => `<li class="${(L[a.id] || {}).logged_in ? "ok" : ""}"><span class="d"></span>${a.name} <span class="v">${(L[a.id] || {}).logged_in ? "로그인됨" : "로그인 안 됨"}</span></li>`).join("")}
        <li class="${notifyReady() ? "ok" : ""}"><span class="d"></span>알림 <span class="v">${notifyText()}</span></li>
        <li class="${S.profile_filled ? "ok" : ""}"><span class="d"></span>내 정보 <span class="v">${S.profile_filled ? "저장됨" : "비어 있음"}</span></li>
        <li class="${st.contest_chosen ? "ok" : ""}"><span class="d"></span>공모전 <span class="v">${st.contest_chosen ? contestText() : "고르지 않음 (기본값 사용)"}</span></li>
      </ul>
      <div class="row" style="margin-top:24px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span><button class="btn dark" id="finish">시작 <span class="arrow">→</span></button></div>`;
  }
}

// ---------- contest filters (saved into the engine's policy.json) ----------
let CONTEST = null, CT = null;   // server answer, the user's draft
const CT_ROWS = [
  ["contest_onsite_writing", "현장에서 쓰는 공모", "백일장, 현장 시험처럼 정해진 장소에 가서 쓰거나 시험을 보는 공모전", [["allow", "응모"], ["exclude", "제외"]]],
  ["contest_onsite_final", "본선 현장 발표", "본선이나 최종 심사에서 현장에 가서 발표, 면접, 시연을 하는 공모전 (온라인 발표는 해당 없음)", [["allow", "응모"], ["ask_over", "상금이 크면 묻기"], ["exclude", "제외"]]],
  ["contest_mail_only", "우편 제출", "우편이나 방문으로만 받는 공모전. 부치는 일은 직접 합니다", [["allow", "응모"], ["exclude", "제외"]]],
  ["contest_ceremony", "시상식 참석 필수", "수상하면 시상식에 꼭 가야 하는 공모전", [["allow", "응모"], ["exclude", "제외"]]],
];
const allOffline = () => CT && CT_ROWS.every(([k]) => CT[k] === "exclude");
function contestText() {
  if (!CONTEST) return "저장됨";
  const v = CONTEST.values, names = CONTEST.fields.filter(f => v.contest_fields.includes(f.id)).map(f => f.label);
  return `분야 ${names.length ? names.join(", ") : "없음"}`;
}
function contestStep() {
  if (!CT) return `<p class="sub">불러오는 중</p>`;
  const won = Math.round((CT.contest_prize_ask_krw || 0) / 10000);
  return `
      <div class="eyebrow">Step 7</div><h2>공모전 고르기</h2>
      <p class="sub">AI가 찾은 공모전 가운데 응모할 것만 고릅니다. 나중에 설정에서 바꿀 수 있습니다.</p>
      <div class="eyebrow" style="margin-top:18px">분야 <span class="sub">(여러 개 고를 수 있음)</span></div>
      <div class="choice mini">${CONTEST.fields.map(f => `<div class="opt ${CT.contest_fields.includes(f.id) ? "on" : ""}" data-ctfield="${f.id}"><h3>${esc(f.label)}</h3><span class="tick"></span></div>`).join("")}</div>
      <div class="eyebrow" style="margin-top:8px">오프라인 조건</div>
      <div class="opt ${allOffline() ? "on" : ""}" data-ctalloff style="margin:12px 0 4px"><h3>오프라인 완전 제외</h3><p>현장에 가는 일, 우편 제출, 시상식 참석이 있는 공모전은 모두 뺍니다.</p><span class="tick"></span></div>
      ${CT_ROWS.map(([k, n, d, opts]) => `<div class="setting"><div class="txt"><b>${n}</b><div class="sub">${d}</div>
          ${k === "contest_onsite_final" && CT[k] === "ask_over" ? `<div class="row" style="margin-top:8px"><span class="sub">최고 상금이</span><input class="input" id="ct-won" type="number" min="0" step="10" value="${won}" style="width:110px;height:34px"><span class="sub">만 원을 넘거나 금액이 없으면 응모 전에 묻습니다</span></div>` : ""}</div>
        <div class="seg">${opts.map(([v, l]) => `<button data-ctkey="${k}" data-ctval="${v}" class="${CT[k] === v ? "on" : ""}">${l}</button>`).join("")}</div></div>`).join("")}
      <div class="row" style="margin-top:22px"><button class="btn ghost" data-prev>이전</button><span class="spacer"></span><button class="btn primary" id="ct-save">저장하고 다음 <span class="arrow">→</span></button></div>`;
}
async function loadContest() {
  CONTEST = await api("/api/contest");
  if (!CT) CT = JSON.parse(JSON.stringify(CONTEST.values));
}
function readWon() {
  const el = $("#ct-won");
  if (el) CT.contest_prize_ask_krw = Math.max(0, Math.round(+el.value || 0)) * 10000;
}

function codexBox(dv) {
  return dv.device ? `<div class="card soft" style="margin-top:14px">
      <div class="eyebrow">Codex 계정 ${dv.i}</div>
      <p style="margin:8px 0 4px">브라우저에서 ChatGPT 계정으로 로그인한 뒤, 이 코드를 입력하세요. 코드는 복사돼 있으니 붙여 넣기(Ctrl+V)만 하면 됩니다.</p>
      <div class="code-big">${esc(dv.code)}</div>
      <div class="row"><button class="btn sm" id="cx-open">브라우저 다시 열기</button><span class="sub">코드는 15분 동안 쓸 수 있습니다.</span></div></div>`
    : `<div class="card soft" style="margin-top:14px"><div class="eyebrow">Codex 계정 ${dv.i}</div>
      <p style="margin:8px 0">열린 브라우저에서 ChatGPT 계정으로 로그인하세요. 끝나면 다음으로 넘어갑니다.</p>
      <div class="row"><button class="btn sm" id="cx-open">브라우저 다시 열기</button></div></div>`;
}

// ---------- notices: telegram / email / none ----------
function notifyReady() {
  const st = S.settings, ch = st.notify || (st.telegram_chat_id ? "telegram" : "");
  return ch === "none" || (ch === "telegram" && !!st.telegram_chat_id) || (ch === "email" && !!st.email_address && !!st.email_password);
}
function notifyText() {
  const st = S.settings, ch = st.notify || (st.telegram_chat_id ? "telegram" : "");
  if (ch === "telegram") return st.telegram_chat_id ? "텔레그램 @" + esc(st.telegram_bot || "") : "텔레그램 연결 안 됨";
  if (ch === "email") return st.email_password ? "이메일 " + esc(st.email_to || st.email_address) : "이메일 연결 안 됨";
  if (ch === "none") return "받지 않음 (확인할 일 탭에서 확인)";
  return "정하지 않음";
}
let tgMode = "ai", tgAgree = false, tgOpened = false, guideAt = 0, tgAgent = "", tgAgents = null, tgName = "", tgUser = "", tgRetry = "", tgClip = null, tgBotUrl = "";
const GUIDE = [
  "텔레그램 검색창에 BotFather를 입력하고, 이름 옆에 파란 체크(✓)가 있는 @BotFather를 고릅니다. 비슷한 이름의 다른 계정은 고르지 마세요.",
  "아래 입력칸에 /newbot 을 입력해 보냅니다. 처음이면 먼저 START(시작)를 누릅니다.",
  "봇 이름을 물어보면 아무 이름이나 보냅니다. 예: 내 응모 알림",
  "봇 아이디를 물어보면 끝이 bot 인 아이디를 보냅니다. 예: my_entry_alert_bot (이미 있는 아이디면 다른 아이디를 보냅니다). 그러면 토큰이 나옵니다. 토큰을 누르면 복사됩니다.",
  "토큰을 복사하면 Entry Pilot 화면에 봇 이름이 나옵니다. 확인을 누르면 만든 봇의 대화창이 열리고, 그 대화창에서 START(시작)를 누르면 연결이 끝납니다.",
];
function tgPanel(st) {
  if (st.telegram_chat_id) return `<div class="row" style="margin-top:18px"><span class="pill ok"><i></i>연결됨 @${esc(st.telegram_bot || "")}</span>
      <button class="btn sm ghost" id="tg-test">테스트 보내기</button><button class="btn sm ghost" id="tg-redo">다른 봇으로 바꾸기</button></div>`;
  return `<div class="seg" style="margin-top:18px"><button data-tgmode="ai" class="${tgMode === "ai" ? "on" : ""}">AI가 만들기</button>
      <button data-tgmode="self" class="${tgMode === "self" ? "on" : ""}">직접 만들기</button></div>
    ${tgMode === "ai" ? tgAiPanel(st) : tgSelfPanel(st)}`;
}
function tgAiPanel(st) {
  const T = (S.tasks || {}).tgbot, run = T && T.state === "running";
  const NAMES = { claude: ["Claude", "Anthropic"], codex: ["Codex", "OpenAI"] };
  const [who, firm] = NAMES[tgAgent] || ["고른 AI", "그 AI 회사(Claude는 Anthropic, Codex는 OpenAI)"];
  if (tgAgents === null) return `<p class="sub" style="margin-top:14px">확인 중</p>`;
  if (!tgAgents.length) return `<p class="note" style="margin-top:14px">AI가 만들려면 Claude나 Codex에 로그인되어 있어야 합니다. Gemini는 브라우저를 다루지 못합니다. 직접 만들기를 쓰세요.</p>`;
  if (run) return `<div class="card soft" style="margin-top:14px"><div class="progress"><i style="width:${T.pct}%"></i></div><div class="sub mono">${esc(T.text)}</div></div>`;
  if (!st.telegram_chat_id && (st.telegram_token || (T && T.state === "waiting"))) {
    const linkHref = safeUrl(tgBotUrl || (st.telegram_bot ? "https://t.me/" + encodeURIComponent(st.telegram_bot) : ""));
    const warn = T && T.warning ? `<div class="err" style="margin-top:10px">${esc(T.warning)}</div>` : "";
    return `<div class="card soft" style="margin-top:14px">
      <div class="row"><span class="pill warn"><i></i>연결 대기${st.telegram_bot ? " @" + esc(st.telegram_bot) : ""}</span>
        <span class="sub">아직 봇 대화가 연결되지 않았습니다.</span></div>
      <p style="margin:12px 0 8px">아래 봇 링크를 눌러 텔레그램에서 <b>시작(START)</b>을 누르면 자동으로 연결됩니다.</p>
      ${linkHref ? `<div class="row" style="margin:10px 0"><a class="btn primary" id="tg-bot-link" href="${esc(linkHref)}" target="_blank" rel="noopener noreferrer">텔레그램에서 봇 열기</a>
        <code id="tg-bot-url">${esc(linkHref)}</code></div>` : `<div class="row" style="margin:10px 0"><button class="btn primary" id="tg-openbot">텔레그램에서 봇 열기</button></div>`}
      ${warn}
      <div class="row" style="margin-top:12px"><button class="btn sm ghost" id="tg-openbot">봇 다시 열기</button><span class="spacer"></span><button class="btn sm ghost" id="tg-redo">다른 봇으로 바꾸기</button></div></div>`;
  }
  if (T && T.state === "done") return `<div class="card soft" style="margin-top:14px"><div class="sub mono">${esc(T.text)}</div></div>`;
  if (tgOpened) return `<div class="card soft" style="margin-top:14px">
      ${T && T.state === "error" ? `<div class="err" style="margin:0 0 12px">${esc(T.error)}</div>` : `<ol class="how"><li><span>방금 열린 브라우저 창에 텔레그램 웹 QR 코드가 보입니다.</span></li>
        <li><span>휴대폰 텔레그램에서 <b>설정 &gt; 기기 &gt; 데스크톱 기기 연결</b>을 누르고 그 QR을 찍습니다.</span></li>
        <li><span>브라우저 창에 대화 목록이 보이면 아래 칸을 채우고(비워도 됩니다) 버튼을 누릅니다. 브라우저 창은 닫지 마세요.</span></li></ol>`}
      <div class="form2">
        <div class="field"><label>봇 이름 (비우면 "Entry Pilot 알림")</label><input class="input" id="tg-name" maxlength="64" value="${esc(tgName)}" placeholder="예: 내 응모 알림" ${tgRetry === "username" ? "" : "autofocus"}></div>
        <div class="field"><label>봇 아이디 (비우면 무작위)</label><input class="input" id="tg-user" maxlength="32" value="${esc(tgUser)}" placeholder="영문, 숫자, _ 5~32자, 끝이 bot" ${tgRetry === "username" ? "autofocus" : ""}></div></div>
      <div class="row"><button class="btn sm ghost" id="tg-ai-web">텔레그램 웹 창 다시 열기</button>${tgRetry ? `<button class="btn sm ghost" id="tg-ai-cancel">그만하기</button>` : ""}<span class="spacer"></span>
        <button class="btn dark" id="tg-ai-go">${T && T.state === "error" ? "다시 시도" : `로그인했어요. ${who}에게 맡기기`}</button></div></div>`;
  return `<div class="card soft consent" style="margin-top:14px">
      <b>어떤 AI에게 맡길까요</b>
      <div class="choice" style="grid-template-columns:repeat(${tgAgents.length},1fr);margin:8px 0 14px">${tgAgents.map(a => `<div class="opt ${tgAgent === a ? "on" : ""}" data-tgagent="${a}"><h3>${NAMES[a][0]}</h3><p>로그인된 ${NAMES[a][0]} 계정으로 합니다.</p><span class="tick"></span></div>`).join("")}</div>
      <h3>AI에게 맡기기 전에 읽어 주세요</h3>
      <b>무엇을 하나요</b>
      <ul><li>Entry Pilot 브라우저 창에 텔레그램 웹이 열립니다. 휴대폰 텔레그램으로 QR 코드를 찍어 로그인합니다.</li>
        <li>로그인된 ${who}가 BotFather 대화에서 봇을 만들고, 받은 토큰으로 Entry Pilot에 연결합니다.</li>
        <li>끝나면 ${who}가 텔레그램 웹에서 로그아웃하고, Entry Pilot이 그 브라우저에 남은 텔레그램 웹 데이터를 지웁니다.</li></ul>
      <b>내 정보는 어디로 가나요</b>
      <ul><li>Entry Pilot에는 개발자가 운영하는 서버가 없고, 프로그램 안에 개발자에게 정보를 보내는 코드가 없습니다.</li>
        <li>Entry Pilot이 직접 연결하는 곳은 텔레그램(api.telegram.org), 설치할 때의 Node.js(nodejs.org), Git(github.com), AI 프로그램(registry.npmjs.org), Gemini 키 확인(Google), 내가 넣은 메일 서버, 응모할 이벤트 사이트입니다.</li>
        <li>${who}가 텔레그램 웹 화면을 읽으면, 읽은 내용은 ${firm} 서버로 보내져 처리됩니다. ${who}를 직접 쓸 때와 같습니다. 개발자는 이 내용을 받지 않습니다.</li>
        <li>봇 토큰은 이 PC에 Windows 암호화로 저장되고 텔레그램에만 보냅니다.</li></ul>
      <b>지시한 것과 막지 못하는 것</b>
      <ul><li>${who}에게는 BotFather 대화와 새 봇 대화만 열고, 다른 대화, 연락처, 설정은 열거나 읽지 말라고 지시합니다. 지시 전문은 앱 폴더의 _internal\\prompts\\telegram-bot.md 에 있습니다.</li>
        <li>이것은 AI에게 주는 지시입니다. 로그인된 텔레그램 웹에서 AI가 다른 대화를 열지 못하게 막는 기술 장치는 없습니다.</li>
        <li>AI가 한 일은 data\\logs\\telegram-bot.log 에 남습니다. 끝난 뒤 휴대폰 텔레그램의 설정 &gt; 기기에서 연결된 기기를 직접 확인할 수 있습니다.</li>
        <li>맡기기 싫으면 위의 직접 만들기를 쓰세요.</li></ul>
      <label class="agree"><input type="checkbox" id="tg-agree" ${tgAgree ? "checked" : ""}> 위 내용을 읽었고 ${who}에게 맡기는 데 동의합니다</label>
      <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn dark" id="tg-ai-web" ${tgAgree && tgAgent ? "" : "disabled"}>동의하고 텔레그램 웹 열기</button></div></div>`;
}
function tgSelfPanel(st) {
  const tok = !!st.telegram_token;
  const linkHref = tok ? safeUrl(tgBotUrl || (st.telegram_bot ? "https://t.me/" + encodeURIComponent(st.telegram_bot) : "")) : "";
  return `<div class="guide" style="margin-top:14px">
      <div class="row"><b>${guideAt + 1} / ${GUIDE.length}</b><span class="sub" style="flex:1">${GUIDE[guideAt]}</span></div>
      <img src="/guide/tg${guideAt + 1}.png" alt="">
      <div class="row"><button class="btn sm ghost" id="guide-prev" ${guideAt ? "" : "disabled"}>이전 그림</button>
        ${guideAt === 0 ? `<button class="btn sm" id="tg-botfather">텔레그램에서 BotFather 열기</button>` : ""}
        <span class="spacer"></span>${tok ? `<span class="pill warn"><i></i>연결 대기 @${esc(st.telegram_bot || "")}</span>${linkHref ? `<a class="btn sm primary" id="tg-bot-link" href="${esc(linkHref)}" target="_blank" rel="noopener noreferrer">봇 열기</a>` : ""}<button class="btn sm ghost" id="tg-openbot">봇 다시 열기</button>` : `<span class="pill"><i></i>토큰 기다리는 중</span>`}
        <button class="btn sm ghost" id="guide-next" ${guideAt < GUIDE.length - 1 ? "" : "disabled"}>다음 그림</button></div>
    </div>
    ${!tok && tgClip ? `<div class="card soft" style="margin-top:10px"><div class="row">
      <div class="txt" style="flex:1"><b>복사된 봇: ${esc(tgClip.name || tgClip.username)}${tgClip.username ? ` (@${esc(tgClip.username)})` : ""}</b>
        <div class="sub">방금 만든 봇이 맞으면 확인을 눌러 저장하고 연결하세요.</div></div>
      <button class="btn sm primary" id="tg-confirm">확인</button></div></div>` : ""}
    <details style="margin-top:6px"><summary class="sub" style="cursor:pointer">토큰을 직접 붙여 넣기</summary>
      <div class="row" style="margin-top:8px"><input class="input" id="tg-token" placeholder="123456789:AA..." style="flex:1"><button class="btn sm" id="tg-save">확인</button></div></details>`;
}
// e-mail: the user picks "직접 만들기" (default, assumption) or "AI가 만들기" (Gmail only)
let mailMode = "self", mailAddr = null, mailAgree = false, mailOpened = false, mailAgent = "", mailAgents = null, mailNeed = "", mailProv = "", mailClip = null;
const mailProvider = a => {
  if (!a || !a.includes("@")) return "";
  const d = a.split("@").pop().trim().toLowerCase();
  return { "gmail.com": "google", "googlemail.com": "google", "naver.com": "naver", "daum.net": "daum", "hanmail.net": "daum" }[d] || "other";
};
function mailPanel(st) {
  if (mailAddr === null) mailAddr = st.email_address || "";
  mailProv = mailProvider(mailAddr);
  return `<div class="seg" style="margin-top:18px"><button data-mailmode="self" class="${mailMode === "self" ? "on" : ""}">직접 만들기</button>
      <button data-mailmode="ai" class="${mailMode === "ai" ? "on" : ""}">AI가 만들기</button></div>
    <div class="form2" style="margin-top:14px">
      <div class="field"><label>보내는 Gmail</label><input class="input" id="em-addr" type="email" placeholder="me@gmail.com" value="${esc(mailAddr)}"></div>
      ${mailMode === "self" ? `<div class="field"><label>Gmail 앱 비밀번호</label><input class="input" id="em-pass" type="password" placeholder="${st.email_password ? "저장됨 (바꾸려면 새로 입력)" : "16자리"}"></div>` : ""}
      <div class="field"><label>알림 받을 이메일 (아무 주소, 비우면 보내는 Gmail)</label><input class="input" id="em-to" type="email" placeholder="예: me@naver.com" value="${esc(st.email_to || "")}"></div></div>
    <p class="note">알림은 Gmail에서 보냅니다. 받는 주소는 네이버, 다음, 회사 메일 등 아무 주소나 됩니다.</p>
    <div id="em-help">${mailHelp(st)}</div>
    ${mailMode === "self" ? `<div class="row" style="margin-top:12px"><button class="btn" id="em-save">확인 메일 보내기</button>${st.email_password ? `<span class="pill ok"><i></i>연결됨</span>` : ""}</div>`
      : (st.email_password ? `<div class="row" style="margin-top:12px"><span class="pill ok"><i></i>연결됨 ${esc(st.email_address || "")}</span></div>` : "")}`;
}
function mailHelp(st) {
  return mailMode === "self" ? mailSelf(st, mailProvider(mailAddr)) : mailAi(st, mailProvider(mailAddr));
}
function mailSelf(st, prov) {
  const store = `<p class="note">비밀번호는 Windows 암호화(DPAPI)로 이 PC에만 저장하고 메일 서버에만 보냅니다.</p>`;
  if (!prov) return `<p class="sub" style="margin-top:12px">위에 보내는 Gmail 주소를 먼저 입력하세요.</p>`;
  if (prov === "google") return `<div class="guide" style="margin-top:12px">
      <ol class="how"><li><span>Google 계정에 2단계 인증이 켜져 있어야 합니다. 꺼져 있으면 먼저 켭니다.</span></li>
        <li><span>앱 비밀번호 화면에서 앱 이름에 <b>Entry Pilot</b>을 쓰고 <b>만들기</b>를 누릅니다.</span></li>
        <li><span>나온 16자리 비밀번호를 복사하면 화면에 가려진 비밀번호가 나옵니다. 확인을 누르면 확인 메일을 보냅니다. 직접 붙여 넣어도 됩니다.</span></li></ol>
      <div class="row"><button class="btn sm" data-mailopen="apppw">Google 앱 비밀번호 화면 열기</button>
        <button class="btn sm ghost" data-mailopen="twosv">2단계 인증 화면 열기</button><span class="spacer"></span>
        <span class="pill"><i></i>복사한 비밀번호 기다리는 중</span></div>
      ${mailClip ? `<div class="card soft" style="margin-top:10px"><div class="row">
        <div class="txt" style="flex:1"><b>복사된 앱 비밀번호: <code>${esc(mailClip.masked)}</code></b>
          <div class="sub">방금 만든 앱 비밀번호가 맞으면 확인을 눌러 확인 메일을 보내고 연결하세요.</div></div>
        <button class="btn sm primary" id="em-confirm">확인</button></div></div>` : ""}
      <img src="/guide/gm1.png" alt=""><img src="/guide/gm2.png" alt="">
    </div>${store}`;
  return `<p class="note" style="margin-top:12px">보내는 메일은 Gmail만 됩니다. Gmail 주소를 넣으세요. 알림을 받는 주소는 아래 칸에 아무 주소나 넣으면 됩니다.</p>`;
}
function mailAi(st, prov) {
  const T = (S.tasks || {}).mailbot, run = T && T.state === "running";
  const NAMES = { claude: ["Claude", "Anthropic"], codex: ["Codex", "OpenAI"] };
  const [who, firm] = NAMES[mailAgent] || ["고른 AI", "그 AI 회사(Claude는 Anthropic, Codex는 OpenAI)"];
  if (!prov) return `<p class="sub" style="margin-top:12px">위에 이메일 주소를 먼저 입력하세요.</p>`;
  if (prov !== "google") return `<p class="note" style="margin-top:12px">보내는 메일은 Gmail만 됩니다. 위의 직접 만들기를 쓰세요.</p>`;
  if (mailAgents === null) return `<p class="sub" style="margin-top:12px">확인 중</p>`;
  if (!mailAgents.length) return `<p class="note" style="margin-top:12px">AI가 만들려면 Claude나 Codex에 로그인되어 있어야 합니다. Gemini는 브라우저를 다루지 못합니다. 직접 만들기를 쓰세요.</p>`;
  if (run) return `<div class="card soft" style="margin-top:12px"><div class="progress"><i style="width:${T.pct}%"></i></div><div class="sub mono">${esc(T.text)}</div></div>`;
  if (T && T.state === "done" && st.email_password) return `<div class="card soft" style="margin-top:12px"><div class="sub mono">${esc(T.text)}</div></div>`;
  if (mailOpened) return `<div class="card soft" style="margin-top:12px">
      ${T && T.state === "error" ? `<div class="err" style="margin:0 0 12px">${esc(T.error)}</div>` : `<ol class="how"><li><span>방금 열린 브라우저 창에 Google 로그인이 보입니다. 비밀번호와 2단계 인증은 직접 입력합니다. AI는 입력하지 않습니다.</span></li>
        <li><span>앱 비밀번호 화면이 보이면 아래 버튼을 누릅니다. 브라우저 창은 닫지 마세요.</span></li></ol>`}
      <div class="row"><button class="btn sm ghost" id="em-ai-web">Google 창 다시 열기</button>
        ${mailNeed === "twosv" ? `<button class="btn sm" data-mailopen="twosv">2단계 인증 화면 열기</button>` : ""}<span class="spacer"></span>
        <button class="btn dark" id="em-ai-go">${T && T.state === "error" ? "다시 시도" : `로그인했어요. ${who}에게 맡기기`}</button></div></div>`;
  return `<div class="card soft consent" style="margin-top:12px">
      <b>어떤 AI에게 맡길까요</b>
      <div class="choice" style="grid-template-columns:repeat(${mailAgents.length},1fr);margin:8px 0 14px">${mailAgents.map(a => `<div class="opt ${mailAgent === a ? "on" : ""}" data-mailagent="${a}"><h3>${NAMES[a][0]}</h3><p>로그인된 ${NAMES[a][0]} 계정으로 합니다.</p><span class="tick"></span></div>`).join("")}</div>
      <h3>AI에게 맡기기 전에 읽어 주세요</h3>
      <b>무엇을 하나요</b>
      <ul><li>Entry Pilot 브라우저 창에 Google 앱 비밀번호 화면이 열립니다. Google 로그인과 2단계 인증은 내가 직접 합니다.</li>
        <li>로그인된 ${who}가 그 화면에서 이름이 Entry Pilot인 앱 비밀번호 하나를 만들고, 나온 16자리를 Entry Pilot에 넘깁니다.</li>
        <li>Entry Pilot이 그 비밀번호로 확인 메일을 보내 연결합니다. 끝나면 ${who}가 그 화면의 탭을 닫습니다.</li>
        <li>2단계 인증이 꺼져 있으면 ${who}는 멈추고, Entry Pilot이 2단계 인증 화면을 여는 버튼을 보여 줍니다. ${who}가 보안 설정을 바꾸지 않습니다.</li></ul>
      <b>내 정보는 어디로 가나요</b>
      <ul><li>Entry Pilot에는 개발자가 운영하는 서버가 없고, 프로그램 안에 개발자에게 정보를 보내는 코드가 없습니다.</li>
        <li>${who}가 Google 앱 비밀번호 화면을 읽으면, 읽은 내용은 ${firm} 서버로 보내져 처리됩니다. ${who}를 직접 쓸 때와 같습니다. 개발자는 이 내용을 받지 않습니다.</li>
        <li>앱 비밀번호는 이 PC에 Windows 암호화(DPAPI)로 저장되고 메일 서버(smtp.gmail.com)에만 보냅니다.</li>
        <li>AI가 넘기는 동안 비밀번호는 data\\work 폴더의 임시 파일에 잠깐 있다가 지워집니다. ${who}에게는 비밀번호를 화면에 출력하지 말라고 지시했고, Entry Pilot은 실행 기록에서 비밀번호를 지웁니다. ${who} 프로그램이 따로 남기는 자체 대화 기록까지 지우지는 못합니다.</li></ul>
      <b>지시한 것과 막지 못하는 것</b>
      <ul><li>${who}에게는 앱 비밀번호 화면만 열고, Gmail, 다른 Google 화면, 다른 앱 비밀번호, 설정은 열거나 읽지 말라고 지시합니다. Google 비밀번호와 2단계 인증 코드는 입력하지 말라고 지시합니다. 지시 전문은 앱 폴더의 _internal\\prompts\\email-app-password.md 에 있습니다.</li>
        <li>이것은 AI에게 주는 지시입니다. 로그인된 Google 브라우저에서 AI가 다른 Google 화면을 열지 못하게 막는 기술 장치는 없습니다.</li>
        <li>AI가 한 일은 data\\logs\\email-app-password.log 에 남습니다.</li>
        <li>되돌리려면 같은 Google 앱 비밀번호 화면에서 Entry Pilot 항목을 삭제합니다. 그러면 저장된 비밀번호는 쓸 수 없게 됩니다.</li>
        <li>맡기기 싫으면 위의 직접 만들기를 쓰세요.</li></ul>
      <label class="agree"><input type="checkbox" id="em-agree" ${mailAgree ? "checked" : ""}> 위 내용을 읽었고 ${who}에게 맡기는 데 동의합니다</label>
      <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn dark" id="em-ai-web" ${mailAgree && mailAgent ? "" : "disabled"}>동의하고 Google 앱 비밀번호 화면 열기</button></div></div>`;
}
async function pollMailbot() {
  for (;;) {
    await new Promise(r => setTimeout(r, 2000));
    await refresh();
    const T = (S.tasks || {}).mailbot;
    if (route === "setup" && step === 3) { const h = $("#em-help"); if (h) h.innerHTML = mailHelp(S.settings); }
    if (!T || T.state !== "running") {
      if (T && T.state === "error") { const i = await api("/api/email/info"); mailNeed = i.need || ""; render(); }
      if (T && T.state === "done") { toast("이메일 연결됨. 확인 메일을 보냈습니다."); render(); }
      return;
    }
  }
}
// while the e-mail panel is open with a Gmail address (own way): detect a copied Google app password and ask to confirm
let mailTimer = null;
function watchMail() {
  if (mailTimer) return;
  mailTimer = setInterval(wrap(async () => {
    const st = S.settings, on = route === "setup" && step === 3 && st.notify === "email" && mailMode === "self" && mailProvider(mailAddr || "") === "google";
    if (!on) { clearInterval(mailTimer); mailTimer = null; return; }
    const r = await api("/api/email/clip?address=" + encodeURIComponent(mailAddr));
    if (r.found && r.ok && r.pending) {
      if (!mailClip || mailClip.masked !== r.masked) {
        mailClip = { masked: r.masked || "" };
        const h = $("#em-help"); if (h) h.innerHTML = mailHelp(S.settings); else render();
      }
    } else if (mailClip) {
      mailClip = null;
      const h = $("#em-help"); if (h) h.innerHTML = mailHelp(S.settings); else render();
    }
  }), 2000);
}
async function pollTgbot() {
  for (;;) {
    await new Promise(r => setTimeout(r, 2000));
    await refresh();
    const T = (S.tasks || {}).tgbot;
    if (route === "setup" && step === 3) $("#step-body").innerHTML = stepBody();
    if (!T || T.state !== "running") {
      if (T && T.state === "error") { const i = await api("/api/tgbot/info"); tgRetry = i.retry || ""; render(); }
      else if (S.settings.telegram_chat_id) { tgBotUrl = ""; toast("텔레그램 연결됨"); render(); }
      else if (T && (T.state === "waiting" || T.state === "done")) {
        const lnk = await api("/api/telegram/link");
        if (lnk && lnk.url) tgBotUrl = lnk.url;
        toast("연결 대기: 텔레그램에서 봇의 시작(START)을 누르세요.");
        await render();
        watchTelegram();
      }
      return;
    }
  }
}
// while the Telegram panel is open: check for a copied bot token (user confirms before save), then wait for Start on the bot
let tgTimer = null;
function watchTelegram() {
  if (tgTimer) return;
  tgTimer = setInterval(wrap(async () => {
    const st = S.settings, on = route === "setup" && step === 3 && st.notify === "telegram" && !st.telegram_chat_id;
    if (!on) { clearInterval(tgTimer); tgTimer = null; return; }
    const T = (S.tasks || {}).tgbot;
    if (T && T.state === "running") return;
    if (!st.telegram_token) {
      if (tgMode !== "self") return;
      const r = await api("/api/telegram/clip");
      if (r.found && r.ok) {
        if (!tgClip || tgClip.username !== r.username || tgClip.name !== r.name) {
          tgClip = { name: r.name || "", username: r.username || "" };
          render();
        }
      } else if (r.found && !r.ok) {
        if (r.error) toast(r.error, true);
      } else if (tgClip) {
        tgClip = null;
        render();
      }
      return;
    }
    const r = await api("/api/telegram/find", { renew: true });
    if (r.ok) { tgBotUrl = ""; toast("텔레그램 연결됨"); await refresh(); render(); return; }
    if (r.bot_url && r.bot_url !== tgBotUrl) {
      const hadUrl = !!tgBotUrl;
      tgBotUrl = r.bot_url;
      await refresh();
      render();
      if (r.renewed && hadUrl) toast("연결 코드가 만료되어 새 링크로 바꿨습니다.");
    }
  }), 2000);
}

const acctN = id => Math.max(1, Math.min(5, +((S.settings.accounts || {})[id] || 1)));
function loginRow(a, l) {
  if (a.id === "gemini") return `<div class="login-row">
    <div class="logo">${a.mark}</div>
    <div style="flex:1"><h3>${a.name}</h3><div class="who">${l.logged_in ? "API 키 저장됨" : "API 키 필요 (aistudio.google.com 에서 무료 발급)"}</div>
      ${l.logged_in ? "" : `<div class="row" style="margin-top:8px"><input class="input" id="gm-key" placeholder="AIza..." style="flex:1;max-width:420px"><button class="btn sm" id="gm-save">키 저장</button></div>`}</div>
    ${l.logged_in ? `<span class="pill ok"><i></i>연결됨</span><button class="btn sm ghost" data-logout="gemini">지우기</button>` : ""}
  </div>`;
  const rows = (l.accounts && l.accounts.length ? l.accounts : [{ i: 1, logged_in: l.logged_in, who: l.who, installed: l.installed }]);
  return rows.map(r => `<div class="login-row">
    <div class="logo">${a.mark}</div>
    <div style="flex:1"><h3>${a.name}${rows.length > 1 ? ` <span class="sub">계정 ${r.i}</span>` : ""}</h3>
      <div class="who">${r.logged_in ? "로그인됨 " + esc(r.who || "") : (r.installed === false ? "설치 안 됨" : "대기")}</div></div>
    ${r.logged_in ? `<span class="pill ok"><i></i>연결됨</span><button class="btn sm ghost" data-logout="${a.id}" data-i="${r.i}">로그아웃</button>` : ""}
  </div>`).join("");
}

const PF = [["name", "이름", "text", "홍길동"], ["birth", "생년월일", "date", ""], ["gender", "성별", "select", ["", "남", "여"]],
  ["phone", "휴대폰", "tel", "010-0000-0000"], ["email", "이메일", "email", "me@example.com"], ["postcode", "우편번호", "text", ""],
  ["address", "주소", "text", "경품 받을 주소"], ["residence_region", "사는 지역", "text", "예: 서울시 마포구"],
  ["occupation", "직업", "text", "예: 대학생, 회사원"], ["job_role", "직무", "text", "선택"],
  ["school", "학교", "text", "공모전 양식에 씀, 선택"], ["major", "전공", "text", "선택"], ["grade", "학년", "text", "선택"]];
let PROFILE = {};
function profileFields() {
  return PF.map(([k, label, type, ph]) => `<div class="field"><label>${label}</label>${type === "select"
    ? `<select class="input" name="${k}">${ph.map(o => `<option ${PROFILE[k] === o ? "selected" : ""}>${o}</option>`).join("")}</select>`
    : `<input class="input" name="${k}" type="${type}" placeholder="${ph}" value="${esc(PROFILE[k] || "")}">`}</div>`).join("") +
    `<div class="field"><label>인스타그램 아이디</label><input class="input" name="sns.instagram" placeholder="선택" value="${esc((PROFILE.sns || {}).instagram || "")}"></div>
     <div class="field"><label>친구 태그용 인스타 아이디</label><input class="input" name="friend_tags" placeholder="예: @friend1 @friend2 (태그 이벤트에 씀, 선택)" value="${esc(PROFILE.friend_tags || "")}"></div>
     <div class="field"><label>유튜브 채널명</label><input class="input" name="sns.youtube" placeholder="선택" value="${esc((PROFILE.sns || {}).youtube || "")}"></div>`;
}
function readProfileForm() {
  const f = new FormData($("#pf")), out = { sns: {} };
  for (const [k, v] of f.entries()) { if (k.startsWith("sns.")) out.sns[k.slice(4)] = v; else out[k] = v; }
  return out;
}

// ---------- dashboard ----------
function home() {
  const e = S.engine || {}, st = e.states || {};
  const todo = (st.owner || 0) + (st.manual || 0) + (e.xlsx_pending || 0);
  return `
  <section class="hero">
    <div class="eyebrow">대시보드</div>
    <h1>${e.running || (S.settings.modes.contests && (S.contest_engine || {}).running) ? "응모 중" : "멈춤"}</h1>
    ${todo ? `<p class="lead">확인할 일 <b>${todo}건</b></p>` : ""}
    <div class="row center" style="margin-top:26px">
      ${e.running || (S.settings.modes.contests && (S.contest_engine || {}).running) ? `<button class="btn" id="stop">멈추기</button>` : `<button class="btn primary" id="start">시작 <span class="arrow">→</span></button>`}
      ${todo ? `<button class="btn" data-go="todo">확인할 일 보기</button>` : ""}
    </div>
  </section>
  ${S.settings.modes.contests ? contestCard() : ""}
  <div class="grid g4">
    ${stat("오늘 응모", e.today_submitted || 0, "건")}${stat("전체 응모", e.submitted || 0, "건")}
    ${stat("진행 중", (st.new || 0) + (st.verifying || 0) + (st.ready || 0) + (st.submitting || 0), "건")}${stat("당첨", e.wins || 0, "건")}
  </div>
  <div class="section">
    <div class="section-head"><div><div class="eyebrow">기록</div><h2>진행 상황</h2></div><span class="spacer"></span>
      ${(e.open_errors || []).length ? `<span class="pill bad"><i></i>오류 ${(e.open_errors || []).length}건</span>` : ""}</div>
    <div class="live">
      <div class="live-head"><span class="chip">엔진</span><span class="chip ${e.running ? "ok" : ""}">${e.running ? "실행 중" : "멈춤"}</span>
        <span class="chip">AI 작업 ${e.active_runs || 0}개</span>
        <span class="streaming ${e.running ? "" : "off"}"><i></i>${e.running ? "실시간" : "대기"}</span></div>
      <pre id="live-log">불러오는 중</pre>
    </div>
  </div>`;
}
function contestCard() {
  const ce = S.contest_engine || {}, cs = ce.states || {};
  const ans = (cs.proposal || 0) + (cs.review || 0);
  return `<div class="card soft" style="margin:16px 0"><div class="row"><div class="txt" style="flex:1"><div class="eyebrow">공모전</div>
      <b>${ce.running ? "실행 중" : "멈춤"}</b><div class="sub">답할 것 ${ans}건, 진행 중 ${(cs.new || 0) + (cs.verifying || 0) + (cs.draft || 0) + (cs.drafting || 0) + (cs.ready || 0) + (cs.submitting || 0)}건, 제출 ${cs.submitted || 0}건</div></div>
      <button class="btn sm ${ans ? "dark" : ""}" data-go="contests">공모전 보기</button></div></div>`;
}
const stat = (k, n, u) => `<div class="card stat"><div class="eyebrow">${k}</div><div class="n">${n}<small>${u}</small></div></div>`;

async function paintLog() {
  const el = $("#live-log"); if (!el) return;
  const r = await api("/api/log");
  const lines = (r.engine || []).slice(-40);
  el.innerHTML = lines.length ? lines.map(l => {
    const m = l.match(/^(\S+ \S+|\d\d:\d\d:\d\d)\s+(.*)$/);
    return m ? `<span class="t">${esc(m[1])}</span>  ${esc(m[2])}` : esc(l);
  }).join("\n") : `<span class="t">기록 없음</span>`;
  el.scrollTop = el.scrollHeight;
}

// ---------- todo ----------
async function todo() {
  return `
  <section class="hero small"><h2 style="font-size:34px">확인할 일</h2>
    <p class="lead">AI가 묻는 것, 본인인증처럼 직접 해야 하는 일</p>
    <div class="row center" style="margin-top:20px"><div class="seg">
      <button data-todo="owner" class="${todoState === "owner" ? "on" : ""}">AI가 묻는 것</button>
      <button data-todo="manual" class="${todoState === "manual" ? "on" : ""}">직접 할 일</button>
      <button data-todo="submitted" class="${todoState === "submitted" ? "on" : ""}">응모 완료</button></div></div>
  </section>
  <div id="xlsx-pending"></div>
  <div class="items" id="items"><div class="empty">불러오는 중</div></div>`;
}
const NEED = { login: "로그인 필요", identity: "본인인증", payment: "결제", sns: "SNS 활동", upload: "파일 올리기", app: "앱 전용", kakao: "카카오톡", residency: "거주 조건", local_phone: "현지 번호", other: "직접 확인" };
const XLSX_KIND = { done: "완료 표시", skip: "제외 표시", answer: "답변 입력" };
function renderXlsxPending(pending) {
  if (!pending.length) return "";
  return `<div class="card soft" style="margin-bottom:18px">
    <div class="row" style="margin-bottom:12px">
      <div class="txt" style="flex:1"><b>엑셀에서 바꾼 항목 ${pending.length}건</b>
        <div class="sub">직접_참여_목록.xlsx 에서 적힌 변경입니다. 확인을 눌러야 반영됩니다.</div></div>
      <button class="btn sm dark" id="xlsx-confirm-all">모두 확인</button>
    </div>
    <div class="items">${pending.map(p => {
      const u = safeUrl(p.url);
      return `<div class="item"><div>
        <div class="t">${esc(p.title || p.item_id)}</div>
        <div class="m"><span>${esc(p.item_id)}</span>${p.org ? `<span>${esc(p.org)}</span>` : ""}<span>${esc(XLSX_KIND[p.kind] || p.kind)}</span>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" style="color:var(--blue)">페이지 열기</a>` : ""}</div>
        <div class="q">${esc(p.text)}</div>
      </div><div class="row"><button class="btn sm primary" data-xlsx-confirm="${esc(p.item_id)}" data-xlsx-ver="${esc(p.version)}" data-xlsx-hash="${esc(p.hash || p.content_hash || "")}">확인</button></div></div>`;
    }).join("")}</div>
  </div>`;
}
async function paintItems() {
  const box = $("#items"); if (!box) return;
  const r = await api(`/api/items?state=${todoState}&limit=80`);
  const pending = r.xlsx_pending || [];
  xlsxShown = pending.map(p => ({ id: p.item_id || p.id, version: +p.version, hash: p.hash || p.content_hash || "" }));
  const xp = $("#xlsx-pending");
  if (xp) xp.innerHTML = renderXlsxPending(pending);
  const list = r.items || [];
  if (!list.length) { box.innerHTML = `<div class="empty">${todoState === "submitted" ? "응모한 이벤트 없음" : "확인할 일 없음"}</div>`; return; }
  box.innerHTML = list.map(it => {
    const u = safeUrl(it.url);
    return `
    <div class="item"><div>
      <div class="t">${esc(it.title || it.id)}</div>
      <div class="m">${it.org ? `<span>${esc(it.org)}</span>` : ""}${it.deadline ? `<span>마감 ${esc(it.deadline)}</span>` : ""}
        ${it.need ? `<span>${esc(NEED[it.need] || it.need)}</span>` : ""}${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" style="color:var(--blue)">페이지 열기</a>` : ""}</div>
      ${it.question || it.reason ? `<div class="q">${esc(it.question || it.reason)}</div>` : ""}
      ${todoState === "owner" ? `<div class="row" style="margin-top:10px"><input class="input" id="ans-${esc(it.id)}" placeholder="AI에게 줄 답 (비우면 그대로 다시 시도)" style="flex:1;height:36px"></div>` : ""}
    </div><div class="row">${todoState === "owner"
      ? `<button class="btn sm dark" data-act="retry" data-id="${esc(it.id)}">답하고 다시 시도</button><button class="btn sm ghost" data-act="skip" data-id="${esc(it.id)}">제외</button>`
      : todoState === "manual" ? `<button class="btn sm dark" data-act="done" data-id="${esc(it.id)}">완료</button><button class="btn sm ghost" data-act="skip" data-id="${esc(it.id)}">제외</button>`
      : `<span class="pill ok"><i></i>${esc(it.submitted_by || "")}</span>`}</div></div>`;
  }).join("");
}

// ---------- contests: the user's review of every analysis and finished work ----------
let cxId = "";   // the contest item open in the tab ("" = the list)
const CX_GROUPS = [["answer", "답할 것", "분석서와 완성작입니다. 통과를 눌러야 다음 단계로 갑니다. 제출은 완성작을 통과시킨 뒤에만 합니다."],
  ["held", "검증 기다리는 분석서", ""], ["owner", "AI가 묻는 것", ""], ["manual", "직접 할 일", ""], ["running", "진행 중", ""], ["done", "제출 완료", ""]];
const dday = n => n === null || n === undefined ? "" : n < 0 ? "마감 지남" : n === 0 ? "오늘 마감" : `D-${n}`;
function contestsView() {
  return `<section class="hero small"><h2 style="font-size:34px">공모전</h2>
    <p class="lead">AI가 찾고 분석하고 작성합니다. 내가 보고 통과를 눌러야 제출합니다.</p></section>
    <div id="cx-box"><div class="empty">불러오는 중</div></div>`;
}
function cxRow(it) {
  const m = [it.org, it.prize, it.deadline ? `마감 ${it.deadline} ${dday(it.days_left)}` : "", it.need, it.work_type].filter(Boolean);
  return `<div class="item"><div>
      <div class="t"><a href="#" data-cx-open="${esc(it.id)}">${esc(it.title)}</a></div>
      <div class="m"><span>${esc(it.id)}</span><span>${esc(it.state_ko)}</span>${m.map(x => `<span>${esc(x)}</span>`).join("")}</div>
      ${it.note && ["owner", "manual", "failed"].includes(it.state) ? `<div class="q">${esc(it.note)}</div>` : ""}
    </div><div class="row"><button class="btn sm ${["proposal", "review"].includes(it.state) ? "dark" : ""}" data-cx-open="${esc(it.id)}">${["proposal", "review"].includes(it.state) ? "보고 답하기" : "보기"}</button></div></div>`;
}
async function paintContests() {
  const box = $("#cx-box"); if (!box) return;
  if (cxId) return paintContestItem(box);
  const r = await api("/api/contest/overview");
  if (r.ok === false) { box.innerHTML = `<div class="err">${esc(r.error)}</div>`; return; }
  const e = S.contest_engine || {};
  const head = `<div class="row" style="margin-bottom:18px">${e.running ? `<span class="pill run"><i></i>공모전 엔진 실행 중</span><button class="btn sm" id="stop">멈추기</button>`
    : `<span class="pill"><i></i>멈춤</span><button class="btn sm primary" id="start">시작</button>`}</div>`;
  const groups = CX_GROUPS.filter(([k]) => (r[k] || []).length).map(([k, name, sub]) => `
    <div class="section" style="margin-top:34px"><div class="section-head"><div><h2>${name} <span class="sub">${r[k].length}건</span></h2>${sub ? `<p class="sub">${sub}</p>` : ""}</div></div>
    <div class="items">${r[k].slice(0, 60).map(cxRow).join("")}</div></div>`).join("");
  box.innerHTML = head + (groups || `<div class="empty">아직 공모전이 없습니다. 시작을 누르면 AI가 찾기 시작합니다.</div>`);
}
function cxUnit(u) {
  const src = x => `/api/contest/${x}&t=${encodeURIComponent(TOKEN)}`;
  const head = `<div class="eyebrow" style="margin:18px 0 8px">${esc(u.label)}</div>`;
  if (u.type === "target") {
    const recips = Array.isArray(u.to) ? u.to : (u.to ? [u.to] : []);
    return head + `<div class="card soft"><table class="cx-tbl">${u.how ? `<tr><th>제출 방법</th><td>${esc(u.how)}</td></tr>` : ""}${recips.length ? `<tr><th>받는 곳</th><td style="word-break:break-all">${recips.map(x => `<div><code>${esc(x)}</code></div>`).join("")}</td></tr>` : ""}</table></div>`;
  }
  if (u.type === "fields") return head + `<div class="card soft"><table class="cx-tbl">${u.rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table></div>`;
  if (u.type === "mail") return head + `<div class="card soft">${u.subject ? `<b>제목: ${esc(u.subject)}</b>` : ""}<pre class="cx-pre">${esc(u.body)}</pre></div>`;
  if (u.type === "image") return head + `<img class="cx-img" loading="lazy" alt="${esc(u.label)}" src="${esc(src(u.url))}">`;
  if (u.type === "attachment") return head + `<div class="card soft"><table class="cx-tbl"><tr><th>파일명</th><td style="word-break:break-all">${esc(u.name)}</td></tr><tr><th>크기</th><td>${esc(u.size != null ? Number(u.size).toLocaleString() + " 바이트" : "")}</td></tr><tr><th>SHA-256</th><td class="mono" style="word-break:break-all">${esc(u.sha256 || "")}</td></tr></table></div>`;
  if (u.type === "text") return head + `<div class="card soft"><pre class="cx-pre">${esc(u.text)}</pre></div>`;
  return head + `<div class="note">${esc(u.text)}</div>`;
}
async function paintContestItem(box) {
  const r = await api("/api/contest/item?id=" + encodeURIComponent(cxId));
  if (r.ok === false || !r.item) { cxId = ""; return render(); }
  const it = r.item, st = r.status || {};
  const rows = r.rows.map(x => `<div class="setting"><div class="txt"><b>${esc(x.label)}</b></div>
      <div style="flex:3;word-break:break-all">${Array.isArray(x.value) ? `<ul style="margin:0;padding-left:18px">${x.value.map(v => `<li>${esc(v)}</li>`).join("")}</ul>` : esc(x.value)}</div></div>`).join("");
  let answer = "";
  if (r.answerable) {
    const rev = it.state === "review";
    answer = `<div class="card" style="margin-top:22px"><div class="eyebrow">${rev ? "완성작" : "분석서"}</div><h3 style="margin-top:6px">${rev ? "이대로 제출해도 될까요" : "이 방향으로 작성해도 될까요"}</h3>
      <p class="sub">${rev ? "통과를 누르면 AI가 위 내용 그대로 제출합니다." : "통과를 누르면 AI가 작품을 쓰기 시작합니다. 작품은 다시 보여 드립니다."} 수정은 메모에 고칠 내용을 적습니다.</p>
      <textarea class="input" id="cx-note" rows="3" style="width:100%;height:auto;padding:10px 12px;margin:12px 0" placeholder="고칠 내용 (수정을 누를 때만 필요)"></textarea>
      <div class="row"><button class="btn dark" data-cx-yes="${esc(it.id)}" data-cx-hash="${esc(r.draft_hash || "")}">통과</button><button class="btn" data-cx-revise="${esc(it.id)}">수정</button>
        <span class="spacer"></span><button class="btn ghost danger" data-cx-skip="${esc(it.id)}">안 함</button></div></div>`;
  } else if (it.state === "proposal") {
    answer = `<div class="note">다른 AI가 검증하는 중입니다. 끝나면 답할 수 있습니다.</div>`;
  } else if (it.state === "owner" || it.state === "failed") {
    answer = `<div class="card" style="margin-top:22px"><div class="eyebrow">AI가 묻는 것</div>${st.question || it.note ? `<p>${esc(st.question || it.note)}</p>` : ""}
      <input class="input" id="cx-note" placeholder="AI에게 줄 답 (비우면 그대로 다시 시도)" style="width:100%;margin:10px 0">
      <div class="row"><button class="btn dark" data-cx-act="retry" data-cx-id="${esc(it.id)}">답하고 다시 시도</button><button class="btn ghost" data-cx-act="skip" data-cx-id="${esc(it.id)}">제외</button></div></div>`;
  } else if (it.state === "manual") {
    answer = `<div class="card" style="margin-top:22px"><div class="eyebrow">직접 할 일</div><p>${esc(it.need || "직접 확인")}${it.note ? ": " + esc(it.note) : ""}</p>
      <p class="sub">보낼 내용은 아래 파일에 있습니다. 직접 낸 뒤 완료를 누르세요.</p>
      <div class="row"><button class="btn dark" data-cx-act="done" data-cx-id="${esc(it.id)}">완료</button><button class="btn ghost" data-cx-act="skip" data-cx-id="${esc(it.id)}">제외</button></div></div>`;
  }
  const status = [it.state === "submitted" ? `제출 완료 ${st.submitted_at || ""}` : "", st.evidence ? `확인 기록: ${st.evidence}` : "",
    st.review_note ? `내가 요청한 수정: ${st.review_note}` : "", st.analysis_note ? `내가 요청한 재분석: ${st.analysis_note}` : ""].filter(Boolean);
  const itemUrl = safeUrl(it.url);
  box.innerHTML = `
    <div class="row" style="margin-bottom:14px"><button class="btn sm ghost" data-cx-back>← 목록</button><span class="pill"><i></i>${esc(it.state_ko)}</span>
      ${it.days_left !== null ? `<span class="pill ${it.days_left <= 3 ? "bad" : ""}"><i></i>${esc(dday(it.days_left))}</span>` : ""}</div>
    <div class="card"><div class="eyebrow">${esc(it.id)} ${esc(it.org)}</div><h2 style="margin:6px 0 4px">${itemUrl ? `<a href="${esc(itemUrl)}" target="_blank" rel="noopener noreferrer" style="color:inherit">${esc(it.title)}</a>` : esc(it.title)}</h2>
      ${r.note ? `<p class="sub">${esc(r.note)}</p>` : ""}${rows}
      ${status.map(x => `<div class="note">${esc(x)}</div>`).join("")}</div>
    ${r.dossier ? `<details class="card soft" style="margin-top:16px"><summary style="cursor:pointer"><b>분석서 전체</b>${r.dossier.translated ? "" : ` <span class="sub">(한국어 번역 준비 중, 지금은 원문)</span>`}</summary><pre class="cx-pre">${esc(r.dossier.text)}</pre></details>` : ""}
    ${(r.units || []).length ? `<h3 style="margin:26px 0 0">보내는 내용</h3>${r.units.map(cxUnit).join("")}` : ""}
    ${(r.files || []).length ? `<div class="card soft" style="margin-top:18px"><div class="row"><div class="txt" style="flex:1"><b>보낼 파일</b><div class="sub">${r.files.map(f => esc(f.name)).join(", ")}</div></div>
      ${r.folder ? `<button class="btn sm" data-cx-folder>폴더 열기</button>` : ""}</div></div>` : ""}
    ${answer}`;
}

// ---------- settings ----------
const INTEGRATIONS = [["lch_wiki", "LCH-WIKI", "지식 기록 도구"], ["llm_relay", "LLM-RELAY", "결정을 묻고 답을 받는 도구"], ["llm_console", "LLM-CONSOLE", "AI 창을 모아 보는 도구"]];
function settingsView() {
  const st = S.settings, L = S.logins || {};
  return `
  <section class="hero small"><h2 style="font-size:34px">설정</h2></section>
  <div class="grid g2">
    <div class="card"><div class="eyebrow">AI</div><h3 style="margin-top:6px">연결된 AI</h3>
      ${AGENTS.map(a => `<div class="setting"><div class="txt"><b>${a.name}</b><div class="sub">${(L[a.id] || {}).logged_in ? "로그인됨" : "로그인 안 됨"}</div></div>
        <label class="switch"><input type="checkbox" data-agent-sw="${a.id}" ${st.agents[a.id] ? "checked" : ""}><span></span></label></div>`).join("")}
      <div class="row" style="margin-top:8px"><button class="btn sm" data-goto-step="1">설치와 로그인 다시 하기</button></div></div>
    <div class="card"><div class="eyebrow">동작</div><h3 style="margin-top:6px">한 번에 돌릴 AI 작업 수</h3>
      <div class="setting"><div class="txt"><div class="sub">PC가 느려지면 줄이세요. 기본 3</div></div>
        <select class="input" id="max-agents" style="width:90px">${[1, 2, 3, 4, 5, 6].map(n => `<option ${+st.max_agents === n ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div class="setting"><div class="txt"><b>응모 종류</b><div class="sub">${[st.modes.events ? "경품 이벤트" : "", st.modes.contests ? "공모전" : ""].filter(Boolean).join(", ")}</div></div><button class="btn sm" data-goto-step="0">바꾸기</button></div>
      <div class="setting"><div class="txt"><b>알림</b><div class="sub">${notifyText()}</div></div><button class="btn sm" data-goto-step="3">바꾸기</button></div>
      <div class="setting"><div class="txt"><b>공모전 고르기</b><div class="sub">${esc(contestText())}</div></div><button class="btn sm" data-goto-step="6">바꾸기</button></div>
      <div class="setting"><div class="txt"><b>내 정보</b><div class="sub">응모 양식에 쓰는 정보</div></div><button class="btn sm" data-goto-step="4">고치기</button></div>
      <div class="setting"><div class="txt"><b>사이트 로그인</b><div class="sub">응모용 브라우저의 로그인</div></div><button class="btn sm" data-goto-step="5">열기</button></div></div>
  </div>
  <div id="unbound-secrets"></div>
  <div class="card" style="margin-top:16px"><div class="eyebrow">외부 도구 연결</div><h3 style="margin-top:6px">나중에 연결할 도구</h3>
    <p class="sub">공개 전의 도구라서 아직 연결할 수 없습니다. 연결 자리만 만들어 두었습니다.</p>
    ${INTEGRATIONS.map(([k, n, d]) => `<div class="setting"><div class="txt"><b>${n}</b><div class="sub">${d}</div></div>
      <span class="pill"><i></i>아직 지원하지 않음</span><label class="switch" title="아직 지원하지 않음"><input type="checkbox" disabled><span></span></label></div>`).join("")}</div>
  ${S.portable ? `<div class="card soft" style="margin-top:16px"><div class="row"><div class="txt" style="flex:1"><b>포터블판</b><div class="sub">도구, 로그인, 기록을 모두 이 폴더에 저장합니다. 지우려면 앱을 닫고 폴더를 지우세요.</div></div>
    <button class="btn sm" id="open-folder-p">폴더 열기</button></div></div>` : `<div class="card soft" style="margin-top:16px"><div class="row"><div class="txt" style="flex:1"><b>Entry Pilot 지우기</b><div class="sub">설치한 도구, 로그인, 기록을 모두 지웁니다.</div></div>
    <button class="btn sm danger" id="uninstall">삭제</button></div></div>`}`;
}
async function paintUnboundSecrets() {
  const box = $("#unbound-secrets"); if (!box) return;
  const r = await api("/api/secrets/unbound");
  const rows = [];
  if (Array.isArray(r.events) || Array.isArray(r.contests)) {
    (r.events || []).forEach(k => rows.push({ key: k, engine: "events" }));
    (r.contests || []).forEach(k => rows.push({ key: k, engine: "contests" }));
  } else {
    (r.keys || []).forEach(k => rows.push({ key: k, engine: "" }));
  }
  box.innerHTML = `<div class="card" style="margin-top:16px"><div class="eyebrow">사이트 키 도메인 연결</div><h3 style="margin-top:6px">미지정 사이트 키</h3>
    <p class="sub">secrets.env에 넣은 키는 사용할 정확한 https 로그인 호스트(예: example.co.kr)를 여기서 직접 연결해야 입력됩니다.</p>
    ${rows.length ? rows.map((it, i) => `<div class="setting"><div class="txt"><b><code>${esc(it.key)}</code></b>${it.engine === "contests" ? ` <span class="sub">(공모전)</span>` : ""}</div>
      <div class="row"><input class="input" id="bind-dom-${i}" placeholder="예: example.co.kr" style="width:240px;height:34px">
        <button class="btn sm primary" data-bind-key="${esc(it.key)}" data-bind-engine="${esc(it.engine)}" data-bind-idx="${i}">연결</button></div></div>`).join("")
      : `<div class="sub" style="margin-top:10px">도메인이 지정되지 않은 사이트 키가 없습니다.</div>`}</div>`;
}

// ---------- log ----------
function logView() {
  return `<section class="hero small"><h2 style="font-size:34px">기록</h2></section>
  <div class="live"><div class="live-head"><span class="chip">scheduler.log</span></div><pre id="log-engine"></pre></div>
  <div class="live" style="margin-top:16px"><div class="live-head"><span class="chip">app.log</span></div><pre id="log-app"></pre></div>`;
}
async function paintFullLog() {
  if (!$("#log-engine")) return;
  const r = await api("/api/log");
  $("#log-engine").textContent = (r.engine || []).join("\n") || "기록 없음";
  $("#log-app").textContent = (r.app || []).join("\n") || "기록 없음";
}

// ---------- render + events ----------
async function render() {
  const v = $("#view");
  const wiz = route === "setup" || !S.settings.setup_done;
  if (wiz ? step === 6 || step === 7 : route === "settings") await loadContest();
  if (!S.settings.setup_done) { route = "setup"; if (step === 4) PROFILE = await api("/api/profile"); v.innerHTML = wizard(); }
  else if (route === "todo") { v.innerHTML = await todo(); paintItems(); }
  else if (route === "settings") { v.innerHTML = settingsView(); paintUnboundSecrets(); }
  else if (route === "contests" && S.settings.modes.contests) { v.innerHTML = contestsView(); paintContests(); }
  else if (route === "log") { v.innerHTML = logView(); paintFullLog(); }
  else if (route === "setup") { if (step === 4) PROFILE = await api("/api/profile"); v.innerHTML = wizard(); }
  else { route = "home"; v.innerHTML = home(); paintLog(); }
  paintChrome();
  if (route === "setup" && step === 3 && S.settings.notify === "email") {
    watchMail();
    if (mailAgents === null) api("/api/email/info").then(r => { mailAgents = r.agents || []; mailNeed = r.need || ""; render(); }).catch(() => {});
  }
  if (route === "setup" && step === 3 && S.settings.notify === "telegram" && !S.settings.telegram_chat_id) {
    watchTelegram();
    if (tgAgents === null) api("/api/tgbot/info").then(r => { tgAgents = r.agents || []; tgRetry = r.retry || ""; if (r.bot_url) tgBotUrl = r.bot_url; render(); }).catch(() => {});
    else if (S.settings.telegram_token && !tgBotUrl) api("/api/telegram/link").then(r => { if (r && r.url) { tgBotUrl = r.url; render(); } }).catch(() => {});
  }
}
const wrap = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message || String(e), true); } };

document.addEventListener("click", wrap(async ev => {
  const t = ev.target.closest("button, a, .opt, li[data-step], #tg-agree, #em-agree"); if (!t) return;
  if (t.matches("a[href='#']")) ev.preventDefault();
  const d = t.dataset;
  if (t.id === "reopen-window") {
    const c = new URLSearchParams(location.search).get("c") || "";
    if (!c) {
      showLaunchFailure("이 창은 만료됐습니다. 바탕화면이나 시작 메뉴에서 Entry Pilot을 다시 실행하세요.");
      return toast("Entry Pilot을 다시 실행하면 새 창이 열립니다.", true);
    }
    t.disabled = true;
    try {
      await refresh();
      return render();
    } catch (e) {
      showLaunchFailure(e.message || String(e));
      return toast(e.message || String(e), true);
    }
  }
  if (d.go) { route = d.go; if (route !== "setup") await refresh(); return render(); }
  if (d.step !== undefined && route === "setup") { step = +d.step; return render(); }
  if (d.agent && t.classList.contains("opt") && ev.target.closest(".stepper")) return;
  if (d.agent && t.classList.contains("opt")) {
    await api("/api/settings", { agents: { [d.agent]: !S.settings.agents[d.agent] } }); await refresh(); return render();
  }
  if (d.mode) {
    const nextVal = !S.settings.modes[d.mode];
    const m = { ...S.settings.modes, [d.mode]: nextVal };
    if (!m.events && !m.contests) return toast("하나는 켜져 있어야 합니다", true);
    await api("/api/settings", { modes: { [d.mode]: nextVal } }); await refresh(); return render();
  }
  if (d.interest) {
    const cur = (S.settings.interests && typeof S.settings.interests === "object") ? S.settings.interests : {};
    await api("/api/settings", { interests: { [d.interest]: !cur[d.interest] } }); await refresh(); return render();
  }
  if (d.cxOpen !== undefined) { cxId = d.cxOpen; return render(); }
  if ("cxBack" in d) { cxId = ""; return render(); }
  if ("cxFolder" in d) { const r = await api("/api/contest/open-folder", { id: cxId }); if (r.ok === false) toast(r.error, true); return; }
  if (d.cxYes !== undefined || d.cxRevise !== undefined || d.cxSkip !== undefined) {
    const id = d.cxYes ?? d.cxRevise ?? d.cxSkip, action = d.cxYes !== undefined ? "yes" : d.cxRevise !== undefined ? "revise" : "skip";
    const note = ($("#cx-note") || {}).value || "";
    if (action === "revise" && !note.trim()) return toast("고칠 내용을 메모에 적어 주세요", true);
    if (action === "skip" && !confirm("이 공모전을 안 하기로 할까요?")) return;
    t.disabled = true;
    const payload = { id, action, note };
    if (action === "yes" && d.cxHash !== undefined) payload.draft_hash = d.cxHash;
    const r = await api("/api/contest/decide", payload);
    if (!r.ok) {
      t.disabled = false;
      toast(r.error || "실패", true);
      if (r.stale || r.refresh) { await refresh(); return render(); }
      return;
    }
    toast(r.message); cxId = r.next || ""; await refresh(); return render();
  }
  if (d.cxAct) {
    t.disabled = true; const ans = $("#cx-note");
    const r = await api("/api/contest/act", { action: d.cxAct, id: d.cxId, note: ans ? ans.value : "" });
    if (r.ok === false) { t.disabled = false; return toast(r.error || "실패", true); }
    toast("처리함"); cxId = ""; await refresh(); return render();
  }
  if (d.acct) {
    const n = Math.max(1, Math.min(5, acctN(d.acct) + +d.d));
    await api("/api/settings", { accounts: { [d.acct]: n } }); await refresh(); return render();
  }
  if (t.id === "cx-open") return api("/api/logins/open-url", {});
  if (t.id === "login-start") { await api("/api/logins/start", {}); pollLogins(); await refresh(); return render(); }
  if ("next" in d) { step = Math.min(STEPS.length - 1, step + 1); await refresh(); await render(); if (step === 3 && S.settings.notify === "telegram") watchTelegram(); return autoLogin(); }
  if ("prev" in d) { step = Math.max(0, step - 1); return render(); }
  if (d.gotoStep) { step = +d.gotoStep; route = "setup"; return render(); }
  if (t.id === "install") { await api("/api/install", {}); pollInstall(); return render(); }
  if (d.login) { await api(`/api/login/${d.login}`, {}); toast("로그인 창을 열었습니다"); pollLogin(d.login); return; }
  if (d.logout) { await api(`/api/logout/${d.logout}`, { i: +(d.i || 1) }); await refresh(); return render(); }
  if (t.id === "login-refresh") { await api("/api/logins/refresh"); await refresh(); return render(); }
  if (t.id === "gm-save") { const r = await api("/api/gemini-key", { key: $("#gm-key").value }); if (!r.ok) return toast(r.error || "키 확인 실패", true); toast("Gemini 키 저장됨"); await refresh(); return render(); }
  if (d.notify) { await api("/api/settings", { notify: d.notify }); await refresh(); await render(); if (d.notify === "telegram") watchTelegram(); if (d.notify === "email") watchMail(); return; }
  if (d.mailmode) { mailMode = d.mailmode; if (mailMode === "self") watchMail(); return render(); }
  if (d.mailopen) { await api("/api/email/open", { what: d.mailopen }); if (d.mailopen === "apppw") watchMail(); return; }
  if (d.mailagent) { mailAgent = d.mailagent; return render(); }
  if (t.id === "em-agree") { mailAgree = t.checked; return render(); }
  if (t.id === "em-ai-web") { t.disabled = true; const r = await api("/api/email/open-google", {}); mailOpened = true; mailNeed = ""; if (r.ok === false) toast(r.error || "창을 열지 못했습니다", true); await refresh(); return render(); }
  if (t.id === "em-ai-go") {
    const r = await api("/api/email/start", { agent: mailAgent, address: mailAddr || "" });
    if (r.ok === false) return toast(r.error, true);
    mailNeed = ""; pollMailbot(); await refresh(); return render();
  }
  if (d.tgmode) { tgMode = d.tgmode; if (tgMode === "self") watchTelegram(); return render(); }
  if (t.id === "guide-prev") { guideAt = Math.max(0, guideAt - 1); return render(); }
  if (t.id === "guide-next") { guideAt = Math.min(GUIDE.length - 1, guideAt + 1); return render(); }
  if (t.id === "tg-agree") { tgAgree = t.checked; return render(); }
  if (t.id === "tg-ai-web") { t.disabled = true; const r = await api("/api/tgbot/open-web", {}); tgOpened = true; if (r.ok === false) toast(r.error || "창을 열지 못했습니다", true); await refresh(); return render(); }
  if (d.tgagent) { tgAgent = d.tgagent; return render(); }
  if (t.id === "tg-ai-go") {
    tgName = ($("#tg-name") || {}).value || ""; tgUser = ($("#tg-user") || {}).value || "";
    const r = await api("/api/tgbot/start", { agent: tgAgent, name: tgName, username: tgUser });
    if (r.ok === false) return toast(r.error, true);
    tgRetry = ""; pollTgbot(); await refresh(); return render();
  }
  if (t.id === "tg-ai-cancel") {
    const r = await api("/api/tgbot/cancel", {});
    tgOpened = false; tgRetry = "";
    if (r.ok && r.cleared !== false) {
      toast(r.message || "텔레그램 웹 데이터를 지웠습니다. 휴대폰 텔레그램 설정 > 기기에서 Telegram Web 세션도 끊으세요.");
    } else {
      toast(r.error || r.message || "브라우저의 텔레그램 웹 기록을 지우지 못했습니다. 열린 브라우저와 휴대폰 설정 > 기기에서 세션을 직접 종료하세요.", true);
    }
    await refresh(); return render();
  }
  if (t.id === "tg-redo") { if (!confirm("지금 봇 연결을 끊고 새로 만들까요?")) return; await api("/api/telegram/reset", {}); tgOpened = false; tgAgree = false; tgClip = null; tgBotUrl = ""; await refresh(); return render(); }
  if (t.id === "tg-botfather") { await api("/api/telegram/open", { what: "botfather" }); return watchTelegram(); }
  if (t.id === "tg-openbot") { const r = await api("/api/telegram/open", { what: "bot" }); if (r && r.bot_url) { tgBotUrl = r.bot_url; render(); } return watchTelegram(); }
  if (t.id === "tg-confirm") {
    t.disabled = true;
    const r = await api("/api/telegram/confirm", { username: (tgClip || {}).username || "" });
    t.disabled = false;
    if (!r.ok) return toast(r.error || "토큰 확인 실패", true);
    tgClip = null;
    if (r.bot_url) tgBotUrl = r.bot_url;
    toast(`@${r.username} 봇 확인. 텔레그램에서 시작을 누르세요.`);
    await refresh(); await render(); return watchTelegram();
  }
  if (t.id === "tg-save") { const r = await api("/api/telegram/token", { token: $("#tg-token").value }); if (!r.ok) return toast(r.error || "토큰 확인 실패", true); tgClip = null; if (r.bot_url) tgBotUrl = r.bot_url; toast(`@${r.username} 봇 확인. 텔레그램에서 시작을 누르세요.`); await refresh(); await render(); return watchTelegram(); }
  if (t.id === "em-confirm") {
    t.disabled = true;
    const addr = (($("#em-addr") || {}).value || mailAddr || "").trim();
    const r = await api("/api/email/confirm", { address: addr, masked: (mailClip || {}).masked || "" });
    t.disabled = false;
    mailClip = null;
    if (!r.ok) { const h = $("#em-help"); if (h) h.innerHTML = mailHelp(S.settings); return toast(r.error || "연결 실패", true); }
    toast("확인 메일을 보냈습니다");
    await refresh(); return render();
  }
  if (t.id === "em-save") { t.disabled = true; const r = await api("/api/email/connect", { address: ($("#em-addr").value || "").trim(), password: $("#em-pass").value }); t.disabled = false; if (!r.ok) return toast(r.error || "연결 실패", true); mailClip = null; toast("확인 메일을 보냈습니다"); await refresh(); return render(); }
  if (t.id === "tg-test") { const r = await api("/api/telegram/test", {}); return toast(r.ok ? "보냄" : r.error, !r.ok); }
  if (t.id === "pf-save") { ev.preventDefault(); await api("/api/profile", readProfileForm()); toast("저장함"); step++; await refresh(); return render(); }
  if (d.site) { t.disabled = true; const r = await api("/api/site-login", { site: d.site, engine: d.engine || "events" }); t.disabled = false; return toast(r.ok === false ? (r.error || "창을 열지 못했습니다") : "로그인 창을 열었습니다", r.ok === false); }
  if (d.bindKey) {
    const inp = document.getElementById("bind-dom-" + d.bindIdx);
    const domain = (inp ? inp.value : "").trim();
    if (!domain) return toast("로그인 도메인을 입력하세요", true);
    t.disabled = true;
    const r = await api("/api/secrets/bind", { key: d.bindKey, domain, engine: d.bindEngine || "" });
    t.disabled = false;
    if (!r || !r.ok) return toast((r && r.error) || "도메인 연결 실패", true);
    toast(`${d.bindKey} → ${r.domain} 연결됨`);
    return paintUnboundSecrets();
  }
  if (d.ctfield) { readWon(); const f = CT.contest_fields; CT.contest_fields = f.includes(d.ctfield) ? f.filter(x => x !== d.ctfield) : [...f, d.ctfield]; return render(); }
  if (d.ctkey) { readWon(); CT[d.ctkey] = d.ctval; return render(); }
  if ("ctalloff" in d) { readWon(); const off = !allOffline(); CT_ROWS.forEach(([k]) => { CT[k] = off ? "exclude" : CONTEST.values[k] === "exclude" ? "allow" : CONTEST.values[k]; }); return render(); }
  if (t.id === "ct-save") {
    readWon();
    const r = await api("/api/contest", CT);
    if (r.ok === false) return toast(r.error, true);
    CONTEST = r; CT = JSON.parse(JSON.stringify(r.values)); toast("저장함");
    step++; await refresh(); return render();
  }
  if (t.id === "finish") { await api("/api/finish-setup", {}); const r = await api("/api/engine/start", {}); if (r.ok === false) toast(r.error, true); route = "home"; await refresh(); return render(); }
  if (t.id === "start") { const r = await api("/api/engine/start", {}); if (r.ok === false) toast(r.error, true); else toast("시작함"); await refresh(); return render(); }
  if (t.id === "stop") { await api("/api/engine/stop", {}); toast("진행 중인 작업을 마치고 멈춥니다"); await refresh(); return render(); }
  if (d.todo) { todoState = d.todo; return render(); }
  if (d.xlsxConfirm) {
    t.disabled = true;
    const r = await api("/api/xlsx/confirm", { id: d.xlsxConfirm, version: +d.xlsxVer, hash: d.xlsxHash || "" });
    if (!r || r.ok === false) toast((r && r.error) || "실패", true); else toast("반영함");
    await refresh(); return paintItems();
  }
  if (t.id === "xlsx-confirm-all") {
    t.disabled = true;
    const items = xlsxShown.slice();
    if (!items.length) { t.disabled = false; return paintItems(); }
    const r = await api("/api/xlsx/confirm", { items });
    if (!r || r.ok === false) toast((r && r.error) || "실패", true); else toast(`${r.count || 0}건 반영함`);
    await refresh(); return paintItems();
  }
  if (d.act) { t.disabled = true; const ans = document.getElementById("ans-" + d.id); const r = await api("/api/item/act", { action: d.act, id: d.id, note: ans ? ans.value : "" }); if (r.ok === false) toast(r.error || "실패", true); else toast("처리함"); await refresh(); return paintItems(); }
  if (t.id === "uninstall") {
    if (!confirm("Entry Pilot과 로그인, 기록을 모두 지울까요?")) return;
    t.disabled = true;
    const r = await api("/api/uninstall", {});
    if (!r || !r.ok) { t.disabled = false; return toast((r && r.error) || "삭제하지 못했습니다.", true); }
    document.body.innerHTML = '<div class="empty" style="margin-top:30vh">삭제했습니다. 창을 닫으세요.</div>';
    return;
  }
  if (t.id === "open-folder" || t.id === "open-folder-p") { ev.preventDefault(); return api("/api/open-folder", {}); }
}));
document.addEventListener("input", ev => {
  if (ev.target.id !== "em-addr") return;
  mailAddr = ev.target.value;
  const h = $("#em-help"), p = mailProvider(mailAddr);
  if (h && p !== mailProv) { mailProv = p; h.innerHTML = mailHelp(S.settings); if (p === "google") watchMail(); }
});
document.addEventListener("change", wrap(async ev => {
  const t = ev.target;
  if (t.id === "em-to") { const r = await api("/api/email/to", { to: t.value.trim() }); if (r.ok === false) return toast(r.error, true); await refresh(); }
  if (t.dataset.agentSw) { await api("/api/settings", { agents: { [t.dataset.agentSw]: t.checked } }); await refresh(); }
  if (t.dataset.modeSw) { await api("/api/settings", { modes: { [t.dataset.modeSw]: t.checked } }); await refresh(); }
  if (t.dataset.interestSw) { await api("/api/settings", { interests: { [t.dataset.interestSw]: t.checked } }); await refresh(); }
  if (t.id === "max-agents") { await api("/api/settings", { max_agents: +t.value }); toast("다음 시작부터 적용됩니다"); await refresh(); }
}));
addEventListener("scroll", () => $(".nav").classList.toggle("scrolled", scrollY > 4));

async function pollInstall() {
  for (;;) {
    await new Promise(r => setTimeout(r, 1500));
    await refresh();
    const T = (S.tasks || {}).install;
    if (route === "setup" && step === 1) $("#step-body").innerHTML = stepBody();
    if (!T || T.state !== "running") {
      if (T && T.state === "done" && route === "setup" && step === 1) { step = 2; await render(); autoLogin(); }
      return;
    }
  }
}
// entering the login step starts the login windows by itself when some account is not logged in yet
async function autoLogin() {
  if (route !== "setup" || step !== 2) return;
  const st = S.settings, L = S.logins || {}, Q = (S.tasks || {}).logins;
  if (Q && (Q.state === "running" || Q.state === "error")) return;
  if (!AGENTS.some(a => st.agents[a.id] && a.id !== "gemini" && !(L[a.id] || {}).all_in)) return;
  await api("/api/logins/start", {}); pollLogins(); await refresh(); render();
}
async function pollLogins() {
  for (;;) {
    await new Promise(r => setTimeout(r, 2000));
    await refresh();
    const Q = (S.tasks || {}).logins;
    if (route === "setup" && step === 2) $("#step-body").innerHTML = stepBody();
    if (!Q || Q.state !== "running") {
      await api("/api/logins/refresh"); await refresh();
      if (route === "setup" && step === 2) {
        if (Q && Q.state === "done" && stepDone(2)) { step = 3; toast("로그인됨"); if (S.settings.notify === "telegram") watchTelegram(); }
        render();
      }
      return;
    }
  }
}
async function pollLogin(agent) {
  for (let i = 0; i < 120; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const st = await api(`/api/login/${agent}`);
    if (st.logged_in) { await api("/api/logins/refresh"); await refresh(); if (route === "setup") render(); toast("로그인됨"); return; }
  }
}

// heartbeat keeps the app alive while this window is open; light auto refresh on the dashboard
setInterval(() => { if (TOKEN) api("/api/heartbeat", {}).catch(() => {}); }, 5000);
setInterval(wrap(async () => {
  if (!TOKEN || !S) return;
  if (route === "home") { await refresh(); const st = S.engine || {}; const h = $(".hero h1"); if (h) h.textContent = st.running || (S.settings.modes.contests && (S.contest_engine || {}).running) ? "응모 중" : "멈춤"; paintLog(); }
  if (route === "todo") paintItems();
  if (route === "contests" && !cxId) { await refresh(); paintContests(); }
}), 8000);
const SPLASH_MIN_MS = 1600;  // the start screen stays at least this long, then fades once the first screen is drawn
const splashAt = Date.now();
function hideSplash() {
  const el = $("#splash");
  if (!el) return;
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 600); }, Math.max(0, SPLASH_MIN_MS - (Date.now() - splashAt)));
}
wrap(async () => {
  try {
    await refresh();
    if (S.tasks && S.tasks.install && S.tasks.install.state === "running") { step = 1; pollInstall(); }
    if (S.tasks && S.tasks.logins && S.tasks.logins.state === "running") { step = 2; pollLogins(); }
    await render();
  } catch (e) {
    showLaunchFailure(e.message || String(e));
    throw e;
  } finally {
    hideSplash();  // also when loading failed: the error toast must be visible
  }
})();
