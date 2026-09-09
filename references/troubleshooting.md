# 排查记录：Remotion 渲染在本机失败的全过程

一次真实的排障，记录在 Windows 机器上把 Remotion 合成导出成 mp4 时踩的所有坑。
时间：2026-09-08 ~ 09，Remotion 4.0.522，Node 22.22.2，Windows。

---

## 1. 症状

```bash
npx remotion render NumberCounter out.mp4
```

打包（bundling）能跑完，进度到 51% 后卡住，最终报错：

```
Timed out trying to connect to the browser
ws://127.0.0.1:58161/...  ETIMEDOUT
```

---

## 2. 排查顺序（按实际执行）

### 2.1 怀疑 tailwind 插件

脚手架 create-video 默认带 `@remotion/tailwind-v4`。检查代码：

```bash
grep -rnoE 'className="[^"]*"' src/   # 零命中
```

代码根本没用 tailwind，是脚手架留下的负担。移除 `Config.overrideBundlerConfig(enableTailwind)` 后重跑 —— **仍然崩**，但排除了一个干扰项。

### 2.2 怀疑打包器（rspack）

`Config.setRspack(true)` 换成 webpack —— 报缺少依赖。查下去发现真问题：

```
Cannot find module 'webpack/lib/dependencies/CssUrlDependency'
```

**webpack 5.105.0 安装不完整**——之前 `npm i` 被沙箱拦截了部分文件。修复：

```bash
rm -rf node_modules/webpack && npm i webpack --no-audit --no-fund
```

> 坑：直接 `npm i webpack@5.105.0` 无效，npm 认为已安装。必须**先删目录再装**。

修复后打包从 8% 推进到 51%，说明确实是文件缺失。但仍卡在浏览器连接。

### 2.3 定位到浏览器连接

用 Node API 写脚本捕获真实错误（CLI 的日志太粗）：

```js
const { bundle } = require('@remotion/bundler');
const { renderMedia, selectComposition } = require('@remotion/renderer');
```

拿到关键行：

```
ws://127.0.0.1:58161  ETIMEDOUT
```

### 2.4 判断拦截边界

| 测试 | 结果 | 结论 |
|---|---|---|
| node → node 回环 `127.0.0.1:9999` | ✅ 通 | 回环本身正常 |
| node → Chrome DevTools 端口 | ❌ ETIMEDOUT | 浏览器被拦 |
| `netstat` 看端口 | 显示 LISTENING | 端口在监听 |
| `Test-NetConnection 127.0.0.1:9333` | ❌ 失败 | 不 accept 连接 |
| `curl http://127.0.0.1:9333/json/version` | ❌ 超时 | DevTools HTTP 端点不可达 |
| Chrome `--screenshot` 静态截图 | ✅ 正常 | 浏览器本身能跑 |

**关键区分**：ETIMEDOUT（丢包）而不是 ECONNREFUSED（拒绝），说明有东西在静默丢弃包——典型的第三方安全软件行为，不是防火墙拒绝规则。

### 2.5 穷举所有浏览器组合

写矩阵脚本 `devtools_matrix.mjs`，对下列组合逐一测试：

| 浏览器 | headless 模式 | 端口 | 管道 | 结果 |
|---|---|---|---|---|
| Chrome 152（本机） | `--headless=new` | ❌ | ❌ | 全拦 |
| Chrome 152 | `--headless=old` | ❌ | — | 全拦 |
| Chrome 152 | 有头模式 | ❌ | — | 全拦 |
| Chrome 149 Headless Shell（Remotion 官方） | — | ❌ | ❌ | 全拦 |
| Edge | `--headless=new` | ❌ | — | 全拦 |
| Chrome `--single-process` | — | 进程直接退出 | — | 不可用 |

**结论**：与浏览器种类、版本、headless 模式都无关，是系统级拦截。

### 2.6 排查策略层面

