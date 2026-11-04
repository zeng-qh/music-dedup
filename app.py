#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音乐去重 Web 服务（Docker / 树莓派适用）"""
import hashlib, json, os, re, shutil, threading, time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from flask import Flask, jsonify, request, send_file

MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", "/music"))
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ape", ".aac", ".m4a", ".ogg",
              ".wma", ".opus", ".alac", ".dsd", ".dff", ".dsf"}
LOSSLESS = {".flac", ".wav", ".ape", ".alac", ".dsd", ".dff", ".dsf"}

# ---------- 可配置的规则项 ----------
# 每项规则由「默认值 + 说明 + 可选项」构成，前端据此渲染配置面板
DEFAULT_OPTIONS = {
    "threshold": 0.85,        # 相似度阈值
    "live_policy": "avoid",   # 现场版策略
    "hash_enabled": True,     # 是否启用哈希去重
    "preselect": True,        # 是否默认勾选建议删除项
    "strip_bitrate": True,    # 清洗时去掉码率/格式标记
    "strip_source": True,     # 清洗时去掉平台水印
    "strip_version": True,    # 清洗时去掉版本标记（live/remix 等）
    "ignore_case": True,      # 忽略大小写
    "loose_match": False,     # 宽松匹配（放宽候选筛选）
}

LIVE_POLICIES = {
    "avoid": "避开现场版：专辑/录音室版永远优先，即使 live 版本音质更高。适合追求原版听感",
    "quality": "只看音质：不区分录音类型，纯按无损格式与码率排序。适合不在意版本来源",
    "prefer": "优先现场版：live 版本排在最前。适合偏好现场氛围与即兴演绎",
}

# 噪声词表按类别组织，便于按开关组合
NOISE_BITRATE = [
    r"(?i)\b(320k|256k|192k|128k|64k|flac|mp3|wav|ape|aac|m4a|ogg|wma|hq|sq|cbr|vbr)\b",
    r"(无损|無損|高音质|高音質|高品质|高品質|母带|母帶)",
]
NOISE_SOURCE = [
    r"(?i)\b(网易云音乐|qq音乐|酷狗音乐|酷我音乐|咪咕音乐|虾米音乐|酷狗|酷我|咪咕|虾米)\b",
    r"(网易云|QQ音乐|酷狗|酷我|咪咕|虾米)",
]
NOISE_VERSION = [
    r"(?i)\b(live|lives|unplugged|remix|cover|demo|acoustic|instrumental|karaoke|"
    r"remaster|remastered|deluxe|bonus|edit|version|radio\s*edit)\b",
    r"(现场版|現場版|现场|現場|演唱会版|演唱會版|演唱会|演唱會|音乐会|音樂會|"
    r"巡演|巡迴|试听|試聽|伴奏|纯音乐|純音樂|翻唱|重制版|重製版|豪华版|豪華版)",
]
NOISE_STRUCT = [r"\[.*?\]", r"\(.*?\)", r"（.*?）", r"【.*?】", r"[_\-\.]+"]

# 默认（全部开启）的词表，兼容旧逻辑
NOISE = NOISE_STRUCT + NOISE_BITRATE + NOISE_SOURCE + NOISE_VERSION
CHUNK = 1024 * 1024

app = Flask(__name__)
state = {"status": "idle", "progress": "", "result": None,
         "lock": threading.Lock(), "started_at": 0}
# 扫描取消 / 中断信号
CANCEL = threading.Event()


class ScanCancelled(Exception):
    pass


def build_noise(opts):
    """按配置组装噪声规则

    括号/标点等结构符号只在三种语义清洗都关闭时才整体去除；
    否则交由语义规则处理，保证「关闭忽略版本标记」时 (Live) 能保留下来参与比对。
    """
    semantic_on = any(opts.get(k, True) for k in
                      ("strip_bitrate", "strip_source", "strip_version"))
    pats = []
    # 标点类分隔符始终规范化
    pats += [r"[_\-\.]+"]
    if opts.get("strip_bitrate", True): pats += NOISE_BITRATE
    if opts.get("strip_source", True):  pats += NOISE_SOURCE
    if opts.get("strip_version", True): pats += NOISE_VERSION
    # 无任何语义清洗时，只去掉括号符号本身，保留括号内文字
    if not semantic_on:
        pats += [r"[\[\]\(\)（）【】]"]
    else:
        # 去掉空括号残留，如「晴天（）」
        pats += [r"\(\s*\)", r"（\s*）", r"\[\s*\]", r"【\s*】"]
    return pats


def norm(name, opts=None):
    """归一化文件名：去扩展名、噪声标记、标点，可选忽略大小写"""
    opts = opts or {}
    s = os.path.splitext(name)[0]
    if opts.get("ignore_case", True):
        s = s.lower()
    for p in build_noise(opts):
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


