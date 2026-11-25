#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""studio_bridge.py — EconTone Studio 的 Python 入口。

它不实现任何写作规则：只负责把 5 个私有脚本的 JSON 合成一份 RunResult，
并补上引擎本身不提供的东西——字符区间、严重度、指纹、错误归一化。

    python studio_bridge.py health  --engine <skills-root>
    python studio_bridge.py measure --file draft.md --profile body --engine <root> --out run.json

只用标准库；不联网；读写都按 UTF-8（读 utf-8-sig，写无 BOM）。
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time

# Windows 下 stdout 默认是 GBK：强制 UTF-8，否则输出中文 JSON 时直接崩。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
del _stream

SKILLS = [
    "econtone-score",
    "econtone-polish",
    "econtone-ai-trace",
    "econtone-prosody",
    "econtone-equilibrium",
]

SCRIPTS = {
    "score": ("econtone-score", "score.py"),
    "terminology": ("econtone-polish", "terminology_check.py"),
    "mechanical": ("econtone-polish", "mechanical_check.py"),
    "register": ("econtone-ai-trace", "scan_register.py"),
    "detect": ("econtone-ai-trace", "scan_detectability.py"),
    "prosody": ("econtone-prosody", "prosody_check.py"),
}

SEVERITY = {
    "BANNED": "error",
    "MIXED": "error",
    "ACRONYM": "warn",
    "TIER_HIGH": "warn",
}


def default_engine():
    home = os.path.expanduser("~")
    return os.path.join(home, ".dsh", "skills")


