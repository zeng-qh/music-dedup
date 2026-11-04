#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则引擎自测：不依赖网络与浏览器，直接调用 app 内部函数

用法：  python3 tests/test_rules.py
需要 ffmpeg 生成测试音频（已装在容器/CI 环境）；无 ffmpeg 时自动跳过相关用例。
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print(f"  {mark}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def make_flac(path, dur=20, sr=44100, ch=1):
    """构造合法 FLAC 头：只有 STREAMINFO，无实际音频数据，仅供解析测试"""
    total_samples = int(dur * sr)
    si = bytearray(34)
    si[0:2] = (4096).to_bytes(2, "big")      # min block size
    si[2:4] = (4096).to_bytes(2, "big")      # max block size
    # sample rate 20bit | channels-1 3bit | bps-1 5bit | total 36bit
    v = (sr << 44) | ((ch - 1) << 41) | ((16 - 1) << 36) | total_samples
    si[10:18] = v.to_bytes(8, "big")
    hdr = b"fLaC" + bytes([0x80 | 0, 0, 0, 34])   # last-block STREAMINFO
    path.write_bytes(hdr + bytes(si) + b"\x00" * 100)


def ffmpeg_gen(path, dur=20, br="320k", fmt="mp3"):
    codec = {"mp3": "libmp3lame", "flac": "flac"}[fmt]
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={dur}",
           "-af", "aformat=channel_layouts=stereo"]
    if fmt == "mp3":
        cmd += ["-c:a", codec, "-b:a", br]
    else:
        cmd += ["-c:a", codec]
    cmd += [str(path), "-y"]
    subprocess.run(cmd, check=True)


def run_scan(root, **opts):
    app.MUSIC_ROOT = Path(root)
    o = dict(app.DEFAULT_OPTIONS)
    o.update(opts)
    return app.do_scan(o)


# ---------------------------------------------------------------- 元数据解析
def test_probe(tmp):
    print("\n[元数据探测]")
    fl = tmp / "a.flac"
    make_flac(fl, dur=30, sr=44100, ch=2)
    info = app.probe_audio(fl)
    check("FLAC 采样率", info["sample_rate"] == 44100, info)
    check("FLAC 声道", info["channels"] == 2, info)
    check("FLAC 位深", info["bits"] == 16, info)
    check("FLAC 时长", abs((info["duration"] or 0) - 30) < 0.05, info)

    if has_ffmpeg():
        mp3 = tmp / "b.mp3"
        ffmpeg_gen(mp3, dur=12, br="192k")
        m = app.probe_audio(mp3)
        check("MP3 码率", m["bitrate"] == 192, m)
        check("MP3 时长", abs((m["duration"] or 0) - 12) < 1.0, m)
        check("MP3 声道", m["channels"] == 2, m)

    bogus = tmp / "c.mp3"
    bogus.write_bytes(b"not audio at all")
    check("损坏文件不抛异常", app.probe_audio(bogus)["bitrate"] is None)


# ---------------------------------------------------------------- 相似度算法
def test_similarity():
    print("\n[相似度算法]")
    a, b = "周杰伦 晴天", "晴天 周杰伦"
    check("sequence 对词序敏感", app.similarity(a, b, "sequence") < 0.7,
          app.similarity(a, b, "sequence"))
    check("token 对词序不敏感", app.similarity(a, b, "token") == 1.0,
          app.similarity(a, b, "token"))
    check("hybrid 取较高分", app.similarity(a, b, "hybrid") == 1.0)

    check("不同歌曲相似度低",
          app.similarity("周杰伦 晴天", "林俊杰 江南", "hybrid") < 0.5)
    check("完全相同返回 1", app.similarity("abc", "abc", "sequence") == 1.0)


# ---------------------------------------------------------------- 核心词匹配
def test_core_match():
    print("\n[核心词匹配]")
    cases = [
        ("周杰伦 晴天 加长版", "周杰伦 晴天", True),
        ("陈奕迅 富士山下 重制版", "陈奕迅 富士山下", True),
        ("五月天 突然好想你 伴奏版", "五月天 突然好想你", True),
        ("周杰伦 晴天", "林俊杰 江南", False),
        ("五月天 突然好想你", "五月天 突然好想你 live", False),
    ]
    for a, b, want in cases:
        got = app.core_match(a, b)
        check(f"core_match({a!r}, {b!r})", got == want, f"got={got} want={want}")


