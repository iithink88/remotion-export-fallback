#!/usr/bin/env python3
"""
Remotion 降级导出：逐帧截图 + ffmpeg 合成 mp4。

适用场景：本机安全软件/防火墙拦截了 Chrome DevTools 端口，
导致 `npx remotion render` 卡在连接浏览器阶段（ws://127.0.0.1:<port> ETIMEDOUT）。
本方案不依赖 DevTools，改用 Chrome 的 `--screenshot` 静态截图逐帧抓取，
再用 ffmpeg 合成，画面与 Studio 渲染一致（同一份 React 组件 + 同一份 props）。

工作原理：
  1. 解析 src/Root.tsx，自动提取指定合成的 component / durationInFrames / fps /
     width / height / defaultProps（含 Root.tsx 的 import，保证 zColor() 等可用）
  2. 生成一个帧查看页 frame.tsx（用 @remotion/player 的 initialFrame 定位帧）
  3. esbuild 打包成单个 app.js
  4. Chrome --screenshot 并行抓取 N 帧 PNG
  5. ffmpeg 合成为 H.264 mp4

用法：
  python export_fallback.py --project <Remotion项目根> --composition <合成ID> \
      --out <输出.mp4> [--workers 4] [--max-frames 30] [--props props.json]

常用参数：
  --project         Remotion 项目根目录（默认当前目录）
  --composition     合成 ID（默认取 Root.tsx 里注册的第一个）
  --out             输出 mp4 路径（默认 <project>/out/<合成ID>.mp4）
  --workers         并行截图进程数（默认 4，慢机器改 2）
  --max-frames      只渲染前 N 帧（冒烟测试用）
  --start-frame     起始帧（默认 0）
  --props           用 JSON 覆盖 defaultProps
  --chrome          指定浏览器路径（默认自动探测）
  --crf             ffmpeg 质量（默认 18，越小越清晰）
  --keep-frames     保留中间 PNG 帧（默认合成后删除）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_lock = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 浏览器 / ffmpeg


def find_chrome(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def find_esbuild(project: str) -> str | None:
    """优先用 @esbuild 平台原生 exe——.cmd 包装器会把 stderr 吞掉，报错看不到。"""
    import glob

    natives = glob.glob(os.path.join(project, "node_modules", "@esbuild", "*", "bin", "esbuild.exe"))
    natives += glob.glob(os.path.join(project, "node_modules", "@esbuild", "*", "esbuild.exe"))
    if natives:
        return natives[0]
    for rel in (
        os.path.join("node_modules", ".bin", "esbuild.cmd"),
        os.path.join("node_modules", ".bin", "esbuild"),
        os.path.join("node_modules", "esbuild", "bin", "esbuild"),
    ):
        p = os.path.join(project, rel)
        if os.path.exists(p):
            return p
    return shutil.which("esbuild")


def find_ffmpeg() -> str | None:
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_ffmpeg() -> str:
    ff = find_ffmpeg()
    if ff:
        return ff
    log("  未找到 ffmpeg，尝试用清华源安装 imageio-ffmpeg ...")
    py = sys.executable
    subprocess.run(
        [py, "-m", "pip", "install", "imageio-ffmpeg",
         "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "--proxy", "", "--quiet"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"无法获取 ffmpeg: {exc}\n请手动安装 ffmpeg 并加入 PATH。")


# ---------------------------------------------------------------- 解析 Root.tsx


def _match_brace(text: str, start: int) -> int:
    """给定 '{' 的下标，返回配对的 '}' 下标。"""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        elif ch in "\"'`":
            quote = ch
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                i += 1
        i += 1
    return -1


def parse_root(root_path: str, comp_id: str | None) -> dict:
    """从 Root.tsx 解析某个 <Composition> 的元数据与 defaultProps。"""
    src = open(root_path, encoding="utf-8-sig").read()

    # 定位合成 id
    if comp_id:
        m = re.search(r"id\s*=\s*[\"']" + re.escape(comp_id) + r"[\"']", src)
        if not m:
            raise SystemExit(f"在 {root_path} 里找不到合成 id={comp_id}")
    else:
        m = re.search(r"id\s*=\s*[\"']([^\"']+)[\"']", src)
        if not m:
            raise SystemExit(f"在 {root_path} 里找不到任何 <Composition id=...>")
        comp_id = m.group(1)

    start = src.rfind("<Composition", 0, m.start())
    if start == -1:
        raise SystemExit("无法定位 <Composition 标签起点")
    end = src.find("/>", m.end())
    block = src[start: end + 2] if end != -1 else src[start:]

    def num(key: str, default: int) -> int:
        mm = re.search(key + r"\s*=\s*\{?\s*(\d+)\s*\}?", block)
        return int(mm.group(1)) if mm else default

    comp = re.search(r"component\s*=\s*\{?\s*([A-Za-z_$][\w$]*)", block)
    component = comp.group(1) if comp else None

    # defaultProps={{ ... }} → 取内层对象字面量原文
    # 也可能是 defaultProps={someVar} 这种变量引用，两种都要能处理
    props_src = "{}"
    dp = re.search(r"defaultProps\s*=\s*\{", block)
    if dp:
        nxt = re.match(r"\s*\{", block[dp.end():])
        if nxt:
            # 情形 A：defaultProps={{ ... }} → 取内层那一对花括号
            open_brace = dp.end() + nxt.end() - 1
            close = _match_brace(block, open_brace)
            if close != -1:
                props_src = block[open_brace: close + 1]
        else:
            # 情形 B：defaultProps={someVar} → 直接引用变量
            close = _match_brace(block, dp.end() - 1)
            inner = block[dp.end(): close].strip() if close != -1 else ""
            props_src = inner if inner else "{}"

    # 复制 Root.tsx 的 import 语句，保证 zColor() 等辅助函数可用
    imports = re.findall(r"import\s[\s\S]*?from\s*[\"'][^\"']+[\"'];?", src)
    imports += re.findall(r"import\s*[\"'][^\"']+[\"'];?", src)

    return {
        "id": comp_id,
        "component": component,
        "durationInFrames": num("durationInFrames", 150),
        "fps": num("fps", 30),
        "width": num("width", 1920),
        "height": num("height", 1080),
        "props_src": props_src,
        "imports": "\n".join(dict.fromkeys(imports)),
    }


# ---------------------------------------------------------------- 生成帧页面


FRAME_TSX = """{imports}
import React from "react";
import {{ createRoot }} from "react-dom/client";
import {{ Player }} from "@remotion/player";

