/* 국내주식 매물대 스크리너 — 정적 대시보드 (외부 라이브러리 없음) */
'use strict';

const PAGE = 60;
const state = {
  all: [],        // 전체 종목
  rows: [],       // 필터·정렬 결과
  shown: 0,
  meta: null,
  filters: {
    market: 'all', profit: 'all', value: false, cum: 'all',
    vwap: false, rsiMin: null, rsiMax: null, div: false, q: '', sort: 'mc'
  },
  preset: 'all',
  period: 5       // 매물대 기준 기간(년): 1~5
};

/* ---------------- 포맷 ---------------- */
const nf = new Intl.NumberFormat('ko-KR');
const fmtInt = v => (v === null || v === undefined || Number.isNaN(v)) ? '–' : nf.format(Math.round(v));

function fmtEok(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  const a = Math.abs(v);
  if (a >= 10000) {
    const jo = v / 10000;
    return (Math.abs(jo) >= 100 ? jo.toFixed(0) : jo.toFixed(1)) + '조';
  }
  return nf.format(Math.round(v)) + '억';
}
const fmtPct = v => (v === null || v === undefined || Number.isNaN(v)) ? '–'
  : (v > 0 ? '+' : '') + v.toFixed(1) + '%';
const fmtDy = v => (v === null || v === undefined || Number.isNaN(v)) ? '–' : v.toFixed(2) + '%';
const signCls = v => (v === null || v === undefined) ? '' : (v > 0 ? 'pos' : (v < 0 ? 'neg' : ''));
const mktName = m => (m === 'K' ? '코스피' : '코스닥');