def is_live(name):
    """判断是否为现场版：live / 現場 / 演唱会 / 音乐会 / unplugged 现场等"""
    s = name.lower()
    # 明确标注为现场的标记（含中文、英文、常见括号写法）
    live_pat = re.compile(
        r"(?<![\w])(live|lives|live\s*version|unplugged|concert|in\s*concert)"
        r"|现场|現場|演唱会|演唱會|音乐会|音樂會|现场版|現場版|演唱会版|巡演|巡迴"
        r"|\(live\)|\[live\]|【live】|（live）|\[现场\]|\(现场\)|【现场】|（现场）",
        re.IGNORECASE)
    # 排除：live at / live in 后接地名也算现场；但 "delivery/alive" 等误判由 \b 处理
    return bool(live_pat.search(s))


def qscore(path, br, live_policy="avoid"):
    """综合评分：按现场版策略决定录音类型的权重

    live_policy 策略：
      avoid  —— 避开现场版（默认）：录音室版永远优先，即使 live 音质更高
      quality —— 只看音质：不区分录音类型，纯按无损/码率排
      prefer —— 优先现场版：live 排在最前（适合偏好现场氛围的用户）

    同层内统一按「无损格式 > 码率 > 文件大小」排序。
    """
    is_lv = is_live(path.name)
    if live_policy == "quality":
        tier = 0                                   # 不看录音类型
    elif live_policy == "prefer":
        tier = 30000000 if is_lv else 20000000     # 现场版优先
    else:  # avoid
        tier = 10000000 if is_lv else 30000000     # 现场版垫底
    s = tier
    if path.suffix.lower() in LOSSLESS:
        s += 100000
    s += (br or 0)
    s += path.stat().st_size / (100 * 1024 * 1024)
    return s


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


def do_scan(opts):
    threshold = float(opts.get("threshold", 0.85))
    files = [p for p in MUSIC_ROOT.rglob("*")
             if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    state["progress"] = f"扫描到 {len(files)} 个音频文件，正在分组…"

    # ---------- 相似名聚类：先粗筛再精算，避免 O(n²) 全量比对 ----------
    nmap = {p: norm(p.name, opts) for p in files}
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
            # 首字符不同大概率不相似，快速跳过（宽松模式关闭此预筛）
            if not opts.get("loose_match") and k1[0] != k2[0]:
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
                      key=lambda x: -qscore(x[0], x[1], opts.get("live_policy", "avoid")))
        sim.append({
            "name": nmap[info[0][0]] or info[0][0].stem,
            "files": [{"path": str(p), "ext": p.suffix.lower(),
                       "bitrate": br, "size": p.stat().st_size,
                       "live": is_live(p.name),
                       "size_h": human(p.stat().st_size),
                       "keep": i == 0} for i, (p, br) in enumerate(info)]
        })

    # ---------- 哈希去重（按大小预分组，只对同大小文件算 MD5）----------
    hash_dups = []
    if not opts.get("hash_enabled", True):
        state["progress"] = "已按配置跳过哈希去重"
        hash_dups = []
    else:
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
            "root": str(MUSIC_ROOT), "finished_at": time.time(),
            "options": opts}


# ---------- 扫描结果存档：下次免扫描 ----------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def archive_list():
    """列出所有已存档的扫描记录（按时间倒序）"""
    items = []
    for f in DATA_DIR.glob("scan_*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "file": f.name,
                "root": meta.get("root", ""),
                "threshold": meta.get("threshold", 0.85),
                "finished": meta.get("finished_at", 0),
                "total_files": meta.get("total_files", 0),
                "sim_count": len(meta.get("sim", [])),
                "hash_count": len(meta.get("hash", [])),
            })
        except Exception:
            continue
    items.sort(key=lambda x: -x["finished"])
    return items


def archive_save(result, threshold):
    """按 目录+阈值 命名存档，同目录同阈值覆盖旧档"""
    import hashlib as _h
    key = _h.md5(f"{result['root']}|{threshold}".encode()).hexdigest()[:10]
    f = DATA_DIR / f"scan_{key}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({**result, "threshold": threshold},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)
    return f.name


@app.route("/api/history")
def api_history():
    return jsonify(archive_list())


@app.route("/api/cache/<name>", methods=["GET"])
def api_cache(name):
    """加载一份存档结果，直接置为当前报告（免扫描）"""
    if not re.fullmatch(r"scan_\w+\.json", name):
        return jsonify({"error": "非法文件名"}), 400
    f = DATA_DIR / name
    if not f.exists():
        return jsonify({"error": "存档不存在"}), 404
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"存档损坏: {e}"}), 500
    state["result"] = data
    state["progress"] = (f"已加载存档（{data.get('root','?')}，"
                         f"扫描于 {time.strftime('%m-%d %H:%M', time.localtime(data.get('finished_at', 0)))}）")
    state["status"] = "done"
    return jsonify({"ok": True})


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
    # 合并用户配置与默认值，并做类型/取值校验
    # 兼容两种传参：{"options": {...}, "root": ...} 或 {...} 平铺
    raw = request.json or {}
    src = raw.get("options")
    if not isinstance(src, dict):
        src = raw
    opts = dict(DEFAULT_OPTIONS)
    for k, default in DEFAULT_OPTIONS.items():
        if k not in src:
            continue
        v = src[k]
        if isinstance(default, bool):
            opts[k] = bool(v)
        elif isinstance(default, float):
            try:
                opts[k] = min(1.0, max(0.3, float(v)))
            except (TypeError, ValueError):
                pass
        else:
            if k == "live_policy" and v in LIVE_POLICIES:
                opts[k] = v
    threshold = opts["threshold"]

    def run():
        try:
            res = do_scan(opts)
            res["threshold"] = threshold
            state["result"] = res
            state["progress"] = f"完成，用时 {res['finished_at'] - state['started_at']:.1f}s"
            state["status"] = "done"
            try:
                archive_save(res, threshold)
            except Exception:
                pass  # 存档失败不影响扫描结果展示
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