const qs = new URLSearchParams(window.location.search);
const initialFrame = Number(qs.get("frame") ?? 0);

const inputProps = {props_src};

const App: React.FC = () => (
  <Player
    component={{{component}}}
    inputProps={{inputProps}}
    durationInFrames={{{duration}}}
    fps={{{fps}}}
    compositionWidth={{{width}}}
    compositionHeight={{{height}}}
    initialFrame={{initialFrame}}
    controls={{false}}
    loop={{false}}
    clickToPlay={{false}}
    doubleClickToFullscreen={{false}}
    spaceKeyToPlayOrPause={{false}}
    acknowledgeRemotionLicense
    style={{{{ width: {width}, height: {height} }}}}
  />
);

createRoot(document.getElementById("root")!).render(<App />);
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>{title}</title>
    <style>
      html, body {{
        margin: 0;
        padding: 0;
        width: {width}px;
        height: {height}px;
        overflow: hidden;
        background: #000;
      }}
      #root {{ width: {width}px; height: {height}px; }}
    </style>
  </head>
  <body>
    <div id="root"></div>
    <script src="app.js"></script>
  </body>
</html>
"""


# ---------------------------------------------------------------- 截图 / 合成


def capture(chrome: str, url: str, out: str, width: int, height: int,
            budget: int, profile: str, timeout: int) -> tuple[str, int]:
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--window-size={width},{height}",
        f"--virtual-time-budget={budget}",
        f"--user-data-dir={profile}",
        f"--screenshot={out}",
        url,
    ]
    try:
        subprocess.run(args, check=True, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = os.path.exists(out) and os.path.getsize(out) > 0
        return ("OK" if ok else "EMPTY"), 0
    except subprocess.TimeoutExpired:
        return "TIMEOUT", 0
    except subprocess.CalledProcessError as exc:
        return f"ERR_{exc.returncode}", 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".", help="Remotion 项目根目录")
    ap.add_argument("--composition", default=None, help="合成 ID")
    ap.add_argument("--out", default=None, help="输出 mp4 路径")
    ap.add_argument("--props", default=None, help="JSON 文件，覆盖 defaultProps")
    ap.add_argument("--root-file", default=None, help="Root.tsx 路径（默认 src/Root.tsx）")
    ap.add_argument("--start-frame", type=int, default=0)
    ap.add_argument("--max-frames", type=int, default=None, help="只渲染前 N 帧（冒烟用）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--budget", type=int, default=5000, help="virtual-time-budget(ms)")
    ap.add_argument("--timeout", type=int, default=60, help="单帧超时(秒)")
    ap.add_argument("--retries", type=int, default=2, help="失败帧重试轮数（默认 2）")
    ap.add_argument("--keep-frames", action="store_true", help="保留中间 PNG")
    a = ap.parse_args()

    project = os.path.abspath(a.project)
    if not os.path.isdir(project):
        raise SystemExit(f"项目目录不存在: {project}")

    # 1. 解析合成
    root_file = a.root_file or os.path.join(project, "src", "Root.tsx")
    if not os.path.exists(root_file):
        cands = [os.path.join(project, "src", "Root.tsx"),
                 os.path.join(project, "src", "index.tsx"),
                 os.path.join(project, "src", "index.ts")]
        root_file = next((c for c in cands if os.path.exists(c)), "")
    if not root_file:
        raise SystemExit("找不到 Root.tsx / index.tsx")

    meta = parse_root(root_file, a.composition)
    props_src = meta["props_src"]
    if a.props:
        props_src = json.dumps(json.load(open(a.props, encoding="utf-8-sig")),
                               ensure_ascii=False, indent=2)

    total = meta["durationInFrames"]
    frames = list(range(a.start_frame, total))
    if a.max_frames:
        frames = frames[: a.max_frames]
    if not frames:
        raise SystemExit("没有需要渲染的帧")

    out_mp4 = a.out or os.path.join(project, "out", f"{meta['id']}.mp4")
    os.makedirs(os.path.dirname(os.path.abspath(out_mp4)), exist_ok=True)

    log("=" * 60)
    log(f"合成: {meta['id']}  组件: {meta['component']}")
    log(f"规格: {meta['width']}x{meta['height']} @ {meta['fps']}fps，共 {len(frames)} 帧")
    log(f"输出: {out_mp4}")
    log("=" * 60)

    # 2. 准备浏览器 / esbuild / ffmpeg
    chrome = find_chrome(a.chrome)
    if not chrome:
        raise SystemExit("未找到 Chrome/Edge，请用 --chrome 指定路径")
    esbuild = find_esbuild(project)
    if not esbuild:
        raise SystemExit("未找到 esbuild，请在项目里执行 npm i 后重试")
    ffmpeg = ensure_ffmpeg()
    log(f"浏览器: {chrome}")
    log(f"esbuild: {esbuild}")
    log(f"ffmpeg : {ffmpeg}")

    # 3. 生成工作目录与帧页面
    work = os.path.join(os.environ.get("TEMP", "."), f"rmframes-{meta['id']}")
    out_dir = os.path.join(work, "out")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    if not meta["component"]:
        raise SystemExit("未能从 Root.tsx 解析出 component，请检查合成注册写法")

    tsx = FRAME_TSX.format(
        imports=meta["imports"],
        props_src=props_src,
        component=meta["component"],
        duration=meta["durationInFrames"],
        fps=meta["fps"],
        width=meta["width"],
        height=meta["height"],
    )
    tsx_path = os.path.join(project, "src", "__rm_frame.tsx")
    with open(tsx_path, "w", encoding="utf-8") as f:
        f.write(tsx)

    html_path = os.path.join(work, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(INDEX_HTML.format(title=meta["id"], width=meta["width"], height=meta["height"]))

    js_path = os.path.join(work, "app.js")
    log("\n[1/3] esbuild 打包帧页面 ...")
    cmd = [esbuild, tsx_path, "--bundle", f"--outfile={js_path}",
           "--jsx=automatic", "--loader:.css=css",
           '--define:process.env.NODE_ENV="production"', "--log-level=warning"]
    r = subprocess.run(cmd, cwd=project, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(js_path):
        log(r.stdout or "")
        log(r.stderr or "")
        raise SystemExit("esbuild 打包失败")
    log(f"      OK  app.js {os.path.getsize(js_path) // 1024} KB")

    url_base = "file:///" + html_path.replace("\\", "/")

    # 4. 并行截图
    log(f"\n[2/3] 逐帧截图 {len(frames)} 帧（workers={a.workers}）...")
    t0 = time.time()
    fails: list[tuple[int, str]] = []

    def worker(frame: int, timeout: int) -> tuple[int, str]:
        out = os.path.join(out_dir, f"f{frame:05d}.png")
        # 用线程 id 做 profile 目录名：同一线程串行复用，不同线程互不冲突。
        # 早期版本按 i % workers 分配，会让两个并发实例抢同一个 user-data-dir
        # 而随机 TIMEOUT（实测 150 帧里挂掉末尾 4 帧）。
        profile = os.path.join(work, f"profile-{threading.get_ident()}")
        st, _ = capture(chrome, f"{url_base}?frame={frame}", out,
                        meta["width"], meta["height"], a.budget, profile, timeout)
        return frame, st

    ok_frames: set[int] = set()
    fails: list[tuple[int, str]] = []

    for round_no in range(a.retries + 1):
        pending = [f for f in frames if f not in ok_frames]
        if not pending:
            break
        if round_no > 0:
            log(f"      重试第 {round_no} 轮：{len(pending)} 帧未成功 ...")
            time.sleep(2)
        workers = a.workers if round_no == 0 else max(1, a.workers // 2)
        timeout = a.timeout if round_no == 0 else int(a.timeout * 1.5)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(worker, f, timeout): f for f in pending}
            for fut in as_completed(futs):
                frame, st = fut.result()
                with _lock:
                    if st == "OK":
                        ok_frames.add(frame)
                    else:
                        fails.append((frame, st))
                    done = len(ok_frames) + sum(
                        1 for f, _ in fails if f not in ok_frames)
                    if done % 10 == 0 or done == len(frames):
                        log(f"      进度 {done}/{len(frames)}  "
                            f"成功 {len(ok_frames)}  用时 {int(time.time() - t0)}s")

    # 仍未成功的帧：用最近的成功帧补齐，保证时长正确（画面静止 1-2 帧，几乎看不出）
    missing = [f for f in frames if f not in ok_frames]
    if missing:
        if not ok_frames:
            raise SystemExit("所有帧截图均失败，无法合成")
        log(f"      补齐 {len(missing)} 帧（复用最近成功帧）: {missing[:8]}")
        for f in missing:
            nearest = max((k for k in ok_frames if k <= f), default=min(ok_frames))
            src_png = os.path.join(out_dir, f"f{nearest:05d}.png")
            dst_png = os.path.join(out_dir, f"f{f:05d}.png")
            if os.path.exists(src_png):
                shutil.copyfile(src_png, dst_png)

    ok_n = len(frames)
    log(f"      截图完成: 成功 {len(ok_frames)}/{len(frames)}"
        + (f"，补齐 {len(missing)}" if missing else ""))

    # 5. ffmpeg 合成
    log("\n[3/3] ffmpeg 合成 mp4 ...")
    start_idx = frames[0]
    cmd = [ffmpeg, "-y",
           "-framerate", str(meta["fps"]),
           "-start_number", str(start_idx),
           "-i", os.path.join(out_dir, f"f%05d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", str(a.crf), "-preset", "fast",
           out_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(out_mp4):
        log((r.stderr or "")[-1500:])
        raise SystemExit("ffmpeg 合成失败")

    size_kb = os.path.getsize(out_mp4) / 1024
    dur = len(frames) / meta["fps"]
    log(f"      ffmpeg exit={r.returncode}")
    log("\n" + "=" * 60)
    log(f"SUCCESS: {out_mp4}")
    log(f"         {size_kb:.0f} KB   约 {dur:.2f} 秒   "
        f"{meta['width']}x{meta['height']} @{meta['fps']}fps")
    log("=" * 60)

    # 6. 清理
    try:
        os.remove(tsx_path)
    except OSError:
        pass
    if not a.keep_frames:
        shutil.rmtree(work, ignore_errors=True)
    else:
        log(f"中间帧保留在: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
