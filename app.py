#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音乐去重 Web 服务（Docker / 树莓派适用）"""
import hashlib, os, re, shutil, threading, time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from flask import Flask, jsonify, request, send_file

MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", "/music"))
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ape", ".aac", ".m4a", ".ogg",
              ".wma", ".opus", ".alac", ".dsd", ".dff", ".dsf"}
LOSSLESS = {".flac", ".wav", ".ape", ".alac", ".dsd", ".dff", ".dsf"}
NOISE = [r"\[.*?\]", r"\(.*?\)", r"（.*?）", r"【.*?】",
         r"(?i)\b(320k|128k|192k|256k|flac|mp3|无损|高音质|高品质|HQ|SQ|live|现场版|remix|cover)\b",
         r"(?i)(网易云音乐|QQ音乐|酷狗|酷我|咪咕|虾米)", r"[_\-\.]+"]
CHUNK = 1024 * 1024

app = Flask(__name__)
state = {"status": "idle", "progress": "", "result": None,
         "lock": threading.Lock(), "started_at": 0}
# 扫描取消 / 中断信号
CANCEL = threading.Event()


class ScanCancelled(Exception):
    pass


def norm(name):
    s = os.path.splitext(name)[0].lower()
    for p in NOISE:
        s = re.sub(p, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def read_bitrate(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
        ext = path.suffix.lower()
        if ext == ".flac" and head[:4] == b"fLaC":
            import struct
            si = head[8:42]
            sr = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
            total = ((si[13] & 0x0F) << 32) | struct.unpack(">I", si[14:18])[0]
            if sr and total:
                dur = total / sr
                return int(path.stat().st_size * 8 / dur / 1000) if dur else None
        if ext == ".mp3":
            for i in range(len(head) - 4):
                if head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0:
                    tbl = {1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96,
                           8: 112, 9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320}
                    if ((head[i + 1] >> 3) & 3) == 3 and ((head[i + 1] >> 1) & 3) == 1:
                        return tbl.get((head[i + 2] >> 4) & 15)
                    break
    except Exception:
        pass
    return None


def qscore(path, br):
    s = 100000 if path.suffix.lower() in LOSSLESS else 0
    return s + (br or 0) + path.stat().st_size / (100 * 1024 * 1024)


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def do_scan(threshold):
    files = [p for p in MUSIC_ROOT.rglob("*")
             if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    state["progress"] = f"扫描到 {len(files)} 个音频文件，正在分组…"

    # ---------- 相似名聚类：先粗筛再精算，避免 O(n²) 全量比对 ----------
    nmap = {p: norm(p.name) for p in files}
    parent = {p: p for p in files}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 归一化名完全相同 / 空名 -> 直接合并（O(n)）
    by_norm = defaultdict(list)
    for p in files:
        by_norm[nmap[p]].append(p)
    for same in by_norm.values():
        for p in same[1:]:
            union(same[0], p)

    # 只对「长度相近 + 首字符相同或共享前缀」的候选对做精细比对
    keys = [k for k in by_norm if k]
    n = len(keys)
    for i, k1 in enumerate(keys):
        if CANCEL.is_set():
            raise ScanCancelled()
        if i % 50 == 0:
            state["progress"] = f"相似度比较 {i}/{n}"
        l1 = len(k1)
        for j in range(i + 1, n):
            k2 = keys[j]
            # 长度差异过大直接跳过（相似度不可能达标）
            if abs(l1 - len(k2)) > l1 * (1 - threshold) + 2:
                continue
            # 首字符不同且无公共前缀，大概率不相似，快速跳过
            if k1[0] != k2[0]:
                continue
            if SequenceMatcher(None, k1, k2).ratio() >= threshold:
                for a in by_norm[k1]:
                    for b in by_norm[k2]:
                        union(a, b)

    groups = defaultdict(list)
    for p in files:
        groups[find(p)].append(p)

    sim = []
    total = len(groups)
    for idx, g in enumerate(groups.values()):
        if len(g) < 2:
            continue
        if CANCEL.is_set():
            raise ScanCancelled()
        if idx % 50 == 0:
            state["progress"] = f"整理相似分组 {idx}/{total}"
        info = sorted(((p, read_bitrate(p)) for p in g),
                      key=lambda x: -qscore(x[0], x[1]))
        sim.append({
            "name": nmap[info[0][0]] or info[0][0].stem,
            "files": [{"path": str(p), "ext": p.suffix.lower(),
                       "bitrate": br, "size": p.stat().st_size,
                       "size_h": human(p.stat().st_size),
                       "keep": i == 0} for i, (p, br) in enumerate(info)]
        })

    # ---------- 哈希去重（按大小预分组，只对同大小文件算 MD5）----------
    state["progress"] = "计算哈希去重…"
    by_size = defaultdict(list)
    for p in files:
        try:
            by_size[p.stat().st_size].append(p)
        except OSError:
            pass
    # 只保留大小出现多次的组，避免无谓 IO
    candidates = {s: ps for s, ps in by_size.items() if len(ps) > 1 and s > 0}
    total_cand = sum(len(v) for v in candidates.values())
    done_cand = 0
    hash_dups = []
    for size, ps in candidates.items():
        by_hash = defaultdict(list)
        for p in ps:
            if CANCEL.is_set():
                raise ScanCancelled()
            done_cand += 1
            if done_cand % 20 == 0:
                state["progress"] = f"计算哈希 {done_cand}/{total_cand}"
            try:
                by_hash[md5_of(p)].append(p)
            except OSError:
                pass
        for h, g in by_hash.items():
            if len(g) > 1:
                gs = sorted(g, key=lambda p: len(str(p)))
                hash_dups.append({
                    "md5": h[:12],
                    "files": [{"path": str(p), "size": size, "size_h": human(size),
                               "keep": i == 0} for i, p in enumerate(gs)]
                })
    return {"sim": sim, "hash": hash_dups, "total_files": len(files),
            "root": str(MUSIC_ROOT), "finished_at": time.time()}


@app.route("/")
def index():
    # 页面是纯静态 HTML（含大量 CSS/JS 花括号），不经 Jinja 渲染
    return send_file("templates/index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    with state["lock"]:
        if state["status"] == "running":
            return jsonify({"error": "扫描进行中"}), 409
        state["status"] = "running"
        state["result"] = None
        state["progress"] = "准备扫描…"
        state["started_at"] = time.time()
    CANCEL.clear()
    threshold = float((request.json or {}).get("threshold", 0.85))

    def run():
        try:
            res = do_scan(threshold)
            state["result"] = res
            state["progress"] = f"完成，用时 {res['finished_at'] - state['started_at']:.1f}s"
            state["status"] = "done"
        except ScanCancelled:
            state["status"] = "idle"
            state["progress"] = "扫描已取消"
        except MemoryError:
            state["status"] = "error"
            state["progress"] = "内存不足：音乐文件过多，请缩小扫描范围或提高阈值"
        except Exception as e:
            state["status"] = "error"
            state["progress"] = f"{type(e).__name__}: {e}"

    threading.Thread(target=run, daemon=True, name="scan-worker").start()
    return jsonify({"ok": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    CANCEL.set()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    # 看门狗：若标记 running 但线程已消失（容器 OOM 重启、线程崩溃），自动复位
    if state["status"] == "running":
        alive = any(t.name == "scan-worker" and t.is_alive()
                    for t in threading.enumerate())
        if not alive:
            state["status"] = "idle"
            state["progress"] = "上次扫描被中断（可能内存不足），请重试"
    return jsonify({"status": state["status"], "progress": state["progress"],
                    "server_time": time.time(),
                    "result": state["result"] if state["status"] == "done" else None})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    paths = request.json.get("paths", [])
    trash = MUSIC_ROOT / ".trash"
    trash.mkdir(exist_ok=True)
    ok, fail = [], []
    for s in paths:
        p = Path(s)
        # 安全校验：必须在音乐目录内、是音频文件
        try:
            p.resolve().relative_to(MUSIC_ROOT.resolve())
        except ValueError:
            fail.append({"path": s, "err": "路径越界"})
            continue
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            fail.append({"path": s, "err": "不是有效音频文件"})
            continue
        try:
            dst = trash / p.name
            i = 1
            while dst.exists():
                dst = trash / f"{p.stem}_{i}{p.suffix}"
                i += 1
            shutil.move(str(p), str(dst))
            ok.append(s)
        except OSError as e:
            fail.append({"path": s, "err": str(e)})
    return jsonify({"moved": ok, "failed": fail,
                    "note": f"已移入回收目录 {trash}，确认无误后可手动清空"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8321)
