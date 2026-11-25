#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""emit-client.py — 把设计稿 HTML 转成 DSH 客户端插件模块。

输入： ui/econtone-studio.html
输出： lib/client.js / lib/standalone.html / build/preview.html

做四件事：
  1. 抽出 <style>，把所有选择器限定在 .ets-root 下（避免污染宿主 DOM）
  2. 抽出 <main class="main"> 与 toast，作为面板内容
  3. 把内联脚本包成 initStudio(root)，所有查询限定在 root 内
  4. 生成 __ModuleLoader__ 模块：注册 sidebar.panellist + main
"""
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "ui", "econtone-studio.html")
OUT = os.path.join(ROOT, "lib", "client.js")

DROP_PREFIX = (".sb", ".shell")


def read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def top_blocks(css):
    """Yield (prelude, body) for every top-level block."""
    i, n, depth, start, prelude = 0, len(css), 0, 0, ""
    blocks = []
    while i < n:
        c = css[i]
        if c == "{":
            if depth == 0:
                prelude = css[start:i]
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = css[css.index("{", start) + 1:i]
                blocks.append((prelude.strip(), body))
                start = i + 1
        i += 1
    return blocks


def prefix_selectors(prelude):
    parts = [p.strip() for p in prelude.split(",") if p.strip()]
    out = []
    for p in parts:
        if p in (":root", "html", "body"):
            out.append(".ets-root")
        elif p.startswith(":root") or p.startswith("html") or p.startswith("body"):
            # :root[data-theme="mint"] -> .ets-root[data-theme="mint"]
            # html.xxx -> .ets-root.xxx ；保留属性/类后缀，但不加后代空格
            tag = ":root" if p.startswith(":root") else ("html" if p.startswith("html") else "body")
            out.append(".ets-root" + p[len(tag):])
        elif p == "*":
            out.append(".ets-root *")
        elif p.startswith(".") and p.split("{")[0].startswith(DROP_PREFIX):
            return None
        else:
            out.append(".ets-root " + p)
    # de-dup while keeping order
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return ", ".join(uniq)


def scope(css):
    out = []
    for prelude, body in top_blocks(strip_comments(css)):
        if prelude.startswith("@media"):
            out.append("%s{\n%s\n}" % (prelude, scope(body)))
        elif prelude.startswith("@keyframes"):
            name = prelude.split()[1]
            out.append("@keyframes ets-%s{%s}" % (name, body))
        elif prelude.startswith("@"):
            out.append("%s{%s}" % (prelude, body))
        else:
            sel = prefix_selectors(prelude)
            if sel:
                out.append("%s{%s}" % (sel, body))
    text = "\n".join(out)
    # animation 名称加前缀，避免与宿主同名 keyframes 冲突
    text = re.sub(r"(animation\s*:\s*)([A-Za-z_-]+)",
                  lambda m: m.group(1) + "ets-" + m.group(2), text)
    return text


ENGINE_JS = r"""
  /* ================= 引擎对接（P3.5） ================= */
  var API_BASE = '/econtone-studio/api';
  var LAST_RUN = null;
  var LAST_MEASURE = null;
  var DECISIONS = {};
  var DECISION_RUN = null;
  var SYNCING = false;
  var LAST_SEEN = 0;
  var DIRTY = false;
  var dirtyTimer = null;
  var TASKS = [];
  var ACTIVE = null;
  var POLL_MS = 500;
  var POLL_MAX = 420;  /* 210s: agent 路径最长 180s + 宽容 */

  function et(zh, en){ return lang === 'en' ? en : zh; }
  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function apiFetch(path, opts){
    return fetch(API_BASE + path, opts).then(function(r){
      return r.json().catch(function(){
        return { ok: false, error: { code: 'HTTP_' + r.status, message: 'bad response' } };
      });
    }).then(function(body){
      if (!body || body.ok !== true){
        var e = (body && body.error) || {};
        var err = new Error((e.code ? e.code + ' · ' : '') + (e.message || 'request failed'));
        err.code = e.code;
        throw err;
      }
      return body.data;
    });
  }

  var CODE_ZH = {
    BANNED: '术语表禁用', ACRONYM: '缩写未展开', MIXED: '中英混排',
    SLURRY: '长句连排', POUNDING: '重音密集', ROBOTIC: '句式机械',
    DENSE: '句子过密', FLAT: '语调平直', REGISTER: '模板化表达', TIER: '痕迹风险'
  };
  function codeLabel(code){
    if (lang === 'en') return code || '';
    return CODE_ZH[code] || code || '';
  }

  function p2Label(evidence){
    var s = String(evidence || '');
    if (lang === 'en') return s;
    if (/^clean$/i.test(s)) return '无问题';
    if (/claims match/i.test(s)) return '主张与证据一致';
    if (/no stated design/i.test(s)) return '未声明识别策略';
    if (/causal verb/i.test(s)) return '有因果动词但缺少识别语言';
    if (/without a magnitude/i.test(s)) return '显著性陈述缺少幅度';
    return s;
  }

  function evidenceOf(f){ return (f.evidence && f.evidence[0]) || {}; }

  function dimLines(run, id){
    var list = run.findings || [];
    var out = [];
    var MAX = 1, MAXLEN = 96;
    var clip = function(s){
      s = String(s == null ? '' : s);
      return s.length > MAXLEN ? s.slice(0, MAXLEN) + '…' : s;
    };
    if (id === 'P2'){
      var row = ((run.score || {}).rows || []).filter(function(r){ return r.id === 'P2'; })[0];
      if (row && row.evidence) out.push(p2Label(row.evidence));
      return out;
    }
    var want = id === 'P1' ? 'polish' : (id === 'T1' ? 'trace' : 'prosody');
    var hits = [];
    list.forEach(function(f){
      if (f.player !== want) return;
      var ev = evidenceOf(f);
      var text = f.original || ev.message || '';
      var cl = codeLabel(ev.code);
      var prefix = (cl && cl !== 'MODEL') ? cl + (lang === 'en' ? ': ' : '：') : '';
      hits.push(clip(prefix + text));
    });
    hits.slice(0, MAX).forEach(function(x){ out.push(x); });
    if (hits.length > MAX) out.push(et('\u5176\u4f59 ' + (hits.length - MAX) + ' \u6761', 'and ' + (hits.length - MAX) + ' more'));
    return out;
  }

  function isChinese(text){
    var t = String(text || '');
    if (!t) return false;
    var cjk = (t.match(/[\u4e00-\u9fff]/g) || []).length;
    return cjk >= 8 && cjk / t.length >= 0.15;
  }
  function syncDraftVisibility(){
    var box = $('#sec-draft');
    if (!box) return;
    var text = (draft && draft.value) || '';
    if (!isChinese(text)) { box.hidden = true; return; }
    box.hidden = false;
    var d = (LAST_MEASURE && LAST_MEASURE.draft) ? String(LAST_MEASURE.draft) : '';
    if (d) { renderDraft({ draft: d }); return; }
    if (box.dataset.busy === '1') {
      $('#draftText').innerHTML = '<p class="draft-pending">' + esc(et('\u6b63\u5728\u7ffb\u8bd1\u4e2d\u2026', 'Translating\u2026')) + '</p>';
    } else {
      $('#draftText').innerHTML = '<p class="draft-pending">' + esc(et('\u5c1a\u672a\u751f\u6210\u521d\u7a3f', 'No draft yet')) + '</p>';
    }
  }

  function renderDraft(run){
    var box = $('#sec-draft');
    if (!box) return;
    var draft = run && run.draft ? String(run.draft) : '';
    if (!draft){ box.hidden = true; $('#draftText').innerHTML = ''; return; }
    box.hidden = false;
    $('#draftText').innerHTML = draft.split(/\n{2,}/).map(function(p){
      return '<p>' + esc(p) + '</p>';
    }).join('');
  }

  function normRow(r){
    if (!r) return 0;
    var sc = Number(r.score), mx = Number(r.max);
    if (!isFinite(sc)) return 0;
    return mx > 0 ? Math.round(100 * sc / mx) : Math.round(sc);
  }
  function weightsOf(){
    var w = (GS && GS.weights) || {};
    return { P1: Number(w.P1) || 0, P2: Number(w.P2) || 0, T1: Number(w.T1) || 0, R1: Number(w.R1) || 0 };
  }
  function weightedTotal(rows){
    var w = weightsOf(), sum = 0, acc = 0;
    (rows || []).forEach(function(r){
      var ww = Number(w[r.id]);
      if (!isFinite(ww) || ww <= 0) return;
      sum += ww; acc += normRow(r) * ww;
    });
    return sum > 0 ? Math.round(acc / sum) : 0;
  }
  function repaintTotals(){
    var rows = (LAST_MEASURE && LAST_MEASURE.score && LAST_MEASURE.score.rows) || [];
    var tot = weightedTotal(rows);
    var ring = document.querySelector('#scoreRow .total .ring');
    var num = document.querySelector('#scoreRow .total .ring b');
    if (ring) ring.style.setProperty('--p', tot);
    if (num) num.textContent = tot;
    if (LAST_RUN) safeRender(renderFinalScore, LAST_RUN);
  }

  function renderScore(run){
    var box = $('#scoreRow'); if (!box) return;
    var s = run.score || {};
    var rows = s.rows || [];
    var html = rows.map(function(r){
      var pct = r.max ? Math.round(100 * r.score / r.max) : 0;
      var warn = (r.id === 'T1' || r.id === 'R1') ? ' warn' : '';
      var name = t('sc.' + String(r.id).toLowerCase());
      var norm = normRow(r);
      var wv = (weightsOf()[r.id] == null) ? '' : weightsOf()[r.id];
      var lines = dimLines(run, r.id);
      var inner = lines.length
        ? lines.map(function(x){ return '<div class="li">' + esc(x) + '</div>'; }).join('')
        : '<div class="none">' + esc(et('无问题', 'No issues')) + '</div>';
      return '<div class="sc' + warn + '" data-dim="' + esc(r.id) + '">'
        + '<div class="top"><b>' + esc(r.id) + '</b><span>' + esc(name) + '</span>'
        + '<em class="num">' + norm + '<i>/100</i></em></div>'
        + '<div class="bar"><i style="width:' + norm + '%"></i></div>'
        + '<div class="ev">' + esc(r.evidence || '') + '</div>'
        + '<div class="sc-issues" data-dim="' + esc(r.id) + '">' + inner + '</div>'
        + '<div class="wt"><span>' + esc(t('sc.weight')) + '</span>'
        + '<input type="number" min="0" max="100" step="1" data-w="' + esc(r.id) + '" value="' + esc(wv) + '" aria-label="weight" />'
        + '<i>%</i></div>'
        + '</div>';
    }).join('');
    var total = weightedTotal(rows);
    var lat = (s.editing_latitude == null) ? '—' : s.editing_latitude;
    html += '<div class="total" data-dim="TOTAL"><div class="ring" style="--p:' + total + '">'
      + '<b class="num">' + total + '</b></div>'
      + '<div class="tx"><b>' + esc(t('sc.total')) + '</b>'
      + '<div class="adv">' + esc(s.advice || '') + '</div></div></div>';
    box.innerHTML = html;
  }

  function renderIssues(run){
    var box = $('#issuesGrid'); if (!box) return;
    var list = run.findings || [];
    var count = $('#issueCount'); if (count) count.textContent = list.length;
    if (!list.length){
      box.innerHTML = '<div class="note" style="grid-column:1/-1">'
        + esc(et('未发现问题', 'No findings')) + '</div>';
      return;
    }
    box.innerHTML = list.map(function(f){
      var sev = f.severity === 'error' ? 'err' : (f.severity === 'warn' ? 'warn' : 'info');
      var icon = f.severity === 'error' ? '⛔' : (f.severity === 'warn' ? '⚠' : 'ℹ');
      var ev = evidenceOf(f);
      var title = f.original ? '<code>' + esc(f.original) + '</code>' : esc(f.reason || '');
      var why = f.original ? esc(f.reason || '') : '';
      var loc = f.paragraph
        ? et('第 ' + f.paragraph + ' 段', 'paragraph ' + f.paragraph)
        : et('全文', 'whole text');
      return '<article class="card-i ' + sev + '" data-ref="' + esc(f.id) + '">'
        + '<div class="c-top"><span class="sev sev-' + sev + '">' + icon + ' '
        + esc(codeLabel(ev.code)) + '</span>'
        + '<span class="src">' + esc(f.player || '') + '</span></div>'
        + '<div class="c-ttl">' + title + '</div>'
        + (why ? '<div class="c-why">' + why + '</div>' : '')
        + '<div class="c-meta"><span>' + esc(loc) + '</span><span class="grow"></span></div>'
        + '</article>';
    }).join('');
  }

  function renderStatus(run, elapsedMs){
    var fp = $('#stFp');
    if (fp) fp.textContent = 'fp ' + ((run.engine && run.engine.fingerprint) || '—');
    var tm = $('#stTime');
    if (tm) tm.textContent = elapsedMs ? (elapsedMs / 1000).toFixed(1) + 's' : '—';
    var risk = null;
    (run.findings || []).forEach(function(f){
      var ev = evidenceOf(f);
      if (risk == null && ev.source === 'trace' && ev.code === 'TIER') risk = ev.value;
    });
    var rk = $('#stRisk');
    if (rk) rk.textContent = 'risk_index ' + (risk == null ? '—' : Number(risk).toFixed(1));
    var prof = $('#stProfile');
    if (prof && run.profile) prof.textContent = run.profile;
  }

  function setCertTab(enabled){
    var tab = root.querySelector('.dtab[data-dtab="moves"]');
    if (!tab) return;
    tab.classList.toggle('disabled', !enabled);
    tab.disabled = !enabled;
    if (!enabled && tab.classList.contains('on')) {
      try { activateDTab('diff'); } catch (e) {}
    }
  }

  function normalizeMove(raw){
    if (typeof raw === 'string'){
      var s = raw.trim();
      var id = '', player = '';
      var mm = /^([a-z]\d+)\s+([a-z-]+)/i.exec(s);
      if (mm){ id = mm[1]; player = mm[2]; }
      var v = '';
      if (/拒绝|rejected|否决/i.test(s)) v = 'rejected';
      else if (/跳过|stale/i.test(s)) v = 'stale';
      else if (/接受|accepted|应用|applied/i.test(s)) v = 'accepted';
      return { id: id, player: player, kind: '', verdict: v, metric: s };
    }
    var m = raw || {};
    var vv = String(m.verdict || m.status || (m.accepted === true ? 'accepted' : (m.accepted === false ? 'rejected' : '')));
    var text = m.metric || m.reason || m.note || m.detail || m.description || '';
    return {
      id: m.id || '', player: m.player || m.engine || '',
      kind: m.kind || m.type || '', verdict: vv, metric: text,
    };
  }
  function renderMoves(run){
    var box = $('#movesList');
    if (!box) return;
    var cert = run && run.certificate;
    var moves = [];
    if (cert) (cert.rounds || []).forEach(function(rd){
      (rd.moves || []).forEach(function(raw){ moves.push({ round: rd.round, m: normalizeMove(raw) }); });
    });
    if (!moves.length){
      box.innerHTML = '<div class="move-empty">' + esc(et('本次运行没有候选动作', 'No moves in this run')) + '</div>';
      return;
    }
    box.innerHTML = moves.map(function(x){
      var m = x.m;
      var v = String(m.verdict || '');
      var cls = /^accepted/.test(v) ? 'y' : (/^rejected/.test(v) ? 'n' : 'p');
      var icon = cls === 'y' ? '\u2713' : (cls === 'n' ? '\u2715' : '\u00b7');
      var tag = cls === 'y' ? t('mv.accepted')
        : (cls === 'n' ? t('mv.rejected')
        : (/^stale/.test(v) ? et('\u5df2\u8df3\u8fc7', 'Skipped') : t('mv.pending')));
      var player = String(m.player || '').toLowerCase();
      return '<div class="move">'
        + '<div class="st ' + cls + '">' + icon + '</div>'
        + '<div class="b"><div class="l1">'
        + (m.id ? '<b>' + esc(m.id) + '</b>' : '')
        + (m.player ? '<span class="pl pl-' + esc(player) + '">' + esc(m.player) + '</span>' : '')
        + (m.kind ? '<span class="kind">' + esc(m.kind) + '</span>' : '')
        + '<span class="st-tag st-' + cls + '">' + esc(tag) + '</span></div>'
        + '<div class="reason">' + esc(m.metric || '') + '</div>'
        + '<div class="metrics"><span>' + esc(et('\u7b2c ' + x.round + ' \u8f6e', 'round ' + x.round)) + '</span></div>'
        + '</div></div>';
    }).join('');
  }

  function renderCertificate(run){
    var cert = run.certificate;
    var mc = $('#moveCount');
    var totalMoves = 0;
    if (cert) (cert.rounds || []).forEach(function(rd){ totalMoves += (rd.moves || []).length; });
    if (mc) mc.textContent = totalMoves;
    if (!cert) return;
    var kv = $('.cert .kv');
    if (kv){
      var accepted = 0, rejected = 0;
      (cert.rounds || []).forEach(function(rd){
        (rd.moves || []).forEach(function(m){
          if (m.accepted) accepted++;
          else if (!m.stale && !m.error) rejected++;
        });
      });
      var g = cert.game || {};
      kv.innerHTML = ''
        + '<dt>' + esc(et('形式', 'Form')) + '</dt><dd><code>' + esc(g.model || '') + '</code></dd>'
        + '<dt>' + esc(et('联盟', 'Coalition')) + '</dt><dd><code>' + esc(g.cooperation || 'none') + '</code></dd>'
        + '<dt>' + esc(et('模式', 'Mode')) + '</dt><dd><code>' + esc(cert.mode || '') + '</code></dd>'
        + '<dt>' + esc(et('迭代成本', 'Iter. cost')) + '</dt><dd><code>' + esc(String(g.iteration_cost == null ? '' : g.iteration_cost)) + '</code></dd>'
        + '<dt>' + esc(et('轮次', 'Rounds')) + '</dt><dd>' + ((cert.rounds || []).length) + '</dd>'
        + '<dt>' + esc(et('记录', 'Record')) + '</dt><dd>'
        + esc(et('接受 ', 'accepted ') + accepted + et(' · 拒绝 ', ' · rejected ') + rejected) + '</dd>'
        + '<dt>' + esc(et('停止', 'Stopped')) + '</dt><dd>' + esc(cert.stopped_because || '') + '</dd>';
    }
    var rounds = $('.cert .rounds');
    if (rounds && (cert.rounds || []).length){
      rounds.innerHTML = cert.rounds.map(function(rd){
        var n = (rd.moves || []).length;
        return '<div class="round"><span class="rn">R' + rd.round + '</span><div class="rb">'
          + '<b>' + esc(rd.accepted || et('无可用动作', 'no acceptable move')) + '</b>'
          + '<p>' + n + esc(et(' 个候选', ' candidates')) + '</p></div></div>';
      }).join('');
    }
  }

  function renderRewrite(run){
    var out = $('#outText');
    if (!out) return;
    var base = run.baseText;
    if (!base) {
      var typed = (draft && draft.value) || '';
      base = (isChinese(typed) && LAST_MEASURE && LAST_MEASURE.draft)
        ? String(LAST_MEASURE.draft)
        : typed;
    }
    var text = run.finalText || '';
    var edits = (run.edits || []).slice().sort(function(a, b){
      return a.base_range.start - b.base_range.start;
    });
    var plainOut = function(s){
      out.innerHTML = String(s || '').split(/\n{2,}/).map(function(p){
        return '<p>' + esc(p) + '</p>';
      }).join('');
      var cc = $('#outCount'); if (cc) cc.textContent = 0;
      var ww = $('#outWords'); if (ww) ww.textContent = 0;
    };
    if (!text && !base){
      out.innerHTML = '<div class="out-empty">' + esc(et('还没有改写稿，点「润色」开始', 'No rewrite yet \u2014 press Rewrite')) + '</div>';
      return;
    }
    if (!base){ plainOut(text); return; }
    if (!edits.length){ plainOut(text || base); return; }
    var html = '', cur = 0, accepted = 0, words = 0;
    edits.forEach(function(e, idx){
      var br = e.base_range || {}, ar = e.after_range || {};
      var s = br.start, en = br.end;
      if (!(s >= 0) || !(en >= s) || en > base.length || s < cur) return;
      html += esc(base.slice(cur, s));
      if (decisionOf(e.id) === 'no') {
        html += esc(base.slice(s, en));
      } else {
        accepted++;
        var n = idx + 1;
        var del = String(e.original || base.slice(s, en) || '');
        var add = (e.op === 'delete') ? '' : String(text.slice(ar.start, ar.end) || '');
        if (del) {
          html += '<span class="dele" data-ref="' + esc(e.id) + '"><s>' + esc(del)
            + '</s><sup>' + n + '</sup></span>';
        }
        if (add) {
          var why = String(e.reason || '');
          html += '<mark data-ref="' + esc(e.id) + '" tabindex="0"'
            + (why ? ' title="' + esc(why) + '"' : '') + '>'
            + esc(add) + '<sup>' + n + '</sup></mark>';
          words += add.trim() ? add.trim().split(/\s+/).length : 0;
        }
      }
      cur = en;
    });
    html += esc(base.slice(cur));
    out.innerHTML = html.split(/\n{2,}/).map(function(p){
      return '<p>' + p + '</p>';
    }).join('');
    Array.prototype.forEach.call(out.querySelectorAll('mark[data-ref]'), function(m){
      m.addEventListener('click', function(){ jumpToHunk(m.getAttribute('data-ref')); });
      m.addEventListener('keydown', function(ev){
        if (ev.key === 'Enter') jumpToHunk(m.getAttribute('data-ref'));
      });
    });
    var c = $('#outCount'); if (c) c.textContent = accepted;
    var w = $('#outWords'); if (w) w.textContent = words;
  }

  function whyText(e){
    var ev = (e.evidence && e.evidence[0]) || {};
    var r = String(e.reason || ev.message || '');
    if (!r) return et('模型未单独给出这一条的理由', 'the model gave no per-edit reason here');
    if (lang === 'zh'){
      if (r.indexOf('accepted:') === 0) return '引擎判定接受（' + r.slice(9).trim() + '）';
      if (r.indexOf('rejected:') === 0) return '引擎判定拒绝（' + r.slice(9).trim() + '）';
      if (ev.source === 'mechanical') return '拼写 / 冠词 / 空格修正';
    }
    return r;
  }

  var ICON_OK = '<svg viewBox="0 0 24 24" fill="none"><path d="m5 12.5 4.5 4.5L19 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var ICON_NO = '<svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

  function decisionOf(id){ return (DECISIONS && DECISIONS[id] === 'no') ? 'no' : 'yes'; }
  function acceptedCount(run){
    var edits = (run && run.edits) || [], n = 0;
    edits.forEach(function(e){ if (decisionOf(e.id) !== 'no') n++; });
    return n;
  }
  function appliedText(run){
    run = run || LAST_RUN;
    if (!run) return '';
    var base = run.baseText || '';
    var text = run.finalText || '';
    var edits = (run.edits || []).slice().sort(function(a, b){ return a.base_range.start - b.base_range.start; });
    if (!base || !text || !edits.length) return text || base;
    var out = '', cur = 0;
    edits.forEach(function(e){
      var br = e.base_range || {}, ar = e.after_range || {};
      var s = br.start, en = br.end;
      if (!(s >= 0) || !(en >= s) || en > base.length || s < cur) return;
      out += base.slice(cur, s);
      out += (decisionOf(e.id) === 'no') ? base.slice(s, en) : text.slice(ar.start, ar.end);
      cur = en;
    });
    out += base.slice(cur);
    return out;
  }
  function adoptNote(){
    if (!LAST_RUN || !(LAST_RUN.edits || []).length) return '';
    var total = LAST_RUN.edits.length, acc = acceptedCount(LAST_RUN);
    return et('已采纳 ' + acc + '/' + total + ' 处 · 复制与保存按采纳结果',
              acc + '/' + total + ' accepted · copy and save follow your choices');
  }
  function refreshAdopt(){
    if (!LAST_RUN || !(LAST_RUN.edits || []).length) return;
    var el = $('#adoptCount');
    if (el) {
      el.textContent = acceptedCount(LAST_RUN);
      var holder = el.parentNode;
      if (holder) { var i = holder.querySelector('i'); if (i) i.textContent = '/' + LAST_RUN.edits.length; }
    }
    var note = root.querySelector('#diffNote');
    if (note) note.textContent = adoptNote();
  }
  function saveDecisions(){
    if (!LAST_RUN || !LAST_RUN.runId) return;
    apiFetch('/run/decisions', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ runId: LAST_RUN.runId, decisions: DECISIONS }),
    }).catch(function(){});
  }
  function setDecision(id, state){
    DECISIONS[id] = (state === 'no') ? 'no' : 'yes';
    var art = null;
    try { art = root.querySelector('.hunk[data-live="1"][data-hunk="' + String(id).replace(/"/g, '') + '"]'); } catch (e) {}
    if (art) {
      art.classList.toggle('ok', DECISIONS[id] !== 'no');
      art.classList.toggle('no', DECISIONS[id] === 'no');
    }
    refreshAdopt();
    try { if (LAST_RUN) renderRewrite(LAST_RUN); } catch (e) {}
    saveDecisions();
  }

  function renderDiff(run){
    var box = $('#dtab-diff');
    if (!box) return;
    var base = run.baseText;
    if (!base) {
      var typed = (draft && draft.value) || '';
      base = (isChinese(typed) && LAST_MEASURE && LAST_MEASURE.draft)
        ? String(LAST_MEASURE.draft)
        : typed;
    }
    var text = run.finalText || '';
    var edits = run.edits || [];
    var head = '<div class="dh"><span></span><span>' + esc(t('cmp.original'))
      + '</span><span>' + esc(t('cmp.revised')) + '</span><span></span></div>';
    var dc = $('#diffCount'); if (dc) dc.textContent = edits.length;
    if (!edits.length){
      box.innerHTML = head + '<div class="note">'
        + esc(et('本次改写没有产生可应用的改动', 'No applicable edits this run')) + '</div>';
      return;
    }
    box.innerHTML = head + '<div class="dh-note" id="diffNote">' + esc(adoptNote()) + '</div>' + edits.map(function(e, idx){
      var s = e.base_range.start, en = e.base_range.end;
      var c0 = Math.max(0, s - 36), c1 = Math.min(base.length, en + 36);
      var before = base.slice(c0, s), after = base.slice(en, c1);
      var gone = base.slice(s, en);
      var add = text.slice(e.after_range.start, e.after_range.end);
      var nil = '<span class="nil">\u2205</span>';
      var original = esc(before) + (gone ? '<del>' + esc(gone) + '</del>' : nil) + esc(after);
      var revised = esc(before) + (add ? '<ins>' + esc(add) + '</ins>' : nil) + esc(after);
      var ev = (e.evidence && e.evidence[0]) || {};
      var kind = ev.source === 'model' ? et('模型改写', 'model') : et('机械修正', 'mechanical');
      return '<article class="hunk ' + (decisionOf(e.id) === 'no' ? 'no' : 'ok') + '" data-live="1" data-hunk="' + esc(e.id) + '">'
        + '<header class="hunk-head"><span class="hk-no mono">#' + (idx + 1) + '</span>'
        + '<span class="hk-id mono">' + esc(e.id) + '</span>'
        + '<span class="pl pl-polish">polish</span>'
        + '<span class="sev sev-info">' + esc(kind) + '</span>'
        + '<span class="hk-actions" style="margin-left:auto">'
        + '<button class="mini" type="button" data-dec="yes" title="' + esc(t('hn.accept')) + '" aria-label="' + esc(t('hn.accept')) + '">' + ICON_OK + '</button>'
        + '<button class="mini rej" type="button" data-dec="no" title="' + esc(t('hn.ignore')) + '" aria-label="' + esc(t('hn.ignore')) + '">' + ICON_NO + '</button>'
        + '</span></header>'
        + '<div class="hunk-body"><div class="del">…' + original + '…</div>'
        + '<div class="ins">…' + revised + '…</div></div>'
        + '<div class="hunk-why"><b>' + esc(et('改动依据', 'Why')) + '</b>'
        + esc(whyText(e)) + '</div></article>';
    }).join('');
  }

  function renderFinalScore(run){
    var box = $('#outScore'); if (!box) return;
    var fs = run.finalScore;
    if (!fs){ box.innerHTML = ''; return; }
    var before = null;
    if (run.score && Array.isArray(run.score.rows) && run.score.rows.length) before = weightedTotal(run.score.rows);
    else if (run.score && run.score.total != null) before = run.score.total;
    else if (LAST_MEASURE && LAST_MEASURE.score) before = weightedTotal(LAST_MEASURE.score.rows || []);
    var after = (fs.rows && fs.rows.length) ? weightedTotal(fs.rows) : ((fs.total == null) ? 0 : fs.total);
    var d = (before == null) ? 0 : (after - before);
    var rows = (fs.rows || []).map(function(r){
      return '<span class="r">' + esc(r.id) + ' <b>' + normRow(r) + '</b></span>';
    }).join('');
    box.innerHTML = '<span>' + esc(et('改写后', 'After')) + '</span>'
      + '<span class="scorepair"><span class="big">' + after + '</span><span class="of">/100</span></span>'
      + (before == null ? '' : '<span class="delta' + (d < 0 ? ' down' : '') + '">' + (d >= 0 ? '+' : '') + d + '</span>')
      + rows
      + '<span class="r">' + esc(et('改写前 ', 'before ') + (before == null ? '\u2014' : before)) + '</span>';
  }

  function fmtStamp(iso){
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace('T', ' ');
    var p2 = function(n){ return (n < 10 ? '0' : '') + n; };
    return p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
  }
  function historyPush(run){
    if (!run || !run.runId || !TASKS || !TASKS.length) return;
    var t = activeTask();
    if (!t) return;
    if (!Array.isArray(t.history)) t.history = [];
    if (t.history.some(function(x){ return x.runId === run.runId; })) return;
    t.history.unshift({ runId: run.runId, createdAt: run.createdAt || '' });
    t.history = t.history.slice(0, 30);
    saveTasks();
  }
  function renderHistory(){
    var sel = $('#histSel');
    if (!sel) return;
    var t = activeTask();
    var list = (t && Array.isArray(t.history)) ? t.history : [];
    if (!list.length) { sel.hidden = true; sel.innerHTML = ''; return; }
    sel.hidden = false;
    sel.innerHTML = list.map(function(x){
      return '<option value="' + esc(x.runId) + '">#' + esc(String(x.runId).replace(/^r-/, '').slice(0, 8))
        + '  ·  ' + esc(fmtStamp(x.createdAt)) + '</option>';
    }).join('');
    if (LAST_RUN && LAST_RUN.runId) sel.value = LAST_RUN.runId;
  }
  function loadHistoryRun(runId){
    if (!runId) return;
    var out = $('#outText');
    if (out) out.innerHTML = '<div class="out-working"><div class="wk-head">'
      + esc(et('\u6b63\u5728\u8bfb\u53d6\u5386\u53f2\u6539\u5199\u2026', 'Loading this rewrite\u2026')) + '</div></div>';
    apiFetch('/run?id=' + encodeURIComponent(runId)).then(function(st){
      if (st.status === 'done' && st.result) {
        renderAll(st.result, 0);
        renderHistory();
        toast(et('\u5df2\u5207\u5230\u8be5\u6b21\u6539\u5199', 'Loaded that rewrite'));
      } else {
        toast(et('\u8fd9\u6b21\u6539\u5199\u5df2\u8fc7\u671f', 'That rewrite has expired'));
      }
    }).catch(function(){
      toast(et('\u8fd9\u6b21\u6539\u5199\u5df2\u8fc7\u671f', 'That rewrite has expired'));
    });
  }
  var histSel = $('#histSel');
  if (histSel) histSel.addEventListener('change', function(){ loadHistoryRun(histSel.value); });

  function safeRender(fn, a, b){
    try { fn(a, b); }
    catch (e) { if (window.console && console.warn) console.warn('[EconTone] render step failed:', e && e.message); }
  }
  function renderAll(run, elapsedMs){
    var isMeasure = run.mode === 'measure' || (!!run.score && !run.finalText);
    if (isMeasure) {
      var cm = activeTask();
      if (cm && cm.scoreCleared) { LAST_MEASURE = null; emptyScoreCard(); return; }
      LAST_MEASURE = run;
      safeRender(renderScore, run);
      safeRender(renderIssues, run);
      safeRender(renderStatus, run, elapsedMs);
      safeRender(renderDraft, run);
      return;
    }
    LAST_RUN = run;
    if (run.runId !== DECISION_RUN) {
      DECISION_RUN = run.runId || null;
      DECISIONS = (run.decisions && typeof run.decisions === 'object') ? run.decisions : {};
    }
    paintBadge();
    // 评分已由自动评分负责：改写返回不覆盖主评分卡
    var cw = activeTask();
    if (!LAST_MEASURE && run.score && !(cw && cw.scoreCleared)) {
      LAST_MEASURE = { score: run.score, findings: run.findings, mode: 'measure' };
      renderScore(LAST_MEASURE);
      renderIssues(LAST_MEASURE);
    }
    safeRender(renderCertificate, run);
    safeRender(renderMoves, run);
    safeRender(setCertTab, !!run.certificate);
    safeRender(renderRewrite, run);
    safeRender(renderDiff, run);
    safeRender(refreshAdopt);
    safeRender(renderFinalScore, run);
    safeRender(historyPush, run);
    safeRender(renderHistory);
    var stp = $('#outStamp');
    if (stp){
      try {
        var nEdits = (run.edits || []).length;
        var rid = String(run.draftId || '').trim() || String(run.runId || '').replace(/^r-/, '');
        var curLen = (draft && draft.value) ? draft.value.length : 0;
        var baseLen = (typeof run.baseText === 'string') ? run.baseText.length : null;
        stp.hidden = false;
        stp.classList.remove('stale');
        stp.textContent = (rid ? rid + ' \u00b7 ' : '') + nEdits + ' ' + et('\u5904\u6539\u52a8', 'edits')
          + (baseLen == null ? '' : ' \u00b7 ' + baseLen + ' ' + et('\u5b57\u7b26', 'chars'));
        if (baseLen != null && baseLen !== curLen){
          stp.classList.add('stale');
          toast(et('\u672c\u6b21\u7ed3\u679c\u57fa\u4e8e ' + baseLen + ' \u5b57\u7b26\u7684\u6587\u672c\uff0c\u4e0e\u5f53\u524d\u8f93\u5165\uff08' + curLen + '\uff09\u4e0d\u4e00\u81f4\uff0c\u8bf7\u91cd\u65b0\u6da6\u8272',
            'This result used ' + baseLen + ' characters; the editor has ' + curLen + '. Polish again.'));
        }
      } catch (e) {}
    }
  }

  function poll(runId, onDone, onFail, onProgress){
    var tries = 0;
    var timer = setInterval(function(){
      tries++;
      if (tries > POLL_MAX){ clearInterval(timer); onFail(new Error('POLL_TIMEOUT')); return; }
      apiFetch('/run?id=' + encodeURIComponent(runId)).then(function(st){
        if (st.status === 'done' && st.result){ clearInterval(timer); onDone(st.result); }
        else if (st.status === 'error'){
          clearInterval(timer);
          onFail(new Error((st.error && st.error.message) || 'run failed'));
        } else if (onProgress) {
          onProgress(st);
        }
      }).catch(function(err){ clearInterval(timer); onFail(err); });
    }, POLL_MS);
  }

  function startRun(mode, onDone, onFail, onProgress){
    var text = (draft && draft.value) || '';
    if (!text.trim()){ onFail(new Error(et('先粘贴一段文本', 'Paste some text first'))); return; }
    var started = Date.now();
    apiFetch('/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: text, profile: resolveSection(), mode: mode,
        taskId: (activeTask() || {}).id || '',
        draftId: (mode === 'arbitrate') ? (PENDING_DRAFT_ID || '') : '',
        order: GS.order, coalition: GS.coalition, actionMode: GS.mode,
        engine: GS.engine, model: GS.model,
        uiLang: lang, budgetSec: GS.budget, conn: GS.conn,
        baselineScore: (LAST_MEASURE && LAST_MEASURE.score) ? LAST_MEASURE.score : null,
        draftText: (LAST_MEASURE && LAST_MEASURE.draft) ? LAST_MEASURE.draft : '',
        latitude: (LAST_RUN && LAST_RUN.score && LAST_RUN.score.editing_latitude != null)
          ? LAST_RUN.score.editing_latitude : null,
        journal: ($('#mJournal') || {}).value || '',
        field: ($('#mField') || {}).value || '',
        section: ($('#mSection') || {}).value || '',
        extent: (function(){
          var el = $('#extent');
          if (!el) return 'auto';
          return String(el.value) === '0' ? 'auto' : (el.value + '%');
        })() }),
    }).then(function(res){
      if (res && res.status === 'done' && res.result){ onDone(res.result, Date.now() - started); return; }
      saveJob({ runId: res.runId, mode: mode, startedAt: started });
      poll(res.runId, function(result){
        clearJob();
        onDone(result, Date.now() - started);
      }, function(err){
        clearJob();
        onFail(err);
      }, onProgress || function(){});
    }).catch(onFail);
  }

  var PENDING_DRAFT_ID = '';
  function newDraftId(){
    return 'd-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 5);
  }
  function runArbitrate(){
    var btn = $('#btnRun'); var out = $('#outText'); var oscore = $('#outScore');
    PENDING_DRAFT_ID = newDraftId();
    var curTask = activeTask();
    if (curTask){
      curTask.lastDraftId = PENDING_DRAFT_ID;
      try { snapshotTask(); pushState(0); saveTasks(); } catch (e) {}
    }
    if (out) out.innerHTML = '<div class="out-working">' + esc(et('正在改写中…', 'Rewriting…')) + '</div>';
    if (oscore) oscore.innerHTML = '';
    if (btn) btn.disabled = true;
    startRun('arbitrate', function(run, ms){
      try { renderAll(run, ms); }
      finally {
        if (btn) btn.disabled = false;
        var ta = activeTask();
        if (ta){ ta.lastRunId = run.runId || ta.lastRunId; ta.lastRunAt = Date.now(); ta.lastDraftId = run.draftId || ta.lastDraftId || PENDING_DRAFT_ID; }
        try { pushState(0); } catch (e) {}
        saveState();
      }
      var stop = (run.certificate && run.certificate.stopped_because) || '';
      toast(et('改写完成', 'Rewrite done') + (stop ? '：' + stop : ''));
    }, function(err){
      if (btn) btn.disabled = false;
      if (out) out.innerHTML = '<div class="out-working">'
        + esc(et('改写失败：', 'Rewrite failed: ') + (err.message || err)) + '</div>';
      toast(et('改写失败：', 'Rewrite failed: ') + (err.message || err));
    }, function(st){
      paintWorking(st, out);
    });
  }

  /* 进行中的作业：切走再回来也能接着等 */
  var JOB_KEY = 'ets.job.v1';
  function saveJob(job){
    try { localStorage.setItem(JOB_KEY, JSON.stringify(job)); } catch (e) {}
  }
  function clearJob(){
    try { localStorage.removeItem(JOB_KEY); } catch (e) {}
  }
  function loadJob(){
    var raw = null;
    try { raw = localStorage.getItem(JOB_KEY); } catch (e) {}
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }
  function paintWorking(st, out){
    if (!out || !st) return;
    var secs = Math.round((st.elapsedMs || 0) / 1000);
    var text = st.phase === 'rewrite'
      ? et('正在改写中… 已生成 ' + (st.chars || 0) + ' 字 · ' + secs + 's',
           'Rewriting… ' + (st.chars || 0) + ' chars · ' + secs + 's')
      : et('正在裁决…（play.py 测量候选）· ' + secs + 's',
           'Arbitrating… · ' + secs + 's');
    var budget = Number(GS && GS.budget) || 60;
    if (secs > budget + 2){
      text += ' · ' + et('已超期望时间，继续等待…', 'past the expected time, still running…');
    }
    var pre = String(st.preview || '');
    out.innerHTML = '<div class="out-working">'
      + '<div class="wk-head">' + esc(text) + '</div>'
      + (pre ? '<div class="wk-pre">' + esc(pre) + '</div>' : '')
      + '</div>';
  }
  function resumeJob(){
    var job = loadJob();
    if (!job || !job.runId) return;
    if (Date.now() - (job.startedAt || 0) > 30 * 60 * 1000){ clearJob(); return; }
    var isMeasure = job.mode !== 'arbitrate';
    var out = $('#outText');
    var btn = isMeasure ? $('#btnScore') : $('#btnRun');
    if (isMeasure){
      setScoreBusy(true);
      if (!LAST_MEASURE) emptyScoreCard();
      var box0 = $('#scoreRow');
      if (box0 && !LAST_MEASURE){
        box0.innerHTML = '<div class="sc-eval"><span>' + esc(et('正在评估中…', 'Evaluating…')) + '</span>'
          + '<div class="eval-bar"><i></i></div></div>';
      }
    } else {
      if (btn) btn.disabled = true;
      if (out){
        out.innerHTML = '<div class="out-working"><div class="wk-head">'
          + esc(et('正在恢复上一次改写…', 'Resuming the previous rewrite…'))
          + '</div></div>';
      }
    }
    poll(job.runId, function(run, ms){
      clearJob();
      if (isMeasure){
        var t1 = activeTask();
        if (t1) t1.scoreCleared = false;
        LAST_MEASURE = run;
        safeRender(renderScore, run);
        safeRender(renderIssues, run);
        safeRender(renderStatus, run, ms);
      }
      renderAll(run, ms);
      var tr = activeTask();
      if (tr){ tr.lastRunId = run.runId || tr.lastRunId; tr.lastRunAt = Date.now(); }
      if (isMeasure){ try { snapshotTask(); pushState(0); saveTasks(); } catch (e) {} }
      saveState();
      if (isMeasure) setScoreBusy(false);
      else if (btn) btn.disabled = false;
      toast(et('恢复完成', 'Resumed and finished'));
    }, function(err){
      clearJob();
      if (isMeasure){
        setScoreBusy(false);
        var box = $('#scoreRow');
        if (box) box.innerHTML = '<div class="sc-eval">'
          + esc(et('评估失败：', 'Evaluation failed: ') + (err.message || err)) + '</div>';
      } else {
        if (btn) btn.disabled = false;
        if (out) out.innerHTML = '<div class="out-working">'
          + esc(et('改写失败：', 'Rewrite failed: ') + (err.message || err)) + '</div>';
      }
    }, function(st){
      if (!isMeasure) paintWorking(st, out);
    });
  }

  /* 状态持久化：切到对话/别的插件再回来，内容不丢 */
  var STORE_KEY = 'ets.state.v1';
  var saveTimer = null;
  function collectState(){
    var j = $('#mJournal'), f = $('#mField'), s = $('#mSection'), n = $('#mNote');
    return {
      text: (draft && draft.value) || '',
      meta: {
        journal: j ? j.value : '',
        field: f ? f.value : '',
        section: s ? s.value : '',
        note: n ? n.value : '',
      },
      run: LAST_RUN,
    };
  }
  function saveState(){
    DIRTY = true;
    clearTimeout(dirtyTimer);
    dirtyTimer = setTimeout(function(){ DIRTY = false; }, 1600);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function(){
      try { localStorage.setItem(STORE_KEY, JSON.stringify(collectState())); } catch (e) {}
      try { snapshotTask(); saveTasks(); } catch (e) {}
    }, 400);
  }
  function restoreState(){
    var raw = null;
    try { raw = localStorage.getItem(STORE_KEY); } catch (e) {}
    if (!raw) return;
    var st = null;
    try { st = JSON.parse(raw); } catch (e) { return; }
    if (st.meta){
      var pairs = [['mJournal', 'journal'], ['mField', 'field'], ['mSection', 'section'], ['mNote', 'note']];
      pairs.forEach(function(p){
        var el = $('#' + p[0]);
        if (el) el.value = st.meta[p[1]] || '';
      });
    }
    if (typeof st.text === 'string' && draft) draft.value = st.text;
    refreshCounters(); resolveSection();
    if (st.run && st.run.score) renderAll(st.run, 0);
  }
  if (draft) draft.addEventListener('input', saveState);
  ['mJournal', 'mField', 'mSection', 'mNote'].forEach(function(id){
    var el = $('#' + id);
    if (el) el.addEventListener('input', saveState);
  });
  restoreState();
  loadTasks();
  resumeJob();
  loadBinding();

  /* 工作对话绑定：列出现有会话 + 新建；一律显示对话标题，完整 session id 收进悬停提示 */
  function shortId(id){
    var s = String(id || '');
    return s.length > 16 ? s.slice(0, 9) + '\u2026' + s.slice(-4) : s;
  }
  /* 标题优先；没有标题才退回短 id，保证每一行仍能认出是哪个对话 */
  function convLabel(title, id){
    var t = String(title || '').trim();
    return t || shortId(id);
  }
  function idTip(id){
    var v = String(id || '');
    return v ? ' title="' + esc(v) + '"' : '';
  }
  function boundOf(b){
    var id = (b && b.bound && b.bound.sessionId) ? String(b.bound.sessionId) : '';
    if (!id) return { id: '', label: '' };
    return { id: id, label: convLabel((b && b.boundTitle) || (b.bound && b.bound.title), id) };
  }
  function renderBind(b){
    var mask = $('#bindMask');
    if (!mask) return;
    mask.hidden = false;
    var cwd = $('#bindCwd');
    if (cwd) cwd.value = (b.bound && b.bound.cwd) || b.defaultCwd || '';
    var bd = boundOf(b);
    var boundId = bd.id;
    var now = $('#bindNow');
    if (now) {
      now.innerHTML = boundId
        ? esc(t('bind.now')) + '<b' + idTip(boundId) + '>' + esc(bd.label) + '</b>'
        : esc(t('bind.none'));
    }
    var list = $('#bindList');
    if (!list) return;
    var rows = ['<label class="bind-row"><input type="radio" name="bindSel" value=""' + (boundId ? '' : ' checked') + '><span>'
      + esc(et('新建 EconTone 对话', 'New “EconTone” conversation')) + '</span></label>'];
    (b.sessions || []).forEach(function(s){
      var id = String(s.id || '');
      rows.push('<label class="bind-row"><input type="radio" name="bindSel" value="' + esc(id) + '"' + (id === boundId ? ' checked' : '') + '>'
        + '<span class="bind-name"' + idTip(id) + '>' + esc(convLabel(s.title, id)) + '</span>'
        + '<span class="bind-cwd">' + esc(s.cwd || '') + '</span>'
        + '<span class="bind-evs mono">' + esc(String(s.events == null ? 0 : s.events)) + '</span></label>');
    });
    list.innerHTML = rows.join('');
  }

  function loadBinding(){
    apiFetch('/binding').then(function(b){
      var nm = $('#bindName');
      var bd = boundOf(b);
      if (nm) {
        nm.textContent = bd.label || et('未绑定', 'unbound');
        nm.title = bd.id;
      }
      if (b && b.bound) return;
      renderBind(b || {});
    }).catch(function(){});
  }

  var bindOk = $('#bindOk');
  if (bindOk) bindOk.addEventListener('click', function(){
    var sel = root.querySelector('input[name="bindSel"]:checked');
    var cwdEl = $('#bindCwd');
    apiFetch('/binding', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        cwd: cwdEl ? cwdEl.value : '',
        sessionId: sel ? sel.value : '',
        name: 'EconTone',
      }),
    }).then(function(b){
      var m = $('#bindMask'); if (m) m.hidden = true;
      var sid = String((b && b.sessionId) || '');
      var sname = convLabel(b && b.title, sid);
      var nm2 = $('#bindName');
      if (nm2) {
        nm2.textContent = sname;
        nm2.title = sid;
      }
      var label = (b && b.created) ? t('bind.created') : t('bind.switched');
      toast(label + sname);
    }).catch(function(err){
      toast(et('绑定失败：', 'Bind failed: ') + (err.message || err));
    });
  });

  var bindProbe = $('#bindProbe');
  if (bindProbe) bindProbe.addEventListener('click', function(){
    bindProbe.disabled = true;
    apiFetch('/binding/probe', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })
      .then(function(r){
        toast(t('bind.probeOk') + String((r && r.reply) || '').slice(0, 40));
        bindProbe.disabled = false;
      })
      .catch(function(err){
        toast(t('bind.probeFail') + ((err && err.message) || err));
        bindProbe.disabled = false;
      });
  });

  var btnBind = $('#btnBind');
  if (btnBind) btnBind.addEventListener('click', function(){
    apiFetch('/binding').then(function(b){ renderBind(b || {}); }).catch(function(){});
  });

  var bindCancel = $('#bindCancel');
  if (bindCancel) bindCancel.addEventListener('click', function(){
    var m = $('#bindMask'); if (m) m.hidden = true;
  });

  // 三种关闭方式：右上角 ×、点遮罩空白、Esc
  var bindClose = $('#bindClose');
  if (bindClose) bindClose.addEventListener('click', function(){
    var m = $('#bindMask'); if (m) m.hidden = true;
  });

  var bindMaskEl = $('#bindMask');
  if (bindMaskEl) bindMaskEl.addEventListener('click', function(e){
    if (e.target === bindMaskEl) bindMaskEl.hidden = true;
  });

  if (!window.__etsBindWired) {
    window.__etsBindWired = true;
    document.addEventListener('keydown', function(e){
      if (e.key !== 'Escape') return;
      var m = document.getElementById('bindMask');
      if (m && !m.hasAttribute('hidden')) m.hidden = true;
    });
  }

  /* ===== \u4efb\u52a1\u6807\u7b7e\u9875 ===== */
  var TASKS_KEY = 'ets.tasks.v1';
  var ACTIVE_KEY = 'ets.active.v1';
  var PARAMS_KEY = 'ets.params.v1';
  function collectParams(){
    return {
      model: GS.model, order: GS.order, coalition: GS.coalition, mode: GS.mode,
      budget: GS.budget, conn: GS.conn, engine: GS.engine,
      weights: GS.weights || { P1: 40, P2: 40, T1: 10, R1: 10 },
      extent: ($('#extent') || {}).value || '0',
    };
  }
  function applyParams(p){
    if (!p || typeof p !== 'object') return;
    if (p.model) GS.model = p.model;
    if (p.order) GS.order = p.order;
    if (p.coalition) GS.coalition = p.coalition;
    if (p.mode) GS.mode = p.mode;
    if (p.budget) GS.budget = p.budget;
    GS.conn = (p.conn === 'stream') ? 'stream' : 'agent';
    if (p.engine) GS.engine = p.engine;
    if (p.weights && typeof p.weights === 'object'){
      GS.weights = {
        P1: Number(p.weights.P1), P2: Number(p.weights.P2),
        T1: Number(p.weights.T1), R1: Number(p.weights.R1),
      };
      ['P1','P2','T1','R1'].forEach(function(k){ if (!isFinite(GS.weights[k])) GS.weights[k] = 0; });
    }
    var ex = $('#extent');
    if (ex && p.extent != null) ex.value = p.extent;
    try { paintGame(); paintExtent(); } catch (e) {}
  }
  var paramsSaveTimer = null;
  function scheduleSaveParams(){
    if (SYNCING) return;
    clearTimeout(paramsSaveTimer);
    paramsSaveTimer = setTimeout(function(){
      try { localStorage.setItem(PARAMS_KEY, JSON.stringify(collectParams())); } catch (e) {}
      saveTasks();
    }, 300);
  }

  function blankTask(){
    return {
      id: 't-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 5),
      name: '', text: '', meta: { journal: '', field: '', section: '', note: '' },
      extent: null, run: null,
    };
  }
  function activeTask(){
    if (!TASKS || !TASKS.length) return null;
    for (var i = 0; i < TASKS.length; i++) if (TASKS[i].id === ACTIVE) return TASKS[i];
    return null;
  }
  var taskSaveTimer = null;
  function pushState(attempt){
    attempt = attempt || 0;
    if (!TASKS || !TASKS.length) return;
    var anyContent = TASKS.some(function(t){
      return t && (((t.text || '').trim().length > 0) || t.measured || (t.run && t.run.finalText));
    });
    if (!anyContent) return;
    if (SYNCING && attempt < 6){
      setTimeout(function(){ pushState(attempt + 1); }, 500);
      return;
    }
    try {
      var stamp = Date.now();
      apiFetch('/state', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ state: {
          tasks: TASKS, active: ACTIVE, params: collectParams(),
          theme: theme, lang: lang, savedAt: stamp,
        } }),
      }).then(function(){ LAST_SEEN = stamp; }).catch(function(){
        if (attempt < 6) setTimeout(function(){ pushState(attempt + 1); }, 800);
      });
    } catch (e) {}
  }
  function saveTasks(){
    if (!TASKS || !TASKS.length) return;
    try {
      localStorage.setItem(TASKS_KEY, JSON.stringify(TASKS));
      localStorage.setItem(ACTIVE_KEY, ACTIVE || '');
    } catch (e) {}
    clearTimeout(taskSaveTimer);
    taskSaveTimer = setTimeout(function(){ pushState(0); }, 400);
  }
  function snapshotTask(){
    var t = activeTask(); if (!t) return;
    if (draft) t.text = draft.value;
    var j = $('#mJournal'), f = $('#mField'), sec = $('#mSection'), n = $('#mNote');
    t.meta = {
      journal: j ? j.value : '', field: f ? f.value : '',
      section: sec ? sec.value : '', note: n ? n.value : '',
    };
    var ex = $('#extent'); if (ex) t.extent = String(ex.value);
    t.run = LAST_RUN || t.run || null;
    t.measured = LAST_MEASURE || null;
    if (!t.renamed) t.name = (t.text || '').trim().slice(0, 24);
  }
  function clearRunUI(){
    LAST_RUN = null;
    LAST_MEASURE = null;
    var a = $('#scoreRow'); if (a) a.innerHTML = '';
    var stamp = $('#outStamp'); if (stamp){ stamp.hidden = true; stamp.textContent = ''; stamp.classList.remove('stale'); }
    var b = $('#issuesGrid'); if (b) b.innerHTML = '';
    var c = $('#outText'); if (c) c.innerHTML = '';
    var d = $('#outScore'); if (d) d.innerHTML = '';
    renderDraft(null);
    paintBadge();
  }
  function applyTask(t){
    if (!t) return;
    if (draft) draft.value = t.text || '';
    var j = $('#mJournal'), f = $('#mField'), sec = $('#mSection'), n = $('#mNote');
    if (j) j.value = (t.meta && t.meta.journal) || '';
    if (f) f.value = (t.meta && t.meta.field) || '';
    if (sec) sec.value = (t.meta && t.meta.section) || '';
    if (n) n.value = (t.meta && t.meta.note) || '';
    var ex = $('#extent');
    if (ex) {
      ex.value = t.extent || '0';
      try { ex.dispatchEvent(new Event('input')); } catch (e) {}
    }
    refreshCounters(); resolveSection();
    clearRunUI();
    LAST_MEASURE = t.scoreCleared ? null : (t.measured || null);
    if (LAST_MEASURE) { renderScore(LAST_MEASURE); renderIssues(LAST_MEASURE); }
    else {
      var bs = $('#btnScore');
      if (bs && bs.dataset.busy === '1'){
        var bx = $('#scoreRow');
        if (bx) bx.innerHTML = '<div class="sc-eval"><span>' + esc(et('正在评估中…', 'Evaluating…')) + '</span>'
          + '<div class="eval-bar"><i></i></div></div>';
      } else {
        emptyScoreCard();
      }
    }
    if (t.run) renderAll(t.run, 0);
    else {
      var outEmpty = $('#outText');
      if (outEmpty) outEmpty.innerHTML = '<div class="out-empty">'
        + esc(et('还没有改写稿，点「润色」开始', 'No rewrite yet \u2014 press Rewrite')) + '</div>';
      var outSc = $('#outScore'); if (outSc) outSc.innerHTML = '';
    }
    syncDraftVisibility();
    renderHistory();
    paintBadge();
  }
  function renderTabs(){
    var box = $('#taskTabs'); if (!box) return;
    var html = TASKS.map(function(t, i){
      var label = (t.name || t.text || '').trim().slice(0, 18) || et('\u65b0\u4efb\u52a1', 'New task');
      return '<button class="task-tab' + (t.id === ACTIVE ? ' on' : '') + '" data-t="' + t.id + '">'
        + '<span class="ord">#' + (i + 1) + '</span>'
        + '<span class="nm">' + esc(label) + '</span>'
        + (TASKS.length > 1 ? '<span class="x" data-x="' + t.id + '">\u00d7</span>' : '')
        + '</button>';
    }).join('');
    html += '<button class="task-add" id="taskAdd">+ ' + esc(et('\u65b0\u4efb\u52a1', 'New task')) + '</button>';
    box.innerHTML = html;
    $$('#taskTabs .task-tab').forEach(function(b){
      b.addEventListener('click', function(ev){
        if (ev.target && ev.target.getAttribute && ev.target.getAttribute('data-x')) return;
        switchTask(b.getAttribute('data-t'));
      });
    });
    $$('#taskTabs .x').forEach(function(x){
      x.addEventListener('click', function(ev){
        ev.stopPropagation();
        closeTask(x.getAttribute('data-x'));
      });
    });
    $$('#taskTabs .task-tab').forEach(function(b){
      b.addEventListener('dblclick', function(ev){
        ev.stopPropagation();
        var id = b.getAttribute('data-t');
        var t = null;
        for (var i = 0; i < TASKS.length; i++) if (TASKS[i].id === id) t = TASKS[i];
        if (!t) return;
        var nm = b.querySelector('.nm');
        if (!nm) return;
        var inp = document.createElement('input');
        inp.className = 'task-rename';
        inp.value = t.name || '';
        nm.replaceWith(inp);
        inp.focus(); inp.select();
        var done = false;
        var commit = function(ok){
          if (done) return;
          done = true;
          if (ok) { t.name = inp.value.trim().slice(0, 40); t.renamed = true; }
          saveTasks(); renderTabs();
        };
        inp.addEventListener('keydown', function(e){
          if (e.key === 'Enter') commit(true);
          if (e.key === 'Escape') commit(false);
        });
        inp.addEventListener('blur', function(){ commit(true); });
      });
    });
    var add = box.querySelector('.task-add');
    if (add) add.addEventListener('click', function(){ newTask(false); });
  }
  function switchTask(id){
    if (!id || id === ACTIVE) return;
    snapshotTask(); saveTasks();
    ACTIVE = id; saveTasks();
    applyTask(activeTask()); renderTabs(); saveState();
  }
  function newTask(silent){
    if (ACTIVE) { snapshotTask(); saveTasks(); }
    var t = blankTask();
    TASKS.push(t); ACTIVE = t.id;
    saveTasks(); applyTask(t); renderTabs();
    if (!silent) toast(et('\u5df2\u65b0\u5efa\u4efb\u52a1', 'New task created'));
  }
  function closeTask(id){
    if (TASKS.length <= 1) return;
    TASKS = TASKS.filter(function(t){ return t.id !== id; });
    if (ACTIVE === id) { ACTIVE = TASKS[0].id; applyTask(activeTask()); }
    saveTasks(); renderTabs();
  }
  function adoptTasks(list, active){
    if (Array.isArray(list) && list.length){
      TASKS = list;
      ACTIVE = (active && TASKS.some(function(t){ return t.id === active; })) ? active : TASKS[0].id;
    } else {
      try { TASKS = JSON.parse(localStorage.getItem(TASKS_KEY) || '[]') || []; } catch (e) { TASKS = []; }
      try { ACTIVE = localStorage.getItem(ACTIVE_KEY) || null; } catch (e) { ACTIVE = null; }
      if (!Array.isArray(TASKS) || !TASKS.length) { newTask(true); return; }
      if (!TASKS.some(function(t){ return t.id === ACTIVE; })) ACTIVE = TASKS[0].id;
    }
    applyTask(activeTask()); renderTabs();
  }
  function loadTasks(){
    var localParams = null;
    try { localParams = JSON.parse(localStorage.getItem(PARAMS_KEY) || 'null'); } catch (e) {}
    apiFetch('/state').then(function(r){
      var st = r && r.state;
      if (st && st.theme) { try { applyTheme(st.theme); } catch (e) {} }
      if (st && st.lang) { try { applyLang(st.lang); } catch (e) {} }
      adoptTasks(st && st.tasks, st && st.active);
      applyParams((st && st.params) || localParams);
      applyConnDefault();
      if (st && st.savedAt) LAST_SEEN = st.savedAt;
    }).catch(function(){
      adoptTasks(null, null);
      applyParams(localParams);
      applyConnDefault();
    });
  }

  /* ===== \u4efb\u52a1\u7f16\u53f7 + \u91cd\u65b0\u8fde\u63a5 ===== */
  function paintBadge(){
    var b = $('#runBadge'); if (!b) return;
    var r = LAST_RUN;
    if (r && r.runId && r.finalText) {
      b.hidden = false;
      b.textContent = '#' + String(r.runId).replace(/^r-/, '').slice(0, 8);
    } else {
      b.hidden = true;
      b.textContent = '';
    }
  }
  function refreshRun(){
    var job = loadJob();
    var rid = (job && job.runId) || (LAST_RUN && LAST_RUN.runId);
    if (!rid) { toast(et('\u8fd8\u6ca1\u6709\u53ef\u5237\u65b0\u7684\u6539\u5199', 'Nothing to refresh yet')); return; }
    var out = $('#outText');
    if (out) out.innerHTML = '<div class="out-working"><div class="wk-head">'
      + esc(et('\u6b63\u5728\u91cd\u65b0\u8fde\u63a5\u2026', 'Reconnecting\u2026')) + '</div></div>';
    var wasMeasure = !!(LAST_RUN && LAST_RUN.runId === rid && !LAST_RUN.finalText);
    apiFetch('/run?id=' + encodeURIComponent(rid)).then(function(st){
      if (st.status === 'done' && st.result) {
        clearJob(); renderAll(st.result, 0); paintBadge(); saveState();
        if (wasMeasure && !st.result.finalText) {
          toast(et('\u8fd9\u4e00\u6b21\u53ea\u6709\u8bc4\u5206\uff0c\u6ca1\u6709\u6539\u5199\u7a3f', 'This run only has a score, no rewrite'));
        }
        snapshotTask(); saveTasks();
        toast(et('\u5df2\u91cd\u65b0\u8fde\u63a5', 'Reconnected'));
      } else if (st.status === 'running') {
        resumeJob();
      } else if (st.status === 'error') {
        toast(et('\u6539\u5199\u5931\u8d25\uff1a', 'Rewrite failed: ')
          + ((st.error && st.error.message) || ''));
      }
    }).catch(function(){
      if (out) out.innerHTML = '<div class="out-working">'
        + esc(et('\u4efb\u52a1\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u53d1\u8d77\u6539\u5199', 'This run has expired; start a new rewrite'))
        + '</div>';
      toast(et('\u4efb\u52a1\u5df2\u8fc7\u671f', 'Run expired'));
    });
  }
  /* ===== 评分（右上角按钮触发，不再输入即评） ===== */
  var scoreDoneTimer = null;
  function emptyScoreCard(){
    var box = $('#scoreRow');
    if (!box) return;
    box.innerHTML = '<div class="sc-eval">'
      + esc(et('\u70b9\u51fb\u53f3\u4e0a\u89d2\u300c\u8bc4\u5206\u300d\u5f00\u59cb\u8bc4\u4f30', 'Click Score to evaluate')) + '</div>';
  }
  function scoreBars(on){
    var bars = document.querySelectorAll('#scoreRow .sc .bar i, #scoreRow .sc-eval .eval-bar i');
    Array.prototype.forEach.call(bars, function(b){
      if (on){
        var parent = b.parentNode;
        var pw = parent ? parent.getBoundingClientRect().width : 0;
        var bw = b.getBoundingClientRect().width;
        var pct = pw > 0 ? Math.round(bw / pw * 100) : 0;
        if (pct > 100) pct = 100;
        b.dataset.prevW = b.style.width || '';
        b.dataset.prevT = b.style.transition || '';
        b.style.transition = 'none';
        b.style.width = pct + '%';
        void b.offsetWidth;
        var dur = 14 + Math.min(8, Math.round(pct / 15));
        b.style.transition = 'width ' + dur + 's cubic-bezier(.15,.75,.3,1) ' + (pct >= 94 ? 0 : 0.2) + 's';
        b.style.width = (pct >= 94 ? pct : Math.max(pct + 3, 94)) + '%';
      } else {
        b.style.transition = b.dataset.prevT || '';
        b.style.width = b.dataset.prevW || '';
      }
    });
  }
  function setScoreBusy(on){
    var b = $('#btnScore');
    var row = $('#scoreRow');
    if (row) row.classList[on ? 'add' : 'remove']('is-measuring');
    try { scoreBars(on); } catch (e) {}
    var c = $('#btnScoreClear');
    if (c) c.disabled = !!on;
    if (!b) return;
    if (on) { clearTimeout(scoreDoneTimer); b.classList.remove('is-done'); }
    b.dataset.busy = on ? '1' : '0';
    b.disabled = !!on;
    b.textContent = on ? et('\u8bc4\u5206\u4e2d\u2026', 'Scoring\u2026') : t('sc.run');
  }
  function flashScoreDone(){
    var b = $('#btnScore');
    if (!b) return;
    clearTimeout(scoreDoneTimer);
    b.textContent = et('\u2713 \u5df2\u8bc4\u5206', '\u2713 Scored');
    b.classList.add('is-done');
    scoreDoneTimer = setTimeout(function(){
      b.classList.remove('is-done');
      b.textContent = t('sc.run');
    }, 1000);
  }
  function runScore(){
    var text = (draft && draft.value) || '';
    if (text.trim().length < 20){
      toast(et('\u8bf7\u5148\u8f93\u5165\u6b63\u6587', 'Add the text first'));
      return;
    }
    var b = $('#btnScore');
    if (b && b.dataset.busy === '1') return;
    var zh = isChinese(text);
    var sec = $('#sec-draft');
    if (zh && sec && !(LAST_MEASURE && LAST_MEASURE.draft)) {
      sec.hidden = false;
      sec.dataset.busy = '1';
      $('#draftText').innerHTML = '<p class="draft-pending">' + esc(et('\u6b63\u5728\u7ffb\u8bd1\u4e2d\u2026', 'Translating\u2026')) + '</p>';
    }
    var box = $('#scoreRow');
    if (box && !LAST_MEASURE) {
      box.innerHTML = '<div class="sc-eval"><span>' + esc(et('\u6b63\u5728\u8bc4\u4f30\u4e2d\u2026', 'Evaluating\u2026')) + '</span>'
        + '<div class="eval-bar"><i></i></div></div>';
    }
    setScoreBusy(true);
    startRun('measure', function(run){
      var t0 = activeTask();
      if (t0){ t0.scoreCleared = false; t0.lastRunId = run.runId || t0.lastRunId; t0.lastRunAt = Date.now(); }
      renderAll(run, 0);
      if (sec) sec.dataset.busy = '0';
      setScoreBusy(false);
      flashScoreDone();
      syncDraftVisibility();
      snapshotTask();
      try { pushState(0); } catch (e) {}
      saveTasks();
    }, function(err){
      if (sec) sec.dataset.busy = '0';
      setScoreBusy(false);
      if (zh) syncDraftVisibility();
      if (box) box.innerHTML = '<div class="sc-eval">'
        + esc(et('\u8bc4\u4f30\u5931\u8d25\uff1a', 'Evaluation failed: ') + (err.message || err)) + '</div>';
    }, null);
  }
  var btnScore = $('#btnScore');
  if (btnScore) btnScore.addEventListener('click', function(){ runScore(); });
  var btnScoreClear = $('#btnScoreClear');
  if (btnScoreClear) btnScoreClear.addEventListener('click', function(){
    LAST_MEASURE = null;
    var t = activeTask();
    if (t) { t.measured = null; t.scoreCleared = true; }
    emptyScoreCard();
    var grid = $('#issuesGrid'); if (grid) grid.innerHTML = '';
    snapshotTask(); saveTasks();
    toast(et('\u5df2\u6e05\u7a7a\u8bc4\u5206', 'Score cleared'));
  });
  if (draft) draft.addEventListener('input', function(){ syncDraftVisibility(); });

  /* ===== 评分卡：磁吸 + 详情浮窗 ===== */
  var scMask = $('#scMask'), scTitle = $('#scModalTitle'), scScore = $('#scModalScore');
  var scEv = $('#scModalEv'), scList = $('#scModalList');
  function closeScoreModal(){ if (scMask) scMask.hidden = true; }
  function openScoreModal(dim){
    if (!LAST_MEASURE){ toast(et('\u5148\u70b9\u8bc4\u5206\uff0c\u518d\u770b\u8be6\u60c5', 'Score first, then open the detail')); return; }
    var s = LAST_MEASURE.score || {}, rows = s.rows || [];
    var items = [], meta = [];
    if (dim === 'TOTAL'){
      scTitle.textContent = t('sc.total');
      scScore.textContent = (s.total == null ? '\u2014' : s.total) + ' / 100';
      scEv.textContent = s.advice || '';
      if (s.band) meta.push(et('\u6863\u4f4d', 'Band') + ' \u00b7 ' + s.band);
      if (s.editing_latitude != null) meta.push(et('\u7f16\u8f91\u5e45\u5ea6', 'Latitude') + ' \u00b7 ' + s.editing_latitude + '%');
    } else {
      var row = rows.filter(function(r){ return r.id === dim; })[0] || {};
      scTitle.textContent = dim + ' \u00b7 ' + t('sc.' + String(dim).toLowerCase());
      scScore.textContent = (row.score == null ? '\u2014' : row.score) + ' / ' + (row.max == null ? '\u2014' : row.max);
      scEv.textContent = row.evidence || '';
      var want = dim === 'P1' ? 'polish' : (dim === 'T1' ? 'trace' : 'prosody');
      if (dim !== 'P2'){
        ((LAST_MEASURE.findings) || []).forEach(function(f){
          if (f.player !== want) return;
          var ev = evidenceOf(f);
          var cl = codeLabel(ev.code);
          items.push({
            code: (cl && cl !== 'MODEL') ? cl : '',
            text: f.original || ev.message || '',
            note: f.paragraph ? et('\u7b2c ' + f.paragraph + ' \u6bb5', 'paragraph ' + f.paragraph) : et('\u5168\u6587', 'whole text')
          });
        });
      } else {
        var p2 = p2Label(row.evidence || '');
        if (p2) items.push({ code: 'P2', text: p2, note: '' });
      }
    }
    var out = '';
    if (meta.length) out += '<div class="sc-meta">' + meta.map(function(m){ return '<span>' + esc(m) + '</span>'; }).join('') + '</div>';
    if (items.length){
      out += items.map(function(it){
        return '<div class="sc-m-item">' + (it.code ? '<span class="code">' + esc(it.code) + '</span>' : '')
          + '<p>' + esc(it.text) + '</p>' + (it.note ? '<small>' + esc(it.note) + '</small>' : '') + '</div>';
      }).join('');
    }
    if (!out) out = '<div class="sc-m-item"><p>' + esc(et('\u6ca1\u6709\u66f4\u591a\u7ec6\u8282', 'No further detail')) + '</p></div>';
    scList.innerHTML = out;
    if (scMask) scMask.hidden = false;
  }
  if (scMask){
    var scCloseBtn = $('#scClose'), scOkBtn = $('#scOk');
    if (scCloseBtn) scCloseBtn.addEventListener('click', closeScoreModal);
    if (scOkBtn) scOkBtn.addEventListener('click', closeScoreModal);
    scMask.addEventListener('click', function(e){ if (e.target === scMask) closeScoreModal(); });
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && !scMask.hidden) closeScoreModal(); });
  }
  /* ===== 本地 skill 选择 ===== */
  var skillMask = $('#skillMask'), skillList = $('#skillList');
  var skillSearch = $('#skillSearch'), skillAll = $('#skillAll');
  var SKILLS = [], skillQuery = '';
  function closeSkillPicker(){ if (skillMask) skillMask.hidden = true; }
  function skillVisible(){
    return SKILLS.filter(function(s){
      if (!(skillAll && skillAll.checked) && s.kind !== 'writing') return false;
      if (!skillQuery) return true;
      var hay = (String(s.name || s.id) + ' ' + String(s.description || '')).toLowerCase();
      return hay.indexOf(skillQuery) >= 0;
    });
  }
  function renderSkillList(){
    if (!skillList) return;
    if (!SKILLS.length){
      skillList.innerHTML = '<div class="skill-empty">' + esc(et('\u6ca1\u6709\u627e\u5230\u672c\u5730 skill', 'No local skills found')) + '</div>';
      return;
    }
    var list = skillVisible();
    if (!list.length){
      skillList.innerHTML = '<div class="skill-empty">' + esc(et('\u6ca1\u6709\u5339\u914d\u7684 skill\uff1b\u53ef\u52fe\u9009\u300c\u663e\u793a\u5168\u90e8\u300d', 'No match; tick "Show all skills"')) + '</div>';
      return;
    }
    skillList.innerHTML = list.map(function(s){
      var desc = String(s.description || '').slice(0, 120);
      return '<button class="skill-item" type="button" data-skill="' + esc(s.id) + '">'
        + '<b>' + esc(s.name || s.id) + '</b>'
        + (desc ? '<i>' + esc(desc) + '</i>' : '')
        + '</button>';
    }).join('');
  }
  function openSkillPicker(){
    if (!skillMask) return;
    skillMask.hidden = false;
    skillQuery = '';
    if (skillSearch) skillSearch.value = '';
    if (skillList) skillList.innerHTML = '<div class="skill-empty">' + esc(et('\u6b63\u5728\u8bfb\u53d6\u672c\u5730 skill\u2026', 'Reading local skills\u2026')) + '</div>';
    apiFetch('/skills').then(function(r){
      SKILLS = (r && r.skills) || [];
      renderSkillList();
    }).catch(function(){ SKILLS = []; renderSkillList(); });
  }
  if (skillSearch) skillSearch.addEventListener('input', function(){
    skillQuery = String(skillSearch.value || '').trim().toLowerCase();
    renderSkillList();
  });
  if (skillAll) skillAll.addEventListener('change', function(){ renderSkillList(); });
  if (skillMask){
    var skillCloseBtn = $('#skillClose'), skillOkBtn = $('#skillOk');
    if (skillCloseBtn) skillCloseBtn.addEventListener('click', closeSkillPicker);
    if (skillOkBtn) skillOkBtn.addEventListener('click', closeSkillPicker);
    skillMask.addEventListener('click', function(e){ if (e.target === skillMask) closeSkillPicker(); });
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && !skillMask.hidden) closeSkillPicker(); });
    if (skillList) skillList.addEventListener('click', function(e){
      var btn = e.target && e.target.closest ? e.target.closest('.skill-item') : null;
      if (!btn) return;
      GS.engine = 'skill:' + btn.getAttribute('data-skill');
      closeSkillPicker();
      paintGame();
      toast(et('\u5f15\u64ce\u5df2\u5207\u6362\u4e3a ', 'Engine set to ') + btn.getAttribute('data-skill'));
    });
  }

  /* ===== 历史记录页 ===== */
  var histPage = $('#histPage'), histList = $('#histList'), histPager = $('#histPager');
  var histSearch = $('#histSearch'), histModeSel = $('#histModeSel');
  var HIST_RUNS = [], histQuery = '', histMode = 'all', histPage_ = 1;
  var HIST_PAGE_SIZE = 5;
  function histWhen(ts){
    if (!ts) return '';
    var d = new Date(ts);
    var p2 = function(n){ return (n < 10 ? '0' : '') + n; };
    return (d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
  }
  function histScore(sc){
    if (!sc) return null;
    if (Array.isArray(sc.rows) && sc.rows.length) return weightedTotal(sc.rows);
    return (sc.total == null) ? null : sc.total;
  }
  function histDiffHtml(r){
    var base = String(r.baseText || '');
    var text = String(r.finalText || '');
    var edits = (r.edits || []).slice().sort(function(a, b){
      return ((a.base_range || {}).start || 0) - ((b.base_range || {}).start || 0);
    });
    if (!base || !edits.length) return esc(text || base);
    var html = '', cur = 0;
    edits.forEach(function(e, idx){
      var br = e.base_range || {}, ar = e.after_range || {};
      var s = br.start, en = br.end;
      if (!(s >= 0) || !(en >= s) || en > base.length || s < cur) return;
      html += esc(base.slice(cur, s));
      var del = String(e.original || base.slice(s, en) || '');
      var add = (e.op === 'delete') ? '' : String(text.slice(ar.start, ar.end) || '');
      var n = idx + 1;
      if (del) html += '<span class="dele"><s>' + esc(del) + '</s><sup>' + n + '</sup></span>';
      if (add) html += '<mark>' + esc(add) + '<sup>' + n + '</sup></mark>';
      cur = en;
    });
    html += esc(base.slice(cur));
    return html;
  }
  function histDetailHtml(r){
    var base = String(r.baseText || '');
    var fin = String(r.finalText || '');
    var s1 = histScore(r.score), s2 = histScore(r.finalScore);
    var head = function(label, sc){
      return '<h5>' + esc(label)
        + (sc == null ? '' : ' <b>' + esc(String(sc)) + '</b><span>/ 100</span>')
        + '</h5>';
    };
    if (!base && !fin) return '<div class="skill-empty">' + esc(et('\u8fd9\u6b21\u8fd0\u884c\u6ca1\u6709\u4fdd\u5b58\u6587\u672c', 'No text stored for this run')) + '</div>';
    var cols = '<div class="hist-cols">'
      + '<div class="hist-col">' + head(et('\u539f\u6587', 'Original'), s1)
      + '<pre class="hist-text">' + esc(base || et('\uff08\u65e0\uff09', '(none)')) + '</pre></div>';
    if (fin){
      cols += '<div class="hist-col">' + head(et('\u6da6\u8272\u540e', 'Polished'), s2)
        + '<pre class="hist-text">' + histDiffHtml(r) + '</pre>'
        + '<div class="hist-legend">'
        + '<label><input type="checkbox" class="hist-showdel" /> ' + esc(et('\u663e\u793a\u5220\u9664', 'Show deletions')) + '</label>'
        + '<span>' + esc(et('\u7eff\u5e95 = \u65b0\u589e\u6216\u66ff\u6362', 'green = added/replaced')) + '</span>'
        + '</div></div>';
    }
    cols += '</div>';
    return cols;
  }
  function toggleHistDetail(item){
    var detail = item.querySelector('.hist-detail');
    if (!detail) return;
    var row = item.querySelector('.hist-row');
    var chev = item.querySelector('.hist-chev');
    if (!detail.hidden){
      detail.hidden = true;
      if (row) row.classList.remove('open');
      if (chev) chev.textContent = '\u25b8';
      return;
    }
    Array.prototype.forEach.call(document.querySelectorAll('#histList .hist-item'), function(other){
      if (other === item) return;
      var od = other.querySelector('.hist-detail');
      if (!od || od.hidden) return;
      od.hidden = true;
      var orow = other.querySelector('.hist-row'); if (orow) orow.classList.remove('open');
      var ochev = other.querySelector('.hist-chev'); if (ochev) ochev.textContent = '\u25b8';
    });
    if (row) row.classList.add('open');
    if (chev) chev.textContent = '\u25be';
    try {
      item.scrollIntoView({ block: 'start', behavior: 'smooth' });
      setTimeout(function(){
        var se = document.scrollingElement || document.documentElement;
        if (se && se.scrollTop > 64) se.scrollTop = se.scrollTop - 64;
        var sc = null;
        for (var el = item.parentElement; el && el !== document.body; el = el.parentElement){
          var st2 = window.getComputedStyle(el);
          if ((st2.overflowY === 'auto' || st2.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 4){ sc = el; break; }
        }
        if (sc && sc.scrollTop > 64) sc.scrollTop = sc.scrollTop - 64;
      }, 320);
    } catch (e) {}
    if (detail.dataset.loaded === '1'){ detail.hidden = false; return; }
    var runId = item.getAttribute('data-run');
    detail.hidden = false;
    detail.innerHTML = '<div class="skill-empty">' + esc(et('\u6b63\u5728\u8bfb\u53d6\u2026', 'Loading\u2026')) + '</div>';
    apiFetch('/run?id=' + encodeURIComponent(runId)).then(function(st){
      if (!st || !st.result){ detail.innerHTML = '<div class="skill-empty">' + esc(et('\u7ed3\u679c\u5df2\u4e22\u5931', 'Result missing')) + '</div>'; return; }
      detail.innerHTML = histDetailHtml(st.result);
      detail.dataset.loaded = '1';
      var delBox = detail.querySelector('.hist-showdel');
      var pre = detail.querySelector('.hist-text');
      if (delBox && pre) delBox.addEventListener('change', function(){ pre.classList.toggle('show-del', !!delBox.checked); });
    }).catch(function(){
      detail.innerHTML = '<div class="skill-empty">' + esc(et('\u8bfb\u53d6\u5931\u8d25', 'Failed to load')) + '</div>';
    });
  }
  function histGroupOf(runs){
    var groups = [], map = {};
    runs.forEach(function(r){
      var k = r.textKey || r.runId;
      if (!map[k]){ map[k] = { key: k, preview: r.preview || r.taskName || '', runs: [] }; groups.push(map[k]); }
      map[k].runs.push(r);
    });
    return groups;
  }
  function histRender(){
    if (!histList) return;
    var q = String(histQuery || '').trim().toLowerCase();
    var runs = (HIST_RUNS || []).filter(function(r){
      if (histMode !== 'all' && r.mode !== histMode) return false;
      if (!q) return true;
      var hay = [r.preview, r.draftId, r.runId, r.taskName, histWhen(r.startedAt),
        r.mode === 'measure' ? 'score \u8bc4\u5206' : 'rewrite \u6539\u5199'].join(' ').toLowerCase();
      return hay.indexOf(q) >= 0;
    });
    var groups = histGroupOf(runs);
    var pages = Math.max(1, Math.ceil(groups.length / HIST_PAGE_SIZE));
    if (histPage_ > pages) histPage_ = pages;
    var slice = groups.slice((histPage_ - 1) * HIST_PAGE_SIZE, histPage_ * HIST_PAGE_SIZE);
    if (!groups.length){
      histList.innerHTML = '<div class="skill-empty">' + esc(et('\u6ca1\u6709\u5339\u914d\u7684\u8bb0\u5f55', 'No matching entries')) + '</div>';
    } else {
      histList.innerHTML = slice.map(function(g){
        var latest = g.runs[0];
        var rows = g.runs.map(function(x){
          var num = x.draftId || String(x.runId || '').replace(/^r-/, '');
          var mode = x.mode === 'measure' ? et('\u8bc4\u5206', 'Score') : et('\u6539\u5199', 'Rewrite');
          var stat = (x.mode === 'measure' && x.score != null) ? (x.score + ' / 100')
            : (x.edits ? x.edits + ' ' + et('\u5904\u6539\u52a8', 'edits') : '');
          return '<div class="hist-item" data-run="' + esc(x.runId) + '">'
            + '<div class="hist-row">'
            + '<span class="hist-when">' + esc(histWhen(x.startedAt)) + '</span>'
            + '<span class="hist-num">' + esc(num) + '</span>'
            + '<span class="hist-mode">' + esc(mode) + '</span>'
            + '<span class="hist-stat">' + esc(stat) + '</span>'
            + '<span class="grow"></span>'
            + '<span class="hist-chev">\u25b8</span>'
            + '<button class="tiny" type="button" data-open="' + esc(x.runId) + '" data-task="' + esc(x.taskId) + '">'
            + esc(et('\u6253\u5f00', 'Open')) + '</button>'
            + '</div>'
            + '<div class="hist-detail" hidden></div>'
            + '</div>';
        }).join('');
        return '<div class="hist-group">'
          + '<div class="hist-group-head" data-key="' + esc(g.key) + '">'
          + '<span class="hist-group-preview">' + esc(g.preview || et('\uff08\u65e0\u6587\u672c\uff09', '(no text)')) + '</span>'
          + '<span class="hist-group-count">' + g.runs.length + ' ' + esc(et('\u6b21\u8fd0\u884c', 'runs')) + '</span>'
          + '<span class="hist-group-when">' + esc(histWhen(latest && latest.startedAt)) + '</span>'
          + '<span class="hist-toggle">\u25be</span>'
          + '</div>'
          + '<div class="hist-group-body" hidden>' + rows + '</div>'
          + '</div>';
      }).join('');
    }
    if (histPager){
      histPager.innerHTML = '<span>' + esc(et('\u7b2c ' + histPage_ + ' / ' + pages + ' \u9875', 'Page ' + histPage_ + ' / ' + pages)) + '</span>'
        + '<button class="tiny" type="button" data-p="prev"' + (histPage_ <= 1 ? ' disabled' : '') + '>' + esc(et('\u4e0a\u4e00\u9875', 'Prev')) + '</button>'
        + '<button class="tiny" type="button" data-p="next"' + (histPage_ >= pages ? ' disabled' : '') + '>' + esc(et('\u4e0b\u4e00\u9875', 'Next')) + '</button>';
    }
  }
  function closeHistory(){
    if (histPage) histPage.hidden = true;
    var wrap = document.querySelector('.wrap'); if (wrap) wrap.hidden = false;
  }
  function openHistory(){
    if (!histPage || !histList) return;
    var wrap = document.querySelector('.wrap'); if (wrap) wrap.hidden = true;
    histPage.hidden = false;
    histQuery = ''; histPage_ = 1;
    if (histSearch) histSearch.value = '';
    if (histModeSel) histModeSel.value = 'all';
    histMode = 'all';
    histList.innerHTML = '<div class="skill-empty">' + esc(et('\u6b63\u5728\u8bfb\u53d6\u2026', 'Loading\u2026')) + '</div>';
    apiFetch('/history').then(function(r){
      HIST_RUNS = (r && r.runs) || [];
      histRender();
    }).catch(function(){
      histList.innerHTML = '<div class="skill-empty">' + esc(et('\u8bfb\u53d6\u5931\u8d25', 'Failed to load')) + '</div>';
    });
  }
  var btnHistory = $('#btnHistory');
  if (btnHistory) btnHistory.addEventListener('click', function(){
    if (histPage && !histPage.hidden) closeHistory();
    else openHistory();
  });
  var histBack = $('#histBack');
  if (histBack) histBack.addEventListener('click', closeHistory);
  if (histSearch) histSearch.addEventListener('input', function(){
    histQuery = String(histSearch.value || '');
    histPage_ = 1;
    histRender();
  });
  if (histModeSel) histModeSel.addEventListener('change', function(){
    histMode = histModeSel.value || 'all';
    histPage_ = 1;
    histRender();
  });
  if (histPager) histPager.addEventListener('click', function(e){
    var btn = e.target && e.target.closest ? e.target.closest('button[data-p]') : null;
    if (!btn) return;
    if (btn.getAttribute('data-p') === 'prev') histPage_ = Math.max(1, histPage_ - 1);
    else histPage_ = histPage_ + 1;
    histRender();
  });
  if (histList) histList.addEventListener('click', function(e){
    var head = e.target && e.target.closest ? e.target.closest('.hist-group-head') : null;
    if (!head) return;
    var body = head.parentNode.querySelector('.hist-group-body');
    if (!body) return;
    body.hidden = !body.hidden;
    var t = head.querySelector('.hist-toggle');
    if (t) t.textContent = body.hidden ? '\u25be' : '\u25b4';
  });
  if (histList) histList.addEventListener('click', function(e){
    if (!e.target || !e.target.closest) return;
    if (e.target.closest('button[data-open]')) return;
    if (e.target.closest('.hist-detail')) return;
    var item = e.target.closest('.hist-item');
    if (!item) return;
    toggleHistDetail(item);
  });
  if (histList) histList.addEventListener('click', function(e){
    var btn = e.target && e.target.closest ? e.target.closest('button[data-open]') : null;
    if (!btn) return;
    var runId = btn.getAttribute('data-open');
    var taskId = btn.getAttribute('data-task');
    closeHistory();
    apiFetch('/run?id=' + encodeURIComponent(runId)).then(function(st){
      if (!st || !st.result){ toast(et('\u8fd9\u6b21\u8fd0\u884c\u7684\u7ed3\u679c\u5df2\u4e22\u5931', 'This run has no stored result')); return; }
      var t = null;
      (TASKS || []).forEach(function(x){ if (x && x.id === taskId) t = x; });
      if (t){ ACTIVE = t.id; renderTabs(); applyTask(t); }
      renderAll(st.result, 0);
      if (t){ t.run = st.result; t.lastRunId = st.result.runId || runId; t.lastDraftId = st.result.draftId || t.lastDraftId; }
      try { snapshotTask(); pushState(0); saveTasks(); } catch (e) {}
      toast(et('\u5df2\u6253\u5f00\u8fd9\u6b21\u8fd0\u884c', 'Opened this run'));
    }).catch(function(err){
      toast(et('\u6253\u5f00\u5931\u8d25\uff1a', 'Open failed: ') + (err.message || err));
    });
  });

  /* 版本更新提示：/build 与当前构建不一致时，顶部提示一键刷新 */
  (function(){
    var lastBuild = window.__etsBuild || '';
    if (!lastBuild) return;
    var shown = false;
    function showUpdate(v){
      if (shown) return;
      shown = true;
      var host = document.querySelector('.ets-root') || document.body;
      var bar = document.createElement('div');
      bar.className = 'ets-update';
      bar.innerHTML = '<span>' + (et ? et('\u63d2\u4ef6\u5df2\u66f4\u65b0\u5230 v' + v, 'Plugin updated to v' + v) : ('v' + v)) + '</span>';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = (et ? et('\u5237\u65b0\u9875\u9762', 'Reload page') : 'Reload');
      btn.addEventListener('click', function(){ location.reload(); });
      bar.appendChild(btn);
      host.appendChild(bar);
    }
    setInterval(function(){
      fetch('/econtone-studio/api/build', { cache: 'no-store' }).then(function(r){ return r.json() }).then(function(j){
        var v = j && j.data && j.data.version;
        if (v && v !== lastBuild) showUpdate(v);
      }).catch(function(){});
    }, 6000);
  })();

  var scRow = $('#scoreRow');
  if (scRow) scRow.addEventListener('input', function(e){
    var inp = e.target && e.target.closest ? e.target.closest('input[data-w]') : null;
    if (!inp) return;
    var id = inp.getAttribute('data-w');
    var v = Math.round(Number(inp.value));
    if (!isFinite(v)) v = 0;
    v = Math.max(0, Math.min(100, v));
    if (!GS.weights) GS.weights = { P1: 40, P2: 40, T1: 10, R1: 10 };
    GS.weights[id] = v;
    repaintTotals();
    if (typeof scheduleSaveParams === 'function') scheduleSaveParams();
  });
  if (scRow) scRow.addEventListener('click', function(e){
    if (e.target && e.target.closest && e.target.closest('input, label, .wt')) return;
    var el = e.target && e.target.closest ? e.target.closest('.sc, .total') : null;
    if (!el) return;
    var dim = el.getAttribute('data-dim');
    if (!dim){ toast(et('\u5148\u70b9\u8bc4\u5206\uff0c\u518d\u770b\u8be6\u60c5', 'Score first, then open the detail')); return; }
    openScoreModal(dim);
  });
  /* ===== GitHub 仓库入口（开源后填写 REPO_URL） ===== */
  var REPO_URL = 'https://github.com/pykkkkk/EconTone-Studio';
  var btnRepo = $('#btnRepo');
  if (btnRepo) btnRepo.addEventListener('click', function(){
    if (!REPO_URL){
      toast(et('\u4ed3\u5e93\u5730\u5740\u5c06\u5728\u5f00\u6e90\u540e\u516c\u5e03', 'The repository link will be published after open-sourcing'));
      return;
    }
    apiFetch('/open-page', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url: REPO_URL })
    }).catch(function(){
      try { window.open(REPO_URL, '_blank', 'noopener'); } catch (e) {}
    });
  });

  /* ===== 按运行记录取回结果 ===== */
  var btnRunRefresh = $('#btnRunRefresh');
  if (btnRunRefresh) btnRunRefresh.addEventListener('click', function(){
    var t = activeTask();
    if (!t){ toast(et('还没有任务', 'No task yet')); return; }
    btnRunRefresh.disabled = true;
    var q = '/run/refresh?task=' + encodeURIComponent(t.id);
    if (t.lastDraftId) q += '&draft=' + encodeURIComponent(t.lastDraftId);
    apiFetch(q).then(function(r){
      btnRunRefresh.disabled = false;
      if (!r) return;
      if (r.status === 'running' && r.runId){
        saveJob({ runId: r.runId, mode: 'measure', startedAt: r.startedAt || Date.now() });
        resumeJob();
        toast(et('运行中，正在接回…', 'Still running; re-attaching…'));
        return;
      }
      if (r.status === 'done' && r.result){
        if (r.runId && t.run && t.run.runId === r.runId){
          toast(et('没有更新的结果', 'No newer result'));
          return;
        }
        t.lastRunId = r.runId || t.lastRunId;
        if (r.draftId) t.lastDraftId = r.draftId;
        t.lastRunAt = r.startedAt || Date.now();
        renderAll(r.result, 0);
        snapshotTask();
        try { pushState(0); } catch (e) {}
        saveTasks();
        toast(et('已刷新到最新结果', 'Refreshed to the latest result'));
        return;
      }
      toast(et('没有找到这次运行的结果', 'No stored result for this task'));
    }).catch(function(e){
      btnRunRefresh.disabled = false;
      toast(et('刷新失败：', 'Refresh failed: ') + (e.message || e));
    });
  });

  var btnRefresh = $('#btnRefresh');
  if (btnRefresh) btnRefresh.addEventListener('click', function(){ refreshRun(); });

  /* 每个输入框一个一键清空的 × */
  function addClearButtons(){
    var list = root.querySelectorAll('input[type="text"], input:not([type])');
    Array.prototype.forEach.call(list, function(inp){
      if (inp.dataset.hasClear === '1') return;
      if (inp.id === 'budget' || inp.id === 'extent') return;
      inp.dataset.hasClear = '1';
      var wrap = document.createElement('span');
      wrap.className = 'inp-wrap';
      inp.parentNode.insertBefore(wrap, inp);
      wrap.appendChild(inp);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'inp-x';
      btn.textContent = '\u00d7';
      btn.title = '\u6e05\u7a7a';
      wrap.appendChild(btn);
      var sync = function(){ wrap.classList.toggle('has-val', !!inp.value); };
      inp.addEventListener('input', sync);
      btn.addEventListener('click', function(ev){
        ev.preventDefault();
        inp.value = '';
        sync();
        try { inp.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
        inp.focus();
      });
      sync();
    });
  }
  addClearButtons();

  apiFetch('/health').then(function(h){
    var scripts = (h.engine && h.engine.scripts) || {};
    var n = 0;
    Object.keys(scripts).forEach(function(k){ if (scripts[k].exists) n++; });
    var dot = $('#stDot');
    if (dot) dot.className = n === 5 ? 'dot' : 'dot r';
    var fp = $('#stFp');
    if (fp) fp.textContent = 'fp ' + ((h.engine && h.engine.fingerprint) || '—');
  }).catch(function(){
    var dot = $('#stDot');
    if (dot) dot.className = 'dot r';
  });

  var diffBox = $('#dtab-diff');
  if (diffBox) diffBox.addEventListener('click', function(e){
    var tgt = e.target;
    var btn = (tgt && tgt.closest) ? tgt.closest('button.mini') : null;
    if (!btn) return;
    var art = btn.closest('.hunk');
    if (!art || art.getAttribute('data-live') !== '1') return;
    setDecision(art.getAttribute('data-hunk'), btn.getAttribute('data-dec'));
    e.preventDefault(); e.stopPropagation();
  });

  var saveOut = $('#saveOut');
  if (saveOut) saveOut.addEventListener('click', function(){
    if (!LAST_RUN || !LAST_RUN.runId){ toast(t('toast.copyEmpty')); return; }
    saveOut.disabled = true;
    apiFetch('/run/apply', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ runId: LAST_RUN.runId, text: appliedText(LAST_RUN), decisions: DECISIONS }),
    }).then(function(r){
      toast(t('toast.saved') + ((r && r.path) || ''));
      saveOut.disabled = false;
    }).catch(function(err){
      toast(t('toast.saveFail') + ((err && err.message) || err));
      saveOut.disabled = false;
    });
  });

  function applyConnDefault(){
    var KEY = 'ets.connDefault.v1';
    var seen = null;
    try { seen = localStorage.getItem(KEY); } catch (e) {}
    if (seen) return;                       // 用户已经手动选过，尊重选择
    GS.conn = 'agent';
    if (TASKS && TASKS.length) {
      TASKS.forEach(function(t){
        if (t && t.params && typeof t.params === 'object') t.params.conn = 'agent';
      });
    }
    try { localStorage.setItem(KEY, 'agent'); } catch (e) {}
    try { $$('#connSeg button').forEach(function(b){ b.classList.toggle('on', b.dataset.c === GS.conn); }); } catch (e) {}
    try { paintGame(); } catch (e) {}
    try { scheduleSaveParams(); } catch (e) {}
    try { saveTasks(); } catch (e) {}
  }

  var popout = $('#btnPopout');
  if (popout && window.__etsStandalone) popout.style.display = 'none';
  if (popout && !window.__etsStandalone) popout.addEventListener('click', function(){
    var url = '/econtone-studio/page';
    var full = location.origin + url;
    var fallback = function(){
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(full);
      } catch (e) {}
      toast(t('open.blocked') + full);
    };
    // 宿主优先：Electron shell.openExternal 或系统默认浏览器（cmd start / open / xdg-open）
    apiFetch('/open-page', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    }).then(function(r){
      if (r && r.opened) { toast(t('open.opened') + (r.url || full)); return; }
      var win = null;
      try { win = window.open(url, 'econtone-studio', 'width=1360,height=900,menubar=no,toolbar=no,location=no'); } catch (e) {}
      if (!win) fallback();
    }).catch(fallback);
  });

  /* ===== 两个窗口同步：以宿主 /state 为准 ===== */
  function applyRemoteState(st){
    if (!st) return false;
    var changed = false;
    SYNCING = true;
    try {
      if (st.theme && st.theme !== theme) { try { applyTheme(st.theme); } catch (e) {} }
      if (st.lang && st.lang !== lang) { try { applyLang(st.lang); } catch (e) {} }
      if (Array.isArray(st.tasks) && st.tasks.length) {
        st.tasks.forEach(function(rt){
          if (!rt || !rt.id) return;
          var lt = null;
          (TASKS || []).forEach(function(x){ if (x && x.id === rt.id) lt = x; });
          if (!lt) return;
          if (!rt.run && lt.run) rt.run = lt.run;
          if (!rt.measured && lt.measured) rt.measured = lt.measured;
        });
        TASKS = st.tasks;
        ACTIVE = (st.active && st.tasks.some(function(t){ return t.id === st.active; })) ? st.active : st.tasks[0].id;
        applyParams(st.params || null);
        renderTabs();
        applyTask(activeTask());
        changed = true;
      } else if (st.params) {
        applyParams(st.params);
        changed = true;
      }
      if (st.savedAt) LAST_SEEN = st.savedAt;
    } finally {
      setTimeout(function(){ SYNCING = false; }, 400);
    }
    return changed;
  }

  function syncFromHost(silent){
    return apiFetch('/state').then(function(r){
      var st = r && r.state;
      if (!st) { if (!silent) toast(t('sync.none')); return; }
      var ok = applyRemoteState(st);
      if (!silent) toast(ok ? t('sync.done') : t('sync.none'));
    }).catch(function(){ if (!silent) toast(t('sync.none')); });
  }

  var btnSync = $('#btnSync');
  if (btnSync) btnSync.addEventListener('click', function(){ syncFromHost(false); });

  function pollState(){
    if (SYNCING || DIRTY) return;
    if (document.visibilityState === 'hidden') return;
    apiFetch('/state').then(function(r){
      var st = r && r.state;
      if (!st) return;
      if (st.savedAt && st.savedAt === LAST_SEEN) return;
      applyRemoteState(st);
    }).catch(function(){});
  }
  setInterval(pollState, 4000);

  var heroVer = $('#heroVer');
  var buildVersion = (typeof BUILD !== 'undefined' && BUILD) ? BUILD : (window.__etsBuild || '');
  if (heroVer && buildVersion) heroVer.textContent = 'v' + buildVersion;

  root.__etsAction = function(kind){
    if (kind === 'score'){ runMeasure(); return true; }
    if (kind === 'run'){ runArbitrate(); return true; }
    return false;
  };
