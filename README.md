# music-dedup

音乐文件去重 Web 工具（Docker 部署，适配树莓派 / NAS）。

## 功能

- **相似文件名分组**：归一化文件名后按相似度聚类，组内按音质排序（无损优先，有损按码率），标记建议保留/删除
- **哈希去重**：文件大小相同再算 MD5，完全重复的文件仅标记，由用户勾选判断
- **Web 界面**：浏览器扫描、查看报告、勾选删除
- **安全删除**：删除的文件先移入音乐目录下 `.trash` 回收目录，确认无误后手动清空

## 使用

```bash
# 方式一：docker compose（修改 volumes 中的音乐目录路径）
docker compose up -d --build

# 方式二：直接使用 CI 构建的镜像
docker run -d --name music-dedup \
  -p 8321:8321 \
  -v /你的音乐目录:/music \
  ghcr.io/zeng-qh/music-dedup:latest
```

浏览器访问 `http://<主机IP>:8321`。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSIC_ROOT` | `/music` | 容器内音乐目录 |

## CI

Push 到 main 分支或打 `v*` tag 时，GitHub Actions 自动构建 linux/amd64 + linux/arm64 双架构镜像并推送到 GHCR。
