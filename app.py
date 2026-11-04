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
    # ---- 新增规则 ----
    "match_algo": "sequence",  # 相似度算法
    "cross_ext": True,         # 跨格式匹配（.flac / .wav 视为同一首）
    "core_match": True,        # 核心词匹配（差异仅在版本修饰时也归组）
    "dur_check": True,         # 时长一致性校验
    "dur_tolerance": 3.0,      # 时长容差（秒）
    "dur_penalty": "flag",     # 时长差异大时的处理方式
    "low_bitrate": 0,          # 低码率清理阈值（0 = 关闭）
    "prefer_lossless": True,   # 无损优先于有损
    "path_bias": "short",      # 路径偏好（决定多份等价时保留哪份）
    "norm_name": False,        # 扫描后按规范重命名
}

LIVE_POLICIES = {
    "avoid": "避开现场版：专辑/录音室版永远优先，即使 live 版本音质更高。适合追求原版听感",
    "quality": "只看音质：不区分录音类型，纯按无损格式与码率排序。适合不在意版本来源",
    "prefer": "优先现场版：live 版本排在最前。适合偏好现场氛围与即兴演绎",
}

# 相似度算法：不同算法对「词序不同 / 拼写差异」的容忍度不一样
MATCH_ALGOS = {
    "sequence": "字符序列（默认）：逐字符比较，对拼写差异敏感，对词序变化不敏感",
    "token": "词集合：先按空格/标点切词再比对，"
             "能识别「晴天 周杰伦」与「周杰伦 - 晴天」。适合中英文混排、命名不统一的曲库",
    "hybrid": "混合：取两者的较高分，最宽松，也最容易把不同歌归成一组",
}

# 时长差异较大时的处理策略
DUR_PENALTIES = {
    "flag": "仅提示：仍然归为一组，但在界面标注「时长差异」供你人工判断（推荐）",
    "split": "拆成两组：视为不同版本，各自独立成组，互不判重",
    "deprioritize": "降权保留：仍归为一组，但时长吻合的那份优先保留",
}

# 路径偏好：同一首歌有多个等价副本时，优先保留路径符合哪种习惯的文件
PATH_BIASES = {
    "short": "路径最短：层级最浅的优先保留，适合以「主库」为主的目录结构",
    "long": "路径最长：层级最深的优先，适合把整理好的归档放在深层目录",
    "mtime": "最近修改：修改时间最新的优先（通常是刚整理过的那份）",
    "none": "不区分：完全按音质决定，不看存放位置",
}

# 噪声词表按类别组织，便于按开关组合
NOISE_BITRATE = [
    # 码率标记：320k / 320kbps / 320 kbps
    r"(?i)(?<![a-z0-9])(320|256|192|128|64)\s*k(bps)?(?![a-z])",
    # 格式标记：前后不能紧跟字母，避免误伤 hqx 之类的普通词
    r"(?i)(?<![a-z0-9])(flac|mp3|wav|ape|aac|m4a|ogg|wma|hq|sq|cbr|vbr)(?![a-z])",
    r"(无损|無損|高音质|高音質|高品质|高品質|母带|母帶|hires|Hi-Res)",
]
NOISE_SOURCE = [
    # 中文平台名没有词边界概念，直接用完整平台名匹配（长的排前面，避免短名先吃掉）
    r"(网易云音乐|网易云|QQ音乐|qq音乐|酷狗音乐|酷我音乐|咪咕音乐|虾米音乐|"
    r"百度音乐|千千音乐|多米音乐|酷狗|酷我|咪咕|虾米|汽水音乐)",
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
    """兼容旧接口"""
    return probe_audio(path).get("bitrate")


# MPEG 帧头码率表（Layer III，kbps）
_MP3_BR = {1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96,
           8: 112, 9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320}
# 采样率表：version 1 / 2 / 2.5
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000],
           0: [11025, 12000, 8000]}