"""


def main():
    html = read(SRC)

    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    main_html = re.search(r"<main class=\"main\">.*?</main>", html, re.S).group(0)
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)

    css = scope(style)
    # 适配宿主面板：填满容器，但不要 overflow:hidden（容器高度未定时会把内容裁没）
    css += """
.ets-root{height:100%;min-height:420px;position:relative;display:flex;flex-direction:column;}
.ets-root .main{flex:1;min-width:0;min-height:0;}
.ets-fatal{position:absolute;left:16px;right:16px;bottom:16px;z-index:99;
  background:#fdeced;color:#8a1f24;border:1px solid #f3c2c5;border-radius:10px;
  padding:12px 14px;font:12px/1.55 ui-monospace,Consolas,monospace;white-space:pre-wrap;
  max-height:50%;overflow:auto;}
"""

    body = main_html.replace('<main class="main">', '<div class="main">', 1)
    body = body.replace("</main>", "</div>", 1)
    body += '\n<div class="toast" id="toast"></div>'

    js = script
    js = js.replace("(function(){", "function initStudio(root){", 1)
    js = js.replace("var $  = function(s,r){ return (r||document).querySelector(s); };",
                    "var $  = function(s,r){ return (r||root).querySelector(s); };")
    js = js.replace("var $$ = function(s,r){ return Array.prototype.slice.call((r||document).querySelectorAll(s)); };",
                    "var $$ = function(s,r){ return Array.prototype.slice.call((r||root).querySelectorAll(s)); };\n"
                    "  function on(id, ev, fn){ var el = $('#'+id); if (el) el.addEventListener(ev, fn); return el; }")
    # 事件绑定统一走 on()：元素缺失时跳过，而不是让整个面板崩掉
    js = re.sub(r"\$\('#([A-Za-z0-9_-]+)'\)\.addEventListener\(", r"on('\1', ", js)
    js = js.replace("draft.addEventListener('input', refreshCounters);",
                    "if (draft) draft.addEventListener('input', refreshCounters);")
    js = js.replace("extent.addEventListener('input', paintExtent);",
                    "if (extent) extent.addEventListener('input', paintExtent);")
    js = js.replace("document.documentElement.lang = (lang === 'en') ? 'en' : 'zh-CN';",
                    "root.setAttribute('lang', (lang === 'en') ? 'en' : 'zh-CN');")
    js = js.replace("document.querySelector(", "root.querySelector(")
    # 主题设在面板根节点上（设计稿里是 html 元素，客户端里是 .ets-root）
    js = js.replace("var themeEl = document.documentElement;", "var themeEl = root;")
    # 设计稿里的侧边栏模拟入口不属于面板内容，去掉它的接线
    js = re.sub(r"\n[ \t]*\$\('#entryStudio'\)\.addEventListener\([^\n]*", "", js)
    # 收尾：把 IIFE 的 })(); 换成「引擎层 + return cleanup + }」
    assert js.rstrip().endswith("})();"), js.rstrip()[-40:]
    js = js.rstrip()[:-len("})();")] + ENGINE_JS + """
  return function(){ if (toastT) clearTimeout(toastT); };
}
"""
    # 自检：脚本里 $() 引用的每个 id 都必须真的在面板 HTML 里
    referenced = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    present = set(re.findall(r'id="([^"]+)"', body))
    missing = sorted(referenced - present)
    if missing:
        raise SystemExit("emit-client: script references missing ids: %s" % missing)

    def lit(s):
        assert "`" not in s and "${" not in s
        return s

    client = """window.__ModuleLoader__.load({
  id: 'econtone-studio',
  factory(require) {
    const ReactMod = require('react');
    const React = (ReactMod && typeof ReactMod.createElement === 'function')
      ? ReactMod
      : (ReactMod && ReactMod.default && typeof ReactMod.default.createElement === 'function'
          ? ReactMod.default
          : ReactMod);
    const h = React.createElement;

    const CSS = `%s`;

    const HTML = '<style>' + CSS + '</style>' + `%s`;

    const BUILD = '__BUILD__';

    function showFatal(node, err) {
      try {
        var box = node.querySelector('.ets-fatal');
        if (!box) { box = document.createElement('div'); box.className = 'ets-fatal'; node.appendChild(box); }
        box.textContent = '[EconTone Studio] build ' + BUILD + ' 渲染失败：\\n' + String((err && err.stack) || err);
      } catch (_) {}
    }

    %s

    function StudioPage() {
      return h('div', {
        className: 'ets-root',
        'data-build': BUILD,
        ref: function (node) {
          if (!node || node.__etsReady) return;
          node.__etsReady = true;
          if (!node.querySelector('.main')) { showFatal(node, new Error('面板标记未注入（HTML 为空）')); return; }
          try { initStudio(node); } catch (e) { console.error('[econtone-studio] init failed', e); showFatal(node, e); }
        },
        dangerouslySetInnerHTML: { __html: HTML },
      });
    }

    function PanelGlyph(props) {
      const size = (props && props.size) || 16;
      return h('svg', {
        'data-dsh-panel-entry': 'econtone-studio',
        viewBox: '0 0 256 256', width: size, height: size,
        'aria-hidden': 'true',
      },
        h('path', { fill: 'currentColor', d: 'M168 56 A74.29 74.29 0 0 1 168 200 A170 170 0 0 0 168 56 Z M88 200 A74.29 74.29 0 0 1 88 56 A170 170 0 0 0 88 200 Z' }),
        h('path', { fill: 'currentColor', d: 'M80 128 A48 48 0 1 0 176 128 A48 48 0 1 0 80 128 Z' }),
        h('path', { fill: '#E8B23A', d: 'M109.66 157.66 L157.66 109.66 L146.34 98.34 L98.34 146.34 Z' })
      );
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.effect(function () {
          const a = ctx.slots.inject('sidebar.panellist', function () {
            return ctx.slots.register({
              name: 'sidebar.panellist',
              id: 'econtone-studio',
              order: 20,
              label: function () { return 'EconTone Studio'; },
            }, PanelGlyph);
          });
          const b = ctx.slots.inject('main', function () {
            return ctx.slots.register({
              name: 'main',
              key: 'econtone-studio',
            }, StudioPage);
          });
          return function () { a(); b(); };
        }, 'econtone-studio: slots');
      },
    };
  },
});
""" % (lit(css), lit(body), js)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 版本号只在 package.json 里维护，构建时注入，避免两处漂移
    import json as _json
    version = _json.loads(read(os.path.join(ROOT, "package.json")))["version"]
    client = client.replace("__BUILD__", version)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(client)
    print("wrote", OUT, len(client), "chars", "build", version)

    # 同一份适配产物，出两个独立页：
    #   build/preview.html  —— 设计预览，双击就能看
    #   standalone.html —— 插件路由 /econtone-studio/page 返回的正式独立页
    PAGE_TPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>html,body{margin:0;height:100%%;background:#fff;} body{overflow:hidden;} .ets-root{height:100vh;}</style>
<style>%s</style>
</head>
<body>
<div class="ets-root">%s</div>
<script>%s</script>
<script>window.__etsStandalone = true; window.__etsBuild = '%s';</script>
<script>
(function(){
  var root = document.querySelector('.ets-root');
  try { initStudio(root); }
  catch (e) {
    var box = document.createElement('div');
    box.className = 'ets-fatal';
    box.textContent = '%s initStudio failed:' + String.fromCharCode(10) + ((e && e.stack) || e);
    root.appendChild(box);
  }
})();
</script>
</body>
</html>
"""

    def write_page(path, title, tag, ver):
        page = PAGE_TPL % (title, lit(css), lit(body), js, ver, tag)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(page)
        print("wrote", os.path.normpath(path), len(page), "chars")

    write_page(os.path.join(ROOT, "build", "preview.html"),
               "EconTone Studio \u00b7 preview", "[preview]", version)
    write_page(os.path.join(ROOT, "lib", "standalone.html"),
               "EconTone Studio", "[standalone]", version)


if __name__ == "__main__":
    main()
