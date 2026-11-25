/**
 * EconTone Studio — host half.
 *
 * Owns one HTTP surface under /econtone-studio/api and one subprocess call per
 * run. All writing rules live in engine/studio_bridge.py; this file only moves
 * bytes and keeps a small job table.
 *
 * Run artifacts: <workspace>/.econtone/runs/<runId>/  (input.md, run.json)
 */
import { fileURLToPath } from 'node:url'
import { mkdir, writeFile, readFile, readdir } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn as spawnProcess } from 'node:child_process'

export const inject = ['webServer', 'subprocess']

const BRIDGE = fileURLToPath(new URL('./engine/studio_bridge.py', import.meta.url))
const PREFIX = '/econtone-studio/api'
const MAX_BODY = 4 * 1024 * 1024

function defaultEngine() {
  return path.join(os.homedir(), '.dsh', 'skills')
}

function send(res, status, payload) {
  const body = JSON.stringify(payload)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(body)
}

function ok(res, data) {
  send(res, 200, { ok: true, data })
}

function fail(res, status, code, message, detail) {
  send(res, status, { ok: false, error: { code, message, detail } })
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY) {
        reject(new Error('BODY_TOO_LARGE'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function sameOrigin(ctx, req) {
  const origin = req.headers.origin
  if (!origin) return true
  try {
    return new URL(origin).host === new URL(ctx.webServer.getOrigin()).host
  } catch {
    return false
  }
}

export function apply(ctx, config = {}) {
  const engineRoot = config.engineRoot || defaultEngine()
  const pythonPath = config.pythonPath || 'python'
  const llmMaxTokens = Number(config.maxTokens || 2800)
  const llmReasoningEffort = config.reasoningEffort || 'low'
  const maxChars = Number(config.maxChars || 200000)
  const timeoutMs = Number(config.timeoutMs || 120000)
  const dshHome = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
  const workspaceRoot = config.workspaceRoot || path.join(dshHome, 'workspace')
  const jobs = new Map()
  const diag = { llm: { at: 0 }, agent: { at: 0 }, lastRun: null }
  let lastAgent = null

  function runRoot() {
    return path.join(workspaceRoot, '.econtone', 'runs')
  }

  function spawnBridge(args, cwd, signal) {
    const spec = {
      argv: [pythonPath, BRIDGE, ...args],
      cwd,
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: 8 << 20, spill: { maxBytes: 64 << 20 } },
        stderr: { maxBytes: 1 << 20 },
      },
      graceMs: 3000,
      signal,
    }
    return ctx.subprocess.spawn(spec)
  }

  async function finish(handle, timeout) {
    let timer
    const timeoutPromise = new Promise((_, reject) => {
      timer = setTimeout(() => {
        try { handle.terminate() } catch {}
        reject(Object.assign(new Error('bridge timeout'), { code: 'TIMEOUT' }))
      }, timeout)
    })
    try {
      const outcome = await Promise.race([handle.done, timeoutPromise])
      const stdout = handle.collected.stdout ? handle.collected.stdout.readFrom(0) : { text: '' }
      const stderr = handle.collected.stderr ? handle.collected.stderr.readFrom(0) : { text: '' }
      return { code: outcome.exitCode, stdout: stdout.text || '', stderr: stderr.text || '' }
    } finally {
      clearTimeout(timer)
    }
  }

  async function callBridge(args, cwd, timeout = timeoutMs) {
    const handle = spawnBridge(args, cwd)
    const out = await finish(handle, timeout)
    if (out.code !== 0) {
      let parsed
      try { parsed = JSON.parse(out.stdout) } catch {}
      const err = (parsed && parsed.error) || {}
      throw Object.assign(new Error(err.message || out.stderr.slice(-400) || 'bridge failed'), {
        code: err.code || 'ENGINE_FAILED',
        detail: out.stderr.slice(-1200),
      })
    }
    const parsed = JSON.parse(out.stdout)
    if (!parsed.ok) {
      throw Object.assign(new Error(parsed.error?.message || 'bridge error'), {
        code: parsed.error?.code || 'ENGINE_FAILED',
      })
    }
    return parsed.data
  }

  async function handleBuild(req, res) {
    try {
      let version = ''
      for (const rel of ['./package.json', '../package.json']) {
        try {
          const pkg = JSON.parse(await readFile(new URL(rel, import.meta.url), 'utf8'))
          version = String(pkg.version || '')
          if (version) break
        } catch {}
      }
      if (!version) throw new Error('package.json not readable')
      ok(res, { version })
    } catch (err) {
      fail(res, 500, 'BUILD_INFO_FAILED', err.message)
    }
  }

  async function handleHealth(req, res) {
    try {
      const data = await callBridge(['health', '--engine', engineRoot, '--json'], os.homedir(), 20000)
      ok(res, data)
    } catch (err) {
      fail(res, 500, err.code || 'ENGINE_FAILED', err.message, err.detail)
    }
  }

  function parseJsonObject(raw) {
    const text = String(raw || '').trim()
    const a = text.indexOf('{')
    const b = text.lastIndexOf('}')
    if (a < 0 || b <= a) return null
    const body = text.slice(a, b + 1)
    try {
      const obj = JSON.parse(body)
      return obj && typeof obj === 'object' ? obj : null
    } catch {}
    // 1) trailing commas
    const repaired = body.replace(/,\s*([}\]])/g, '$1')
    if (repaired !== body) {
      try {
        const obj = JSON.parse(repaired)
        if (obj && typeof obj === 'object') return obj
      } catch {}
    }
    // 2) a dropped closing brace or two — appending them is enough far more often
    //    than it is wrong, and a wrong guess simply fails to parse.
    for (let extra = 1; extra <= 3; extra += 1) {
      try {
        const obj = JSON.parse(body + '}'.repeat(extra))
        if (obj && typeof obj === 'object') return obj
      } catch {}
    }
    return null
  }

  const SCORE_INSTRUCTION = [
    'Call the `econtone-score` skill and score the text below.',
    'Follow that skill exactly: it runs the four checks and returns the weighted scorecard.',
    '',
    'Output format (must follow exactly):',
    'Return ONE JSON object and nothing else:',
    '{"scorecard": {"rows": [{"id": "P1", "score": 0, "max": 40, "evidence": ""}],',
    '  "total": 0, "editing_latitude": 0, "band": "", "advice": ""},',
    ' "issues": [{"original": "<exact substring of the text>", "why": "<one short sentence>", "severity": "error|warn|info"}]}',
    '- "rows" must contain P1 / P2 / T1 / R1 with max 40 / 40 / 10 / 10.',
    '- "why" in ' + '{WHY}',
    '- No markdown fence, no commentary.',
  ].join('\n')

  function scoreInstruction(whyLang) {
    return SCORE_INSTRUCTION.replace('{WHY}', whyLang)
  }

  function isChineseText(t) {
    const m = String(t || '').match(/[\u4e00-\u9fff]/g)
    if (!m || m.length < 8) return false
    return m.length / Math.max(1, String(t).length) >= 0.15
  }

  // 中译英：先用 agent 原生能力出初稿（不调 skill），再对初稿跑 econtone-score；合并成一次对话
  function scoreInstructionZh(whyLang) {
    const lines = [
      'STEP 1 — draft: translate the Chinese passage below into publishable English using only your own ability.',
      'Do NOT call any skill for this step; no skill, no tool, just translate.',
      'STEP 2 — score: call the `econtone-score` skill and score the English draft you produced in step 1.',
      '',
      'Output format (must follow exactly):',
      'Return ONE JSON object and nothing else:',
      '{"draft": "<the English translation from step 1>",',
      ' "scorecard": {"rows": [{"id": "P1", "score": 0, "max": 40, "evidence": ""}],',
      '  "total": 0, "editing_latitude": 0, "band": "", "advice": ""},',
      ' "issues": [{"original": "<exact substring of the draft>", "why": "<one short sentence>", "severity": "error|warn|info"}]}',
      '- "rows" must contain P1 / P2 / T1 / R1 with max 40 / 40 / 10 / 10, scored on the DRAFT.',
      '- "why" in ' + whyLang,
      '- No markdown fence, no commentary.',
    ]
    return lines.join('\n')
  }

  function parseRewritePayload(raw) {
    const text = String(raw || '').trim()
    const obj = parseJsonObject(text)
    if (obj && typeof obj.revised === 'string' && obj.revised.trim()) {
      return {
        revised: obj.revised.trim(),
        changes: Array.isArray(obj.changes) ? obj.changes.filter((c) => c && c.why) : [],
        certificate: obj.certificate && typeof obj.certificate === 'object' ? obj.certificate : null,
        scorecard: obj.scorecard && typeof obj.scorecard === 'object' ? obj.scorecard : null,
      }
    }
    if (text.startsWith('{')) {
      // A JSON payload we could not read is NOT a revised passage. Returning it as
      // text is what produced quote-soup diffs; fail closed and let the host report it.
      return { revised: '', changes: [], certificate: null, scorecard: null }
    }
    return {
      revised: text.replace(/^```[a-z]*\n?/i, '').replace(/```$/, '').trim(),
      changes: [],
      certificate: null,
      scorecard: null,
    }
  }

  const PROMPT_FILE = fileURLToPath(new URL('./prompts/prompt_format.txt', import.meta.url))
  const PROMPT_SINGLE = fileURLToPath(new URL('./prompts/prompt_single.txt', import.meta.url))
  let promptTemplate = null

  async function loadTemplate() {
    if (promptTemplate != null) return promptTemplate
    try {
      promptTemplate = await readFile(PROMPT_FILE, 'utf8')
    } catch {
      promptTemplate = [
        'You are running inside the EconTone workbench.',
        'STEP 1 (mandatory): load the `econtone-equilibrium` skill and follow its protocol.',
        'STEP 2: apply the equilibrium schedule to the passage, then report what changed.',
        '',
        'Run parameters chosen by the user in the workbench (obey them):',
        '- game model: ',
        '- game order: ',
        '- game order: ',
        '- coalition: ',
        '- editing extent: ',
        '- action mode: ',
        '- time budget: ',
        '- section profile: ',
        '- target journal: ',
        '- text position: ',
        '- subject or area: ',
      ].join('\n')
    }
    return promptTemplate
  }

  async function loadSingleTemplate() {
    try {
      return await readFile(PROMPT_SINGLE, 'utf8')
    } catch {
      return ''
    }
  }

  // 单技能模式：填参数、换 {{ENGINE}} / {{WHY_LANG}}、截在 Text as follows
  function buildSingleInstruction(template, p, whyLang) {
    const values = {
      'engine': p.engine,
      'editing extent': p.extent,
      'section profile': p.profile,
      'target journal': p.journal || 'unspecified',
      'text position': p.section || p.profile,
      'subject or area': p.field || 'unspecified',
    }
    const filled = template.replace(/\r\n/g, '\n').split('\n').map((line) => {
      const m = /^-\s*([A-Za-z ]+?)\s*:\s*$/.exec(line)
      if (!m) return line
      const key = m[1].trim().toLowerCase()
      if (!(key in values)) return line
      return '- ' + m[1].trim() + ': ' + (values[key] == null ? '' : values[key])
    }).join('\n')
      .replace(/\{\{ENGINE\}\}/g, p.engine)
      .replace(/\{\{SKILL\}\}/g, p.skill || ('econtone-' + p.engine))
      .replace(/\{\{WHY_LANG\}\}/g, whyLang)
    const cut = filled.search(/Text as follows\s*[\uff1a:]/)
    return (cut >= 0 ? filled.slice(0, cut) : filled).replace(/\s+$/, '')
  }

  // Fill the template's blank fields; the template owns the wording and order.
  function buildInstruction(template, p, whyLang) {
    const values = {
      'game model': p.model,
      'game order': p.order,
      'cooperation': p.cooperation,
      'coalition': p.coalition,
      'editing extent': p.extent,
      'action mode': p.actionMode,
      'time budget': p.budgetSec + 's',
      'section profile': p.profile,
      'target journal': p.journal || 'unspecified',
      'text position': p.section || p.profile,
      'subject or area': p.field || 'unspecified',
    }
    const filled = template.replace(/\r\n/g, '\n').split('\n').map((line) => {
      const m = /^-\s*([A-Za-z ]+?)\s*:\s*$/.exec(line)
      if (!m) return line
      const key = m[1].trim().toLowerCase()
      if (!(key in values)) return line
      const v = values[key]
      return '- ' + m[1].trim() + ': ' + (v == null ? '' : v)
    })
    // the template ends with "Text as follows��": cut it, the caller appends the passage
    const joined = filled.join('\n')
    const cut = joined.search(/Text as follows\s*[\uff1a:]/)
    const head = (cut >= 0 ? joined.slice(0, cut) : joined)
      .replace(/\{\{WHY_LANG\}\}/g, whyLang)
      .replace(/\s+$/, '')
    return head
  }

  function pickParent(ctx) {
    const agents = ctx.get('agents')
    try {
      if (agents && typeof agents.roots === 'function') {
        const roots = agents.roots()
        if (roots.length) return roots[roots.length - 1]
      }
      if (agents && typeof agents.list === 'function') {
        const all = agents.list()
        if (all.length) return all[all.length - 1]
      }
    } catch {}
    return lastAgent
  }

  async function awaitSessionReply(ctx, sessionId, sinceSeq, timeoutMs) {
    const store = ctx.get('sessions')
    if (!store || typeof store.get !== 'function') return null
    const started = Date.now()
    while (Date.now() - started < timeoutMs) {
      const session = store.get(sessionId)
      if (session) {
        const events = session.snapshotEvents(sinceSeq)
        if (events.some((e) => e.type === 'turn/end')) {
          const texts = events
            .filter((e) => e.type === 'assistant/message')
            .map((e) => {
              const blocks = (e.data && e.data.message && e.data.message.content) || []
              return blocks.filter((b) => b && b.type === 'text').map((b) => b.text).join('')
            })
            .filter(Boolean)
          return texts.length ? texts[texts.length - 1] : ''
        }
      }
      await new Promise((r) => setTimeout(r, 500))
    }
    return null
  }

  // 用 sessionController 在固定的 EconTone 会话里跑一轮（已验证的调用链）。
  async function rewriteViaAgent(ctx, text, instruction, budgetMs) {
    const sc = ctx.get('sessionController')
    if (!sc || typeof sc.resolveAgent !== 'function') {
      diag.agent = { at: Date.now(), stage: 'service', hasController: !!sc }
      return null
    }

    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), budgetMs)
    const run = async () => {
      const bound = await readBinding()
      const cwd = (bound && bound.cwd) || workspaceRoot
      let sessionId = bound && bound.sessionId ? String(bound.sessionId) : ''

      if (!sessionId) {
        const created = await sc.create({ cwd })
        sessionId = String(created.sessionId)
        try { await sc.rename({ sessionId, title: 'EconTone' }) } catch {}
        try {
          await writeBinding({
            cwd, sessionId, name: (bound && bound.name) || 'EconTone', boundAt: Date.now(),
          })
        } catch {}
      }

      let resolved = await sc.resolveAgent(sessionId)
      if (!resolved || resolved.error || !resolved.agent) {
        diag.agent = { at: Date.now(), stage: 'resolve', sessionId, bound: sessionId,
          error: (resolved && resolved.error) || 'no agent' }
        // stale binding: start a fresh EconTone conversation once
        const created = await sc.create({ cwd })
        sessionId = String(created.sessionId)
        try { await sc.rename({ sessionId, title: 'EconTone' }) } catch {}
        try {
          await writeBinding({ cwd, sessionId, name: 'EconTone', boundAt: Date.now() })
        } catch {}
        resolved = await sc.resolveAgent(sessionId)
        if (!resolved || resolved.error || !resolved.agent) {
          diag.agent = { at: Date.now(), stage: 'resolve-retry', sessionId,
            error: (resolved && resolved.error) || 'no agent' }
          return null
        }
      }

      const agent = resolved.agent
      const st = await sc.state({ sessionId }, ctrl.signal)
      const afterSeq = st && typeof st.cursor === 'number' ? st.cursor : undefined

      await agent.followup({
        id: randomUUID(),
        role: 'user',
        content: [{ type: 'text', text: instruction + '\n\nText as follows\uff1a\n\n' + text }],
        source: { kind: 'user', form: 'snapshot', sections: [] },
      }, ctrl.signal)

      const texts = []
      let rounds = 0
      let waitReason = null
      let recordCount = 0
      while (rounds < 3) {
        rounds += 1
        const w = await sc.wait(
          { sessionId, timeoutMs: budgetMs, afterSeq, maxMessages: 60 },
          ctrl.signal,
        )
        waitReason = w && w.reason
        const records = (w && w.output && w.output.records) || []
        recordCount = records.length
        for (const rec of records) {
          const ev = rec && rec.event
          if (!ev || ev.type !== 'assistant/message') continue
          const blocks = (ev.data && ev.data.message && ev.data.message.content) || []
          const t = blocks.filter((b) => b && b.type === 'text').map((b) => b.text).join('')
          if (t && !texts.includes(t)) texts.push(t)
        }
        if (texts.some((t) => t.includes('{')) || waitReason !== 'idle') break
      }
      // The JSON body may arrive after a preamble: prefer the message holding it.
      const withJson = texts.filter((t) => t.includes('{'))
      const reply = withJson.length ? withJson[withJson.length - 1] : texts.join('\n\n')
      diag.agent = { at: Date.now(), stage: 'reply', sessionId, waitReason, rounds,
        records: recordCount, replies: texts.length, replyLength: reply.length }
      return reply.length >= 40 ? reply : null
    }

    try {
      return await run()
    } finally {
      clearTimeout(timer)
    }
  }

  async function rewriteCandidate(ctx, text, onProgress, instruction) {
    const llm = ctx.get('llm')
    const def = ctx.get('agentDefaultModel')
    if (!llm || !def || typeof llm.stream !== 'function') {
      diag.llm = { at: Date.now(), stage: 'service', hasLlm: !!llm, hasDefault: !!def }
      return null
    }
    let sel
    try { sel = def.currentSelection() } catch (e) {
      diag.llm = { at: Date.now(), stage: 'selection', error: String(e && e.message) }
      return null
    }
    if (!sel || !sel.provider || !sel.model) {
      diag.llm = { at: Date.now(), stage: 'selection', selection: sel || null }
      return null
    }
    diag.llm = { at: Date.now(), stage: 'streaming', provider: sel.provider, model: sel.model,
      maxTokens: llmMaxTokens, reasoningEffort: llmReasoningEffort, text: 0, thinking: 0 }
    let out = ''
    let thinking = ''
    const stream = llm.stream({
      provider: sel.provider,
      model: sel.model,
      system: instruction,
      messages: [{ role: 'user', content: [{ type: 'text', text }] }],
      temperature: 0.2,
      reasoningEffort: llmReasoningEffort,
      maxTokens: llmMaxTokens,
    })
    for await (const chunk of stream) {
      if (chunk.type === 'text-delta') {
        out += chunk.text
        diag.llm.text = (diag.llm.text || 0) + chunk.text.length
        if (onProgress && out.length % 24 < chunk.text.length) onProgress(out.length, out.slice(-260))
      } else if (typeof chunk.type === 'string' && /reason|think/i.test(chunk.type)) {
        const t = chunk.text || chunk.delta || ''
        if (t) {
          thinking += t
          diag.llm.thinking = (diag.llm.thinking || 0) + t.length
          if (onProgress) onProgress(out.length, thinking.slice(-260))
        }
      }
      if (chunk.type === 'finish') {
        diag.llm.finish = chunk.reason ? chunk.reason.kind : null
        if (chunk.reason && chunk.reason.kind === 'error') {
          diag.llm.failure = (chunk.reason.failure && chunk.reason.failure.message) || null
          return null
        }
      }
    }
    out = out.trim().replace(/^```[a-z]*\n?/i, '').replace(/```$/, '').trim()
    diag.llm.outLength = out.length
    return out.length >= 40 ? out : null
  }

  function patchJob(runId, patch) {
    const job = jobs.get(runId)
    if (job) Object.assign(job, patch)
  }

  async function persistRun(dir, result) {
    try { await writeFile(path.join(dir, 'run.json'), JSON.stringify(result), 'utf8') } catch {}
  }

  function reconcileScore(result) {
    try {
      const s = result && result.score
      if (!s || !Array.isArray(s.rows)) return
      let changed = false
      const findings = Array.isArray(result.findings) ? result.findings : []
      const polish = findings.filter((f) => f && f.player === 'polish')
      const p1 = s.rows.find((r) => r && r.id === 'P1')
      if (p1 && polish.length) {
        const max = Number(p1.max) || 40
        if (Number(p1.score) >= max) {
          const k = Math.min(8, polish.length)
          const next = Math.max(24, max - 2 * k)
          p1.score = next
          p1.evidence = (p1.evidence ? p1.evidence + ' \u00b7 ' : '')
            + `\u811a\u672c\u6838\u5bf9\uff1a\u5217\u51fa ${polish.length} \u6761\u8bed\u57df\u95ee\u9898\uff0c\u6309\u6bcf\u6761 2 \u5206\u6263\u81f3 ${next}`
          changed = true
        }
      }
      const r1 = s.rows.find((r) => r && r.id === 'R1')
      if (r1) {
        const ev = String(r1.evidence || '')
        const mRed = /(\d+)\s*RED/i.exec(ev)
        const mWarn = /(\d+)\s*WARN/i.exec(ev)
        if (mRed || mWarn) {
          const red = mRed ? Number(mRed[1]) : 0
          const warn = mWarn ? Number(mWarn[1]) : 0
          const max = Number(r1.max) || 10
          const expected = Math.max(0, max - 4 * red - 1 * warn)
          if (Number(r1.score) > expected) {
            r1.score = expected
            r1.evidence = ev + ` \u00b7 \u811a\u672c\u6838\u5bf9\uff1a${red} RED / ${warn} WARN\uff0c\u6263\u81f3 ${expected}`
            changed = true
          }
        }
      }
      if (!changed) return
      s.rows.forEach((r) => { r.score = Number(r.score) || 0 })
      s.total = s.rows.reduce((sum, r) => sum + (Number(r.score) || 0), 0)
      result.score = s
    } catch {}
  }

  async function execute(runId, dir, inputPath, outPath, profile, mode, text, order, coalition, actionMode, model, uiLang, budgetSec, conn, extentValue, journal, sectionName, field, latitudeIn, baselineScore, engineName, engineSkill, draftId) {
    try {
      const common = ['--file', inputPath, '--profile', profile,
        '--engine', engineRoot, '--out', outPath, '--run-id', runId, '--json']
      let extra = []
      let rewriteBy = 'llm'
      const whyLang = uiLang === 'en' ? 'English' : 'Chinese (\u7b80\u4f53\u4e2d\u6587)'
      let rewriteError = ''
      const budgetMs = Math.min(180, Math.max(30, Number(budgetSec) || 60)) * 1000
      // 期望时间是提示，不是硬停：到点后继续，只有远超预算的硬上限才失败
      const hardMs = Math.max(budgetMs * 6, 15 * 60 * 1000)
      if (mode === 'arbitrate') {
        patchJob(runId, { phase: 'rewrite', chars: 0 })
        try {
          let rewritten = null
          let by = 'llm'
          const onDelta = (n, preview) => patchJob(runId, { chars: n, preview })
          // 修改幅度：AUTO 不能直接传“auto”，要换成评分给出的 editing latitude
      let extentText = extentValue
      if (extentValue === 'auto') {
        let lat = Number(latitudeIn)
        if (!Number.isFinite(lat) || lat <= 0) {
          try {
            const m = await callBridge(['measure', '--file', inputPath, '--profile', profile,
              '--engine', engineRoot, '--json'], dir, timeoutMs)
            lat = Number(m && m.score && m.score.editing_latitude)
          } catch {}
        }
        if (Number.isFinite(lat) && lat > 0) extentText = Math.round(lat) + '%'
        else extentText = 'auto (infer from econtone-score)'
      }
      const runParams = {
        model, order, coalition, actionMode, budgetSec, profile,
        cooperation: coalition === 'none' ? 'non-cooperative' : 'cooperative',
        extent: extentText, journal, section: sectionName, field, engine: engineName, skill: engineSkill || ('econtone-' + engineName),
      }
      let instruction = ''
      const singleSkill = model === 'none'
      try {
        instruction = singleSkill
          ? buildSingleInstruction(await loadSingleTemplate(), runParams, whyLang)
          : buildInstruction(await loadTemplate(), runParams, whyLang)
        await writeFile(path.join(dir, 'prompt.txt'),
          instruction + '\n\nText as follows\uff1a\n\n' + text, 'utf8')
      } catch {}
          try {
            if (conn === 'agent') {
              rewritten = await Promise.race([
                rewriteViaAgent(ctx, text, instruction, budgetMs),
                new Promise((_, reject) => setTimeout(() => reject(new Error('REWRITE_HARD_TIMEOUT')), hardMs)),
              ])
              if (rewritten) by = 'agent'
            } else {
              rewritten = await Promise.race([
                rewriteCandidate(ctx, text, onDelta, instruction),
                new Promise((_, reject) => setTimeout(() => reject(new Error('REWRITE_HARD_TIMEOUT')), hardMs)),
              ])
            }
          } catch (err) {
            rewriteError = String((err && err.message) || err)
            console.error('[econtone-studio] rewrite failed:', rewriteError)
          }
          rewriteBy = by
          if (conn === 'agent' && !rewritten) {
            patchJob(runId, {
              status: 'error',
              error: { code: 'AGENT_NO_RESULT',
                       message: '子 Agent 没有返回可用结果（未完成或已达硬上限），不生成改写稿'
                         + (rewriteError ? ' · ' + rewriteError : '') },
            })
            return
          }
          if (rewritten) {
            const candDir = path.join(dir, 'cand')
            await mkdir(candDir, { recursive: true })
            const parsed = parseRewritePayload(rewritten)
            if (!parsed.revised) {
              patchJob(runId, {
                status: 'error',
                error: {
                  code: 'BAD_AGENT_PAYLOAD',
                  message: '模型回复的 JSON 无法解析（缺字段或括号不配对），未生成改写稿',
                  detail: String(rewritten).slice(0, 240),
                },
              })
              return
            }
            await writeFile(path.join(candDir, 'model.md'), parsed.revised, 'utf8')
            const movesPath = path.join(dir, 'moves.model.json')
            await writeFile(movesPath, JSON.stringify([
              { id: 'm-model', player: 'polish', kind: 'wording', after: 'cand/model.md' },
            ], null, 2), 'utf8')
            extra = ['--candidates', movesPath]
            if (parsed.changes.length) {
              const reasonsPath = path.join(candDir, 'reasons.json')
              await writeFile(reasonsPath, JSON.stringify(parsed.changes, null, 2), 'utf8')
              extra.push('--reasons', reasonsPath)
            }
            if (by === 'agent') {
              // agent 已跑完脚本：宿主只传交结果
              extra = ['--final', path.join(candDir, 'model.md')]
              if (parsed.changes.length) extra.push('--reasons', path.join(candDir, 'reasons.json'))
              if (parsed.scorecard) {
                const scorePath = path.join(candDir, 'score.json')
                await writeFile(scorePath, JSON.stringify(parsed.scorecard, null, 2), 'utf8')
                extra.push('--score', scorePath)
              }
              if (baselineScore) {
                const basePath = path.join(candDir, 'baseline.json')
                await writeFile(basePath, JSON.stringify(baselineScore, null, 2), 'utf8')
                extra.push('--baseline', basePath)
              }
              if (parsed.certificate) {
                const certPath = path.join(candDir, 'cert.json')
                await writeFile(certPath, JSON.stringify(parsed.certificate, null, 2), 'utf8')
                extra.push('--cert', certPath)
              }
            }
          }
        } catch (err) {
          console.error('[econtone-studio] rewrite failed:', err && err.message)
        }
        if (order === 'simultaneous') extra.push('--rounds', '1')
        extra.push('--coalition', coalition, '--mode', actionMode)
        // play.py 只实现了 stackelberg；nash/bayes/correlated 会报 "planned but not implemented"。
        extra.push('--game', 'stackelberg')
        patchJob(runId, { phase: 'arbitrate' })
      }
      const bridgeTimeout = Math.max(timeoutMs, Math.min(180, Math.max(30, Number(budgetSec) || 60)) * 1000 + 120000)

      if (mode === 'measure') {
        // 评分走 agent（econtone-score），失败才回退到脚本
        patchJob(runId, { phase: 'score', chars: 0 })
        const zhInput = isChineseText(text)
        try {
          const reply = await Promise.race([
            rewriteViaAgent(ctx, text, (zhInput ? scoreInstructionZh : scoreInstruction)('Chinese (\u7b80\u4f53\u4e2d\u6587)'), budgetMs),
            new Promise((_, reject) => setTimeout(() => reject(new Error('REWRITE_HARD_TIMEOUT')), hardMs)),
          ])
          const obj = parseJsonObject(reply)
          if (obj && obj.scorecard && obj.scorecard.rows) {
            const issues = Array.isArray(obj.issues) ? obj.issues : []
            const findings = issues.map((it, i) => ({
              id: 'f-' + String(i + 1).padStart(3, '0'),
              op: 'none',
              base_range: { start: 0, end: 0 },
              after_range: { start: 0, end: 0 },
              original: String(it.original || ''),
              revised: '',
              paragraph: 1,
              player: 'polish',
              kind: 'wording',
              severity: it.severity === 'error' ? 'error' : (it.severity === 'warn' ? 'warn' : 'info'),
              decision: 'pending',
              reason: String(it.why || ''),
              evidence: [{ source: 'model', code: 'MODEL', message: String(it.why || '') }],
              locate: it.original ? 'exact' : 'none',
            }))
            const result = {
              runId,
              createdAt: new Date().toISOString(),
              profile,
              mode: 'measure',
              score: obj.scorecard,
              findings,
              edits: [],
              finalText: null,
              scoreBy: 'agent',
              draft: zhInput ? String(obj.draft || '') : null,
            }
            if (draftId) result.draftId = draftId
            reconcileScore(result)
            await persistRun(dir, result)
            patchJob(runId, { status: 'done', phase: 'done', result })
            return
          }
        } catch (err) {
          console.error('[econtone-studio] agent score failed:', err && err.message)
        }
        patchJob(runId, { phase: 'measure' })
      }

      const data = mode === 'arbitrate'
        ? await callBridge(['arbitrate', ...common, '--run-dir', dir, ...extra], dir, bridgeTimeout)
        : await callBridge(['measure', ...common], dir, bridgeTimeout)
      data.runId = runId
      data.draftId = draftId
      data.runDir = dir
      data.rewriteBy = rewriteBy
      if (rewriteError) data.rewriteError = rewriteError
      reconcileScore(data)
      await persistRun(dir, data)
      patchJob(runId, { status: 'done', phase: 'done', result: data, elapsedMs: Date.now() - (jobs.get(runId)?.startedAt || Date.now()) })
    } catch (err) {
      patchJob(runId, {
        status: 'error',
        error: { code: err.code || 'ENGINE_FAILED', message: err.message, detail: err.detail },
      })
    }
  }

  function bindingPath() {
    return path.join(workspaceRoot, '.econtone', 'binding.json')
  }

  async function writeBinding(data) {
    await mkdir(path.dirname(bindingPath()), { recursive: true })
    await writeFile(bindingPath(), JSON.stringify(data, null, 2), 'utf8')
    return data
  }

  async function readBinding() {
    try {
      const raw = await readFile(bindingPath(), 'utf8')
      const b = JSON.parse(raw)
      return b && b.cwd ? b : null
    } catch {
      return null
    }
  }

  function statePath() {
    return path.join(workspaceRoot, '.econtone', 'state.json')
  }

  async function handleGetState(req, res) {
    try {
      const raw = await readFile(statePath(), 'utf8')
      ok(res, { state: JSON.parse(raw) })
    } catch {
      ok(res, { state: null })
    }
  }

  async function handleSetState(req, res) {
    let body
    try {
      body = JSON.parse(await readBody(req))
    } catch {
      fail(res, 400, 'BAD_REQUEST', 'body must be JSON')
      return
    }
    const state = body && body.state
    if (!state || typeof state !== 'object') { fail(res, 400, 'BAD_STATE', 'state missing'); return }
    await mkdir(path.dirname(statePath()), { recursive: true })
    await writeFile(statePath(), JSON.stringify(state), 'utf8')
    ok(res, { saved: true, bytes: JSON.stringify(state).length })
  }

  function shortId(id) {
    const s = String(id || '')
    return s.length > 16 ? s.slice(0, 9) + '\u2026' + s.slice(-4) : s
  }

  // 会话标题：走 sessionQuery 折叠会话日志里的标题事件，live 与 persisted 都能取到。
  // 取不到（从未产生标题）就留空，由客户端退回短 id 显示——绝不把 id 当名字。
  async function readTitles(ids) {
    const out = Object.create(null)
    const want = (ids || []).map((v) => String(v)).filter(Boolean)
    if (!want.length) return out
    const sq = ctx.get('sessionQuery')
    if (!sq || typeof sq.readTitleSnapshots !== 'function') return out
    try {
      const results = await sq.readTitleSnapshots(want)
      for (const r of results || []) {
        if (!r || r.status !== 'fulfilled') continue
        const snap = r.value && r.value.title
        if (snap && snap.title) out[String(r.sessionId)] = String(snap.title)
      }
    } catch {}
    return out
  }

  async function handleGetBinding(req, res) {
    let sessions = []
    try {
      const store = ctx.get('sessions')
      if (store && typeof store.list === 'function') {
        sessions = store.list().map((s) => ({
          id: String(s.id),
          short: shortId(s.id),
          cwd: (s.header && s.header.cwd) || '',
          events: typeof s.seq === 'number' ? s.seq : 0,
        }))
      }
    } catch {}
    const bound = await readBinding()
    const boundId = bound && bound.sessionId ? String(bound.sessionId) : ''
    const ids = sessions.map((s) => s.id)
    if (boundId && !ids.includes(boundId)) ids.push(boundId)
    const titles = await readTitles(ids)
    for (const s of sessions) {
      s.bound = s.id === boundId
      s.title = titles[s.id] || ''
    }
    ok(res, {
      bound,
      boundShort: boundId ? shortId(boundId) : '',
      boundTitle: boundId ? (titles[boundId] || (bound && bound.title) || '') : '',
      defaultCwd: workspaceRoot,
      sessions,
    })
  }

  async function handleSetBinding(req, res) {
    let body
    try {
      body = JSON.parse(await readBody(req))
    } catch {
      fail(res, 400, 'BAD_REQUEST', 'body must be JSON')
      return
    }
    const cwd = String(body.cwd || '').trim() || workspaceRoot
    if (!path.isAbsolute(cwd)) { fail(res, 400, 'BAD_CWD', 'cwd must be absolute'); return }
    const name = String(body.name || 'EconTone')
    let sessionId = body.sessionId ? String(body.sessionId) : ''
    let created = false
    if (!sessionId) {
      // Empty selection means "create one now", not "create it on the next run":
      // the user pressed a button called 绑定并开始 and should see the conversation exist.
      const sc = ctx.get('sessionController')
      if (!sc || typeof sc.create !== 'function') {
        fail(res, 503, 'NO_SESSION_CONTROLLER', 'sessionController missing; cannot create a conversation')
        return
      }
      try {
        const made = await sc.create({ cwd })
        sessionId = String(made.sessionId)
        created = true
        try { await sc.rename({ sessionId, title: name }) } catch {}
      } catch (err) {
        fail(res, 500, 'CREATE_FAILED', String((err && err.message) || err))
        return
      }
    }
    const title = created ? name : ((await readTitles([sessionId]))[sessionId] || '')
    const data = { cwd, sessionId, short: shortId(sessionId), title, name, boundAt: Date.now() }
    await mkdir(path.dirname(bindingPath()), { recursive: true })
    await writeFile(bindingPath(), JSON.stringify(data, null, 2), 'utf8')
    ok(res, Object.assign({ created }, data))
  }

  async function handleProbeBinding(req, res) {
    const sc = ctx.get('sessionController')
    const out = { steps: [] }
    if (!sc || typeof sc.resolveAgent !== 'function') {
      fail(res, 503, 'NO_SESSION_CONTROLLER', 'sessionController missing', out)
      return
    }
    const b = await readBinding()
    if (!b || !b.sessionId) { fail(res, 400, 'NO_BINDING', 'no conversation bound yet', out); return }
    out.sessionId = String(b.sessionId)
    try {
      const resolved = await sc.resolveAgent(out.sessionId)
      if (!resolved || resolved.error || !resolved.agent) {
        fail(res, 502, 'AGENT_UNAVAILABLE',
          String((resolved && resolved.error) || 'agent not resumable'),
          Object.assign(out, { stage: 'resolve' }))
        return
      }
      out.steps.push('resolveAgent')
      const agent = resolved.agent
      const ctrl = new AbortController()
      const st = await sc.state({ sessionId: out.sessionId }, ctrl.signal)
      const afterSeq = st && typeof st.cursor === 'number' ? st.cursor : undefined
      await agent.followup({
        id: randomUUID(),
        role: 'user',
        content: [{ type: 'text', text: 'Reply with exactly: OK' }],
        source: { kind: 'user', form: 'snapshot', sections: [] },
      }, ctrl.signal)
      out.steps.push('followup')
      const w = await sc.wait({ sessionId: out.sessionId, timeoutMs: 30000, afterSeq, maxMessages: 12 }, ctrl.signal)
      out.waitReason = w && w.reason
      out.steps.push('wait')
      const texts = []
      for (const rec of (w && w.output && w.output.records) || []) {
        const ev = rec && rec.event
        if (!ev || ev.type !== 'assistant/message') continue
        const blocks = (ev.data && ev.data.message && ev.data.message.content) || []
        const t = blocks.filter((x) => x && x.type === 'text').map((x) => x.text).join('')
        if (t) texts.push(t)
      }
      out.reply = texts.length ? texts[texts.length - 1] : ''
      ok(res, out)
    } catch (err) {
      fail(res, 500, err.code || 'BIND_PROBE_FAILED', String((err && err.message) || err),
        Object.assign(out, { stage: out.steps[out.steps.length - 1] || 'start' }))
    }
  }

  function safeRunDir(runId) {
    if (!/^r-[a-z0-9-]+$/.test(String(runId || ""))) return null
    return path.join(runRoot(), String(runId))
  }

  async function handleSetDecisions(req, res) {
    let body
    try { body = JSON.parse(await readBody(req)) } catch { fail(res, 400, "BAD_REQUEST", "body must be JSON"); return }
    const dir = safeRunDir(body.runId)
    if (!dir) { fail(res, 400, "BAD_RUN_ID", String(body.runId || "")); return }
    const decisions = (body.decisions && typeof body.decisions === "object") ? body.decisions : {}
    await mkdir(dir, { recursive: true })
    await writeFile(path.join(dir, "decisions.json"), JSON.stringify(decisions, null, 2), "utf8")
    ok(res, { runId: body.runId, saved: Object.keys(decisions).length })
  }

  async function handleApplyDecisions(req, res) {
    let body
    try { body = JSON.parse(await readBody(req)) } catch { fail(res, 400, "BAD_REQUEST", "body must be JSON"); return }
    const dir = safeRunDir(body.runId)
    if (!dir) { fail(res, 400, "BAD_RUN_ID", String(body.runId || "")); return }
    const text = String(body.text == null ? "" : body.text)
    if (!text.trim()) { fail(res, 400, "EMPTY_TEXT", "nothing to save"); return }
    const decisions = (body.decisions && typeof body.decisions === "object") ? body.decisions : {}
    await mkdir(dir, { recursive: true })
    const outPath = path.join(dir, "final.applied.md")
    await writeFile(path.join(dir, "decisions.json"), JSON.stringify(decisions, null, 2), "utf8")
    await writeFile(outPath, text, "utf8")
    ok(res, { runId: body.runId, path: outPath, chars: text.length })
  }

  async function handleDiag(req, res) {
    let binding = null
    try { binding = await readBinding() } catch {}
    let selection = null
    try { const d = ctx.get('agentDefaultModel'); selection = d ? d.currentSelection() : null } catch (e) { selection = String(e && e.message) }
    ok(res, {
      hasLlm: !!ctx.get('llm'),
      hasSessionController: !!ctx.get('sessionController'),
      selection,
      llmMaxTokens,
      llmReasoningEffort,
      binding,
      engineRoot,
      pythonPath,
      diag,
    })
  }

  // ---- probe: sessionController.resolveAgent + agent.followup + wait ----
  async function handleProbe(req, res) {
    const sc = ctx.get('sessionController')
    if (!sc) { fail(res, 503, 'NO_SESSION_CONTROLLER', 'sessionController missing'); return }
    const out = { steps: [] }
    let sessionId = null
    try {
      const bound = await readBinding()
      const created = await sc.create({ cwd: (bound && bound.cwd) || workspaceRoot })
      sessionId = created.sessionId
      out.sessionId = sessionId
      out.steps.push('create')

      try { await sc.rename({ sessionId, title: 'EconTone probe' }); out.steps.push('rename') } catch {}

      const resolved = await sc.resolveAgent(sessionId)
      if (resolved.error) {
        out.error = resolved.error
        ok(res, out)
        return
      }
      out.steps.push('resolveAgent')
      const agent = resolved.agent
      out.agentKeys = agent ? Object.keys(agent) : []
      out.agentProto = agent ? Object.getOwnPropertyNames(Object.getPrototypeOf(agent)) : []
      out.hasFollowup = typeof agent.followup === 'function'
      out.hasWhenIdle = typeof agent.whenIdle === 'function'
      out.hasStatus = agent.status !== undefined

      if (out.hasFollowup) {
        const probeSignal = new AbortController().signal
        const st0 = await sc.state({ sessionId }, probeSignal)
        const afterSeq = st0 && typeof st0.cursor === 'number' ? st0.cursor : undefined
        const msg = {
          id: randomUUID(),
          role: 'user',
          content: [{ type: 'text', text: 'Reply with exactly: OK' }],
          source: { kind: 'user', form: 'snapshot', sections: [] },
        }
        const ctrl = new AbortController()
        const attempts = [
          ['msg, signal', () => agent.followup(msg, ctrl.signal)],
          ['msg, {signal}', () => agent.followup(msg, { signal: ctrl.signal })],
          ['msg only', () => agent.followup(msg)],
        ]
        out.followupAttempts = []
        for (const [label, fn] of attempts) {
          try {
            await fn()
            out.followupOk = label
            out.followupAttempts.push(label + ':ok')
            break
          } catch (e) {
            out.followupAttempts.push(label + ':' + String(e && e.message).slice(0, 90))
          }
        }
        out.steps.push('followup')
        const w = await sc.wait({ sessionId, timeoutMs: 90000, afterSeq, maxMessages: 30 }, probeSignal)
        out.waitReason = w.reason
        out.steps.push('wait')
        const texts = []
        for (const rec of (w.output && w.output.records) || []) {
          const ev = rec && rec.event
          if (!ev) continue
          if (ev.type !== 'assistant/message') continue
          const blocks = (ev.data && ev.data.message && ev.data.message.content) || []
          const t = blocks.filter((b) => b && b.type === 'text').map((b) => b.text).join('')
          if (t) texts.push(t)
        }
        out.reply = texts.length ? texts[texts.length - 1] : null
      }
      ok(res, out)
    } catch (err) {
      fail(res, 500, err.code || 'PROBE_FAILED', err.message, out)
    }
  }

  async function listSkills() {
    const dir = defaultEngine()
    const out = []
    try {
      const names = await readdir(dir)
      for (const n of names) {
        let text = ''
        try { text = await readFile(path.join(dir, n, 'SKILL.md'), 'utf8') } catch { continue }
        const fm = /^---\s*([\s\S]*?)\s*---/.exec(text)
        let name = n
        let description = ''
        if (fm) {
          const lines = fm[1].split('\n')
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i]
            const nm = /^name:\s*(.+)$/.exec(line)
            if (nm) { name = nm[1].trim().replace(/^["']|["']$/g, ''); continue }
            const dm = /^description:\s*(.*)$/.exec(line)
            if (!dm) continue
            let v = dm[1].trim()
            if (v === '>' || v === '|' || v === '>-' || v === '|-') {
              const parts = []
              for (let j = i + 1; j < lines.length; j++) {
                if (/^\s+\S/.test(lines[j])) parts.push(lines[j].trim())
                else break
              }
              v = parts.join(' ')
            }
            description = v.replace(/^["']|["']$/g, '')
          }
        }
        const hay = (n + ' ' + name + ' ' + description).toLowerCase()
        const writing = /(polish|rewrit|writ|edit|prose|translat|academic|paper|tone|style|humaniz|\u6da6\u8272|\u5199\u4f5c|\u8bba\u6587|\u7ffb\u8bd1|\u6539\u5199|\u6587\u6848)/.test(hay)
        out.push({ id: n, name, description, kind: writing ? 'writing' : 'other' })
      }
    } catch {}
    out.sort((a, b) => String(a.name).localeCompare(String(b.name)))
    return out
  }

  async function handleListSkills(req, res) {
    ok(res, { skills: await listSkills() })
  }

  async function handleRun(req, res) {
    let payload
    try {
      payload = JSON.parse(await readBody(req))
    } catch {
      fail(res, 400, 'BAD_REQUEST', 'body must be JSON')
      return
    }
    const text = String(payload.text || '')
    const profile = ['body', 'abstract', 'intro'].includes(payload.profile) ? payload.profile : 'body'
    const mode = payload.mode === 'arbitrate' ? 'arbitrate' : 'measure'
    const order = payload.order === 'simultaneous' ? 'simultaneous' : 'sequential'
    const coalition = ['none', 'maxsum', 'nash'].includes(payload.coalition) ? payload.coalition : 'none'
    const actionMode = payload.actionMode === 'free' ? 'free' : 'constrained'
    const model = ['stack', 'maxsum', 'nash', 'cournot', 'bertrand', 'none'].includes(payload.model) ? payload.model : 'stack'
    const uiLang = payload.uiLang === 'en' ? 'en' : 'zh'
    const budgetSec = Math.min(180, Math.max(30, Number(payload.budgetSec) || 60))
    const conn = payload.conn === 'stream' ? 'stream' : 'agent'
    const extentValue = typeof payload.extent === 'string' ? payload.extent : 'auto'
    const rawEngine = typeof payload.engine === 'string' ? payload.engine.trim() : 'polish'
    let engineName = 'polish'
    let engineSkill = 'econtone-polish'
    if (rawEngine === 'equilibrium') { engineName = 'equilibrium'; engineSkill = 'econtone-equilibrium' }
    else if (['polish', 'ai-trace', 'prosody'].includes(rawEngine)) { engineName = rawEngine; engineSkill = 'econtone-' + rawEngine }
    else if (rawEngine.startsWith('skill:')) {
      const want = rawEngine.slice(6).trim()
      const skills = await listSkills()
      const hit = skills.find((s) => s.id === want || s.name === want)
      if (hit) { engineName = 'skill:' + hit.id; engineSkill = hit.name }
    }
    const baselineScore = payload.baselineScore && typeof payload.baselineScore === 'object' ? payload.baselineScore : null
    const latitudeIn = Number(payload.latitude)
    const journal = typeof payload.journal === 'string' ? payload.journal.trim() : ''
    const sectionName = typeof payload.section === 'string' ? payload.section.trim() : ''
    const field = typeof payload.field === 'string' ? payload.field.trim() : ''
    if (!text.trim()) { fail(res, 400, 'EMPTY_TEXT', 'text is empty'); return }
    if (text.length > maxChars) {
      fail(res, 413, 'DOC_TOO_LARGE', `text is ${text.length} chars, limit ${maxChars}`)
      return
    }

    // 中译英：改写在英文初稿上进行，不再用中文原文
    const zhInput = isChineseText(text)
    const draftText = typeof payload.draftText === 'string' ? payload.draftText.trim() : ''
    let effectiveText = text
    if (mode === 'arbitrate' && zhInput) {
      if (!draftText) {
        fail(res, 409, 'DRAFT_REQUIRED', '\u6b63\u5728\u751f\u6210\u82f1\u6587\u521d\u7a3f\uff0c\u8bf7\u7a0d\u5019\u518d\u6539\u5199')
        return
      }
      effectiveText = draftText
    }

    const taskId = typeof payload.taskId === 'string' ? payload.taskId : ''
    const draftId = typeof payload.draftId === 'string' ? payload.draftId.trim().slice(0, 64) : ''
    const runId = 'r-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6)
    const dir = path.join(runRoot(), runId)
    await mkdir(dir, { recursive: true })
    const inputPath = path.join(dir, 'input.md')
    await writeFile(inputPath, effectiveText.replace(/\r\n/g, '\n'), 'utf8')
    try {
      await writeFile(path.join(dir, 'meta.json'), JSON.stringify({
        runId, taskId, mode, draftId, startedAt: Date.now(),
      }), 'utf8')
    } catch {}
    const outPath = path.join(dir, 'run.json')
    jobs.set(runId, {
      status: 'running', dir, mode,
      phase: mode === 'arbitrate' ? 'rewrite' : 'measure',
      chars: 0, startedAt: Date.now(),
    })
    void execute(runId, dir, inputPath, outPath, profile, mode, effectiveText, order, coalition, actionMode, model, uiLang, budgetSec, conn, extentValue, journal, sectionName, field, latitudeIn, baselineScore, engineName, engineSkill, draftId)
    ok(res, { runId, status: 'running', mode, runDir: dir })
  }

  async function handleRunRefresh(req, res, url) {
    const taskId = String(url.searchParams.get('task') || '')
    const draftId = String(url.searchParams.get('draft') || '')
    if (!taskId && !draftId) { fail(res, 400, 'BAD_REQUEST', 'task or draft is required'); return }
    const root = runRoot()
    let best = null
    try {
      const names = await readdir(root)
      for (const name of names) {
        let meta = null
        try { meta = JSON.parse(await readFile(path.join(root, name, 'meta.json'), 'utf8')) } catch { continue }
        const sameDraft = draftId && String(meta.draftId || '') === draftId
        const sameTask = taskId && String(meta.taskId || '') === taskId
        if (!sameDraft && !sameTask) continue
        if (!best || (meta.startedAt || 0) > (best.startedAt || 0)) best = meta
      }
    } catch {}
    if (!best) { ok(res, { found: false }); return }
    const foundDraft = String(best.draftId || '')
    const job = jobs.get(best.runId)
    if (job && job.status === 'running') {
      ok(res, { found: true, runId: best.runId, draftId: foundDraft, status: 'running', startedAt: best.startedAt })
      return
    }
    if (job && job.status === 'error') {
      ok(res, { found: true, runId: best.runId, status: 'error', error: job.error, startedAt: best.startedAt })
      return
    }
    try {
      const result = JSON.parse(await readFile(path.join(root, best.runId, 'run.json'), 'utf8'))
      if (result && !result.baseText) {
        try { result.baseText = await readFile(path.join(root, best.runId, 'input.md'), 'utf8') } catch {}
      }
      try {
        const dec = JSON.parse(await readFile(path.join(root, best.runId, 'decisions.json'), 'utf8'))
        if (dec && typeof dec === 'object') result.decisions = dec
      } catch {}
      ok(res, { found: true, runId: best.runId, draftId: foundDraft, status: 'done', startedAt: best.startedAt, result })
    } catch {
      ok(res, { found: true, runId: best.runId, status: 'missing', startedAt: best.startedAt })
    }
  }

  function textHash(s) {
    let h = 5381
    const str = String(s || '')
    for (let i = 0; i < str.length; i++) h = (((h << 5) + h) + str.charCodeAt(i)) >>> 0
    return 'k' + h.toString(36)
  }

  async function handleHistory(req, res) {
    const root = runRoot()
    let names = []
    try { names = await readdir(root) } catch {}
    let tasks = []
    try {
      const st = JSON.parse(await readFile(statePath(), 'utf8'))
      tasks = Array.isArray(st.tasks) ? st.tasks : []
    } catch {}
    const nameOf = {}
    tasks.forEach((t) => { if (t && t.id) nameOf[t.id] = t.name || '' })
    const runs = []
    for (const n of names) {
      let meta = null
      try { meta = JSON.parse(await readFile(path.join(root, n, 'meta.json'), 'utf8')) } catch { continue }
      let result = null
      try { result = JSON.parse(await readFile(path.join(root, n, 'run.json'), 'utf8')) } catch {}
      const job = jobs.get(n)
      let inputText = ''
      try { inputText = await readFile(path.join(root, n, 'input.md'), 'utf8') } catch {}
      const preview = inputText.replace(/\s+/g, ' ').trim().slice(0, 120)
      const textKey = inputText.trim() ? textHash(inputText) : ('run-' + n)
      runs.push({
        textKey, preview,
        runId: n,
        taskId: meta.taskId || '',
        taskName: nameOf[meta.taskId] || '',
        draftId: meta.draftId || (result && result.draftId) || '',
        mode: meta.mode || (result && result.mode) || '',
        startedAt: meta.startedAt || 0,
        status: job ? job.status : (result ? 'done' : 'missing'),
        score: result && result.score ? result.score.total : null,
        edits: result && Array.isArray(result.edits) ? result.edits.length : 0,
      })
    }
    runs.sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0))
    ok(res, { runs })
  }

  async function handleGetRun(req, res, url) {
    const id = url.searchParams.get('id') || ''
    const job = jobs.get(id)
    if (!job) {
      // 进程重启后内存不在：从前次落盘的 run.json 里恢复
      try {
        const raw = await readFile(path.join(runRoot(), id, 'run.json'), 'utf8')
        const result = JSON.parse(raw)
        if (result && !result.baseText) {
          try {
            result.baseText = await readFile(path.join(runRoot(), id, 'input.md'), 'utf8')
          } catch {}
        }
        try {
          const dec = JSON.parse(await readFile(path.join(runRoot(), id, 'decisions.json'), 'utf8'))
          if (dec && typeof dec === 'object') result.decisions = dec
        } catch {}
        ok(res, { runId: id, status: 'done', result, fromDisk: true })
        return
      } catch {}
      fail(res, 404, 'RUN_NOT_FOUND', `no run ${id}`)
      return
    }
    if (job.status === 'error') {
      ok(res, { runId: id, status: 'error', error: job.error })
      return
    }
    if (job.status === 'done') {
      try {
        const dec = JSON.parse(await readFile(path.join(runRoot(), id, 'decisions.json'), 'utf8'))
        if (dec && typeof dec === 'object') job.result.decisions = dec
      } catch {}
      ok(res, { runId: id, status: 'done', result: job.result })
      return
    }
    ok(res, {
      runId: id,
      status: job.status || 'running',
      mode: job.mode,
      phase: job.phase || 'measure',
      chars: job.chars || 0,
      preview: job.preview || '',
      elapsedMs: Date.now() - (job.startedAt || Date.now()),
    })
  }

  // 工作台背后要有 agent：捕获最近创建的 Agent 作为子代理的父节点。
  ctx.effect(() => ctx.on('agent/created', (agent) => { lastAgent = agent }),
    'econtone-studio: track agent')

  const PAGE_PATH = '/econtone-studio/page'
  const PAGE_FILE = fileURLToPath(new URL('./standalone.html', import.meta.url))
  let pageHtml = null
  async function standalonePage() {
    if (pageHtml == null) pageHtml = await readFile(PAGE_FILE, 'utf8')
    return pageHtml
  }

  async function handleOpenPage(req, res) {
    let body = {}
    try { body = JSON.parse(await readBody(req)) } catch {}
    const asked = (body && typeof body.url === 'string') ? body.url.trim() : ''
    const url = /^https:\/\/[^\s]{3,}$/i.test(asked) ? asked : (ctx.webServer.getOrigin() + PAGE_PATH)
    const out = { url }
    if (body && body.dry) { ok(res, Object.assign(out, { opened: false, dry: true })); return }
    try {
      const electron = await import('electron')
      const shell = electron && (electron.shell || (electron.default && electron.default.shell))
      if (shell && typeof shell.openExternal === 'function') {
        await shell.openExternal(url)
        ok(res, Object.assign(out, { opened: true, via: 'electron' }))
        return
      }
    } catch {}
    try {
      const argv = process.platform === 'win32'
        ? ['cmd.exe', '/c', 'start', '', url]
        : (process.platform === 'darwin' ? ['open', url] : ['xdg-open', url])
      const child = spawnProcess(argv[0], argv.slice(1), { detached: true, stdio: 'ignore' })
      child.unref()
      ok(res, Object.assign(out, { opened: true, via: 'spawn' }))
    } catch (err) {
      fail(res, 500, 'OPEN_FAILED', String((err && err.message) || err), out)
    }
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: PAGE_PATH,
    resource: async () => new Response(await standalonePage(), {
      headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' },
    }),
  }), 'econtone-studio: standalone page')

  ctx.effect(() => ctx.webServer.register({
    kind: 'prefix',
    path: PREFIX,
    handler: async (req, res) => {
      if (!sameOrigin(ctx, req)) { fail(res, 403, 'FORBIDDEN', 'cross-origin request'); return }
      const url = new URL(req.url, 'http://localhost')
      const rest = url.pathname.slice(PREFIX.length) || '/'
      try {
        if (req.method === 'GET' && rest === '/health') return await handleHealth(req, res)
        if (req.method === 'GET' && rest === '/build') return await handleBuild(req, res)
        if (req.method === 'GET' && rest === '/skills') return await handleListSkills(req, res)
        if (req.method === 'GET' && rest === '/history') return await handleHistory(req, res)
        if (rest === '/open-page' && req.method === 'POST') return await handleOpenPage(req, res)
        if (rest === '/run/decisions' && req.method === 'POST') return await handleSetDecisions(req, res)
        if (rest === '/run/apply' && req.method === 'POST') return await handleApplyDecisions(req, res)
        if (rest === '/run/refresh' && req.method === 'GET') return await handleRunRefresh(req, res, url)
        if (req.method === 'POST' && rest === '/run') return await handleRun(req, res)
        if (rest === '/state' && req.method === 'GET') return await handleGetState(req, res)
        if (rest === '/state' && req.method === 'POST') return await handleSetState(req, res)
        if (rest === '/binding/probe' && req.method === 'POST') return await handleProbeBinding(req, res)
        if (rest === '/binding' && req.method === 'GET') return await handleGetBinding(req, res)
        if (rest === '/probe' && req.method === 'POST') return await handleProbe(req, res)
        if (rest === '/diag' && req.method === 'GET') return await handleDiag(req, res)
        if (rest === '/binding' && req.method === 'POST') return await handleSetBinding(req, res)
        if (req.method === 'GET' && rest === '/run') return await handleGetRun(req, res, url)
        fail(res, 404, 'NOT_FOUND', `${req.method} ${rest}`)
      } catch (err) {
        fail(res, 500, err.code || 'INTERNAL', err.message, err.detail)
      }
    },
  }), 'econtone-studio: api')

  ctx.effect(() => () => {
    for (const job of jobs.values()) {
      if (job.handle) { try { job.handle.terminate() } catch {} }
    }
  }, 'econtone-studio: jobs')
}