/* 생성 시각을 한국 시간으로 (CI 러너는 UTC로 기록하므로 브라우저에서 KST로 변환) */
function kstStamp(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return String(iso || '').replace('T', ' ').slice(0, 16);
  const d = new Date(t + 9 * 3600 * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

const pocGap = s => { const v = vpOf(s); return (v && v.poc) ? (s.p / v.poc - 1) * 100 : null; };
const vwapGap = s => { const v = vpOf(s); return (v && v.vwap) ? (s.p / v.vwap - 1) * 100 : null; };

/* 선택 기간(1~5년)의 매물대 값. 해당 기간 거래가 없으면 null */
function vpOf(s) {
  if (s.vp) return s.vp[String(state.period)] || null;
  return (s.poc === undefined && s.cum === undefined) ? null
    : { poc: s.poc, vwap: s.vwap, bl: s.bl, bh: s.bh, bins: s.bins, cum: s.cum };
}
const cumOf = s => { const v = vpOf(s); return (v && v.cum !== undefined && v.cum !== null) ? v.cum : null; };

/* 매물대 위치 3단계: 누적 30% 이하 바닥권 / 70% 초과 상단 / 그 사이 중간 */
const posTier = cum => (cum === null || cum === undefined) ? null
  : (cum <= 30 ? { key: 'low', dot: '🟢', label: '바닥권' }
  : (cum <= 70 ? { key: 'mid', dot: '🟡', label: '중간' }
  : { key: 'high', dot: '🔴', label: '상단' }));
const posBadge = cum => {
  const t = posTier(cum);
  if (!t) return '<span class="pos-badge na">– 자료 없음</span>';
  return `<span class="pos-badge ${t.key}">${t.dot} ${t.label}</span>`;
};
const gapCls = v => (v === null || v === undefined) ? '' : (v <= 0 ? 'good' : 'high');

/* 실적 4색: 🟢 3년 흑자 · 🔵 흑자전환 · 🟡 최근 적자 · 🔴 계속 적자 */
const FIN_TXT = { green: '3년 흑자', blue: '흑자전환', yellow: '최근 적자', red: '계속 적자' };
const FIN_ICON = { green: '🟢', blue: '🔵', yellow: '🟡', red: '🔴' };
const finState = (s, idx) => {          // idx: 1=영업이익, 2=당기순이익
  const vals = (s.fin || []).map(r => r[idx]).filter(v => v !== null && v !== undefined);
  if (!vals.length) return null;
  const last = vals[vals.length - 1];
  let key;
  if (vals.every(v => v > 0)) key = 'green';
  else if (last > 0) key = 'blue';                 // 이전 해에 적자가 있었는데 최근 흑자
  else if (vals.some(v => v > 0)) key = 'yellow';  // 흑자였던 해가 있는데 최근 적자
  else key = 'red';
  const label = key === 'green' && vals.length < 3 ? `${vals.length}년 흑자`
    : (key === 'red' && vals.length < 3 ? '적자' : FIN_TXT[key]);
  return { key, label, icon: FIN_ICON[key], years: vals.length };
};
const finBadge = (st, who, full, extraCls = '') => {
  const tag = who ? `<b>${who}</b>` : '';
  if (!st) return `<span class="fin-badge na ${extraCls}" title="${full} 3개년 확정 실적이 없습니다">${tag}–</span>`;
  return `<span class="fin-badge ${st.key} ${extraCls}" title="${full}: ${st.label} (최근 ${st.years}개 사업연도 확정 실적)">` +
    `${tag}${st.icon} ${st.label}</span>`;
};

/* ---------------- 데이터 로드 ---------------- */
async function load() {
  const res = await fetch('data/stocks.json', { cache: 'no-cache' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const data = await res.json();
  state.meta = data.meta;
  state.all = data.stocks || [];
  initMeta();
  initFilterToggle();
  applyPreset('all');
  bindEvents();
}

function initMeta() {
  const m = state.meta;
  document.getElementById('status').textContent =
    `기준일 ${m.as_of} · ${nf.format(m.count)}종목 · ${m.years}년 일봉 · 매물대 ${state.period}년`;
  document.getElementById('status').classList.remove('warn');
  document.getElementById('total').textContent = nf.format(m.count);
  document.getElementById('generated').textContent =
    `데이터 생성 ${kstStamp(m.generated_at)}` +
    (m.failed ? ` · 상장 직후 등으로 데이터가 부족한 ${m.failed}종목 제외` : '');

  const cnt = {
    all: state.all.length,
    bottom: state.all.filter(s => (cumOf(s) ?? 100) <= 30 && s.rsi > 0 && s.rsi <= 35).length,
    mine: state.all.filter(isMine).length
  };
  document.getElementById('cnt-all').textContent = nf.format(cnt.all);
  document.getElementById('cnt-bottom').textContent = nf.format(cnt.bottom);
  document.getElementById('cnt-mine').textContent = nf.format(cnt.mine);
}

function isMine(s) {
  const v = vpOf(s);
  const op = finState(s, 1);
  return !!op && op.key === 'green' && s.pass === 1 && (cumOf(s) ?? 100) <= 30
    && v && v.vwap && s.p <= v.vwap && s.rsi > 0 && s.rsi <= 35;
}

/* ---------------- 필터 ---------------- */
function filterRows() {
  const f = state.filters;
  const q = f.q.trim().toLowerCase();
  let out = state.all.filter(s => {
    if (f.market !== 'all' && s.m !== f.market) return false;
    if (f.profit !== 'all') {
      const op = finState(s, 1);
      const k = op ? op.key : null;
      if (f.profit === 'op3' && k !== 'green') return false;
      if (f.profit === 'turn' && k !== 'blue') return false;
      if (f.profit === 'pos' && !(k === 'green' || k === 'blue')) return false;
    }
    if (f.value && s.pass !== 1) return false;
    if (f.cum !== 'all') {
      const c = cumOf(s);
      if (c === null) return false;
      if (f.cum === 'low' && c > 30) return false;
      if (f.cum === 'mid' && (c <= 30 || c >= 70)) return false;
      if (f.cum === 'high' && c < 70) return false;
    }
    if (f.vwap) {
      const v = vpOf(s);
      if (!(v && v.vwap && s.p <= v.vwap)) return false;
    }
    if (f.rsiMin !== null || f.rsiMax !== null) {
      if (!(s.rsi > 0)) return false;
      if (f.rsiMin !== null && s.rsi < f.rsiMin) return false;
      if (f.rsiMax !== null && s.rsi > f.rsiMax) return false;
    }
    if (q && !(s.n.toLowerCase().includes(q) || s.c.includes(q))) return false;
    if (f.div && !(s.dps > 0)) return false;
    return true;
  });

  const sorters = {
    mc: (a, b) => (b.mc || 0) - (a.mc || 0),
    div: (a, b) => ((b.dy ?? -1) - (a.dy ?? -1)) || ((b.mc || 0) - (a.mc || 0)),
    cum: (a, b) => ((cumOf(a) ?? 101) - (cumOf(b) ?? 101)) || ((a.rsi ?? 101) - (b.rsi ?? 101)),
    rsi: (a, b) => ((a.rsi ?? 101) - (b.rsi ?? 101)) || ((cumOf(a) ?? 101) - (cumOf(b) ?? 101)),
    mult: (a, b) => ((a.mult ?? 1e9) - (b.mult ?? 1e9)) || ((b.mc || 0) - (a.mc || 0)),
    poc: (a, b) => ((pocGap(a) ?? 1e9) - (pocGap(b) ?? 1e9)),
    vwap: (a, b) => ((vwapGap(a) ?? 1e9) - (vwapGap(b) ?? 1e9)),
    name: (a, b) => a.n.localeCompare(b.n, 'ko')
  };
  out.sort(sorters[f.sort] || sorters.mc);
  state.rows = out;
  state.shown = 0;
  renderRows();
  updateFilterCount();
}

/* ---------------- 렌더 ---------------- */
function rowHtml(s) {
  const vp = vpOf(s) || {};
  const pg = pocGap(s), vg = vwapGap(s);
  const cum = cumOf(s);
  const cumTxt = cum === null || cum === undefined ? '–' : cum;
  const rsi = s.rsi;
  const rsiCls = rsi >= 70 ? 'hot' : (rsi <= 35 ? 'cold' : '');
  const rsiWidth = Math.max(2, Math.min(100, rsi || 0));

  let value;
  if (s.mult) {
    value = `<span class="badge ${s.pass === 1 ? 'ok' : 'no'}">${s.mult.toFixed(1)}배 ${s.pass === 1 ? '통과' : ''}` +
      `<span class="badge-bar"><i style="width:${Math.min(100, s.mult * 10)}%"></i></span></span>`;
  } else {
    value = `<span class="badge na">–</span>`;
  }

  const opSt = finState(s, 1), niSt = finState(s, 2);
  const prof = `<div class="fin">${finBadge(opSt, '영익', '영업이익')}${finBadge(niSt, '순익', '당기순이익')}</div>`;

  const short = s.nd && s.nd < 250 ? `<span class="mini" title="상장 1년 미만: 매물대에 반영된 거래일이 ${s.nd}일뿐입니다">1년 미만</span>` : '';
  const stale = s.ld ? `<span class="mini" title="최근 거래일 ${s.ld}">거래정지?</span>` : '';

  return `<div class="row" data-code="${s.c}" role="row" tabindex="0">
    <div class="cell name">
      <div class="name-line"><span class="stock-name">${esc(s.n)}</span><span class="mkt ${s.m}">${mktName(s.m)}</span>${short}${stale}</div>
      <div class="code-line">${s.c}</div>
    </div>
    <div class="cell num" data-label="현재가"><span class="price">${fmtInt(s.p)}</span></div>
    <div class="cell num" data-label="시가총액">${fmtEok(s.mc)}</div>
    <div class="cell num div-cell" data-label="배당" title="${s.dps > 0 ? `${s.dvy}년 주당배당금 ${fmtInt(s.dps)}원 ÷ 현재가` : '최근 사업연도 배당 없음'}">
      <span class="div-y ${s.dy >= 3 ? 'hi' : ''}">${fmtDy(s.dy)}</span>
      ${s.dps > 0 ? `<span class="sub">${fmtInt(s.dps)}원</span>` : ''}
    </div>
    <div class="cell num" data-label="RSI(14)">
      <div class="rsi"><span class="rsi-val ${rsiCls}">${rsi ? rsi.toFixed(1) : '–'}</span>
      <div class="rsi-track"><div class="rsi-fill" style="width:${rsiWidth}%"></div></div></div>
    </div>
    <div class="cell num pos" data-label="매물대 위치" title="${state.period}년 거래량 누적 ${cumTxt}% · POC ${fmtInt(vp.poc)}원">
      <div class="vp-pos">${posBadge(cum)}
      <span class="pos-gap ${gapCls(pg)}">최대 매물대 ${fmtPct(pg)}</span></div>
    </div>
    <div class="cell num" data-label="${state.period}년 평균 대비"><span class="${signCls(vg)}">${fmtPct(vg)}</span></div>
    <div class="cell num" data-label="영업익×10 vs 시총">${value}</div>
    <div class="cell num" data-label="실적 추이">${prof}</div>
  </div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderRows(append) {
  const box = document.getElementById('rows');
  const next = state.rows.slice(state.shown, state.shown + PAGE);
  const html = next.map(rowHtml).join('');
  if (append) box.insertAdjacentHTML('beforeend', html);
  else box.innerHTML = html;
  state.shown += next.length;

  document.getElementById('shown').textContent = nf.format(state.rows.length);
  document.getElementById('empty').hidden = state.rows.length !== 0;
  const more = document.getElementById('more');
  more.hidden = state.shown >= state.rows.length;
  more.textContent = `더 보기 (${nf.format(Math.min(PAGE, state.rows.length - state.shown))}개) · 남은 ${nf.format(state.rows.length - state.shown)}개`;
}

/* ---------------- 상세: 최근 공시·뉴스 (지연 로드) ----------------
   네이버 API 는 CORS 를 열어주지 않아 브라우저에서 직접 못 부른다.
   update_data.py 가 종목별 공시·뉴스를 16개 파일(data/extras/{n}.json)로 나눠
   두었고, 상세 화면을 열 때 그 종목이 든 파일 하나만 받아 캐시한다. */
const EXT_CACHE = {};

function shardOf(code) {
  const n = state.meta && state.meta.extra ? state.meta.extra.shards : 16;
  let s = 0;
  for (const ch of String(code)) if (ch >= '0' && ch <= '9') s += Number(ch);
  return s % n;
}

function extVersion() {
  const g = (state.meta && state.meta.generated_at) || '';
  return g.replace(/\D/g, '').slice(0, 12) || '0';
}

async function loadExtras(s) {
  const shard = shardOf(s.c);
  try {
    if (!EXT_CACHE[shard]) {
      const res = await fetch(`data/extras/${shard}.json?v=${extVersion()}`, { cache: 'no-cache' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      EXT_CACHE[shard] = await res.json();
    }
    renderExtras(s.c, EXT_CACHE[shard][s.c] || null);
  } catch (err) {
    renderExtras(s.c, null, true);
  }
}

/* 공시 제목 키워드 → 배지 (굵직한 이벤트만) */
const DISC_TAGS = [
  [/유상증자|제3자배정/, '유증', 'red'],
  [/무상증자/, '무증', 'blue'],
  [/자사주|자기주식/, '자사주', 'violet'],
  [/소각/, '소각', 'orange'],
  [/공급계약|단일판매|수주/, '수주', 'green'],
  [/잠정.*실적|실적.*공정공시|영업실적/, '실적', 'teal'],
  [/배당/, '배당', 'amber'],
  [/전환사채|신주인수권/, 'CB', 'slate']
];

function discTags(title) {
  const out = [];
  for (const [re, label, cls] of DISC_TAGS) {
    if (re.test(title) && !out.some(t => t[0] === label)) out.push([label, cls]);
    if (out.length >= 2) break;
  }
  return out.map(([label, cls]) => `<span class="d-tag ${cls}">${label}</span>`).join('');
}

const mmdd = d => (d || '').slice(5).replace('-', '.');

function renderExtras(code, x, failed) {
  const dBox = document.getElementById('d-disc');
  const nBox = document.getElementById('d-news');
  const panel = document.getElementById('detail');
  if (!dBox || !nBox || panel.hidden || panel.dataset.code !== code) return;

  if (failed) {
    const msg = '<p class="muted rload">공시·뉴스를 불러오지 못했습니다.</p>';
    dBox.innerHTML = msg; nBox.innerHTML = msg;
    return;
  }

  const discs = (x && x.d) || [];
  dBox.innerHTML = discs.length
    ? discs.map(r => `<div class="ri"><span class="ri-d">${mmdd(r[0])}</span>` +
        `<span class="ri-t">${discTags(r[1])}${esc(r[1])}</span></div>`).join('')
    : '<p class="muted rload">최근 공시가 없습니다.</p>';

  const news = (x && x.n) || [];
  nBox.innerHTML = news.length
    ? news.map(r => {
        const link = /^\d{3}\/\d+$/.test(r[3] || '') ? `https://n.news.naver.com/mnews/article/${r[3]}` : '';
        const inner = `<span class="ri-d">${mmdd(r[0])}</span>` +
          `<span class="ri-t">${esc(r[1])}<span class="ri-m">${esc(r[2] || '')}</span></span>`;
        return link ? `<a class="ri" href="${link}" target="_blank" rel="noopener">${inner}</a>`
          : `<div class="ri">${inner}</div>`;
      }).join('')
    : '<p class="muted rload">최근 뉴스가 없습니다.</p>';
}

/* ---------------- 상세 패널 ---------------- */
function openDetail(code) {
  const s = state.all.find(x => x.c === code);
  if (!s) return;
  const d = document.getElementById('detail');
  d.dataset.code = code;
  document.getElementById('d-name').textContent = s.n;
  document.getElementById('d-sub').textContent =
    `${s.c} · ${mktName(s.m)} · 일봉 ${nf.format(s.nd || 0)}일 · 매물대 ${state.period}년` + (s.ld ? ` · 최근 거래 ${s.ld}` : '');
  document.getElementById('d-body').innerHTML = detailHtml(s);
  d.hidden = false;
  document.body.style.overflow = 'hidden';
  d.querySelector('.detail-panel').scrollTop = 0;
  d.querySelector('.close').focus({ preventScroll: true });
  loadExtras(s);
}

function closeDetail() {
  document.getElementById('detail').hidden = true;
  document.body.style.overflow = '';
}

function detailHtml(s) {
  const vp = vpOf(s) || {};
  const pg = pocGap(s), vg = vwapGap(s);
  const cum = cumOf(s);
  const cumTxt = cum === null ? '–' : cum;
  const P = state.period;

  /* --- 통계 카드 --- */
  const stats = `
  <div class="price-row"><span class="big">${fmtInt(s.p)}</span><span class="unit">원 · 현재가</span></div>
  <div class="dstats">
    <div class="dstat span2">
      <div class="k">매물대 위치 <span class="muted">· ${P}년</span></div>
      <div class="v">${posBadge(cum)}</div>
      <div class="dsub ${gapCls(pg)}">최대 매물대 대비 ${fmtPct(pg)} <span class="muted">(누적 ${cumTxt}%)</span></div>
    </div>
    <div class="dstat"><div class="k">POC (최대 매물대)</div><div class="v">${fmtInt(vp.poc)}<small>원</small></div></div>
    <div class="dstat"><div class="k">시가총액</div><div class="v">${fmtEok(s.mc)}</div></div>
    <div class="dstat"><div class="k">RSI(14)</div><div class="v ${s.rsi >= 70 ? 'pos' : ''}">${s.rsi ? s.rsi.toFixed(1) : '–'}</div></div>
    <div class="dstat"><div class="k">${P}년 평균단가</div><div class="v">${fmtInt(vp.vwap)}<small>원·${fmtPct(vg)}</small></div></div>
    <div class="dstat span2">
      <div class="k">배당수익률 <span class="muted">· ${s.dps > 0 && s.dvy ? `${s.dvy}년 배당` : '최근 사업연도'}</span></div>
      <div class="v ${s.dy >= 3 ? 'pos' : ''}">${s.dps > 0 ? s.dy.toFixed(2) : '–'}<small>${s.dps > 0 ? '%' : ''}</small></div>
      <div class="dsub">${s.dps > 0 ? `현재가 기준${s.dy >= 3 ? ' · 고배당(3%↑)' : ''}` : '무배당'}</div>
    </div>
    <div class="dstat"><div class="k">주당배당금</div><div class="v">${s.dps > 0 ? fmtInt(s.dps) : '–'}<small>${s.dps > 0 ? '원' : ''}</small></div>
      <div class="dsub">${s.dps > 0 ? `${s.dvy}년` : '무배당'}</div></div>
  </div>`;

  /* --- 매물대 차트 --- */
  const chart = vp.bins && vp.bins.length
    ? `<div class="section-title">${P}년 매물대 <span class="hint">가로 막대 = 거래량 비중 · ${vp.bins.length}구간</span></div>
       <div class="vp">${vpSvg(s)}
         <div class="vp-legend">
           <span><i style="background:var(--cur)"></i>현재가</span>
           <span><i style="background:var(--poc)"></i>POC</span>
           <span><i style="background:var(--vwap)"></i>${P}년 평균</span>
         </div>
       </div>`
    : '';

  /* --- 재무 --- */
  const fin = s.fin && s.fin.length ? s.fin : [];
  const opSt = finState(s, 1), niSt = finState(s, 2);
  const years = fin.map(r => (s.fm && s.fm !== 12 ? `${r[0]}.${String(s.fm).padStart(2, '0')}` : `${r[0]}`));
  const finTable = fin.length ? `
    <table class="fin">
      <thead><tr><th>구분 (억원)</th>${years.map(y => `<th>${y}</th>`).join('')}</tr></thead>
      <tbody>
        <tr><td>영업이익</td>${fin.map(r => `<td class="${r[1] < 0 ? 'neg' : (r[1] === null ? '' : 'pos')}">${fmtInt(r[1])}</td>`).join('')}</tr>
        <tr><td>당기순이익</td>${fin.map(r => `<td class="${r[2] < 0 ? 'neg' : (r[2] === null ? '' : 'pos')}">${fmtInt(r[2])}</td>`).join('')}</tr>
      </tbody>
    </table>`
    : `<p class="muted" style="font-size:13px">연간 실적 데이터가 없습니다(신규 상장·자료 미제공 등).</p>`;

  /* --- 시총 vs 영업이익×10 --- */
  const lastOp = fin.length ? fin[fin.length - 1][1] : null;
  let cmp = `<p class="muted" style="font-size:13px">최근 사업연도 영업이익이 없어 비교할 수 없습니다.</p>`;
  if (lastOp && lastOp > 0) {
    const ten = lastOp * 10;
    const max = Math.max(s.mc, ten) || 1;
    cmp = `<div class="cmp">
      <div class="cmp-row"><span class="lbl">시가총액</span>
        <div class="cmp-track"><div class="cmp-bar mc" style="width:${(s.mc / max * 100).toFixed(1)}%"></div></div>
        <span class="val">${fmtEok(s.mc)}</span></div>
      <div class="cmp-row"><span class="lbl">영업이익×10</span>
        <div class="cmp-track"><div class="cmp-bar ten" style="width:${(ten / max * 100).toFixed(1)}%"></div></div>
        <span class="val">${fmtEok(ten)}</span></div>
      <div class="cmp-note">영업이익 배수(시총÷영업이익) <b>${s.mult ? s.mult.toFixed(2) + '배' : '–'}</b> ·
        ${s.pass === 1 ? '<b>기준 통과</b> (10배 미만)' : '<b>기준 미달</b> (10배 이상)'}</div>
    </div>`;
  }

  const note = [];
  if (s.nd && s.nd < 250) note.push(`상장 1년 미만이라 매물대가 ${s.nd}거래일치만 반영됩니다.`);
  if (s.ld) note.push(`최근 거래일이 ${s.ld}로 기준일과 다릅니다(거래정지·관리종목 등).`);

  return `${stats}${chart}
    <div class="section-title">3년 실적 <span class="hint">네이버 금융 연간 확정 실적</span></div>
    <div class="fin-detail"><span class="fin-k">영업이익</span>${finBadge(opSt, '', '영업이익', 'lg')}<span class="fin-k">순이익</span>${finBadge(niSt, '', '당기순이익', 'lg')}</div>
    ${finTable}
    <div class="section-title">시가총액 vs 영업이익×10</div>
    ${cmp}
    <div class="section-title">최근 공시 <span class="hint">네이버 금융${state.meta && state.meta.extra ? ` · 최근 ${state.meta.extra.disc}건` : ''}</span></div>
    <div class="rlist" id="d-disc"><p class="muted rload">불러오는 중…</p></div>
    <div class="section-title">최근 뉴스 <span class="hint">네이버 금융${state.meta && state.meta.extra ? ` · 최근 ${state.meta.extra.news}건` : ''}</span></div>
    <div class="rlist" id="d-news"><p class="muted rload">불러오는 중…</p></div>
    <div class="links">
      <a class="primary" href="https://finance.naver.com/item/main.naver?code=${s.c}" target="_blank" rel="noopener">네이버 금융</a>
      <a href="https://m.stock.naver.com/domestic/stock/${s.c}/notice" target="_blank" rel="noopener">네이버 공시</a>
      <a href="https://dart.fss.or.kr/dsab007/main.do?option=corp&amp;textCrpNm=${encodeURIComponent(s.n)}" target="_blank" rel="noopener">DART 공시</a>
      <a href="https://finance.naver.com/item/coinfo.naver?code=${s.c}" target="_blank" rel="noopener">기업 실적 분석</a>
    </div>
    ${note.length ? `<p class="dnote">⚠ ${note.join(' ')}</p>` : ''}
    <p class="dnote">매물대는 최근 ${P}년 일별 종가·거래량을 20개 가격대로 나눈 분포입니다. 현재가 이하 누적 비중이 낮을수록 위쪽 매물이 적은 하단 구간입니다. 투자 판단 전 공시로 다시 확인하세요.</p>`;
}

/* 매물대 SVG: 가격은 세로축, 거래량 비중은 가로 막대 */
function vpSvg(s) {
  const W = 470, H = 400;
  const L = 54, R = 74, T = 14, B = 16;
  const vp = vpOf(s) || {};
  const bins = vp.bins || [];
  const n = bins.length;
  const lo = vp.bl, hi = vp.bh;
  const P = state.period;
  const span = hi - lo || 1;
  const innerH = H - T - B;
  const innerW = W - L - R;
  const rowH = innerH / n;
  const maxShare = Math.max(...bins, 1);
  const yOf = price => T + (1 - (price - lo) / span) * innerH;
  const yBin = i => T + (n - 1 - i) * rowH;

  let g = '';
  // 가로 눈금 (5구간 가격 라벨)
  for (let i = 0; i <= n; i += 5) {
    const price = lo + span * i / n;
    const y = yOf(price);
    g += `<line x1="${L}" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#eef1f6" stroke-width="1"/>`;
    g += `<text x="${L - 6}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#98a2b3">${priceLabel(price)}</text>`;
  }
  // 막대
  const pocIdx = bins.indexOf(Math.max(...bins));
  bins.forEach((share, i) => {
    const w = Math.max(1.5, share / maxShare * innerW);
    const y = yBin(i) + rowH * 0.16;
    const h = rowH * 0.68;
    const isPoc = i === pocIdx;
    g += `<rect x="${L}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${isPoc ? '#f59e0b' : '#6366f1'}" opacity="${isPoc ? 0.9 : 0.5}"/>`;
    if (w > 40) {
      g += `<text x="${(L + w - 5).toFixed(1)}" y="${(y + h * 0.75).toFixed(1)}" text-anchor="end" font-size="9.5" fill="${isPoc ? '#7c3a00' : '#3730a3'}">${share.toFixed(0)}%</text>`;
    }
  });

  // 기준선 + 오른쪽 가격 라벨
  const marks = [];
  marks.push({ price: s.p, color: '#e11d48', label: priceLabel(s.p) });
  if (vp.poc) marks.push({ price: vp.poc, color: '#d97706', label: priceLabel(vp.poc) });
  if (vp.vwap) marks.push({ price: vp.vwap, color: '#0284c7', label: priceLabel(vp.vwap) });

  marks.forEach(m => {
    const y = yOf(m.price);
    g += `<line x1="${L}" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${m.color}" stroke-width="1.5" stroke-dasharray="5 3"/>`;
  });

  // 라벨 겹침 회피: 위에서부터 12px 간격 확보
  marks.sort((a, b) => yOf(a.price) - yOf(b.price));
  let prevY = -99;
  marks.forEach(m => {
    let y = yOf(m.price);
    if (y - prevY < 12) y = prevY + 12;
    prevY = y;
    g += `<rect x="${W - R + 2}" y="${(y - 6).toFixed(1)}" width="${R - 8}" height="12" rx="2" fill="${m.color}"/>` +
      `<text x="${(W - R + 2 + (R - 8) / 2).toFixed(1)}" y="${(y + 3.6).toFixed(1)}" text-anchor="middle" font-size="9.5" fill="#fff" font-weight="700">${m.label}</text>`;
  });

  // 축
  g += `<line x1="${L}" x2="${L}" y1="${T}" y2="${H - B}" stroke="#d0d5dd" stroke-width="1"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(s.n)} ${P}년 매물대 차트. 현재가 ${s.p}원, POC ${vp.poc}원, ${P}년 평균단가 ${vp.vwap}원">${g}</svg>`;
}

function priceLabel(v) {
  if (v >= 10000) return Math.round(v / 10000) + '만';
  return nf.format(Math.round(v));
}

/* ---------------- 기간 전환 (1~5년) ---------------- */
function setPeriod(p) {
  if (!(p >= 1 && p <= 5) || state.period === p) return;
  state.period = p;
  document.querySelectorAll('.seg-btn[data-period]').forEach(b =>
    b.classList.toggle('active', Number(b.dataset.period) === p));
  updatePeriodLabels();
  if (state.meta) initMeta();
  filterRows();
  const d = document.getElementById('detail');
  if (!d.hidden && d.dataset.code) openDetail(d.dataset.code);
}

function updatePeriodLabels() {
  const P = state.period;
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('lbl-cum', `${P}년 매물대 위치`);
  set('lbl-vwap', `현재가 ≤ ${P}년 평균단가`);
  set('lbl-sort-vwap', `${P}년 평균 대비 낮은 순`);
  set('lbl-th-vwap', `${P}년 평균 대비`);
  set('lbl-legend-vwap', `${P}년 평균`);
}

/* ---------------- 이벤트 ---------------- */
function readFilters() {
  const f = state.filters;
  f.market = document.getElementById('f-market').value;
  f.profit = document.getElementById('f-profit').value;
  f.value = document.getElementById('f-value').checked;
  f.cum = document.getElementById('f-cum').value;
  f.vwap = document.getElementById('f-vwap').checked;
  f.div = document.getElementById('f-div').checked;
  f.q = document.getElementById('f-q').value;
  f.sort = document.getElementById('f-sort').value;
  const mn = document.getElementById('f-rsi-min').value;
  const mx = document.getElementById('f-rsi-max').value;
  f.rsiMin = mn === '' ? null : Number(mn);
  f.rsiMax = mx === '' ? null : Number(mx);
  syncChips();
}

function syncChips() {
  const f = state.filters;
  document.querySelectorAll('.chip[data-rsi]').forEach(ch => {
    const [a, b] = ch.dataset.rsi.split(',').map(Number);
    ch.classList.toggle('on', f.rsiMin === a && f.rsiMax === b);
  });
}

function activeFilterCount() {
  const f = state.filters;
  let n = 0;
  if (f.market !== 'all') n++;
  if (f.profit !== 'all') n++;
  if (f.value) n++;
  if (f.cum !== 'all') n++;
  if (f.vwap) n++;
  if (f.div) n++;
  if (f.rsiMin !== null || f.rsiMax !== null) n++;
  if (f.q.trim()) n++;
  return n;
}

function updateFilterCount() {
  const n = activeFilterCount();
  const el = document.getElementById('filter-count');
  if (el) el.textContent = n ? `${n}개 조건` : '조건 없음';
}

function setPreset(p) {
  state.preset = p;
  document.querySelectorAll('.preset').forEach(b => b.classList.toggle('active', b.dataset.preset === p));
}

function applyPreset(p, doRender = true) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  const check = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };
  if (p === 'all') {
    set('f-market', 'all'); set('f-profit', 'all'); check('f-value', false);
    set('f-cum', 'all'); check('f-vwap', false); check('f-div', false); set('f-q', '');
    set('f-rsi-min', ''); set('f-rsi-max', '');
  } else if (p === 'bottom') {
    set('f-market', 'all'); set('f-profit', 'all'); check('f-value', false);
    set('f-cum', 'low'); check('f-vwap', false); check('f-div', false); set('f-q', '');
    set('f-rsi-min', ''); set('f-rsi-max', '35');
  } else if (p === 'mine') {
    set('f-market', 'all'); set('f-profit', 'op3'); check('f-value', true);
    set('f-cum', 'low'); check('f-vwap', true); check('f-div', false); set('f-q', '');
    set('f-rsi-min', ''); set('f-rsi-max', '35');
  }
  setPreset(p);
  readFilters();
  if (doRender) filterRows();
  if (window.innerWidth < 900) document.getElementById('filters').classList.add('collapsed');
  refreshFiltersToggle();
}

