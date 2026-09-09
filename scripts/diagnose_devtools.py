#!/usr/bin/env python3
"""
诊断：本机能否使用 Remotion 标准渲染（npx remotion render）。

原理：Remotion 通过 Chrome DevTools Protocol 驱动浏览器截图。
若第三方安全软件/防火墙拦截了浏览器的调试端口或调试管道，
`npx remotion render` 会卡在"打开浏览器"阶段并最终 ws 连接超时。

本脚本独立于 Remotion 复现同样的连接，从而提前判断该走哪条路。

用法：
  python diagnose_devtools.py [--chrome <chrome.exe 路径>]

退出码：
  0 = 标准渲染可用（直接 npx remotion render）
  1 = 被拦截，必须走降级方案（逐帧截图 + ffmpeg）
  2 = 没找到浏览器
"""
import argparse
import os
import socket
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def find_chrome(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def tcp_reachable(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def try_devtools_port(chrome: str, port: int) -> tuple[bool, str]:
    """启动 Chrome 的远程调试端口并尝试连接，返回结果与说明。"""
    profile = os.path.join(os.environ.get("TEMP", "."), f"rmdiag-{port}")
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    proc = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 给浏览器 12 秒启动（慢机器上首次启动可能更久）
        for _ in range(24):
            time.sleep(0.5)
            if tcp_reachable(port, timeout=1.5):
                return True, f"端口 {port} 可连接"
            if proc.poll() is not None:
                return False, f"浏览器进程提前退出 (code={proc.returncode})"
        return False, f"端口 {port} 连接超时（浏览器在跑但不 accept）"
    except Exception as exc:  # noqa: BLE001
        return False, f"启动异常: {exc}"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def try_devtools_pipe(chrome: str) -> tuple[bool, str]:
    """测试 --remote-debugging-pipe（不经网络的通信方式）。"""
    profile = os.path.join(os.environ.get("TEMP", "."), "rmdiag-pipe")
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--remote-debugging-pipe",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # 管道模式下，Chrome 会在 fd3/fd4 通信；stdout 上不应立刻出现 DevTools 横幅。
        # 这里只判断进程能否存活并输出 DevTools 监听信息。
        time.sleep(6)
        if proc.poll() is not None:
            return False, "管道模式进程提前退出"
        # 尝试读一小段 stdout（非阻塞）
        try:
            os.set_blocking(proc.stdout.fileno(), False)
            data = proc.stdout.read() or b""
        except Exception:  # noqa: BLE001
            data = b""
        if b"DevTools listening" in data:
            return True, "管道模式有响应"
        return False, "管道模式无响应"
    except Exception as exc:  # noqa: BLE001
        return False, f"管道模式异常: {exc}"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def try_screenshot(chrome: str) -> tuple[bool, str]:
    """测试无调试端口的静态截图能力（降级方案依赖它）。"""
    out = os.path.join(os.environ.get("TEMP", "."), "rmdiag-shot.png")
    html = os.path.join(os.environ.get("TEMP", "."), "rmdiag.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write("<html><body style='background:#0b1026'></body></html>")
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=800,450",
        "--virtual-time-budget=3000",
        f"--screenshot={out}",
        f"file:///{html.replace(chr(92), '/')}",
    ]
    try:
        subprocess.run(args, check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = os.path.exists(out) and os.path.getsize(out) > 0
        return ok, ("静态截图可用" if ok else "静态截图无产物")
    except subprocess.TimeoutExpired:
        return False, "静态截图超时"
    except subprocess.CalledProcessError as exc:
        return False, f"静态截图失败 (code={exc.returncode})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", default=None, help="指定浏览器可执行文件路径")
    args = ap.parse_args()

    chrome = find_chrome(args.chrome)
    if not chrome:
        print("[X] 未找到 Chrome/Edge，请先安装 Chrome。")
        return 2
    print(f"浏览器: {chrome}")

    print("\n[1/3] 测试 DevTools 调试端口 ...")
    port_ok, port_msg = try_devtools_port(chrome, 9333)
    print(f"      {port_msg}")

    print("[2/3] 测试 DevTools 管道模式 ...")
    pipe_ok, pipe_msg = try_devtools_pipe(chrome)
    print(f"      {pipe_msg}")

    print("[3/3] 测试静态截图能力 ...")
    shot_ok, shot_msg = try_screenshot(chrome)
    print(f"      {shot_msg}")

    print("\n" + "=" * 56)
    if port_ok or pipe_ok:
        print("结论: 标准渲染可用 → 直接跑")
        print("      npx remotion render <CompositionId> out.mp4")
        return 0

    if shot_ok:
        print("结论: 浏览器调试通道被拦截，但静态截图可用")
        print("      → 使用降级方案: scripts/export_fallback.py")
        return 1

    print("结论: 浏览器既不能调试也不能截图，本机环境无法渲染 Remotion 视频。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
