# 直播源自动构建器（GitHub Actions 版）

每天自动拉取公开直播源 → 两级体检（含分片深检）→ 按频道择优 → 生成 `live.m3u` → 提交回仓库。
客户端用 jsDelivr 直链订阅，无需服务器、无需开电脑、零成本。

---

## 一、5 分钟部署

### 1. 建仓库
在 GitHub 新建一个 **Public** 仓库（私有仓库 jsDelivr 读不了），例如 `live-tv`。

### 2. 上传文件
把本目录下所有文件推到仓库根目录，结构必须是：

```
live-tv/
├── .github/
│   └── workflows/
│       └── update.yml
├── build_playlist.py
├── requirements.txt
└── README.md
```

### 3. 开启 Actions 写入权限
仓库 → Settings → Actions → General → **Workflow permissions** →
选 **Read and write permissions** → Save

> 不设这一步，定时任务能跑但无法提交结果。

### 4. 手动跑一次
仓库 → Actions → 左侧「自动更新直播源」→ Run workflow → Run。
约 3~5 分钟，看到 `live.m3u` 出现在仓库根目录就成功了。

### 5. 拿到订阅地址

```
https://cdn.jsdelivr.net/gh/你的用户名/仓库名@main/live.m3u
```

填进你的 APP / TVBox / 播放器即可。jsDelivr 有全球 CDN 缓存，国内访问稳定。

---

## 二、默认行为

| 项 | 值 |
|---|---|
| 运行频率 | 每 6 小时（UTC `0 */6 * * *`） |
| 数据来源 | vbskycn/iptv、iptv-org/cn（均带 3 级镜像降级） |
| 频道范围 | 央视 + 卫视 + 主要省市台（见脚本 `CHANNEL_KEYWORDS`） |
| 频道归并 | 同一频道不论清晰度（600P/720P/1080P/4K）只出一条，绝不按清晰度拆台 |
| 输出 | `live.m3u`（每台一条）、`sources.json`（每台多源池）、`report.csv`（体检明细）、`stats.json` |


### 三个输出文件怎么用

| 文件 | 结构 | 用途 |
|---|---|---|
| `live.m3u` | 每台一条（取当前最优源） | 直接订阅，播放器里干净不重复 |
| `sources.json` | `[{name, category, urls:[{url,status,latency_ms,source}]}]` | 自家 APP 读它做多源故障切换 |
| `report.csv` | 每一条候选的体检明细 | 排查哪个源挂了 |

> `sources.json` 里同一个频道会挂多条线路（已按延迟排序），
> 你的 APP 播放失败时依次换下一条即可，体验等同「一个台里多个源」。

---

## 三、常用调整

### 只保留央视 + 卫视（去掉地方台）
编辑 `build_playlist.py` 的 `CHANNEL_KEYWORDS`，改成：

```python
CHANNEL_KEYWORDS = ["CCTV", "CGTN", "CETV", "卫视", "凤凰"]
```

### 想全部频道都要
```python
CHANNEL_KEYWORDS = None
```

### 改频率
`.github/workflows/update.yml` 里的 cron：

```yaml
- cron: '0 */3 * * *'    # 每 3 小时
- cron: '0 22 * * *'     # 每天北京时间 6:00（UTC 22:00）
```

> 注意：GitHub 定时任务在高峰期可能延迟 10~30 分钟，属正常现象。

### 加自己的源
在 `SOURCES` 里追加一项：

```python
{
    "name": "我的源",
    "mode": "m3u",          # 或 "txt"
    "mirrors": ["https://你的地址/live.m3u"],
},
```

---

## 四、源是怎么被校验、怎么被剔除的

`build_playlist.py` 对每条源做**两级体检**，任何一级不过就踢出列表：

| 级别 | 检查内容 | 判死条件 | 处理 |
|---|---|---|---|
| 一级（粗检） | GET 地址，读首包 512 字节 | 404 / 连不上 / 内容不是 m3u8、TS、FLV | 判 `dead`，直接剔除 |
| 二级（深检，默认开） | 顺 m3u8 追到**真实分片**，试拉 1KB | 子列表 404、分片 404 | 判 `dead` 剔除 |
| | | 子列表/分片 401、403 | 判 `geo`（地域限制），降为备用 |

**为什么必须有二级**：社区源里最多的不是"完全打不开"，而是**假活源**——
播放列表 HTTP 200 正常返回，一播就转圈，因为子列表或分片早就 404 了。
只看首包的粗检抓不到，深检能抓。实测一轮 151 条 HLS 源里揪出 10 条。

- 嫌慢可关：`python build_playlist.py --no-deep`
- 深检结果写进 `report.csv`「详情」列：`分片OK` / `分片 404` / `子列表 HTTP 403`
- `stats.json` 的 `deep_checked`（深检条数）、`deep_removed`（剔除条数）可直接看

被剔除的源不会出现在 `live.m3u`，也不会进 `sources.json` 源池——
所以客户端拿到的列表里不存在"点了播不出来"的死链（残留的只有构建后新失效的）。

---

### 央视白名单与源类型降权（已内置）

- **央视严格白名单**：只保留 `CCTV-1 ~ CCTV-17`、`CCTV-5+`、`CCTV-4K/8K`、`CGTN` 系。
  社区列表里的伪台（`CCTV Billiards`、`CCTV怀旧剧场`、`CCTV Storm Football` 等）
  会直接丢弃，绝不进列表。
- **IPTV 网关型源降权**：`xxx:9901/tsfile/...`、`key=txiptv`、私网 IP 这类
  运营商 IPTV 网关源，海外机房测速很漂亮但普通家宽连不稳。
  源池排序改为：可用 > 公网型 > IPTV 型 > 低延迟，M3U 主选尽量用公网源。
- **`--public-only`**：想更狠可以加这个参数，IPTV 网关型源直接剔除
  （列表更干净，但频道会少一截，比如 CCTV-4 只剩网关源时会被挤掉）。

## 五、⚠️ 重要：测速结果的偏差

GitHub 的服务器在**海外**，而大量国内 IPTV 源按 IP 地域限制访问。
本脚本已针对这点做了处理：

- `--region overseas`（Actions 里默认启用）：遇到 `401/403` 不判死，标记为
  `geo`（地域限制）保留为备用线路
- `--region cn`：在**国内机器**上跑时用这个，403 直接判死，结果最准

**结论**：云端跑出来的列表"可用性"是保守估计（国内实际能播的会更多），
但**延迟数据不代表国内体验**。想要精准的国内优选，可以：

1. 用国内 VPS 跑同样的脚本（`--region cn`），把结果提交到仓库
2. 在自己的 APP 里做最终测速（播放前探测，几百毫秒），这一步最准

---

## 六、本地运行（可选）

```bash
pip install -r requirements.txt
python build_playlist.py --region cn      # 国内环境
python build_playlist.py --region overseas # 模拟海外
```

也可以打包成 EXE：`pip install pyinstaller && pyinstaller -F build_playlist.py`

---

## 七、说明

- 所有源均来自公开社区（vbskycn/iptv、iptv-org），本项目只做聚合、体检、择优
- 源失效是常态，脚本的价值就是「自动帮你筛掉死的」
- 请勿用于商业分发；直播内容版权归各电视台所有
