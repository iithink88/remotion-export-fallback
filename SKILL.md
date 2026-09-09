---
name: remotion-export-fallback
description: 导出 Remotion 视频为 mp4。当 npx remotion render 因浏览器调试端口被拦截而失败时（Chrome DevTools / 安全软件 / 防火墙），用逐帧截图 + ffmpeg 的降级方案完成导出。触发词：导出视频、渲染 mp4、remotion render 失败、DevTools 超时、ws 连接超时、ETIMEDOUT。
version: 1.0.0
agent_created: true
---

# Remotion 视频导出（含降级方案）

把 Remotion 合成导出成 mp4。优先走官方标准渲染，失败时自动切换到**不依赖 Chrome DevTools** 的降级方案。

## 决策流程

```
要导出 mp4
   │
   ├─ 1. 先试标准命令
   │     npx remotion render <CompositionId> out.mp4
   │     ├─ 成功 → 完成
   │     └─ 失败/卡住（连接浏览器超时、ws://127.0.0.1 ETIMEDOUT）
   │
   ├─ 2. 跑诊断确认原因
   │     python scripts/diagnose_devtools.py
   │     ├─ 返回 0（标准渲染可用）→ 回到第 1 步，排查别的原因
   │     └─ 返回 1（调试通道被拦）→ 第 3 步
   │
   └─ 3. 降级导出
         python scripts/export_fallback.py --project <项目根> --composition <合成ID> --out <mp4>
```

**不要一上来就降级**——标准渲染快得多（150 帧约 1 分钟 vs 8 分钟）。只在确认被拦截时才用。

## 1. 标准渲染

```bash
npx remotion render <CompositionId> out.mp4
```

若本机有多套浏览器或 Remotion 自带的 Chrome Headless Shell 下载不动，可指定本机浏览器：

```bash
npx remotion render <CompositionId> out.mp4 --browser-executable="C:/Program Files/Google/Chrome/Application/chrome.exe"
```

> 路径带空格时要加引号；若仍报路径解析错误，改用 `remotion.config.ts` 里 `Config.setBrowserExecutable(...)`。

## 2. 诊断

```bash
python scripts/diagnose_devtools.py
```

依次测试三件事，并给出结论：

| 检查项 | 含义 |
|---|---|
| DevTools 调试端口 | Remotion 驱动浏览器用的 TCP 通道 |
| DevTools 管道模式 | 不经网络的备用通道 |
| 静态截图能力 | 降级方案依赖的能力 |

输出示例（被拦时）：

```
[1/3] 测试 DevTools 调试端口 ...
      端口 9333 连接超时（浏览器在跑但不 accept）
[2/3] 测试 DevTools 管道模式 ...
      管道模式无响应
[3/3] 测试静态截图能力 ...
      静态截图可用
结论: 浏览器调试通道被拦截，但静态截图可用 → 使用降级方案
```

## 3. 降级导出

```bash
python scripts/export_fallback.py \
  --project "E:/path/to/remotion-project" \
  --composition MyComposition \
  --out "C:/Users/me/Desktop/out.mp4"
```

**最简用法**（在项目根目录、只有一个合成时）：

```bash
python scripts/export_fallback.py --project . --out out.mp4
```

### 它内部做了什么

1. 解析 `src/Root.tsx`，自动取出该合成的 `component / durationInFrames / fps / width / height / defaultProps`，并把 Root.tsx 的 import 一起带上（所以 `zColor()` 这类辅助函数不会报错）
2. 生成帧查看页 `src/__rm_frame.tsx`（`@remotion/player` 的 `initialFrame={N}` 定位帧）
3. esbuild 打包成单个 `app.js`（2 秒）
4. Chrome `--screenshot` 并行抓帧（默认 4 路）
5. ffmpeg 合成 H.264 mp4
6. 清理临时文件

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--project` | 当前目录 | Remotion 项目根 |
| `--composition` | Root.tsx 第一个 | 合成 ID |
| `--out` | `<project>/out/<ID>.mp4` | 输出文件 |
| `--props` | — | JSON 文件，覆盖 defaultProps |
| `--workers` | 4 | 并行截图数；慢机器/内存小改 2 |
| `--retries` | 2 | 失败帧的重试轮数（每轮降并发、延超时） |
| `--timeout` | 60 | 单帧超时(秒) |
| `--max-frames` | 全部 | 只渲前 N 帧，用于快速验证 |
| `--start-frame` | 0 | 起始帧 |
| `--crf` | 18 | ffmpeg 质量，越小越清晰越大 |
| `--chrome` | 自动探测 | 浏览器路径 |
| `--budget` | 5000 | `--virtual-time-budget`(ms)；画面没渲染完就调大 |
| `--keep-frames` | 关 | 保留中间 PNG 帧 |

### 改数据再导出

在 Remotion Studio 的 Props 面板里改参数，Studio 会把改动**写回 `src/Root.tsx` 的 defaultProps**。脚本直接读这个文件，所以改完重跑即可，不需要额外传参。

想临时覆盖而不改代码，准备一个 JSON：

```json
{ "targetNumber": 8888888, "unit": "次" }
```

```bash
python scripts/export_fallback.py --project . --composition NumberCounter \
  --out out.mp4 --props props.json
```

## 前置条件

| 依赖 | 检查 | 缺失时 |
|---|---|---|
| Python 3.9+ | `python --version` | 装 Python |
| Chrome 或 Edge | 脚本自动探测常见路径 | 用 `--chrome` 指定 |
| esbuild | 项目 `node_modules` 里已有 | `npm i` |
| ffmpeg | PATH 里有，或 `imageio-ffmpeg` | 脚本会用清华源自动装 imageio-ffmpeg |

## 常见问题

**画面全黑 / 数字停在 0**
`--virtual-time-budget` 太小，页面还没渲染完就截图了。调大：`--budget 9000`。

**截图大量 TIMEOUT**
并行数太高。降到 `--workers 2`，并把 `--timeout` 提到 90。
脚本自带 2 轮重试（降并发 + 超时 ×1.5）；重试用尽仍失败的帧会**复用最近的成功帧补齐**，
保证时长精确，代价是那 1-2 帧画面静止。日志里会打印「补齐 N 帧」。

**esbuild 报 `Expected identifier but found "{"`**
Root.tsx 的 `defaultProps` 写法特殊（比如用了展开运算符或三元）。用 `--props my.json` 手动给 props 绕过解析。

**画面尺寸不对**
脚本用 `--window-size=<合成宽>x<合成高>` + `--force-device-scale-factor=1`。若系统缩放不是 100%，确认 scale factor 生效。

**想保留中间帧做二次处理**
加 `--keep-frames`，帧会留在 `%TEMP%/rmframes-<合成ID>/out/`。

## 已知限制

- 无音频（只合视频轨）。需要音频的话用标准渲染，或用 ffmpeg 另加 `-i audio.mp3 -shortest`
- 透明通道（ProRes 4444）不支持，只能出 H.264
- 比标准渲染慢约 8 倍

完整排查记录见 [references/troubleshooting.md](./references/troubleshooting.md)。