function initFilterToggle() {
  const box = document.getElementById('filters');
  const btn = document.getElementById('toggle-filters');
  if (!box || !btn) return;
  if (window.innerWidth < 900) box.classList.add('collapsed');
  btn.addEventListener('click', () => {
    box.classList.toggle('collapsed');
    refreshFiltersToggle();
  });
  refreshFiltersToggle();
}

function refreshFiltersToggle() {
  const box = document.getElementById('filters');
  const btn = document.getElementById('toggle-filters');
  if (!box || !btn) return;
  const open = !box.classList.contains('collapsed');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  btn.querySelector('.chev').textContent = open ? '▲' : '▼';
  btn.firstChild.textContent = open ? '접기 ' : '상세 조건 ';
}

function bindEvents() {
  document.querySelectorAll('.seg-btn[data-period]').forEach(btn =>
    btn.addEventListener('click', () => setPeriod(Number(btn.dataset.period))));

  document.querySelectorAll('.preset').forEach(btn =>
    btn.addEventListener('click', () => applyPreset(btn.dataset.preset)));

  ['f-market', 'f-profit', 'f-cum', 'f-sort'].forEach(id =>
    document.getElementById(id).addEventListener('change', () => { setPreset(''); readFilters(); filterRows(); }));
  ['f-value', 'f-vwap', 'f-div'].forEach(id =>
    document.getElementById(id).addEventListener('change', () => { setPreset(''); readFilters(); filterRows(); }));

  let t = null;
  document.getElementById('f-q').addEventListener('input', () => {
    clearTimeout(t); t = setTimeout(() => { setPreset(''); readFilters(); filterRows(); }, 180);
  });
  ['f-rsi-min', 'f-rsi-max'].forEach(id =>
    document.getElementById(id).addEventListener('input', () => {
      clearTimeout(t); t = setTimeout(() => { setPreset(''); readFilters(); filterRows(); }, 250);
    }));

  document.querySelectorAll('.chip[data-rsi]').forEach(ch => ch.addEventListener('click', () => {
    const [a, b] = ch.dataset.rsi.split(',');
    const mn = document.getElementById('f-rsi-min'), mx = document.getElementById('f-rsi-max');
    const on = mn.value === a && mx.value === b;
    mn.value = on ? '' : a;
    mx.value = on ? '' : b;
    setPreset(''); readFilters(); filterRows();
  }));

  document.getElementById('reset').addEventListener('click', () => applyPreset('all'));

  document.getElementById('rows').addEventListener('click', e => {
    const row = e.target.closest('.row');
    if (row) openDetail(row.dataset.code);
  });
  document.getElementById('rows').addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('.row');
    if (row) { e.preventDefault(); openDetail(row.dataset.code); }
  });

  document.getElementById('more').addEventListener('click', () => renderRows(true));

  document.getElementById('detail').addEventListener('click', e => {
    if (e.target.closest('[data-close]')) closeDetail();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.getElementById('detail').hidden) closeDetail();
  });

  ['f-rsi-min', 'f-rsi-max'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('blur', () => {
      if (el.value === '') return;
      let v = Number(el.value);
      if (!Number.isFinite(v)) v = '';
      else v = Math.max(0, Math.min(100, v));
      el.value = v;
    });
  });
}

load().catch(err => {
  const st = document.getElementById('status');
  st.textContent = '데이터를 불러오지 못했습니다: ' + err.message;
  st.classList.add('warn');
  console.error(err);
});