def probe_audio(path):
    """读取音频元数据：码率、时长、采样率、声道、位深

    纯标准库实现，不依赖 mutagen/ffprobe，容器内可直接跑。
    读失败时返回字段为 None 的字典，不影响扫描流程。
    """
    info = {"bitrate": None, "duration": None, "sample_rate": None,
            "channels": None, "bits": None}
    try:
        size = path.stat().st_size
        ext = path.suffix.lower()
        with open(path, "rb") as f:
            head = f.read(65536)

        if ext in (".flac", ".alac") or head[:4] == b"fLaC":
            if head[:4] != b"fLaC":
                return info
            import struct
            # 遍历元数据块，找到 STREAMINFO（type 0）
            pos = 4
            while pos + 4 <= len(head):
                hdr = head[pos]
                btype = hdr & 0x7F
                last = hdr & 0x80
                blen = int.from_bytes(head[pos + 1:pos + 4], "big")
                if btype == 0:
                    si = head[pos + 4:pos + 4 + 34]
                    if len(si) < 34:
                        return info
                    sr = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
                    ch = ((si[12] >> 1) & 0x07) + 1
                    bps = (((si[12] & 0x01) << 4) | (si[13] >> 4)) + 1
                    total = ((si[13] & 0x0F) << 32) | \
                        struct.unpack(">I", si[14:18])[0]
                    info.update(sample_rate=sr, channels=ch, bits=bps)
                    if sr and total:
                        dur = total / sr
                        info["duration"] = round(dur, 3)
                        info["bitrate"] = int(size * 8 / dur / 1000)
                    return info
                if last:
                    break
                pos += 4 + blen
            return info

        if ext == ".mp3" or head[:3] == b"ID3" or head[:2] == b"\xff\xfb" \
                or head[:2] == b"\xff\xf3" or head[:2] == b"\xff\xfa":
            # 跳过 ID3v2
            start = 0
            if head[:3] == b"ID3" and len(head) > 10:
                sz = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                start = 10 + sz
            # 找第一个有效的帧头
            for i in range(start, min(len(head) - 4, start + 200000)):
                if head[i] != 0xFF or (head[i + 1] & 0xE0) != 0xE0:
                    continue
                ver = (head[i + 1] >> 3) & 3      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
                layer = (head[i + 1] >> 1) & 3
                bri = (head[i + 2] >> 4) & 0x0F
                sri = (head[i + 2] >> 2) & 0x03
                if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
                    continue
                br = _MP3_BR.get(bri)
                sr = _MP3_SR.get(ver, [44100])[sri]
                info["bitrate"] = br
                info["sample_rate"] = sr
                info["channels"] = 1 if ((head[i + 3] >> 6) & 3) == 3 else 2
                if br and sr:
                    info["duration"] = round(size * 8 / (br * 1000), 2)
                return info
            return info

        if ext == ".wav" and head[:4] == b"RIFF":
            import struct
            pos = 12
            byte_rate = 0
            ch = bps = sr = 0
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                clen = struct.unpack("<I", head[pos + 4:pos + 8])[0]
                if cid == b"fmt ":
                    fmt = head[pos + 8:pos + 8 + 16]
                    if len(fmt) >= 16:
                        ch, sr = struct.unpack("<H", fmt[2:4])[0], \
                            struct.unpack("<I", fmt[4:8])[0]
                        byte_rate = struct.unpack("<I", fmt[8:12])[0]
                        bps = struct.unpack("<H", fmt[14:16])[0]
                elif cid == b"data":
                    info.update(sample_rate=sr, channels=ch or None, bits=bps or None)
                    if byte_rate:
                        dur = clen / byte_rate
                        info["duration"] = round(dur, 3)
                        info["bitrate"] = int(byte_rate * 8 / 1000)
                    return info
                pos += 8 + clen + (clen & 1)
            return info
    except Exception:
        pass
    return info


def _tokens(s):
    """把归一化串切成词元：按空格和常见分隔符拆分"""
    return [t for t in re.split(r"[\s\-_,、/&+]+", s) if t]