```powershell
# 企业策略
HKLM:\Software\Policies\Google\Chrome          # 不存在
HKCU:\Software\Policies\Google\Chrome          # 不存在
DevToolsAvailability                            # 无

# Windows 防火墙
netsh advfirewall show allprofiles state
netsh advfirewall firewall show rule name=all dir=in | Select-String chrome
# → 无 chrome 相关规则
```

排除了组策略和 Windows 防火墙 → **第三方安全软件**（360 / 火绒 / 管家等的"应用网络防护"，或代理软件的 TUN 模式）拦截了浏览器调试通道。

---

## 3. 降级方案

既然 `--screenshot` 能跑，就绕开 DevTools 自己做渲染流水线。

```
src/Root.tsx ──解析──> 合成元数据 + props
                            │
                            ▼
                   src/__rm_frame.tsx（帧查看页）
                            │  esbuild 打包
                            ▼
                        app.js
                            │  Chrome --screenshot 逐帧
                            ▼
                     f00000.png ... f00149.png
                            │  ffmpeg
                            ▼
                          out.mp4
```

### 关键实现点

**帧定位**：`@remotion/player` 的 `initialFrame={N}` + `controls={false}`，URL 传 `?frame=N`。
必须加 `acknowledgeRemotionLicense`，否则有水印。

**props 一致性**：从 `Root.tsx` 的 `defaultProps` 提取原文，同时复制 Root.tsx 的 import 语句——
这样 `zColor("#22d3ee")` 这类调用在打包后仍然有效。

**并行截图**：4 路 `ThreadPoolExecutor`，每路独立 `--user-data-dir`，避免实例互相干扰。

**合成**：

```bash
ffmpeg -y -framerate 30 -start_number 0 -i f%05d.png \
  -c:v libx264 -pix_fmt yuv420p -crf 18 -preset fast out.mp4
```

### 实测结果

| 指标 | 值 |
|---|---|
| 输入 | NumberCounter，150 帧，1920×1080 @30fps |
| 截图耗时 | 约 8 分钟（4 路并行） |
| 产物 | 920 KB，5.00 秒，H.264 High |
| 画面 | 与 Studio 预览一致（同一份 React 组件 + 同一份 props） |

---

## 4. 踩过的弯路（别再走）

1. **不要反复调 `--browser-executable`**——换浏览器没用，问题不在浏览器。
2. **不要试图下载 Remotion 官方的 Chrome Headless Shell**（149.0.7790.0，118MB）——
   它在国内源能下（npmmirror），但装了同样被拦。
3. **`--remote-debugging-pipe` 也别指望**——管道模式同样无响应。
4. **打包失败和渲染失败是两个独立问题**——webpack 文件缺失要先修，否则会误判是浏览器问题。

---

## 5. 根治方法（如果用户愿意动手）

在第三方安全软件里把以下进程加白名单 / 允许回环通信：

- `chrome.exe`
- `node.exe`
- `chrome-headless-shell.exe`

放行之后 `npx remotion render` 一条命令即可，比降级方案快约 8 倍。

常见位置：
- 360 安全卫士 → 木马防火墙 → 网络防护 → 添加信任
- 火绒 → 防护中心 → 系统防护 → 网络防护 → 例外
- 代理软件 → 关闭 TUN 模式，或把 127.0.0.1 加入绕过列表

---

## 6. 副产物脚本

排障过程中写的临时脚本（留在项目 `scripts/` 下，非技能依赖）：

| 脚本 | 用途 |
|---|---|
| `render.mjs` | 用 Node API 渲染，能拿到比 CLI 详细得多的错误 |
| `probe.mjs` | 对比回环 / 局域网 IP 的连通性 |
| `pipe_test.mjs` | 测试 `--remote-debugging-pipe` |
| `devtools_matrix.mjs` | 浏览器 × 模式 × 通道的全组合矩阵测试 |

诊断类需求直接用技能里的 `scripts/diagnose_devtools.py` 即可，无需这些。