@app.route("/api/options")
def api_options():
    """返回全部可配置项的定义（默认值、类型、说明、可选项），供前端渲染"""
    return jsonify({
        "defaults": DEFAULT_OPTIONS,
        "live_policies": LIVE_POLICIES,
        "schema": [
            {
                "key": "threshold", "type": "range", "label": "相似度阈值",
                "min": 0.60, "max": 0.98, "step": 0.01,
                "desc": "文件名相似到什么程度才归为一组。数值越高越严格，"
                        "建议 0.85；调低会匹配到更多不同版本，也更容易误判。",
            },
            {
                "key": "live_policy", "type": "select", "label": "现场版处理",
                "desc": "当同一首歌既有专辑版又有 live 版时，决定组内默认保留哪一个。",
            },
            {
                "key": "hash_enabled", "type": "switch", "label": "哈希去重",
                "desc": "对大小相同的文件计算 MD5，找出内容完全一致的文件。"
                        "关闭可加快扫描，但会漏掉「改名后的同一文件」。",
            },
            {
                "key": "preselect", "type": "switch", "label": "默认勾选建议删除项",
                "desc": "扫描完成后自动把非推荐版本标记为待删除。"
                        "关闭后所有文件默认保留，需你逐个手动选择要删的。",
            },
            {
                "key": "strip_bitrate", "type": "switch", "label": "忽略码率标记",
                "desc": "比较文件名时去掉 320k、FLAC、无损 等标记，"
                        "让「晴天.flac」和「晴天 320k.mp3」能被认成同一首歌。",
            },
            {
                "key": "strip_source", "type": "switch", "label": "忽略平台水印",
                "desc": "去掉网易云、QQ音乐、酷狗等平台后缀，避免同一首歌因下载来源不同被当成两首。",
            },
            {
                "key": "strip_version", "type": "switch", "label": "忽略版本标记",
                "desc": "比较时去掉 live、remix、伴奏、翻唱、remaster 等版本词。"
                        "注意：关闭后 live 版不会与专辑版归为一组，也就不会被判为重复。",
            },
            {
                "key": "ignore_case", "type": "switch", "label": "忽略大小写",
                "desc": "把 Hello 与 hello 视为同一个名字。英文曲库建议开启。",
            },
            {
                "key": "loose_match", "type": "switch", "label": "宽松匹配",
                "desc": "取消「首字符必须相同」的快速预筛，允许标题开头不同的文件参与比对。"
                        "更全面但扫描更慢，大曲库慎用。",
            },
        ],
    })


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


@app.route("/api/rename", methods=["POST"])
def api_rename():
    """重命名单个文件（仅改文件名，不换目录），并同步更新当前报告与存档"""
    data = request.json or {}
    old_path, new_name = data.get("path", ""), (data.get("new_name") or "").strip()
    if not old_path or not new_name:
        return jsonify({"error": "缺少参数"}), 400
    p = Path(old_path)
    # 安全校验：路径必须在音乐目录内
    try:
        p.resolve().relative_to(MUSIC_ROOT.resolve())
    except ValueError:
        return jsonify({"error": "路径越界"}), 400
    if not p.is_file():
        return jsonify({"error": "文件不存在"}), 404
    # 新文件名校验：不允许路径分隔符，扩展名必须是音频格式（缺省则保留原扩展名）
    if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        return jsonify({"error": "文件名不能包含路径分隔符"}), 400
    if Path(new_name).suffix.lower() not in AUDIO_EXTS:
        new_name += p.suffix  # 自动补原扩展名
    target = p.with_name(new_name)
    if target == p:
        return jsonify({"ok": True, "path": str(p), "unchanged": True})
    if target.exists():
        return jsonify({"error": f"目标已存在: {new_name}"}), 409
    try:
        p.rename(target)
    except OSError as e:
        return jsonify({"error": f"重命名失败: {e}"}), 500

    # 同步更新内存中的报告与存档，免去重新扫描
    old, new = str(p), str(target)
    res = state.get("result")
    if res:
        for grp in res.get("sim", []) + res.get("hash", []):
            for f in grp.get("files", []):
                if f.get("path") == old:
                    f["path"] = new
                    if f.get("ext") is not None:
                        f["ext"] = target.suffix.lower()
        try:
            archive_save(res, res.get("threshold", 0.85))
        except Exception:
            pass
    return jsonify({"ok": True, "path": new})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8321)