def core_name(s):
    """取出名字里的「核心词」集合：去掉版本修饰后剩下的部分

    用于解决「晴天」与「晴天 加长版」这类漏判：
    相似度 0.75 达不到阈值，但核心词完全一致，实际是同一首歌的不同版本。
    """
    toks = _tokens(s)
    # 单字修饰词与常见版本后缀视为修饰成分
    mods = {"版", "加长", "完整", "典藏", "珍藏", "特别", "纪念", "新版", "旧版",
            "原版", "正式", "官方", "高清", "修复", "重制", "重置", "伴奏", "纯音乐",
            "试听", "片段", "剪辑", "串烧", "dj", "dj版", "慢摇", "钢琴", "吉他"}
    core = [t for t in toks if t not in mods and not t.endswith("版")]
    return set(core) if core else set(toks)


def core_match(a, b, min_core=1):
    """核心去修饰后完全相同 -> 判定为同一首歌

    min_core=1 是必要的：中文曲库常见「晴天」「江南」这类单词歌名，
    若要求至少 2 个核心词，版本修饰识别对它们完全失效。
    只有 1 个核心词时仍要求它足够长（>=2 字符），避免「版」「b」这类噪声词误合并。
    """
    ca, cb = core_name(a), core_name(b)
    if cb != ca:
        return False
    if len(ca) >= 2:
        return True
    # 单核心词：要求该词本身有辨识度
    return bool(ca) and len(next(iter(ca))) >= 2


def similarity(a, b, algo="sequence"):
    """按算法计算两个归一化名字的相似度（0~1）

    出现分叉：sequence 只看字符顺序，token 只看词是否重合。
    中英文混排、歌手名与歌名顺序颠倒时，token 更准。
    """
    if a == b:
        return 1.0
    if algo == "token":
        ta, tb = set(_tokens(a)), set(_tokens(b))
        if not ta or not tb:
            return 0.0
        inter = len(ta & tb)
        # 用 Dice 系数：对长度差异不敏感，适合词数不同的情况
        return 2 * inter / (len(ta) + len(tb))
    seq = SequenceMatcher(None, a, b).ratio()
    if algo == "hybrid":
        ta, tb = set(_tokens(a)), set(_tokens(b))
        tok = (2 * len(ta & tb) / (len(ta) + len(tb))) if ta and tb else 0.0
        return max(seq, tok)
    return seq


def duration_gap(files):
    """一个组内最长与最短时长的差值（秒），任一文件缺时长则返回 None"""
    ds = [f.get("duration") for f in files if f.get("duration")]
    if len(ds) < 2:
        return None
    return max(ds) - min(ds)


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


def qscore(path, info, opts):
    """综合评分，决定组内保留谁

    优先级从高到低：
      1. 录音类型（由 live_policy 决定方向）
      2. 无损优先（prefer_lossless 可关闭）
      3. 码率
      4. 文件大小（微调）
      5. 路径偏好（path_bias，仅在音质完全相同时起作用）
    """
    br = info.get("bitrate") if isinstance(info, dict) else info
    live_policy = opts.get("live_policy", "avoid")
    is_lv = is_live(path.name)

    if live_policy == "quality":
        tier = 0
    elif live_policy == "prefer":
        tier = 3 if is_lv else 2
    else:  # avoid
        tier = 1 if is_lv else 3
    s = tier * 10 ** 9

    # 无损优先：位阶高于码率，避免「1411kbps WAV」压过「1000kbps 真正的 FLAC」
    lossless = path.suffix.lower() in LOSSLESS
    if lossless:
        s += 10 ** 8 if opts.get("prefer_lossless", True) else 0

    # 码率：整数位，最高 1536kbps 也不会溢出到无损位阶
    s += float(br or 0) * 10 ** 5

    # 文件大小与时间：文件可能在扫描期间被外部删除（NAS/同步盘常见），
    # 取不到就跳过这两项，不影响其余排序依据
    try:
        st = path.stat()
        s += min(st.st_size / (1024 * 1024), 900) * 10 ** 2
        mtime = st.st_mtime
    except OSError:
        mtime = 0

    # 路径偏好：纯小数位，只在以上全部相同时才决定胜负
    bias_mode = opts.get("path_bias", "short")
    if bias_mode == "short":
        s += (1000 - min(len(path.parts), 999)) * 0.01
    elif bias_mode == "long":
        s += min(len(path.parts), 999) * 0.01
    elif bias_mode == "mtime":
        s += (mtime % 100000) * 0.001
    return s