def read_text(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def script_path(engine, key):
    skill, name = SCRIPTS[key]
    return os.path.join(engine, skill, "scripts", name)


def fingerprint(engine):
    h = hashlib.sha256()
    for key in sorted(SCRIPTS):
        p = script_path(engine, key)
        try:
            st = os.stat(p)
            h.update(("%s:%d:%d;" % (key, st.st_size, int(st.st_mtime))).encode())
        except OSError:
            h.update(("%s:missing;" % key).encode())
    return h.hexdigest()[:16]


def call_json(engine, key, args, timeout=120):
    """Run one scanner with --json and return its parsed stdout."""
    p = script_path(engine, key)
    if not os.path.exists(p):
        raise BridgeError("SCANNER_MISSING", "%s 不存在" % p)
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, p] + args + ["--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BridgeError("TIMEOUT", "%s 超过 %ds" % (key, timeout))
    if proc.returncode not in (0, 2):
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise BridgeError("SCANNER_FAILED", "%s 退出码 %d: %s" % (key, proc.returncode, tail))
    out = (proc.stdout or "").strip()
    if not out:
        raise BridgeError("SCANNER_FAILED", "%s 没有输出" % key)
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise BridgeError("SCANNER_FAILED", "%s 输出不是 JSON: %s" % (key, exc))
    return data, round(time.time() - started, 3)


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def line_starts(text):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def paragraphs(text):
    """Return [(start, end)] for blank-line separated paragraphs."""
    out, pos = [], 0
    for block in re.split(r"\n[ \t]*\n", text):
        if block:
            start = text.find(block, pos)
            out.append((start, start + len(block)))
            pos = start + len(block)
    return out


def locate(text, needle, line_no=None, starts=None):
    """Best-effort character range for a needle, preferring the given line."""
    if not needle:
        return None
    if line_no and starts and 0 < line_no <= len(starts):
        s = starts[line_no - 1]
        e = starts[line_no] if line_no < len(starts) else len(text)
        i = text.find(needle, s, e)
        if i >= 0:
            return i, i + len(needle)
    i = text.find(needle)
    if i >= 0:
        return i, i + len(needle)
    # 大小写不同很常见（句首大写），回退到不区分大小写
    m = re.search(re.escape(needle), text, re.IGNORECASE)
    if m:
        return m.start(), m.end()
    return None


def para_of(text, offset, paras):
    for idx, (s, e) in enumerate(paras, 1):
        if s <= offset <= e:
            return idx
    return None


def make_finding(fid, text, paras, starts, original, revised, player, kind,
                 severity, evidence, locate_mode="exact", span=None):
    if span:
        s, e = span
    else:
        s = e = 0
        locate_mode = "none"
    return {
        "id": fid,
        "op": "none",
        "base_range": {"start": s, "end": e},
        "after_range": {"start": s, "end": e},
        "original": original,
        "revised": revised,
        "paragraph": para_of(text, s, paras),
        "player": player,
        "kind": kind,
        "severity": severity,
        "decision": "pending",
        "reason": evidence.get("message", "") if evidence else "",
        "evidence": [evidence] if evidence else [],
        "locate": locate_mode,
    }


def collect_findings(text, data):
    paras = paragraphs(text)
    starts = line_starts(text)
    findings = []
    n = [0]

    def nid():
        n[0] += 1
        return "f-%03d" % n[0]

    term = data.get("terminology") or {}
    for hit in (term.get("hits") or [])[:40]:
        word = hit.get("term") or ""
        span = locate(text, word, hit.get("line"), starts)
        kind = hit.get("kind", "BANNED")
        findings.append(make_finding(
            nid(), text, paras, starts, word, word, "polish", "wording",
            SEVERITY.get(kind, "warn"),
            {"source": "terminology", "code": kind, "term": word,
             "message": hit.get("why", "")},
            "exact" if span else "paragraph", span,
        ))

    pros = data.get("prosody") or {}
    for idx, para in enumerate(pros.get("paragraphs") or []):
        for marker in (para.get("red_line_markers") or [])[:6]:
            sentence = (marker.get("sentence") or "").strip()
            span = locate(text, sentence[:80]) if sentence else None
            findings.append(make_finding(
                nid(), text, paras, starts, sentence, sentence, "prosody", "boundary",
                "warn",
                {"source": "prosody", "code": marker.get("name", "MARKER"),
                 "message": marker.get("message", "")},
                "exact" if span else "paragraph", span,
            ))

    det = data.get("detect") or {}
    risk = det.get("risk") or {}
    tier = str(risk.get("tier") or "")
    if risk.get("risk_index") is not None:
        sev = "warn" if ("ELEVATED" in tier or "HIGH" in tier) else "info"
        findings.append(make_finding(
            nid(), text, paras, starts, "", "", "trace", "wording", sev,
            {"source": "trace", "code": "TIER", "value": risk.get("risk_index"),
             "message": "risk_index %.1f / %s" % (risk.get("risk_index"), tier or "?")},
            "none", None,
        ))

    reg = data.get("register") or {}
    for item in (reg.get("findings") or [])[:10]:
        tokens = item.get("tokens") or {}
        phrase = next(iter(tokens), "")
        span = locate(text, phrase) if phrase else None
        findings.append(make_finding(
            nid(), text, paras, starts, phrase, phrase, "trace", "wording", "warn",
            {"source": "register", "code": "REGISTER",
             "message": "%s · %s" % (item.get("band", ""), phrase)},
            "exact" if span else "paragraph", span,
        ))

    return findings


def cmd_health(args):
    engine = os.path.abspath(args.engine)
    scripts = {}
    for key in SCRIPTS:
        p = script_path(engine, key)
        scripts[key] = {"path": p, "exists": os.path.exists(p)}
    found = all(v["exists"] for v in scripts.values())
    info = {
        "python": {"version": "%d.%d.%d" % sys.version_info[:3], "executable": sys.executable},
        "engine": {
            "root": engine,
            "found": found,
            "fingerprint": fingerprint(engine),
            "skills": {s: os.path.isdir(os.path.join(engine, s)) for s in SKILLS},
            "scripts": scripts,
        },
        "bridgeVersion": "0.1.0",
    }
    if args.json:
        print(json.dumps({"ok": True, "data": info}, ensure_ascii=False, indent=2))
    else:
        print("python   %s" % info["python"]["version"])
        print("engine   %s  found=%s  fp=%s" % (engine, found, info["engine"]["fingerprint"]))
        for key, v in scripts.items():
            print("  %-12s %s" % (key, "ok" if v["exists"] else "MISSING"))
    return 0 if found else 3


def cmd_measure(args):
    engine = os.path.abspath(args.engine)
    text = normalize(read_text(args.file))
    if len(text) > args.max_chars:
        raise BridgeError("DOC_TOO_LARGE", "文本 %d 字符，上限 %d" % (len(text), args.max_chars))

    data, timings, warnings = {}, {}, []
    jobs = [("score", [args.file, "--profile", args.profile])]
    for key in ("terminology", "register", "detect"):
        jobs.append((key, [args.file]))
    jobs.append(("prosody", [args.file, "--profile", args.profile]))

    for key, argv in jobs:
        try:
            data[key], timings[key] = call_json(engine, key, argv)
        except BridgeError as exc:
            if key == "score":
                raise
            warnings.append("%s: %s" % (exc.code, exc.message))
            timings[key] = 0.0

    score = data["score"]
    findings = collect_findings(text, data)

    result = {
        "runId": args.run_id,
        "createdAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": args.profile,
        "mode": "measure",
        "document": {
            "path": os.path.abspath(args.file),
            "sha256": sha256(text),
            "chars": len(text),
        },
        "engine": {"root": engine, "fingerprint": fingerprint(engine)},
        "score": {
            "rows": score.get("rows", []),
            "total": score.get("total"),
            "editing_latitude": score.get("editing_latitude"),
            "band": score.get("band"),
            "advice": score.get("advice", ""),
        },
        "findings": findings,
        "edits": [],
        "certificate": None,
        "warnings": warnings,
        "timings": timings,
    }

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.file)), "run.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    else:
        print("total %s/100  latitude %s%%  band %s  findings %d" % (
            result["score"]["total"], result["score"]["editing_latitude"],
            result["score"]["band"], len(findings)))
        print("-> %s" % out)
    return 0