# ---------------------------------------------------------------- 命名清洗
def test_clean_title():
    print("\n[命名清洗]")
    o = dict(app.DEFAULT_OPTIONS)
    cases = [
        ("周杰伦 - 晴天 320k_网易云音乐.mp3", "周杰伦 - 晴天"),
        ("周杰伦 - 晴天 320kbps.mp3", "周杰伦 - 晴天"),
        ("林俊杰_江南_【高音质】.mp3", "林俊杰 江南"),
        ("Adele - Hello [FLAC].flac", "Adele - Hello"),
        ("周杰伦-晴天QQ音乐.mp3", "周杰伦 - 晴天"),
    ]
    for src, want in cases:
        got = app.clean_title(src, o)
        check(f"clean({src!r})", got == want, f"got={got!r} want={want!r}")
    # 不能误伤普通词
    check("不误伤 hqx", app.clean_title("普通歌名 hqx.mp3", o) == "普通歌名 hqx",
          app.clean_title("普通歌名 hqx.mp3", o))


# ---------------------------------------------------------------- 评分优先级
def test_qscore():
    print("\n[评分优先级]")
    fl = Path("/tmp/_qscore.flac")
    mp3 = Path("/tmp/_qscore.mp3")
    for p in (fl, mp3):
        p.write_bytes(b"x" * 1000)
    o = dict(app.DEFAULT_OPTIONS)
    # 无损优先开启：即使有损码率更高
    s_fl = app.qscore(fl, {"bitrate": 300}, o)
    s_mp3 = app.qscore(mp3, {"bitrate": 320}, o)
    check("无损优先 > 高码率有损", s_fl > s_mp3, f"{s_fl:.0f} vs {s_mp3:.0f}")
    # 关闭后按码率
    o2 = dict(o, prefer_lossless=False)
    check("关闭无损优先后按码率提升",
          app.qscore(fl, {"bitrate": 300}, o2) < app.qscore(mp3, {"bitrate": 320}, o2))
    # live 策略方向
    live = Path("/tmp/_qscore live.flac")
    studio = Path("/tmp/_qscore studio.flac")
    for p in (live, studio):
        p.write_bytes(b"x" * 1000)
    check("avoid: 专辑版胜出",
          app.qscore(studio, {"bitrate": 900}, dict(o, live_policy="avoid")) >
          app.qscore(live, {"bitrate": 900}, dict(o, live_policy="avoid")))
    check("prefer: 现场版胜出",
          app.qscore(live, {"bitrate": 900}, dict(o, live_policy="prefer")) >
          app.qscore(studio, {"bitrate": 900}, dict(o, live_policy="prefer")))
    check("quality: 同音质时并列",
          app.qscore(live, {"bitrate": 900}, dict(o, live_policy="quality")) ==
          app.qscore(studio, {"bitrate": 900}, dict(o, live_policy="quality")))
    for p in (fl, mp3, live, studio):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 端到端场景
def test_scenarios(tmp):
    print("\n[端到端场景]")
    if not has_ffmpeg():
        print("  SKIP  （缺少 ffmpeg）")
        return
    d = tmp / "lib"
    d.mkdir()
    (d / "sub").mkdir()
    # 同名不同格式 + 不同码率 + 现场加长版
    ffmpeg_gen(d / "晴天.flac", dur=30, fmt="flac")
    ffmpeg_gen(d / "晴天 320k.mp3", dur=30, br="320k")
    ffmpeg_gen(d / "晴天 128k.mp3", dur=30, br="128k")
    ffmpeg_gen(d / "晴天 现场加长版.mp3", dur=180, br="320k")
    ffmpeg_gen(d / "sub" / "江南.mp3", dur=40, br="320k")
    ffmpeg_gen(d / "sub" / "江南 旧版.mp3", dur=40, br="64k")

    r = run_scan(d)
    sim = [g for g in r["sim"] if g["kind"] == "sim"]
    names = [g["name"] for g in sim]
    check("按核心词归组", any("晴天" in n for n in names), names)
    qing = [g for g in sim if "晴天" in g["name"]]
    check("短版本与长版本被识别", any(g["dur_warn"] for g in qing),
          [(g["name"], g["dur_gap"], len(g["files"])) for g in qing])
    check("长版本确实并入了同一组",
          any(g["dur_gap"] and g["dur_gap"] > 100 for g in qing),
          [(g["name"], g["dur_gap"]) for g in qing])

    # split 策略应把长版本拆开
    r2 = run_scan(d, dur_penalty="split")
    sim2 = [g for g in r2["sim"] if g["kind"] == "sim" and "晴天" in g["name"]]
    check("split 拆分出独立版本", len(sim2) >= 2, [(g["name"], len(g["files"])) for g in sim2])
    check("拆出的独立版本默认保留",
          all(f["keep"] for g in sim2 if g.get("standalone") for f in g["files"]))

    # 关闭核心词匹配后，长版本不再并组
    r3 = run_scan(d, core_match=False)
    sim3 = [g for g in r3["sim"] if g["kind"] == "sim" and "晴天" in g["name"]]
    check("关闭核心词后长版本独立", all(len(g["files"]) <= 4 for g in sim3),
          [(g["name"], len(g["files"])) for g in sim3])

    # 低码率：把江南组拆掉看孤立文件
    r4 = run_scan(d, low_bitrate=160, core_match=False)
    lows = [g for g in r4["sim"] if g["kind"] == "low"]
    check("低码率分组出现", len(lows) >= 1, [len(g["files"]) for g in lows])

    # 重命名建议：只在保留项名字还不够规范时才有建议
    ffmpeg_gen(d / "sub" / "海阔天空_网易云音乐_320k.mp3", dur=50, br="320k")
    r5 = run_scan(d, norm_name=True)
    plans = r5.get("rename_plans", [])
    check("生成重命名建议", len(plans) >= 1, len(plans))
    check("建议目标是干净名字",
          all("320k" not in p["new_name"] and "128k" not in p["new_name"]
              and "网易云" not in p["new_name"] for p in plans),
          [p["new_name"] for p in plans])
    check("已规范的文件不产生建议",
          all(p["old_name"] != "晴天.flac" for p in plans),
          [p["old_name"] for p in plans])
    # 关闭开关后不应有建议
    r6 = run_scan(d, norm_name=False)
    check("关闭规范重命名后无建议", not r6.get("rename_plans"), r6.get("rename_plans"))