def clean_title(name, opts):
    """从文件名生成规范名：去掉码率/平台/版本噪声，整理分隔符，保留「歌手 - 标题」结构"""
    stem = os.path.splitext(name)[0]
    s = stem
    # 逐类去除噪声（复用归一化词表，但不去大小写、不合并空白）
    if opts.get("strip_bitrate", True):
        for p in NOISE_BITRATE:
            s = re.sub(p, " ", s)
    if opts.get("strip_source", True):
        for p in NOISE_SOURCE:
            s = re.sub(p, " ", s)
    # 版本标记只在用户开启时去除；保留其中文可读性（如「现场版」）
    if opts.get("strip_version", True):
        for p in NOISE_VERSION:
            s = re.sub(p, " ", s)
    # 清理空括号与残留分隔符
    s = re.sub(r"[\(\（\[【]\s*[\)\）\]】]", " ", s)
    s = re.sub(r"[_\.]+", " ", s)
    s = re.sub(r"\s*-\s*", " - ", s)      # 统一成「歌手 - 标题」
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    return s or stem


def preview_rename(groups, opts):
    """扫描后生成重命名建议：只处理保留下来的文件，跳过已经合规的"""
    if not opts.get("norm_name"):
        return []
    plans = []
    for g in groups:
        for f in g.get("files", []):
            if not f.get("keep"):
                continue
            p = Path(f["path"])
            new_stem = clean_title(p.name, opts)
            new_name = new_stem + p.suffix.lower()
            if new_name == p.name:
                continue
            target = p.with_name(new_name)
            plans.append({
                "from": str(p), "old_name": p.name, "new_name": new_name,
                "conflict": target.exists() and target != p,
                "group": g.get("name", ""),
            })
    return plans

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
    algo = opts.get("match_algo", "sequence")
    files = [p for p in MUSIC_ROOT.rglob("*")
             if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    state["progress"] = f"扫描到 {len(files)} 个音频文件，正在分组…"

    # 归一化名字：跨格式匹配关闭时，把扩展名重新拼回名字，避免 flac/wav 被归一成同名
    def key_of(p):
        k = norm(p.name, opts)
        if not opts.get("cross_ext", True):
            k = f"{k} {p.suffix.lower().lstrip('.')}"
        return k

    # ---------- 相似名聚类：先粗筛再精算，避免 O(n²) 全量比对 ----------
    nmap = {p: key_of(p) for p in files}
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

    # 只对「长度相近 + 首字符相同」的候选对做精细比对
    keys = [k for k in by_norm if k]
    n = len(keys)
    # token / hybrid 算法下首字符预筛会漏掉词序打乱的情况，自动放宽
    loose = bool(opts.get("loose_match")) or algo in ("token", "hybrid")
    for i, k1 in enumerate(keys):
        if CANCEL.is_set():
            raise ScanCancelled()
        if i % 50 == 0:
            state["progress"] = f"相似度比较 {i}/{n}"
        l1 = len(k1)
        for j in range(i + 1, n):
            k2 = keys[j]
            # 核心词一致（差异仅在「加长版/重制版」等修饰）时直接合并，
            # 这一步必须先于长度预筛：版本修饰会让名字明显变长
            if opts.get("core_match", True) and core_match(k1, k2):
                for a in by_norm[k1]:
                    for b in by_norm[k2]:
                        union(a, b)
                continue
            # 长度差异过大直接跳过（点集算法不依赖长度差，单独判断）
            if algo == "sequence" and abs(l1 - len(k2)) > l1 * (1 - threshold) + 2:
                continue
            if not loose and k1[0] != k2[0]:
                continue
            if similarity(k1, k2, algo) >= threshold:
                for a in by_norm[k1]:
                    for b in by_norm[k2]:
                        union(a, b)

    groups = defaultdict(list)
    for p in files:
        groups[find(p)].append(p)

    # ---------- 探测元数据（每组共用，避免重复 IO）----------
    meta_cache = {}

    def meta(p):
        if p not in meta_cache:
            meta_cache[p] = probe_audio(p)
        return meta_cache[p]

    def file_obj(p, keep, **extra):
        m = meta(p)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0          # 扫描期间被外部删除/移动，按 0 计，避免中断整次扫描
        d = {"path": str(p), "ext": p.suffix.lower(),
             "bitrate": m.get("bitrate"), "size": size,
             "duration": m.get("duration"), "sample_rate": m.get("sample_rate"),
             "channels": m.get("channels"), "bits": m.get("bits"),
             "live": is_live(p.name),
             "name": p.name,
             "size_h": human(size), "keep": keep}
        d.update(extra)
        return d

    dur_check = opts.get("dur_check", True)
    tol = float(opts.get("dur_tolerance", 3.0))
    penalty = opts.get("dur_penalty", "flag")

    sim = []
    total = len(groups)
    for idx, g in enumerate(groups.values()):
        if len(g) < 2:
            continue
        if CANCEL.is_set():
            raise ScanCancelled()
        if idx % 50 == 0:
            state["progress"] = f"整理相似分组 {idx}/{total}"

        # 时长差异过大 -> 按时长再切一刀（split 策略下互不判重）
        gap = duration_gap([meta(p) for p in g]) if dur_check else None
        big_gap = gap is not None and gap > tol

        if big_gap and penalty == "split":
            # 按时长聚类：差值在容差内的归为一簇
            buckets = []
            for p in sorted(g, key=lambda x: meta(x).get("duration") or 0):
                d = meta(p).get("duration")
                placed = False
                if d is not None:
                    for b in buckets:
                        bd = [meta(q).get("duration") for q in b
                              if meta(q).get("duration")]
                        if bd and abs(d - sum(bd) / len(bd)) <= tol:
                            b.append(p)
                            placed = True
                            break
                if not placed:
                    buckets.append([p])
            # 单文件簇保留为独立条目，避免文件从视图中「消失」
            sub_groups = buckets
        else:
            sub_groups = [g]

        for sg in sub_groups:
            ordered = sorted(sg, key=lambda x: -qscore(x, meta(x), opts))
            gap2 = duration_gap([meta(p) for p in sg]) if dur_check else None
            big2 = gap2 is not None and gap2 > tol
            # 单文件簇是「被时长校验拆出来的独立版本」，标记出来避免误删
            standalone = len(sg) == 1
            # 组名取组内最短的归一化名：反映「这首歌本身」而非某个变体，
            # 也避免 prefer 模式下组名带出「加长版」这类修饰
            gname = min((nmap[p] for p in sg), key=len) or ordered[0].stem
            sim.append({
                "name": gname,
                "kind": "sim",
                "dur_gap": round(gap2, 2) if gap2 is not None else None,
                "dur_warn": bool(big2),
                "standalone": standalone,
                "files": [file_obj(p, True if standalone else i == 0)
                          for i, p in enumerate(ordered)],
            })

    # ---------- 低码率清理：只列「孤立」的低质文件 ----------
    # 已进入判重组的低码率文件不重复列出：那组里通常有更好的副本，
    # 用户在该组内勾选即可，重复出现只会让计数虚高、操作混乱。
    low = []
    low_th = float(opts.get("low_bitrate") or 0)
    if low_th > 0:
        state["progress"] = "筛查低码率文件…"
        in_dup = {p for g in groups.values() if len(g) > 1 for p in g}
        for p in files:
            if CANCEL.is_set():
                raise ScanCancelled()
            if p.suffix.lower() in LOSSLESS:
                continue          # 无损格式没有「低码率」概念
            if p in in_dup:
                continue          # 交给判重组处理，避免重复计数
            br = meta(p).get("bitrate")
            if br is None or br >= low_th:
                continue
            low.append(file_obj(p, False, reason=f"码率 {br}kbps 低于 {int(low_th)}kbps"))
    if low:
        sim.append({
            "name": f"低码率文件（< {int(low_th)}kbps）",
            "kind": "low", "dur_gap": None, "dur_warn": False,
            "files": low,
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
                    gs = sorted(g, key=lambda p: -qscore(p, meta(p), opts))
                    hash_dups.append({
                        "md5": h[:12], "kind": "hash",
                        "name": gs[0].stem, "dur_gap": None, "dur_warn": False,
                        "files": [file_obj(p, i == 0, md5=h[:12])
                                  for i, p in enumerate(gs)]
                    })
    plans = []
    if opts.get("norm_name"):
        state["progress"] = "生成重命名建议…"
        # 覆盖全部音频文件：不只判重组，孤立文件同样值得规范命名
        keep_paths = {p for p in files if p.suffix.lower() in AUDIO_EXTS}
        for g in sim + hash_dups:
            for f in g.get("files", []):
                if not f.get("keep"):
                    keep_paths.discard(Path(f["path"]))
        fake_groups = [{"name": "", "files": [{"path": str(p), "keep": True}
                                              for p in sorted(keep_paths)]}]
        plans = preview_rename(fake_groups, opts)
    return {"sim": sim, "hash": hash_dups, "total_files": len(files),
            "root": str(MUSIC_ROOT), "finished_at": time.time(),
            "rename_plans": plans, "options": opts}


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
    # 各下拉项允许的取值
    ENUMS = {
        "live_policy": LIVE_POLICIES,
        "match_algo": MATCH_ALGOS,
        "dur_penalty": DUR_PENALTIES,
        "path_bias": PATH_BIASES,
    }
    for k, default in DEFAULT_OPTIONS.items():
        if k not in src:
            continue
        v = src[k]
        if isinstance(default, bool):
            opts[k] = bool(v)
        elif isinstance(default, float):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if k == "threshold":
                opts[k] = min(0.98, max(0.30, fv))
            elif k == "dur_tolerance":
                opts[k] = min(120.0, max(0.5, fv))
            else:
                opts[k] = fv
        elif isinstance(default, int):
            try:
                opts[k] = min(400, max(0, int(v)))
            except (TypeError, ValueError):
                pass
        else:
            if k in ENUMS and v in ENUMS[k]:
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
        "match_algos": MATCH_ALGOS,
        "dur_penalties": DUR_PENALTIES,
        "path_biases": PATH_BIASES,
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
            {
                "key": "match_algo", "type": "select", "label": "相似度算法", "group": "匹配",
                "desc": "决定「两个文件名有多像」怎么算。换算法能解决不同命名习惯造成的漏判。",
            },
            {
                "key": "cross_ext", "type": "switch", "label": "跨格式匹配", "group": "匹配",
                "desc": "让「晴天.flac」与「晴天.wav」参与同一组比对。关闭后同名的无损与有损版本"
                        "会各自成组、互不判重——除非你确实想把不同格式分开存放。",
            },
            {
                "key": "core_match", "type": "switch", "label": "识别版本修饰", "group": "匹配",
                "desc": "把「晴天」「晴天 加长版」「晴天 重制版」视为同一首歌。"
                        "这类名字单纯靠相似度算不够（约 0.75，达不到阈值），"
                        "但去掉版本修饰后核心词完全一致。关闭后将严格按字面相似度判断。",
            },
            {
                "key": "dur_check", "type": "switch", "label": "时长一致性校验", "group": "匹配",
                "desc": "读取音频真实时长，比较组内文件是否长度相近。"
                        "同名但时长差很多的往往是不同录音（如 3 分钟单曲 vs 7 分钟现场加长版），"
                        "靠文件名分不出来，只能靠时长。",
            },
            {
                "key": "dur_tolerance", "type": "range-fine", "label": "时长容差（秒）", "group": "匹配",
                "min": 1, "max": 60, "step": 1,
                "desc": "组内最长与最短时长相差多少秒以内算「同一版本」。"
                        "同一首歌的不同压制通常差 1~2 秒（片头静音），现场版则常差几十秒。",
            },
            {
                "key": "dur_penalty", "type": "select", "label": "时长差异处理", "group": "匹配",
                "desc": "检测到组内时长差异超过容差时，希望工具怎么处理这组。",
            },
            {
                "key": "prefer_lossless", "type": "switch", "label": "无损优先", "group": "保留",
                "desc": "同等条件下优先保留 FLAC/WAV/APE 等无损格式，而不是码率更高的有损文件。"
                        "关闭后纯按码率排（此时 1411kbps 的 WAV 同样会赢）。",
            },
            {
                "key": "path_bias", "type": "select", "label": "等价时保留哪份", "group": "保留",
                "desc": "当多个文件的音质完全相同时，用存放位置决定保留谁。",
            },
            {
                "key": "low_bitrate", "type": "range-fine", "label": "低码率清理阈值", "group": "清理",
                "min": 0, "max": 320, "step": 16,
                "desc": "把码率低于此值的有损文件单独列成一组，方便你清理老旧的劣质资源。"
                        "设为 0 关闭。只统计没有重复的文件——有重复的交给判重组处理。",
            },
            {
                "key": "norm_name", "type": "switch", "label": "按规范重命名", "group": "清理",
                "desc": "扫描后对保留下来的文件统一改名为「歌手 - 标题」格式，去掉码率、平台后缀等噪声。"
                        "改名前的原名会记录在结果里，可随时核对。",
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


@app.route("/api/rename_batch", methods=["POST"])
def api_rename_batch():
    """批量执行重命名建议：只处理传入的路径，逐条校验后改名

    任何一条失败都不影响其他条目，返回逐条结果。
    改名后同步刷新内存报告与存档，避免用户需要重新扫描。
    """
    items = (request.json or {}).get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "items 必须是列表"}), 400
    done, failed = [], []
    res = state.get("result")
    for it in items:
        old = (it or {}).get("from") or (it or {}).get("path")
        new_name = (it or {}).get("new_name") or ""
        if not old or not new_name:
            failed.append({"from": old, "err": "参数不完整"})
            continue
        p = Path(old)
        try:
            p.resolve().relative_to(MUSIC_ROOT.resolve())
        except (ValueError, OSError):
            failed.append({"from": old, "err": "路径越界"})
            continue
        if not p.is_file():
            failed.append({"from": old, "err": "文件不存在"})
            continue
        name = new_name.strip()
        if "/" in name or "\\" in name or name in (".", ".."):
            failed.append({"from": old, "err": "文件名非法"})
            continue
        if Path(name).suffix.lower() not in AUDIO_EXTS:
            name += p.suffix
        target = p.with_name(name)
        if target == p:
            continue
        if target.exists():
            failed.append({"from": old, "err": f"目标已存在: {name}"})
            continue
        try:
            p.rename(target)
            done.append({"from": old, "to": str(target), "name": name})
        except OSError as e:
            failed.append({"from": old, "err": str(e)})
            continue
        # 同步内存报告
        if res:
            for grp in res.get("sim", []) + res.get("hash", []):
                for f in grp.get("files", []):
                    if f.get("path") == str(p):
                        f["path"] = str(target)
                        f["name"] = name
            res["rename_plans"] = [x for x in res.get("rename_plans", [])
                                   if x.get("from") != old]
    if res and done:
        try:
            archive_save(res, res.get("threshold", 0.85))
        except Exception:
            pass
    return jsonify({"done": done, "failed": failed,
                    "count": len(done), "failed_count": len(failed)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8321)