def run_mechanical(engine, path, timeout=60):
    """mechanical_check.py 输出的是裸数组，退出码 2 表示「有发现」。"""
    p = script_path(engine, "mechanical")
    if not os.path.exists(p):
        raise BridgeError("SCANNER_MISSING", p)
    try:
        proc = subprocess.run([sys.executable, p, path, "--json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BridgeError("TIMEOUT", "mechanical_check 超时")
    if proc.returncode not in (0, 2):
        raise BridgeError("SCANNER_FAILED", (proc.stderr or proc.stdout)[-400:])
    try:
        return json.loads(proc.stdout or "[]")
    except ValueError:
        raise BridgeError("SCANNER_FAILED", "mechanical_check 输出不是 JSON")


def apply_suggestions(text, findings, limit=200):
    """把 token→suggest 就地替换，返回 (新文本, 实际替换列表)。"""
    hits = []
    used = set()
    for item in findings[:limit]:
        token = item.get("token") or ""
        repl = item.get("suggest")
        if not token or repl is None:
            continue
        start = 0
        while True:
            i = text.find(token, start)
            if i < 0:
                break
            if i not in used:
                hits.append((i, i + len(token), repl))
                used.add(i)
                break
            start = i + 1
    hits.sort(key=lambda h: h[0], reverse=True)
    out = text
    for s, e, repl in hits:
        out = out[:s] + repl + out[e:]
    applied = [{"from": text[s:e], "to": repl, "at": s} for s, e, repl in reversed(hits)]
    return out, applied


def derive(base, after, source="mechanical", reason=""):
    """字符级 diff → EditUnit 列表（合并相邻、跳过等价段）。source/reason 用于说明改动依据。"""
    sm = difflib.SequenceMatcher(None, base, after, autojunk=False)
    raw = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (i1 == i2 and j1 == j2):
            continue
        op = "replace" if (i1 < i2 and j1 < j2) else ("delete" if i1 < i2 else "insert")
        raw.append([op, i1, i2, j1, j2])
    merged = []
    for unit in raw:
        if merged and unit[1] - merged[-1][2] <= 2:
            last = merged[-1]
            last[2] = unit[2]
            last[4] = unit[4]
            last[0] = "replace" if (last[2] > last[1] and last[4] > last[3]) else last[0]
        else:
            merged.append(unit)
    paras = paragraphs(base)
    out = []
    for idx, (op, i1, i2, j1, j2) in enumerate(merged, 1):
        out.append({
            "id": "eu-%04d" % idx,
            "op": op,
            "base_range": {"start": i1, "end": i2},
            "after_range": {"start": j1, "end": j2},
            "original": base[i1:i2],
            "revised": after[j1:j2],
            "paragraph": para_of(base, i1, paras),
            "player": "polish",
            "kind": "wording",
            "severity": "info",
            "decision": "accepted",
            "reason": reason,
            "evidence": [{"source": source,
                          "code": "MODEL" if source == "model" else "MECHANICAL",
                          "message": reason}],
            "locate": "exact",
        })
    return out


def attach_reasons(edits, reasons):
    """把模型给的逐条理由对到 diff 条目上。"""
    if not reasons:
        return edits
    used = set()
    for e in edits:
        hit = None
        for i, r in enumerate(reasons):
            if i in used:
                continue
            o = str(r.get("original") or "").strip()
            v = str(r.get("revised") or "").strip()
            if (o and o == e["original"].strip()) or (v and v == e["revised"].strip()):
                hit = i
                break
        if hit is None:
            for i, r in enumerate(reasons):
                if i in used:
                    continue
                o = str(r.get("original") or "").strip()
                if o and (o in e["original"] or e["original"] in o):
                    hit = i
                    break
        if hit is None:
            continue
        used.add(hit)
        why = str(reasons[hit].get("why") or "").strip()
        if why:
            e["reason"] = why
            if e.get("evidence"):
                e["evidence"][0]["message"] = why
    return edits


def _load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cmd_agent_result(args):
    """Agent 已经跑完脚本：这里只读结果，不再执行任何扫描器。"""
    engine = os.path.abspath(args.engine)
    file_abs = os.path.abspath(args.file)
    run_dir = os.path.abspath(args.run_dir or os.path.dirname(file_abs))
    os.makedirs(run_dir, exist_ok=True)

    base_text = normalize(read_text(file_abs))
    final_text = normalize(read_text(args.final))
    cert = _load_json(args.cert)
    score = _load_json(args.score)
    baseline = _load_json(args.baseline)
    reasons = _load_json(args.reasons)
    if not isinstance(reasons, list):
        reasons = []

    edits = attach_reasons(derive(base_text, final_text, "model", ""), reasons)
    findings = []
    for i, ch in enumerate(reasons, 1):
        why = str(ch.get("why") or "").strip()
        orig = str(ch.get("original") or "").strip()
        if not orig and not why:
            continue
        findings.append({
            "id": "f-%03d" % i, "op": "none",
            "base_range": {"start": 0, "end": 0},
            "after_range": {"start": 0, "end": 0},
            "original": orig, "revised": str(ch.get("revised") or ""),
            "paragraph": 1, "player": "polish", "kind": "wording",
            "severity": "info", "decision": "accepted", "reason": why,
            "evidence": [{"source": "model", "code": "MODEL", "message": why}],
            "locate": "exact" if orig else "none",
        })

    result = {
        "runId": args.run_id,
        "createdAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": args.profile,
        "mode": "agent-arbitrate",
        "document": {"path": file_abs,
                     "sha256": hashlib.sha256(base_text.encode("utf-8")).hexdigest(),
                     "chars": len(base_text)},
        "engine": {"root": engine, "fingerprint": fingerprint(engine)},
        "score": baseline,
        "findings": findings,
        "edits": edits,
        "certificate": cert,
        "candidateDecision": {"id": "m-agent", "accepted": True,
                              "reason": str((cert or {}).get("stopped_because") or "agent")},
        "mechanical": {"applied": 0, "candidate": None, "skipped": "agent mode"},
        "finalText": final_text,
        "baseText": base_text,
        "finalScore": score,
        "warnings": [],
        "timings": {},
    }

    out = args.out or os.path.join(run_dir, "run.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    else:
        print("agent result (no scripts run) -> %s" % out)
    return 0


def cmd_arbitrate(args):
    if args.final:
        return cmd_agent_result(args)
    """先测量，再让 play.py 在候选动作上求解均衡。

    候选动作为空时仍然走完整链路：play.py 会测量基线并立即停止，
    证书里写明「无可用动作」。等 P4/P6 产出 cand/*.md 后，这里只换 moves.json。
    """
    engine = os.path.abspath(args.engine)
    file_abs = os.path.abspath(args.file)
    run_dir = os.path.abspath(args.run_dir or os.path.dirname(file_abs))
    os.makedirs(run_dir, exist_ok=True)

    measure_args = [sys.executable, os.path.abspath(__file__), "measure",
                    "--file", file_abs, "--profile", args.profile,
                    "--engine", engine, "--run-id", args.run_id, "--json"]
    proc = subprocess.run(measure_args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=args.timeout)
    if proc.returncode != 0:
        raise BridgeError("MEASURE_FAILED", (proc.stderr or proc.stdout)[-600:])
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        err = payload.get("error") or {}
        raise BridgeError(err.get("code", "MEASURE_FAILED"), err.get("message", "measure failed"))
    result = payload["data"]

    base_text = normalize(read_text(file_abs))
    cand_dir = os.path.join(run_dir, "cand")
    os.makedirs(cand_dir, exist_ok=True)

    # Agent 路径：均衡由 skill 在子 Agent 里跑，这里只做评分与差分。
    if args.final:
        final_text = normalize(read_text(args.final))
        cert = None
        if args.cert and os.path.exists(args.cert):
            try:
                with open(args.cert, encoding="utf-8-sig") as fh:
                    cert = json.load(fh)
            except ValueError:
                cert = None
        if not isinstance(cert, dict):
            cert = {"source": "agent", "game": {}, "rounds": [],
                    "stopped_because": "agent-equilibrium"}
        reasons = []
        if args.reasons and os.path.exists(args.reasons):
            try:
                with open(args.reasons, encoding="utf-8-sig") as fh:
                    reasons = json.load(fh)
                if not isinstance(reasons, list):
                    reasons = []
            except (OSError, ValueError):
                reasons = []
        result["mode"] = "agent-arbitrate"
        result["certificate"] = cert
        result["candidateDecision"] = {"id": "m-agent", "accepted": True,
                                       "reason": str(cert.get("stopped_because") or "agent")}
        result["mechanical"] = {"applied": 0, "candidate": None, "skipped": "agent mode"}
        result["finalText"] = final_text
        result["edits"] = attach_reasons(derive(base_text, final_text, "model", ""), reasons)

        final_score = None
        if final_text and final_text != base_text:
            scored = os.path.join(run_dir, "final.md")
            with open(scored, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(final_text)
            proc2 = subprocess.run([sys.executable, os.path.abspath(__file__), "measure",
                                    "--file", scored, "--profile", args.profile,
                                    "--engine", engine, "--json"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=args.timeout)
            if proc2.returncode == 0:
                try:
                    payload2 = json.loads(proc2.stdout)
                    if payload2.get("ok"):
                        fs = payload2["data"]["score"]
                        final_score = {"rows": fs.get("rows"), "total": fs.get("total"),
                                       "editing_latitude": fs.get("editing_latitude"),
                                       "band": fs.get("band")}
                except ValueError:
                    final_score = None
        result["finalScore"] = final_score
        out = args.out or os.path.join(run_dir, "run.json")
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        if args.json:
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        else:
            print("agent equilibrium: %s" % (cert.get("stopped_because") or ""))
            print("-> %s" % out)
        return 0

    os.makedirs(cand_dir, exist_ok=True)
    mech_info = {"applied": 0, "candidate": None, "skipped": None}

    # 机械修正是「赛前动作」：play.py 的 gate:"mechanical" 分支有 bug，
    # 所以按引擎的设计意图，在我们这一层先应用到 current.md，再交给博弈。
    after_text, applied = base_text, []
    try:
        findings = run_mechanical(engine, file_abs)
        after_text, applied = apply_suggestions(base_text, findings)
    except BridgeError as exc:
        mech_info["skipped"] = "%s: %s" % (exc.code, exc.message)

    current_text = after_text if (applied and after_text != base_text) else base_text
    current_path = os.path.join(run_dir, "current.md")
    with open(current_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(current_text)

    if applied and current_text != base_text:
        cand_path = os.path.join(cand_dir, "mechanical.md")
        with open(cand_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(current_text)
        mech_info = {"applied": len(applied), "candidate": cand_path, "skipped": None}
    elif not mech_info["skipped"]:
        mech_info["skipped"] = "no mechanical findings"

    moves_path = args.candidates or os.path.join(run_dir, "moves.json")
    if args.candidates:
        with open(args.candidates, encoding="utf-8-sig") as fh:
            raw_moves = json.load(fh)
        for mv in raw_moves:
            mv["base"] = "current.md"
            mv.setdefault("player", "polish")
            mv.setdefault("kind", "wording")
        with open(moves_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(raw_moves, fh, ensure_ascii=False, indent=2)
    elif not os.path.exists(moves_path):
        with open(moves_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump([], fh)

    play = os.path.join(engine, "econtone-equilibrium", "scripts", "play.py")
    if not os.path.exists(play):
        raise BridgeError("SCANNER_MISSING", play)
    cert_path = os.path.join(run_dir, "certificate.json")
    started = time.time()
    proc = subprocess.run([sys.executable, play, "--current", current_path,
                           "--moves", moves_path, "--profile", args.profile,
                           "--rounds", str(args.rounds), "--iteration-cost", "0",
                           "--coalition", args.coalition, "--mode", args.mode,
                           "--out", cert_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=args.timeout)
    if proc.returncode != 0:
        raise BridgeError("ARBITRATE_FAILED", (proc.stderr or proc.stdout)[-800:])
    with open(cert_path, encoding="utf-8-sig") as fh:
        cert = json.load(fh)

    result["mode"] = "arbitrate"
    result["certificate"] = cert
    decision = None
    for rd in cert.get("rounds") or []:
        for mv in rd.get("moves") or []:
            if mv.get("id") == "m-model":
                decision = {"id": "m-model", "accepted": bool(mv.get("accepted")),
                            "reason": mv.get("reason", "")}
    result["candidateDecision"] = decision
    result["timings"]["play"] = round(time.time() - started, 3)
    result["mechanical"] = mech_info

    final_path = cert.get("final_text_path") or file_abs
    try:
        final_text = normalize(read_text(final_path))
    except OSError:
        final_text = base_text
    result["finalText"] = final_text
    result["baseText"] = base_text
    if decision and decision.get("accepted"):
        dsrc, dreason = "model", decision.get("reason", "")
    elif mech_info.get("applied"):
        dsrc, dreason = "mechanical", "\u62fc\u5199 / \u51a0\u8bcd / \u7a7a\u683c\u4fee\u6b63"
    else:
        dsrc, dreason = "model", ""
    reasons = []
    if args.reasons and os.path.exists(args.reasons):
        try:
            with open(args.reasons, encoding="utf-8-sig") as fh:
                reasons = json.load(fh)
            if not isinstance(reasons, list):
                reasons = []
        except (OSError, ValueError):
            reasons = []
    result["edits"] = attach_reasons(derive(base_text, final_text, dsrc, dreason), reasons)

    # 改写后的评分：对 final.md 再跑一次 measure（只取 score 部分）
    final_score = None
    if final_text and final_text != base_text:
        scored_path = os.path.join(run_dir, "final.md")
        with open(scored_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(final_text)
        proc2 = subprocess.run([sys.executable, os.path.abspath(__file__), "measure",
                                "--file", scored_path, "--profile", args.profile,
                                "--engine", engine, "--json"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=args.timeout)
        if proc2.returncode == 0:
            try:
                payload2 = json.loads(proc2.stdout)
                if payload2.get("ok"):
                    fs = payload2["data"]["score"]
                    final_score = {"rows": fs.get("rows"), "total": fs.get("total"),
                                   "editing_latitude": fs.get("editing_latitude"),
                                   "band": fs.get("band")}
            except ValueError:
                final_score = None
    result["finalScore"] = final_score

    out = args.out or os.path.join(run_dir, "run.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    else:
        print("equilibrium: %s" % (cert.get("stopped_because") or ""))
        print("-> %s" % out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="EconTone Studio bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("health")
    h.add_argument("--engine", default=default_engine())
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_health)

    m = sub.add_parser("measure")
    m.add_argument("--file", required=True)
    m.add_argument("--profile", choices=["body", "abstract", "intro"], default="body")
    m.add_argument("--engine", default=default_engine())
    m.add_argument("--out", default=None)
    m.add_argument("--run-id", default="local")
    m.add_argument("--max-chars", type=int, default=200000)
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_measure)

    a = sub.add_parser("arbitrate")
    a.add_argument("--file", required=True)
    a.add_argument("--profile", choices=["body", "abstract", "intro"], default="body")
    a.add_argument("--engine", default=default_engine())
    a.add_argument("--out", default=None)
    a.add_argument("--run-id", default="local")
    a.add_argument("--run-dir", default=None)
    a.add_argument("--candidates", default=None)
    a.add_argument("--reasons", default=None)
    a.add_argument("--final", default=None)
    a.add_argument("--cert", default=None)
    a.add_argument("--score", default=None)
    a.add_argument("--baseline", default=None)
    a.add_argument("--rounds", type=int, default=3)
    a.add_argument("--coalition", "--cooperation", dest="coalition",
                   choices=["none", "maxsum", "nash"], default="none")
    a.add_argument("--mode", choices=["constrained", "free"], default="constrained")
    a.add_argument("--game", choices=["stackelberg", "nash", "bayes", "correlated"], default="stackelberg")
    a.add_argument("--timeout", type=int, default=900)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_arbitrate)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