# ---------------------------------------------------------------- 安全性
def test_security(tmp):
    print("\n[安全与健壮性]")
    # 越界路径
    app.MUSIC_ROOT = Path(tmp) / "music"
    app.MUSIC_ROOT.mkdir(exist_ok=True)
    outside = Path(tmp) / "outside.mp3"
    outside.write_bytes(b"x")
    c = app.app.test_client()
    r = c.post("/api/delete", json={"paths": [str(outside)]})
    check("删除越界路径被拒", r.get_json()["failed"], r.get_json())
    r = c.post("/api/rename", json={"path": str(outside), "new_name": "x.mp3"})
    check("重命名越界路径被拒", r.status_code == 400, r.status_code)
    r = c.post("/api/rename_batch", json={"items": [{"from": str(outside), "new_name": "y.mp3"}]})
    check("批量重命名越界被拒", r.get_json()["failed_count"] == 1, r.get_json())

    # 非法文件名
    inside = app.MUSIC_ROOT / "ok.mp3"
    inside.write_bytes(b"x")
    r = c.post("/api/rename", json={"path": str(inside), "new_name": "../evil.mp3"})
    check("路径分隔符被拒", r.status_code == 400, r.status_code)

    # 参数类型容错
    r = c.post("/api/scan", json={"options": {"threshold": "abc", "dur_tolerance": None,
                                              "low_bitrate": "xx", "match_algo": "nope"}})
    check("非法参数不 500", r.status_code == 200, r.status_code)


# ---------------------------------------------------------------- 配置校验
def test_options_api():
    print("\n[配置接口]")
    c = app.app.test_client()
    d = c.get("/api/options").get_json()
    check("schema 数量", len(d["schema"]) >= 18, len(d["schema"]))
    check("defaults 与 schema 对齐",
          set(d["defaults"]).issubset({x["key"] for x in d["schema"]}),
          set(d["defaults"]) - {x["key"] for x in d["schema"]})
    for k in ("live_policies", "match_algos", "dur_penalties", "path_biases"):
        check(f"下发 {k}", isinstance(d.get(k), dict) and d[k], d.get(k))
    # 每个 select 项都要有对应选项表
    enums = {"live_policy": "live_policies", "match_algo": "match_algos",
             "dur_penalty": "dur_penalties", "path_bias": "path_biases"}
    for item in d["schema"]:
        if item["type"] == "select":
            tbl = enums.get(item["key"])
            check(f"select {item['key']} 有选项", tbl and d.get(tbl), tbl)
            check(f"select {item['key']} 默认值合法",
                  d["defaults"][item["key"]] in d[tbl], d["defaults"][item["key"]])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mdedup_test_"))
    try:
        test_probe(tmp)
        test_similarity()
        test_core_match()
        test_clean_title()
        test_qscore()
        test_scenarios(tmp)
        test_security(tmp)
        test_options_api()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*54}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败用例：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
