# -*- coding: utf-8 -*-
"""
岔音 Xover —— 电子分频双路播放器
一首歌按频率拆开，两台机器各推一段到不同音箱。

依赖安装:
    pip install -r requirements.txt

运行:
    xover.pyw           直接双击即可（无控制台窗口）
    python xover.pyw    命令行启动（可看到日志输出）

说明:
- 音乐放 flac 文件夹、MV 放 mv 文件夹（与脚本同目录），列表里选一首即可播放
- **频率分频**：交叉点以下交给「低频机」(机器1)，以上交给「高频机」(机器2)
- 两台机器各自选择输出设备、各自设 0~1000ms 延迟
- 分频用线性相位 FIR（LP + HP 系数配对），交叉点精确 -6dB，两机相位一致、相加可无损还原
- 进度条可拖动跳转，跳转时两机同步移动、延迟错位保持不变，MV 画面同步跟跳
- 按钮区可勾选“打开MV”；不勾选则只放声音
- 支持音频: flac wav mp3 ogg m4a　支持视频: mp4 mkv avi mov webm ts m4v flv wmv
- 解码、分频、打开设备都在后台线程完成，界面不会卡住
- 界面为黑白双主题，右上角 ◐ 按钮切换，选择会记住（存 settings.json）
"""

import array
import ctypes
import json
import queue
import subprocess
import sys
import threading
import time

# --- 高 DPI 感知：必须在 import tkinter / 创建任何窗口之前设置 ---
# Windows 150% 缩放下若进程是 DPI 未感知，Tk 只会拿到虚拟化后的屏幕尺寸
# （如实际 1920x1200 却报告 1280x800），导致 -fullscreen 窗口只占屏幕左上角。
# customtkinter 内部也会调用 SetProcessDpiAwareness(2)，但那是在 CTk() 里、
# Tk 已经缓存了屏幕尺寸之后，为时已晚，所以这里必须抢先设置。
DPI_AWARE = False
if sys.platform.startswith("win"):
    try:
        DPI_AWARE = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0  # 每显示器感知
    except Exception:
        pass
    if not DPI_AWARE:
        try:
            DPI_AWARE = bool(ctypes.windll.user32.SetProcessDPIAware())     # 兼容旧系统
        except Exception:
            DPI_AWARE = False

import tkinter as tk
from collections import deque
from pathlib import Path

# ------------------------------------------------------------ 项目标识 ----

APP_NAME = "岔音"                 # 中文名（界面、口头称呼）
APP_EN = "Xover"                  # 英文名（仓库、命令、包名）
APP_VERSION = "1.0.0"
APP_TITLE = f"{APP_NAME} {APP_EN}"


def _fatal(msg: str):
    """启动失败提示。.pyw 没有控制台，必须弹窗，否则用户只看到程序闪退。"""
    print(msg)
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, f"{APP_TITLE} 启动失败", 0x10)
    except Exception:
        pass


try:
    import customtkinter as ctk
    import miniaudio
    import numpy as np
except ImportError as e:
    _fatal(f"缺少依赖库：{e}\n\n"
           "请在项目目录执行：\n"
           "  pip install -r requirements.txt")
    sys.exit(1)

try:
    import imageio_ffmpeg
    HAS_FFMPEG = True
except ImportError:
    HAS_FFMPEG = False

# ---------------------------------------------------------------- 常量 ----

SR = 44100
NCH = 2
BASE = Path(__file__).resolve().parent
DIR_MUSIC = BASE / "flac"                # 音乐目录
DIR_MV = BASE / "mv"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".ts", ".wmv", ".mpg", ".mpeg"}
MAX_DELAY_MS = 1000
DEFAULT_LABEL = "默认设备"
SETTINGS = BASE / "settings.json"        # 只存主题偏好这类小东西
F32 = miniaudio.SampleFormat.FLOAT32
S16 = miniaudio.SampleFormat.SIGNED16

# ---- 分频参数 ----
DEFAULT_CROSSOVER_HZ = 200  # 交叉频率默认值（可在界面输入框里改）
MIN_CROSSOVER_HZ = 20
MAX_CROSSOVER_HZ = 20000
XOVER_ORDER = 4096          # FIR 阶数（偶数 -> 奇数抽头）；越大越陡、延迟越大
XOVER_TAPS = XOVER_ORDER + 1
HEADROOM_CEILING = 0.995    # 分频后两段统一归一化的目标峰值（留一点余量防削波）

FONT = "Microsoft YaHei UI"
MONO = "Consolas"           # 时间码用等宽，数字跳动时不抖

# ---- 黑白双主题配色 ----（唯一强调色就是前景黑/白，其余全部灰阶）
THEMES = {
    "dark": {
        "name": "dark", "appearance": "dark",
        "BG": "#111111", "CARD": "#1B1B1B", "BORDER": "#2E2E2E",
        "TEXT": "#ECECEC", "SUB": "#9A9A9A", "FAINT": "#6E6E6E",
        "SEL_BG": "#262626", "HOVER_BG": "#202020",
        "FIELD": "#232323", "FIELD_BORDER": "#333333",
        "BTN": "#242424", "BTN_HOVER": "#303030",
        "ACCENT": "#FFFFFF", "ACCENT_TEXT": "#111111",
        "TRACK": "#3A3A3A", "SCROLL": "#3A3A3A", "SCROLL_HOVER": "#4E4E4E",
        "CHECK_BORDER": "#4A4A4A", "CHECK_MARK": "#111111",
        "SCREEN": "#000000", "SCREEN_TEXT": "#7A7A7A",
        "WARN": "#C8A24A", "ERR": "#D46A6A",
    },
    "light": {
        "name": "light", "appearance": "light",
        "BG": "#F4F4F4", "CARD": "#FFFFFF", "BORDER": "#E4E4E4",
        "TEXT": "#141414", "SUB": "#6B6B6B", "FAINT": "#9B9B9B",
        "SEL_BG": "#EDEDED", "HOVER_BG": "#F6F6F6",
        "FIELD": "#F2F2F2", "FIELD_BORDER": "#DEDEDE",
        "BTN": "#EEEEEE", "BTN_HOVER": "#E2E2E2",
        "ACCENT": "#141414", "ACCENT_TEXT": "#FFFFFF",
        "TRACK": "#DEDEDE", "SCROLL": "#D2D2D2", "SCROLL_HOVER": "#BBBBBB",
        "CHECK_BORDER": "#C8C8C8", "CHECK_MARK": "#FFFFFF",
        "SCREEN": "#0B0B0B", "SCREEN_TEXT": "#8A8A8A",
        "WARN": "#A8762B", "ERR": "#C24A4A",
    },
}

# 下面是运行时主题变量，启动时由 _apply_theme 统一填充。
# 注意：这些值只能在函数「体内」引用，不能拿来当参数默认值 ——
# 默认值是在函数定义时就求值的，那时主题还没套用（还是空串）。
BG = CARD = BORDER = TEXT = SUB = FAINT = ""
SEL_BG = HOVER_BG = FIELD = FIELD_BORDER = ""
BTN = BTN_HOVER = ACCENT = ACCENT_TEXT = ""
TRACK = SCROLL = SCROLL_HOVER = CHECK_BORDER = CHECK_MARK = ""
SCREEN = SCREEN_TEXT = WARN = ERR = ""
THEME_NAME = "light"


def load_theme_name() -> str:
    try:
        return "dark" if json.loads(SETTINGS.read_text("utf-8")).get("theme") == "dark" else "light"
    except Exception:
        return "light"


def save_theme_name(name: str):
    try:
        SETTINGS.write_text(json.dumps({"theme": name}), "utf-8")
    except Exception:
        pass


def _apply_theme(name: str):
    """切换调色板：把所有颜色刷进模块级变量，并通知 customtkinter 换明暗。"""
    global THEME_NAME
    global BG, CARD, BORDER, TEXT, SUB, FAINT, SEL_BG, HOVER_BG
    global FIELD, FIELD_BORDER, BTN, BTN_HOVER, ACCENT, ACCENT_TEXT
    global TRACK, SCROLL, SCROLL_HOVER, CHECK_BORDER, CHECK_MARK
    global SCREEN, SCREEN_TEXT, WARN, ERR
    t = THEMES["dark" if name == "dark" else "light"]
    THEME_NAME = t["name"]
    BG, CARD, BORDER = t["BG"], t["CARD"], t["BORDER"]
    TEXT, SUB, FAINT = t["TEXT"], t["SUB"], t["FAINT"]
    SEL_BG, HOVER_BG = t["SEL_BG"], t["HOVER_BG"]
    FIELD, FIELD_BORDER = t["FIELD"], t["FIELD_BORDER"]
    BTN, BTN_HOVER = t["BTN"], t["BTN_HOVER"]
    ACCENT, ACCENT_TEXT = t["ACCENT"], t["ACCENT_TEXT"]
    TRACK, SCROLL, SCROLL_HOVER = t["TRACK"], t["SCROLL"], t["SCROLL_HOVER"]
    CHECK_BORDER, CHECK_MARK = t["CHECK_BORDER"], t["CHECK_MARK"]
    SCREEN, SCREEN_TEXT = t["SCREEN"], t["SCREEN_TEXT"]
    WARN, ERR = t["WARN"], t["ERR"]
    try:
        ctk.set_appearance_mode(t["appearance"])
    except Exception:
        pass
    return t


# ---------------------------------------------------------------- 解码 ----

_ffmpeg_path = None


def ffmpeg_exe() -> str:
    global _ffmpeg_path
    if _ffmpeg_path is None:
        _ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    return _ffmpeg_path


def decode_to_np(path: Path) -> "np.ndarray":
    """用 ffmpeg 解码为 44100Hz 双声道 float32，返回形状 (帧数, 2) 的连续数组。

    用 ffmpeg 而非 miniaudio.decode_file：后者对非 ASCII（中文）路径会失败。
    """
    cmd = [
        ffmpeg_exe(), "-v", "error", "-i", str(path),
        "-f", "f32le", "-ac", str(NCH), "-ar", str(SR),
        "-c:a", "pcm_f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        msg = (r.stderr.decode("utf-8", "ignore") or "").strip()
        raise RuntimeError(msg.splitlines()[-1][:180] if msg else "无法解码")
    a = np.frombuffer(r.stdout, dtype="<f4")
    usable = (len(a) // NCH) * NCH
    if usable != len(a):
        a = a[:usable]
    return np.ascontiguousarray(a.reshape(-1, NCH))


# ------------------------------------------------ 线性相位 FIR 分频 ----

def fir_lowpass(fc: float, taps: int, sr: int = SR) -> "np.ndarray":
    """窗函数法（Blackman）线性相位低通 FIR，直流增益归一到 1。"""
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * fc / sr * np.sinc(2.0 * fc / sr * n)
    h *= np.blackman(taps)
    s = h.sum()
    if s:
        h /= s
    return h


def fir_highpass(fc: float, taps: int, sr: int = SR) -> "np.ndarray":
    """谱反转法从同一低通得到高通 —— 与低通共享系数，群延迟完全一致。"""
    h = -fir_lowpass(fc, taps, sr)
    h[(taps - 1) // 2] += 1.0          # 加上单位冲激
    return h


def xover_taps_for(fc: float, sr: int = SR) -> int:
    """按分频点自适应抽头数，保证过渡带宽度恒定（音质不随频率下降）。

    窗函数法的过渡带宽度 ≈ K·sr/N，即 N 与 sr/fc 成正比才能保持「相对带宽」恒定。
    这里取 N ≈ 46·sr/fc（对应约 ±1/6 倍频程的窄过渡带），并夹在合理范围内：
      - 下限 XOVER_TAPS：低分频点时不至于太小
      - 上限 65537：高分频点时不会无限增长（延迟/算力可控）
    """
    ideal = int(46.0 * sr / max(fc, 1.0))
    n = max(XOVER_TAPS, ideal)
    n = min(n, 65537)
    return n | 1                       # 强制奇数（线性相位需要）


_XOVER_CACHE = {}


def crossover_pair(fc: float, taps: int = 0):
    """返回 ((h_lp, h_hp), taps)，两者线性相位、交叉点恒 -6dB、相加可无损还原。

    taps=0 时按分频点自适应（见 xover_taps_for）。
    """
    taps = taps or xover_taps_for(fc)
    key = (float(fc), int(taps), SR)
    if key not in _XOVER_CACHE:
        if len(_XOVER_CACHE) > 6:          # 用户反复试不同频率，别无限占内存
            _XOVER_CACHE.clear()
        _XOVER_CACHE[key] = (fir_lowpass(float(fc), taps), fir_highpass(float(fc), taps))
    return _XOVER_CACHE[key], taps


def split_bands(data: "np.ndarray", fc: float):
    """把 (n,2) 的立体声按 fc 拆成 (低通段, 高通段)。

    用 FFT 重叠相加做线性卷积。关键点：
      - 每块读入 hop 个样本，线性卷积结果长度为 hop+L-1，其中尾部 L-1 个样本
        会与【下一块的开头】重叠相加，必须一并写回 out，否则块边界会出现台阶。
      - 因此 FFT 长度 block 必须 >= hop + L - 1。
    两段都是线性相位、时延同为 (L-1)/2，相加可无损还原原信号。

    最后做**统一的余量归一化**（不是削波）：分频后单段峰值可能超过 1.0
    （如高通段可达 1.39），直接送设备会硬削波。这里取两段最大峰值算出一个
    公共增益，两段乘同一个系数 —— 两机之间的响度比例完全不变，
    且归一化后 LP+HP 仍精确等于原信号的同比例缩放，依然无损。
    """
    (h_lp, h_hp), taps = crossover_pair(fc)
    n = data.shape[0]
    L = h_lp.shape[0]
    block = 1 << 16                      # FFT 长度
    while block < L * 2:
        block <<= 1
    hop = block - (L - 1)                # 有效步长，保证 hop + L - 1 == block
    out_lp = np.zeros((n, NCH), dtype=np.float32)
    out_hp = np.zeros((n, NCH), dtype=np.float32)
    HLP = np.fft.rfft(h_lp, block)
    HHP = np.fft.rfft(h_hp, block)
    for start in range(0, n, hop):
        seg = data[start:start + hop]
        m = seg.shape[0]
        if m == 0:
            break
        pad = np.zeros((block, NCH), dtype=np.float32)
        pad[:m] = seg
        S = np.fft.rfft(pad, axis=0)
        # 线性卷积结果长度 m + L - 1，全部写回（尾部与下一块重叠相加）
        y_lp = np.fft.irfft(S * HLP[:, None], block, axis=0)
        y_hp = np.fft.irfft(S * HHP[:, None], block, axis=0)
        out_len = min(block, m + L - 1)
        end = min(n, start + out_len)
        take = end - start
        if take <= 0:
            break
        out_lp[start:end] += y_lp[:take]
        out_hp[start:end] += y_hp[:take]

    # 统一余量：两段同乘一个系数，保持响度比例、消除削波
    peak = max(float(np.max(np.abs(out_lp))), float(np.max(np.abs(out_hp))), 1e-9)
    if peak > HEADROOM_CEILING:
        g = HEADROOM_CEILING / peak
        out_lp *= g
        out_hp *= g
    return out_lp, out_hp


def xover_delay_for(fc: float) -> int:
    """该分频点对应的线性相位群延迟（采样点）=(taps-1)/2。"""
    return (xover_taps_for(fc) - 1) // 2


def make_generator(samples: "np.ndarray", delay_frames: int, state: dict, sample_type: str):
    """miniaudio generator。samples 为 (帧数, 2) 的 float32 数组。

    源位置 = state["offset"] + out_pos - delay_frames
      - state["offset"]  : 曲目起点（拖进度条时由主线程直接改，无缝跳转）
      - delay_frames     : 本机器的延迟（表现为开头静音）
    每个周期必须恰好输出 framecount 帧。
    """
    total = samples.shape[0]
    out_pos = 0
    fc = yield _silence(sample_type, 256)  # 预热（值被忽略）
    while True:
        src = state["offset"] + out_pos - delay_frames
        sil_a = max(0, min(fc, -src))                     # 周期开头静音
        a_start = max(0, min(total, src))
        a_end = max(0, min(total, src + fc))
        n_audio = a_end - a_start
        sil_b = fc - sil_a - n_audio                      # 周期结尾静音
        parts = []
        if sil_a > 0:
            parts.append(np.zeros((sil_a, NCH), dtype=np.float32))
        if n_audio > 0:
            parts.append(samples[a_start:a_end])
        if sil_b > 0:
            parts.append(np.zeros((sil_b, NCH), dtype=np.float32))
        if parts:
            block = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
        else:
            block = np.zeros((0, NCH), dtype=np.float32)
        if block.shape[0] != fc:                          # 兜底，保证长度精确
            fixed = np.zeros((fc, NCH), dtype=np.float32)
            k = min(fc, block.shape[0])
            fixed[:k] = block[:k]
            block = fixed
        out_pos += fc
        state["out"] = out_pos
        state["audio"] = max(0, min(total, state["offset"] + out_pos - delay_frames))
        if total > 0 and state["audio"] >= total:
            state["finished"] = True
        fc = yield _to_pcm(block, sample_type)


def _silence(sample_type: str, frames: int):
    if sample_type == "f":
        return np.zeros((frames, NCH), dtype=np.float32)
    return array.array("h", bytes(frames * NCH * 2))


def _to_pcm(block: "np.ndarray", sample_type: str):
    """float32 (n,2) -> miniaudio 需要的交错 PCM。"""
    if sample_type == "f":
        return np.ascontiguousarray(block.reshape(-1)).tobytes()
    a = np.clip(block, -1.0, 1.0) * 32767.0
    return a.astype("<i2").tobytes()


def fmt_time(frames: int) -> str:
    sec = max(0, frames) / SR
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def stem_of(name: str) -> str:
    """归一化文件名（去掉所有末尾音视频扩展名），如 夜曲.flac.flac -> 夜曲。"""
    s = Path(name).name
    while True:
        p = Path(s)
        if p.suffix.lower() in AUDIO_EXTS or p.suffix.lower() in VIDEO_EXTS:
            s = p.stem
        else:
            return s


def _longest_common_run(a: str, b: str) -> int:
    """最长公共连续子串长度（滚动数组 DP，O(len(a)*len(b)) 但字符串很短）。

    用于 MV 模糊配对：「周杰伦 - 七里香」与「七里香」的公共片段长 3。
    """
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# ---------------------------------------------------------------- MV ----

VW, VH, VFPS = 512, 288, 25          # 默认预览尺寸（16:9），实际随窗口自适应
VID_H = 250                          # 上部区域高度下限（MV 卡 + 两张机器卡）
MIN_LIST_H = 170                     # 曲目列表最小可见高度

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def vfilter(w: int, h: int) -> str:
    """固定输出尺寸（不足处补黑边），避免解析源分辨率。"""
    return (f"fps={VFPS},scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")


def grab_frame(path: Path, at_sec: float = 0.0, size=(VW, VH)):
    """取单帧（用于静止预览），失败返回 None。"""
    w, h = size
    nb = w * h * 3
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{max(0.0, at_sec):.3f}", "-i", str(path),
           "-an", "-vf", vfilter(w, h), "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True)
    return r.stdout[:nb] if len(r.stdout) >= nb else None


def fit_163(w: int, h: int):
    """在给定框内取最大的 16:9 尺寸（偶数），保证画面铺满不留黑边。"""
    w = max(16, int(w))
    h = max(9, int(h))
    if w / h >= 16 / 9:
        fw, fh = int(h * 16 / 9), h
    else:
        fw, fh = w, int(w * 9 / 16)
    return max(16, fw - fw % 2), max(9, fh - fh % 2)


def decode_size_for(w: int, h: int, cap: int = 960):
    """目标显示尺寸 -> 实际解码尺寸：16:9 letterbox，宽不超过 cap。

    小窗传 cap=960 省算力（放大一点看不出来）；
    全屏必须传 cap=0（不设限），否则解码只有窗口的几分之一分辨率，满屏发虚。
    """
    fw, fh = fit_163(w, h)
    if cap and fw > cap:
        fw = cap - cap % 2
        fh = int(fw * 9 / 16)
        fh -= fh % 2
    return fw - fw % 2, fh - fh % 2


class VideoPlayer:
    """MV 画面播放：以音频位置为主时钟，后台线程解码、主线程贴图。"""

    def __init__(self, app: "App", size=(VW, VH)):
        self.app = app
        self.size = size
        self.proc = None
        self.thread = None
        self.queue = deque(maxlen=4)
        self.stopping = False
        self.base_sec = 0.0

    def set_size(self, size):
        self.size = size

    def start(self, path: Path, start_sec: float):
        self.stop()
        w, h = self.size
        frame_bytes = w * h * 3
        self.stopping = False
        self.base_sec = max(0.0, start_sec)
        cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{self.base_sec:.3f}", "-i", str(path),
               "-an", "-vf", vfilter(w, h), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=frame_bytes)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        size = self.size                     # 本线程固定用启动时的尺寸
        frame_bytes = size[0] * size[1] * 3
        idx = 0
        while not self.stopping and proc.poll() is None:
            try:
                data = proc.stdout.read(frame_bytes)
            except (ValueError, OSError):   # 停止时管道被关闭
                break
            if not data or len(data) < frame_bytes:
                break
            ts = self.base_sec + idx / VFPS
            idx += 1
            pos = self.app.video_clock()
            if ts < pos - 0.09:          # 落后就丢帧追赶
                continue
            while not self.stopping and ts > self.app.video_clock() + 0.05:
                time.sleep(0.006)
            if self.stopping:
                break
            self.queue.append((size, ts, data))

    def latest(self, pos: float):
        """取出时间不超过 pos+0.08 的最新一帧：(size, ts, data) 或 None。"""
        out = None
        while self.queue:
            size, ts, data = self.queue.popleft()
            if ts <= pos + 0.08:
                out = (size, ts, data)
            else:
                self.queue.appendleft((size, ts, data))
                break
        return out

    def stop(self):
        self.stopping = True
        p, self.proc = self.proc, None
        if p is not None:
            try:
                if p.stdout:
                    p.stdout.close()
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass
        self.thread = None
        self.queue.clear()


# ---------------------------------------------------------------- 曲目 ----

class Song:
    def __init__(self, stem: str, path, mv=None):
        self.stem = stem
        self.path = path
        self.mv = mv


class SongRow(ctk.CTkFrame):
    """列表里的一行：选中时左侧亮一道竖条，右侧是灰阶的小标记。"""

    def __init__(self, master, song: Song, index: int, on_click):
        super().__init__(master, corner_radius=8, fg_color="transparent", height=34)
        self.song = song
        self.index = index
        self.selected = False
        self.on_click = on_click

        self.bar = ctk.CTkFrame(self, width=3, height=18, corner_radius=2,
                                fg_color="transparent")
        self.bar.pack(side="left", padx=(6, 0))

        self.name = ctk.CTkLabel(self, text=song.stem, font=ctk.CTkFont(FONT, 13),
                                 text_color=TEXT, anchor="w")
        self.name.pack(side="left", padx=(8, 8), pady=5)

        self.b_mv = ctk.CTkLabel(self, text="MV", font=ctk.CTkFont(FONT, 10),
                                 corner_radius=5, width=32, height=19,
                                 fg_color="transparent", border_width=1,
                                 border_color=BORDER,
                                 text_color=TEXT if song.mv else FAINT)
        self.b_mv.pack(side="right", padx=(4, 10), pady=5)

        ext = song.path.suffix.lstrip(".").upper() if song.path else "—"
        self.b_fmt = ctk.CTkLabel(self, text=ext, font=ctk.CTkFont(FONT, 10),
                                  corner_radius=5, width=44, height=19,
                                  fg_color="transparent", border_width=1,
                                  border_color=BORDER, text_color=SUB)
        self.b_fmt.pack(side="right", padx=4, pady=5)

        self.b_split = ctk.CTkLabel(self, text="—", font=ctk.CTkFont(FONT, 10),
                                    corner_radius=5, width=72, height=19,
                                    fg_color="transparent", border_width=1,
                                    border_color=BORDER, text_color=SUB)
        self.b_split.pack(side="right", padx=4, pady=5)

        for w in (self, self.name, self.b_split, self.b_fmt, self.b_mv):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _click(self, _e=None):
        self.on_click(self.index)

    def _enter(self, _e=None):
        if not self.selected:
            self.configure(fg_color=HOVER_BG)

    def _leave(self, _e=None):
        if not self.selected:
            self.configure(fg_color="transparent")

    def set_selected(self, sel: bool):
        self.selected = sel
        self.configure(fg_color=SEL_BG if sel else "transparent")
        self.bar.configure(fg_color=ACCENT if sel else "transparent")

    def set_split(self, hz: float):
        """分频点变了要同步刷新徽章，否则会一直显示旧频率。"""
        self.b_split.configure(text=f"{hz:g}Hz 分频")


# ---------------------------------------------------------------- 机器 ----

class Machine:
    """一台播放机器：一段频段 + 输出设备 + 延迟 + 状态。"""

    def __init__(self, app: "App", master, title: str, sub: str, band: str = "low"):
        self.app = app
        self.title = title
        self.band = band                    # "low" = 交叉点以下 / "high" = 交叉点以上
        self.device = None
        self.state = None
        self.playing = False
        self.total_frames = 0
        self.used_device = ""
        self.sub = sub

        card = ctk.CTkFrame(master, corner_radius=10, fg_color=CARD,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=0, pady=(0, 6))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=11, pady=7)

        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        self.dot = ctk.CTkFrame(head, width=6, height=6, corner_radius=3,
                                fg_color=FAINT)
        self.dot.pack(side="left", padx=(0, 7))
        ctk.CTkLabel(head, text=title, font=ctk.CTkFont(FONT, 13, "bold"),
                     text_color=TEXT).pack(side="left")
        self.sub_label = ctk.CTkLabel(head, text=sub, font=ctk.CTkFont(MONO, 11),
                                      text_color=FAINT)
        self.sub_label.pack(side="right")

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(row, text="输出设备", font=ctk.CTkFont(FONT, 11),
                     text_color=SUB).pack(side="left")
        self.dev_menu = ctk.CTkOptionMenu(
            row, values=[DEFAULT_LABEL], width=158, height=25,
            font=ctk.CTkFont(FONT, 11), dropdown_font=ctk.CTkFont(FONT, 11),
            fg_color=FIELD, button_color=FIELD_BORDER, button_hover_color=BTN_HOVER,
            text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=HOVER_BG,
            dropdown_text_color=TEXT, corner_radius=7, command=self._on_dev_change)
        self.dev_menu.pack(side="right")

        row2 = ctk.CTkFrame(inner, fg_color="transparent")
        row2.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(row2, text="延迟", font=ctk.CTkFont(FONT, 11),
                     text_color=SUB).pack(side="left")
        self.delay_entry = ctk.CTkEntry(
            row2, width=62, height=25, corner_radius=7, font=ctk.CTkFont(MONO, 11),
            fg_color=FIELD, border_color=FIELD_BORDER, text_color=TEXT,
            justify="center", placeholder_text="0")
        self.delay_entry.pack(side="right")
        ctk.CTkLabel(row2, text=f"0~{MAX_DELAY_MS} ms", font=ctk.CTkFont(FONT, 10),
                     text_color=FAINT).pack(side="right", padx=(0, 6))
        self.delay_entry.bind("<FocusOut>", lambda e: self.get_delay())
        self.delay_entry.bind("<Return>", lambda e: self.get_delay())

        self.status = ctk.CTkLabel(inner, text="待播放", font=ctk.CTkFont(FONT, 11),
                                   text_color=SUB, anchor="w", justify="left", wraplength=300)
        self.status.pack(fill="x", pady=(6, 0))

    def _on_dev_change(self, _v=None):
        self.app.check_same_device()

    # ---- 设备 / 延迟 ----

    def device_choice(self):
        """(device_id, 设备名)。按名称查表，不依赖各版本控件 API。"""
        name = self.dev_menu.get()
        if name == DEFAULT_LABEL or name not in self.app.dev_names:
            return None, name
        idx = self.app.dev_names.index(name)
        infos = self.app.dev_infos
        if 1 <= idx <= len(infos):
            return infos[idx - 1]["id"], name
        return None, name

    def get_delay(self) -> int:
        try:
            v = int(float(self.delay_entry.get().strip() or 0))
        except ValueError:
            v = 0
        v = max(0, min(MAX_DELAY_MS, v))
        if self.delay_entry.get() != str(v):
            self.delay_entry.delete(0, "end")
            self.delay_entry.insert(0, str(v))
        return v

    # ---- 状态 ----

    def set_status(self, text: str, color: str = None):
        # 注意：默认值必须写 None 再到函数体里取 SUB。
        # 写成 color: str = SUB 会在「定义时」就把当时的值绑死，
        # 而主题色是启动后由 _apply_theme 填进模块变量的，那时还是空串。
        self.status.configure(text=text, text_color=color or SUB)

    def status_text(self) -> str:
        return self.status.cget("text")

    def set_playing_dot(self, on: bool):
        self.dot.configure(fg_color=ACCENT if on else FAINT)

    def stop_play(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        self.playing = False
        self.state = None
        self.set_playing_dot(False)

    def position_frames(self) -> int:
        return self.state["audio"] if self.state else 0


# ---------------------------------------------------------------- 应用 ----


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} · 双路分频")
        self.geometry("800x600")
        self.minsize(800, 600)
        _apply_theme(load_theme_name())
        self.configure(fg_color=BG)

        self.ctx = miniaudio.Devices()
        self.dev_infos = []
        self.dev_names = [DEFAULT_LABEL]
        self.songs = []
        self.rows = []
        self.mv_files = {}
        self.cache = {}
        self.cross_hz = float(DEFAULT_CROSSOVER_HZ)   # 当前分频点（用户可改）
        self.xover_delay = xover_delay_for(self.cross_hz)   # 当前分频对应的群延迟（采样点）
        self.ui_q = queue.Queue()
        self.play_token = 0
        self._tick_job = None
        self._video_job = None
        self._redraw_job = None
        self.busy = False
        self.playing = False
        self.dragging = False
        self.sel_index = -1
        self.total_frames = 0
        self.seek_frame = 0
        self.tick_perf = 0.0
        self.mv_path = None
        self.mv_active = False
        self.mv_photo = None
        self.preview_size = (VW, VH)   # 预览框当前像素尺寸（随窗口变化）
        self.preview_raw = None        # (size, bytes) 最近一帧原始解码数据
        self._preview_after = None     # 预览重解码防抖句柄
        self._still_seq = 0            # 静帧请求序号，用于丢弃过期结果
        self.mv_last_ts = -1.0
        self.fs_win = None          # 全屏窗口
        self.fs_label = None
        self.fs_photo = None
        self.fs_size = None
        self.video = VideoPlayer(self)

        self._build_header()
        self._build_transport()
        self._build_body()
        self._build_footer()
        self._bind_keys()

        self.refresh_all()
        self.after(60, self._pump)
        self.after(33, self._video_tick)
        # 首帧预览要等窗口真正布局完再量尺寸，否则会先按占位尺寸解一张废帧
        self.after(120, self._resync_preview)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------- 布局 ----

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(12, 8))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        brand = ctk.CTkFrame(left, fg_color="transparent")
        brand.pack(anchor="w")
        ctk.CTkLabel(brand, text=APP_NAME, font=ctk.CTkFont(FONT, 20, "bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(brand, text=APP_EN.upper(), font=ctk.CTkFont(MONO, 10, "bold"),
                     corner_radius=4, width=50, height=18, fg_color="transparent",
                     border_width=1, border_color=BORDER,
                     text_color=FAINT).pack(side="left", padx=(8, 0), pady=(5, 0))
        ctk.CTkLabel(brand, text=f"v{APP_VERSION}", font=ctk.CTkFont(MONO, 10),
                     text_color=FAINT).pack(side="left", padx=(6, 0), pady=(5, 0))
        self.cross_hint = ctk.CTkLabel(left, text="", font=ctk.CTkFont(FONT, 11),
                                       text_color=SUB)
        self.cross_hint.pack(anchor="w", pady=(1, 0))

        self.btn_theme = ctk.CTkButton(
            head, text="◐", width=38, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT, 15), fg_color=BTN, hover_color=BTN_HOVER,
            text_color=TEXT, command=self.toggle_theme)
        self.btn_theme.pack(side="right")
        ctk.CTkButton(head, text="刷新", width=64, height=32, corner_radius=8,
                      font=ctk.CTkFont(FONT, 12), fg_color=BTN, hover_color=BTN_HOVER,
                      text_color=TEXT, command=self.refresh_all).pack(side="right", padx=(0, 8))

    def _build_transport(self):
        card = ctk.CTkFrame(self, corner_radius=10, fg_color=CARD,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=18, pady=(0, 8))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        now = ctk.CTkFrame(top, fg_color="transparent")
        now.pack(side="left")
        self.now_label = ctk.CTkLabel(now, text="未选择曲目", font=ctk.CTkFont(FONT, 15, "bold"),
                                      text_color=TEXT)
        self.now_label.pack(anchor="w")
        self.mv_hint = ctk.CTkLabel(now, text="", font=ctk.CTkFont(FONT, 11), text_color=FAINT)
        self.mv_hint.pack(anchor="w", pady=(1, 0))

        btns = ctk.CTkFrame(top, fg_color="transparent")
        btns.pack(side="right")
        self.mv_var = tk.BooleanVar(value=True)
        self.mv_check = ctk.CTkCheckBox(
            btns, text="打开MV", variable=self.mv_var, command=self._on_mv_toggle,
            font=ctk.CTkFont(FONT, 12), text_color=TEXT, checkbox_width=18, checkbox_height=18,
            corner_radius=5, border_width=1, fg_color=ACCENT, hover_color=ACCENT,
            border_color=CHECK_BORDER, checkmark_color=CHECK_MARK)
        self.mv_check.pack(side="left", padx=(0, 16))

        # 分频点输入：用户可自定 200Hz 换成任意频率
        xf = ctk.CTkFrame(btns, fg_color="transparent")
        xf.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(xf, text="分频点", font=ctk.CTkFont(FONT, 12),
                     text_color=SUB).pack(side="left", padx=(0, 6))
        self.cross_entry = ctk.CTkEntry(
            xf, width=72, height=30, corner_radius=7, font=ctk.CTkFont(MONO, 13, "bold"),
            fg_color=FIELD, border_color=FIELD_BORDER, text_color=TEXT,
            justify="center", placeholder_text=str(DEFAULT_CROSSOVER_HZ))
        self.cross_entry.insert(0, str(DEFAULT_CROSSOVER_HZ))
        self.cross_entry.pack(side="left")
        ctk.CTkLabel(xf, text="Hz", font=ctk.CTkFont(FONT, 12),
                     text_color=SUB).pack(side="left", padx=(4, 0))
        ctk.CTkButton(xf, text="应用", width=48, height=30, corner_radius=7,
                      font=ctk.CTkFont(FONT, 12), fg_color=BTN, hover_color=BTN_HOVER,
                      text_color=TEXT, command=self.apply_crossover).pack(side="left", padx=(6, 0))
        self.cross_entry.bind("<Return>", lambda e: self.apply_crossover())
        self.cross_entry.bind("<FocusOut>", lambda e: self.apply_crossover(quiet=True))

        self.btn_play = ctk.CTkButton(btns, text="播 放", width=100, height=36, corner_radius=8,
                                      font=ctk.CTkFont(FONT, 14, "bold"), fg_color=ACCENT,
                                      hover_color=ACCENT, text_color=ACCENT_TEXT,
                                      command=self.toggle_play)
        self.btn_play.pack(side="left", padx=(0, 8))
        self.btn_stop = ctk.CTkButton(btns, text="停 止", width=100, height=36, corner_radius=8,
                                      font=ctk.CTkFont(FONT, 14, "bold"), fg_color=BTN,
                                      hover_color=BTN_HOVER, text_color=TEXT,
                                      command=self.stop)
        self.btn_stop.pack(side="left")

        bar = ctk.CTkFrame(inner, fg_color="transparent")
        bar.pack(fill="x", pady=(10, 0))
        self.time_now = ctk.CTkLabel(bar, text="00:00", font=ctk.CTkFont(MONO, 12),
                                     text_color=TEXT, width=48)
        self.time_now.pack(side="left")
        self.slider = ctk.CTkSlider(
            bar, from_=0, to=1, height=16, corner_radius=8, button_corner_radius=8,
            button_length=12, fg_color=TRACK, progress_color=ACCENT,
            button_color=ACCENT, button_hover_color=ACCENT, command=self._on_slider_move)
        self.slider.set(0)
        self.slider.pack(side="left", fill="x", expand=True, padx=10)
        self.time_total = ctk.CTkLabel(bar, text="00:00", font=ctk.CTkFont(MONO, 12),
                                       text_color=SUB, width=48)
        self.time_total.pack(side="left")
        self.slider.bind("<Button-1>", self._slider_press)
        self.slider.bind("<ButtonRelease-1>", self._slider_release)

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        # 上部（MV + 两台机器）与下部（曲目列表）按权重分剩余高度：
        #   两块都吃权重，窗口变大时画面和列表一起长。
        #   下部的 minsize 保证列表至少露出一行多，不会压缩到看不见。
        body.rowconfigure(0, weight=3, minsize=VID_H)
        body.rowconfigure(1, weight=2)
        body.columnconfigure(0, weight=1)

        upper = ctk.CTkFrame(body, fg_color="transparent")
        upper.grid(row=0, column=0, sticky="nsew")

        # 左：MV 画面
        vcard = ctk.CTkFrame(upper, corner_radius=10, fg_color=CARD,
                             border_width=1, border_color=BORDER)
        vcard.grid(row=0, column=0, sticky="nsew")
        vhead = ctk.CTkFrame(vcard, fg_color="transparent")
        vhead.pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkLabel(vhead, text="MV 画面", font=ctk.CTkFont(FONT, 13, "bold"),
                     text_color=TEXT).pack(side="left")
        self.mv_state = ctk.CTkLabel(vhead, text="", font=ctk.CTkFont(FONT, 11),
                                     text_color=FAINT)
        self.mv_state.pack(side="left", padx=8)
        self.btn_fs = ctk.CTkButton(vhead, text="全屏", width=56, height=24, corner_radius=7,
                                    font=ctk.CTkFont(FONT, 11), fg_color=BTN,
                                    hover_color=BTN_HOVER, text_color=TEXT,
                                    command=self.toggle_fullscreen)
        self.btn_fs.pack(side="right")
        # 预览框：容器是黑的（不然 16:9 之外会露白），里面的画面按 16:9 居中铺满。
        # 缩放到框的 16:9 后由 _on_video_box_resize 重算解码尺寸，画面不留黑边。
        self.video_box = ctk.CTkFrame(vcard, fg_color=CARD, height=VW * 9 // 16)
        self.video_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.video_label = tk.Label(self.video_box, bg=SCREEN, fg=SCREEN_TEXT,
                                    text="未加载 MV", font=(FONT, 12),
                                    bd=0, highlightthickness=0)
        self.video_label.pack(expand=True)
        self.video_label.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        self.video_box.bind("<Configure>", self._on_video_box_resize)

        upper.columnconfigure(0, weight=1, minsize=320)
        upper.columnconfigure(1, weight=0, minsize=290)
        upper.rowconfigure(0, weight=1)

        # 右：两台机器（按频段分频）。窗口矮时两张卡会超出可视区，
        # 所以套一层可滚动的容器，保证第二台机器的设备/延迟永远够得着。
        right = ctk.CTkScrollableFrame(upper, fg_color="transparent", corner_radius=0,
                                       scrollbar_button_color=SCROLL,
                                       scrollbar_button_hover_color=SCROLL_HOVER)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        self.m_low = Machine(self, right, "机器1 · 低频", f"< {DEFAULT_CROSSOVER_HZ}Hz",
                             band="low")
        self.m_high = Machine(self, right, "机器2 · 高频", f"> {DEFAULT_CROSSOVER_HZ}Hz",
                              band="high")
        self.machines = [self.m_low, self.m_high]

        # 下：曲目列表（吃掉全部剩余高度）
        lcard = ctk.CTkFrame(body, corner_radius=10, fg_color=CARD,
                             border_width=1, border_color=BORDER)
        lcard.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        lhead = ctk.CTkFrame(lcard, fg_color="transparent")
        lhead.pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkLabel(lhead, text="曲目列表", font=ctk.CTkFont(FONT, 13, "bold"),
                     text_color=TEXT).pack(side="left")
        self.count_label = ctk.CTkLabel(lhead, text="", font=ctk.CTkFont(FONT, 11),
                                        text_color=FAINT)
        self.count_label.pack(side="left", padx=8)
        ctk.CTkLabel(lhead, text=f"{DIR_MUSIC.name} 文件夹 · 单击选中",
                     font=ctk.CTkFont(FONT, 11), text_color=FAINT).pack(side="right")
        self.list_area = ctk.CTkScrollableFrame(
            lcard, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=SCROLL, scrollbar_button_hover_color=SCROLL_HOVER)
        self.list_area.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _build_footer(self):
        self.log = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 11),
                                text_color=SUB, anchor="w", justify="left", wraplength=1000)
        self.log.pack(fill="x", padx=20, pady=(0, 10))

    def _bind_keys(self):
        self.bind("<space>", self._key_space)
        self.bind("<Up>", lambda e: self._move_sel(-1))
        self.bind("<Down>", lambda e: self._move_sel(1))

    # ------------------------------------------------ 主题切换 ----

    def toggle_theme(self):
        _apply_theme("light" if THEME_NAME == "dark" else "dark")
        save_theme_name(THEME_NAME)
        # 换肤时重画整个界面（控件都在一个根帧里，重建最省事也最不容易漏）。
        # 用 after 推迟一拍：否则会在这个按钮自己的回调里把它销毁掉。
        self.after(0, self._redraw)
        self.after(30, lambda: self.set_log(
            f"已切换到{'深色' if THEME_NAME == 'dark' else '浅色'}主题"))

    def _redraw(self):
        # 重建期间预览框会经历几次中间尺寸；先把防抖任务和旧帧请求作废，
        # 免得多余的解码结果在布局稳定后又把画面覆盖掉。
        if self._preview_after is not None:
            try:
                self.after_cancel(self._preview_after)
            except Exception:
                pass
            self._preview_after = None
        self._still_seq += 1
        # 全屏窗口不在重建范围内，先关掉 —— 否则它的引用会变成野指针，
        # 重建后按钮显示「全屏」但窗口还在，状态对不上。
        if self.fs_win is not None:
            self._close_fs()
        for child in self.winfo_children():
            child.destroy()
        self.rows = []
        self.configure(fg_color=BG)
        self._build_header()
        self._build_transport()
        self._build_body()
        self._build_footer()
        self._rebuild_list()
        for i, r in enumerate(self.rows):
            r.set_selected(i == self.sel_index)
            r.set_split(self.cross_hz)
        # 还原当前曲目与状态文案
        song = self.selected_song()
        if song is not None:
            self.now_label.configure(text=song.stem)
            self._sync_mv_hint()
            if self.total_frames:
                self.slider.configure(to=max(1, self.total_frames))
                self.slider.set(min(self.total_frames, self.seek_frame))
                self.time_total.configure(text=fmt_time(self.total_frames))
                self.time_now.configure(text=fmt_time(self.seek_frame))
            for m in self.machines:
                m.set_status("播放中" if m.playing else "待播放",
                             TEXT if m.playing else SUB)
                m.set_playing_dot(m.playing)
        else:
            self.now_label.configure(text="未选择曲目")
        self._update_band_labels()
        if self.mv_path is None:
            self.video_label.configure(image="", text="未找到同名 MV\n(mv 文件夹)")
            self.mv_photo = None
        # 重建后预览框尺寸要重新量一次，才能按新布局解出铺满的画面。
        # 这里不直接解码 —— 那时的 preview_size 还是重建前的旧值。
        self.after(160, self._resync_preview)

    def _resync_preview(self):
        """按预览框的真实尺寸校正解码尺寸（窗口刚布局完时调一次）。"""
        try:
            w = self.video_box.winfo_width()
            h = self.video_box.winfo_height()
        except Exception:
            return
        if w < 16 or h < 16:
            return
        size = decode_size_for(w, h)
        changed = size != self.preview_size
        self.preview_size = size
        if self.mv_active and self.playing:
            self.video.set_size(size)
        elif self.mv_path is not None and (changed or self.preview_raw is None):
            self._grab_still(self.seek_frame / SR, size)

    # ------------------------------------------------ 主线程 UI 队列 ----

    def ui(self, fn):
        self.ui_q.put(fn)

    def _pump(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    print("UI 更新异常:", e)
        except queue.Empty:
            pass
        self.after(60, self._pump)

    # ---------------------------------------------------------- 扫描 ----

    def _match_mv(self, song_stem: str):
        """给曲目找一个同名 MV。先精确匹配，再按“包含关系”模糊匹配。

        常见情况：音频叫「周杰伦 - 七里香.flac」，MV 叫「七里香.mp4」——
        两边 stem 不相等但互相包含，这里按最长公共子串把它配起来。
        """
        hit = self.mv_files.get(song_stem)
        if hit is not None:
            return hit
        a = song_stem.lower()
        best, best_len = None, 0
        for mv_stem, path in self.mv_files.items():
            b = mv_stem.lower()
            if not b:
                continue
            if b in a or a in b:                 # 一方包含另一方 → 直接算命中
                score = min(len(a), len(b))
            else:
                score = _longest_common_run(a, b)   # 否则看最长公共片段
                if score < 3:                       # 太短不算，避免误配
                    continue
            if score > best_len:
                best, best_len = path, score
        return best

    def refresh_all(self):
        try:
            self.dev_infos = self.ctx.get_playbacks()
            self.dev_names = [DEFAULT_LABEL] + [d["name"] for d in self.dev_infos]
        except Exception as e:
            self.dev_names = [DEFAULT_LABEL]
            self.set_log(f"枚举输出设备失败: {e}", True)
        for m in self.machines:
            cur = m.dev_menu.get()
            m.dev_menu.configure(values=self.dev_names)
            if cur not in self.dev_names:
                m.dev_menu.set(DEFAULT_LABEL)

        self.mv_files = {}
        if DIR_MV.is_dir():
            self.mv_files = {stem_of(p.name): p for p in sorted(DIR_MV.iterdir())
                             if p.is_file() and p.suffix.lower() in VIDEO_EXTS}

        music = {}
        if DIR_MUSIC.is_dir():
            # 键用归一化 stem_of：避免「夜曲.flac.flac」这类名字配不上 MV
            music = {stem_of(p.name): p for p in sorted(DIR_MUSIC.iterdir())
                     if p.is_file() and p.suffix.lower() in AUDIO_EXTS}
        self.songs = [Song(s, music[s], self._match_mv(s)) for s in sorted(music, key=str.lower)]

        keep = self.sel_index
        self._rebuild_list()
        if self.songs:
            self._select_index(min(keep, len(self.songs) - 1) if keep >= 0 else 0)
        else:
            self.now_label.configure(text="未找到曲目")
            self.set_log(f"未找到音频文件，请把音乐放入 {DIR_MUSIC}", True)
            return
        if len(self.dev_names) == 1:
            self.set_log("未检测到可用的音频输出设备（蓝牙/远程桌面可能未连接），连接后点“刷新”", True)
        else:
            self.check_same_device()

    def _rebuild_list(self):
        for w in self.rows:
            w.destroy()
        self.rows = []
        for i, s in enumerate(self.songs):
            row = SongRow(self.list_area, s, i, self._select_index)
            row.pack(fill="x", pady=1)
            row.set_split(self.cross_hz)
            self.rows.append(row)
        self.count_label.configure(text=f"共 {len(self.songs)} 首")

    def _select_index(self, idx: int):
        if not (0 <= idx < len(self.songs)):
            return
        self.sel_index = idx
        for i, r in enumerate(self.rows):
            r.set_selected(i == idx)
        song = self.songs[idx]
        self.now_label.configure(text=song.stem)
        # 换曲：回到开头
        self.seek_frame = 0
        self.total_frames = 0
        self.slider.configure(to=1)
        self.slider.set(0)
        self.time_now.configure(text="00:00")
        self.time_total.configure(text="--:--")
        for m in self.machines:
            m.set_status("待播放")
            m.set_playing_dot(False)
        self._stop_mv()
        if self.playing:
            self.stop(reset=False)
        self.mv_path = getattr(song, "mv", None)
        self._sync_mv_hint()
        self._load_mv_preview()
        threading.Thread(target=self._preload, args=(song,), daemon=True).start()

    def _sync_mv_hint(self):
        if self.mv_path is None:
            self.mv_hint.configure(text="无同名 MV")
        else:
            self.mv_hint.configure(text=f"MV: {self.mv_path.name}")

    def _move_sel(self, step: int):
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text)):   # 正在输入延迟时不抢方向键
            return
        if not self.songs:
            return
        idx = self.sel_index if self.sel_index >= 0 else 0
        self._select_index(max(0, min(len(self.songs) - 1, idx + step)))

    def _key_space(self, _e=None):
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text)):
            return
        self.toggle_play()

    def selected_song(self):
        return self.songs[self.sel_index] if 0 <= self.sel_index < len(self.songs) else None

    def check_same_device(self):
        a, b = self.m_low.dev_menu.get(), self.m_high.dev_menu.get()
        if a == b:
            self.set_log(f"提示：两台机器都用「{a}」。要分别输出到两个音箱，请给两台机器选不同设备", True)
        else:
            hz = self.cross_hz
            self.set_log(f"输出: 低频(<{hz:g}Hz) → {a}　　高频(>{hz:g}Hz) → {b}")

    def set_log(self, text: str, warn: bool = False):
        self.log.configure(text=text, text_color=WARN if warn else SUB)

    # ------------------------------------------------------ 分频点 ----

    def apply_crossover(self, quiet: bool = False):
        """读取输入框，更新分频点。改了就丢掉分频缓存，下次播放/切歌重新算。"""
        raw = self.cross_entry.get().strip()
        try:
            hz = float(raw)
        except ValueError:
            hz = None
        if hz is None or not (MIN_CROSSOVER_HZ <= hz <= MAX_CROSSOVER_HZ):
            if not quiet:
                self.set_log(f"分频点要在 {MIN_CROSSOVER_HZ}~{MAX_CROSSOVER_HZ}Hz 之间，"
                             f"已改回 {self.cross_hz:g}Hz", True)
            self.cross_entry.delete(0, "end")
            self.cross_entry.insert(0, f"{self.cross_hz:g}")
            return
        if abs(hz - self.cross_hz) < 1e-9:
            return
        was_playing = self.playing
        self.cross_hz = hz
        self.xover_delay = xover_delay_for(hz)
        self.cross_entry.delete(0, "end")
        self.cross_entry.insert(0, f"{hz:g}")
        # 分频结果与频率绑定，全部作废重算
        self.cache.clear()
        self._update_band_labels()
        tag = f"{hz:g}Hz"
        if was_playing:                 # 播放中改分频：自动重启，用新分频点继续
            pos = self.seek_frame
            self.stop(reset=False)
            self.seek_frame = pos
            self.play()
            self.set_log(f"分频点已改为 {tag}，正在用新分频重新播放…")
        else:
            if self.selected_song() is not None:
                threading.Thread(target=self._preload,
                                 args=(self.selected_song(),), daemon=True).start()
            self.set_log(f"分频点已改为 {tag}：低频机播放小于 {tag} 的部分，"
                         f"高频机播放大于 {tag} 的部分")

    def _update_band_labels(self):
        hz = f"{self.cross_hz:g}"
        self.m_low.sub_label.configure(text=f"< {hz}Hz")
        self.m_high.sub_label.configure(text=f"> {hz}Hz")
        self.cross_hint.configure(
            text=f"整首一次解码 · 线性相位 FIR 在 {hz}Hz 分频 · 低频/高频各走一台机器 · 可拖拽跳转")
        for r in self.rows:                 # 列表里的分频徽章也要跟着变
            r.set_split(self.cross_hz)

    # ------------------------------------------------------ MV 画面 ----

    def _load_mv_preview(self):
        """选中曲目后：取 MV 首帧做静帧预览（全屏开着就一起刷新）。"""
        path = self.mv_path
        if path is None:
            self.video_label.configure(image="", text="未找到同名 MV\n(mv 文件夹)")
            self.video_label.image = None
            self.mv_photo = None
            self.preview_raw = None
            self.mv_state.configure(text="—")
            if self.fs_label is not None:
                self.fs_label.configure(image="", text="未找到同名 MV\n(mv 文件夹)")
                self.fs_label.image = None
                self.fs_photo = None
            return
        self.video_label.configure(image="", text="正在读取 MV…")
        self.video_label.image = None
        self.preview_raw = None
        self.mv_state.configure(text=path.name, text_color=FAINT)
        self._sync_preview_size()             # 先对齐真实框尺寸，再解这一帧
        if self._box_ready():
            self._grab_still(0.0, self.preview_size)
        else:
            # 窗口还没布局完，量不到框尺寸 —— 交给 _resync_preview 补这一帧
            self.after(120, self._resync_preview)
        if self.fs_win is not None and self.fs_size:      # 全屏开着 → 同步换这张 MV
            self._grab_still(0.0, self.fs_size, to_fs=True)

    def _box_ready(self) -> bool:
        """预览框是否已经真实布局出尺寸（区别于初始占位值）。"""
        try:
            return self.video_box.winfo_width() >= 16 and self.video_box.winfo_height() >= 16
        except Exception:
            return False

    def _sync_preview_size(self):
        """按预览框的真实尺寸校正 preview_size（不做解码，只对齐数字）。"""
        if not self._box_ready():
            return
        self.preview_size = decode_size_for(self.video_box.winfo_width(),
                                            self.video_box.winfo_height())

    def _grab_still(self, at_sec: float, size, to_fs: bool = False):
        """异步取一帧静帧；to_fs=True 时画到全屏窗口，否则画到小窗。

        解码是异步的，期间窗口可能又被拖动/重排导致目标尺寸变化。
        落地要同时满足两个条件才采纳：序号是最新的、且尺寸仍等于当前预览尺寸。
        否则旧尺寸的结果后到，会把新画面覆盖回去（切主题重建界面时尤其明显）。
        """
        path = self.mv_path
        if path is None or not HAS_PIL:
            return
        if to_fs:
            seq = None
        else:
            self._still_seq += 1
            seq = self._still_seq

        def work():
            try:
                data = grab_frame(path, at_sec, size)
            except Exception:
                data = None
            if to_fs:
                self.ui(lambda: self._show_fs(data, size))
                return

            def land():
                if seq != self._still_seq:         # 已被更新的请求取代
                    return
                self._sync_preview_size()          # 按真实框尺寸复核
                if size == self.preview_size:
                    self._show_embedded(data, size)
            self.ui(land)
        threading.Thread(target=work, daemon=True).start()

    # ---- 预览框尺寸自适应（让画面真正铺满，不留黑边）----

    def _on_video_box_resize(self, event):
        """预览框尺寸变化 → 按框内可用的 16:9 区域重算解码尺寸。

        关键：ffmpeg 的 pad 是按**解码尺寸**补黑边的，所以只把旧帧放大到别的
        长宽比，黑边依旧存在。必须按框中可用区域的长宽比重新解码，画面才铺满。
        """
        if event.width < 16 or event.height < 16:
            return
        size = decode_size_for(event.width, event.height)
        if size == self.preview_size:
            return
        self.preview_size = size
        self._still_seq += 1                  # 让还在路上的旧尺寸帧失效
        self._refit_preview()                 # 先本地缩放顶一下，避免闪白
        # 正在播放时，解码尺寸由 video.size 决定；这里同步过去即可
        if self.mv_active and self.playing:
            self.video.set_size(size)
            return
        # 静止帧：防抖后按新尺寸重新解码一张（拖动窗口时不会疯狂调 ffmpeg）
        if self.mv_path is None or not HAS_PIL:
            return
        if self._preview_after is not None:
            try:
                self.after_cancel(self._preview_after)
            except Exception:
                pass
        self._preview_after = self.after(200, self._redecode_preview)

    def _redecode_preview(self):
        self._preview_after = None
        if self.mv_active and self.playing:
            return
        if self.mv_path is None:
            return
        # 重新按「当前真实框尺寸」算一次，别用被重置过的 preview_size
        try:
            size = decode_size_for(self.video_box.winfo_width(),
                                   self.video_box.winfo_height())
        except Exception:
            size = self.preview_size
        self.preview_size = size
        self._grab_still(self.seek_frame / SR, size)

    def _refit_preview(self):
        """把当前这帧按预览框尺寸重新缩放显示 —— 纯本地缩放，无需解码。"""
        if self.preview_raw is None or not HAS_PIL:
            return
        try:
            img = Image.frombytes("RGB", self.preview_raw[0], self.preview_raw[1])
            if img.size != self.preview_size:
                img = img.resize(self.preview_size, Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return
        self.mv_photo = photo
        self.video_label.configure(image=photo, text="", width=self.preview_size[0],
                                   height=self.preview_size[1])
        self.video_label.image = photo

    def _show_embedded(self, data, size, placeholder="MV 无法解码"):
        if data is None or not HAS_PIL:
            self.video_label.configure(image="", text=placeholder)
            self.video_label.image = None
            self.mv_photo = None
            self.preview_raw = None
            return
        self.preview_raw = (size, data)          # 原始解码帧，供尺寸变化时重新缩放
        self._refit_preview()

    def _show_fs(self, data, size):
        if data is None or not HAS_PIL or self.fs_label is None:
            return
        try:
            photo = ImageTk.PhotoImage(Image.frombytes("RGB", size, data))
        except Exception as e:
            self.fs_label.configure(image="", text=f"画面显示失败\n{e}")
            return
        self.fs_photo = photo                      # 必须先留引用，否则图会被回收
        # 把 label 钉成图像本身的尺寸再贴图。全屏窗口是铺满屏幕的，label 如果
        # 保持 fill="both" 的实际大小，Tk 会按 label 尺寸拉伸贴图 —— 画面就会
        # 变形或只露出一角。显式对齐尺寸后画面才是原样居中的。
        self.fs_label.configure(image=photo, text="", width=size[0], height=size[1])
        self.fs_label.image = photo

    # ---- 全屏 ----

    def toggle_fullscreen(self):
        if self.fs_win is not None:
            self._close_fs()
        else:
            self._open_fs()

    def _phys_screen(self):
        """物理像素的屏幕尺寸。DPI 感知成功时 winfo_screen 就是物理值；
        否则（老系统/设置失败）用 ctk 的缩放系数把虚拟值换算回物理值。"""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if not DPI_AWARE:
            try:
                s = ctk.ScalingTracker.get_window_scaling(self)
            except Exception:
                s = 1.0
            if s and s > 0:
                sw, sh = int(sw * s), int(sh * s)
        return sw, sh

    def _screen_size(self):
        """全屏解码尺寸：按物理屏幕的 16:9 满分辨率解码，不做任何上限压缩。

        这里绝不能用小窗那套 960px 上限 —— 那样全屏只是把小图拉大，
        画面会明显发虚。全屏本来就只有一路画面，解码成本可以接受。
        """
        sw, sh = self._phys_screen()
        return decode_size_for(sw, sh, cap=0)

    def _open_fs(self):
        if not HAS_PIL:
            self.set_log("缺少 Pillow，无法显示画面", True)
            return
        if self.mv_path is None:
            self.set_log(f"这首没有同名 MV（放入 {DIR_MV} 即可自动识别）", True)
            return
        sw, sh = self._phys_screen()
        size = self._screen_size()
        self.fs_size = size
        win = tk.Toplevel(self)
        self.fs_win = win
        win.configure(bg="#000000")
        win.title("MV 全屏")
        # 显式给物理像素的几何尺寸，双保险（部分 DPI 配置下 -fullscreen 不铺满）
        win.geometry(f"{sw}x{sh}+0+0")
        try:
            win.attributes("-fullscreen", True)
        except Exception:
            win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        self.fs_label = tk.Label(win, bg="#000000", fg="#8892A0", font=(FONT, 14),
                                 bd=0, highlightthickness=0, text="正在载入画面…")
        # 居中而非铺满：贴图时会把 label 钉成图像尺寸，铺满的话会被反复拉伸。
        self.fs_label.pack(expand=True)
        win.bind("<Escape>", lambda e: self._close_fs())
        win.bind("<F11>", lambda e: self._close_fs())
        win.bind("<Double-Button-1>", lambda e: self._close_fs())
        win.bind("<space>", lambda e: self.toggle_play())
        win.protocol("WM_DELETE_WINDOW", self._close_fs)
        win.lift()
        win.focus_force()
        self.btn_fs.configure(text="退出全屏", width=68)
        self.video.set_size(size)
        if self.mv_active and self.playing:
            self.video.start(self.mv_path, self.video_clock())
            self.set_log(f"MV 全屏播放中（Esc / 双击退出）：{self.mv_path.name}")
        else:
            self._grab_still(self.seek_frame / SR, size, to_fs=True)
            self.set_log("MV 全屏（当前未播放，显示静帧。Esc / 双击退出）")

    def _close_fs(self):
        if self.fs_win is None:
            return
        try:
            self.fs_win.destroy()
        except Exception:
            pass
        self.fs_win = None
        self.fs_label = None
        self.fs_photo = None
        self.fs_size = None
        self.btn_fs.configure(text="全屏", width=56)
        self.video.set_size(self.preview_size)     # 回到小窗按预览框实际尺寸解码
        show = self.mv_var.get() and self.mv_path is not None
        if show and self.mv_active and self.playing:
            self.video.start(self.mv_path, self.video_clock())
            self.set_log("已退出全屏，回到小窗同步")
        elif show:
            self._grab_still(self.seek_frame / SR, self.preview_size)
            self.set_log("已退出全屏")
        else:
            self.set_log("已退出全屏")

    def _on_mv_toggle(self):
        if not self.mv_var.get():
            self._stop_mv()
            if self.fs_win is not None:      # 关掉 MV 时也退出全屏
                self._close_fs()
            self.set_log("已关闭 MV 画面，只播放声音")
            self._load_mv_preview()
            return
        if self.mv_path is None:
            self.set_log(f"这首没有同名 MV（放入 {DIR_MV} 即可自动识别）", True)
            return
        if self.playing:
            self._start_mv()
            self.set_log(f"已打开 MV：{self.mv_path.name}")
        else:
            self.set_log("已勾选 MV，播放时同步显示")
            self._load_mv_preview()

    def _start_mv(self):
        if self.mv_path is None or not self.mv_var.get():
            return
        self.video.set_size(self.preview_size)     # 小窗按预览框实际尺寸解码，画面铺满
        self.video.start(self.mv_path, self.seek_frame / SR)
        self.mv_active = True
        self.mv_state.configure(text=f"{self.mv_path.name}（同步中）", text_color=ACCENT)

    def _stop_mv(self):
        self.video.stop()
        self.mv_active = False
        if self.mv_path is not None:
            self.mv_state.configure(text=self.mv_path.name, text_color=FAINT)

    def video_clock(self) -> float:
        """当前音频位置（秒），供画面线程对时。"""
        if self.mv_active and self.playing:
            return self.seek_frame / SR + max(0.0, time.perf_counter() - self.tick_perf)
        return self.seek_frame / SR

    def _video_tick(self):
        if self.mv_active and HAS_PIL:
            frame = self.video.latest(self.video_clock())
            if frame:
                size, ts, data = frame
                self.mv_last_ts = ts
                if self.fs_win is not None:      # 全屏时只画全屏，省一半开销
                    self._show_fs(data, size)
                else:
                    self._show_embedded(data, size)
        self.after(33, self._video_tick)

    # ------------------------------------------------------ 预载分频 ----

    def _preload(self, song: Song):
        """解码首帧 + 后台完成分频，之后播放才能真正瞬间开始。"""
        if song.path is None:
            return
        try:
            data = self._get_bands(song.path)[0]      # (data, lp, hp)
        except Exception as e:
            self.ui(lambda e=e: self.set_log(f"解码失败: {e}", True))
            return
        total = data.shape[0]
        if total == 0:
            return

        def apply():
            if self.selected_song() is song:
                self.total_frames = total
                self.slider.configure(to=max(1, total))
                self.slider.set(min(total, self.seek_frame))
                self.time_total.configure(text=fmt_time(total))
                self.time_now.configure(text=fmt_time(self.seek_frame))
        self.ui(apply)

    def _get_bands(self, path: Path):
        """返回 (原始, 低通段, 高通段)。带缓存，分频在后台线程完成。

        缓存键包含当前分频点：改频率后自动按新分频点重算。
        """
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, self.cross_hz)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if len(self.cache) > 1:                      # 一首 300s 歌约 300MB，只留最近一首
            self.cache.clear()
        data = decode_to_np(path)
        if data.shape[0] >= 8:
            lp, hp = split_bands(data, self.cross_hz)
        else:
            lp = hp = data
        entry = (data, lp, hp)
        self.cache[key] = entry
        return entry

    # ---------------------------------------------------------- 播放 ----

    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.busy:
            return
        song = self.selected_song()
        if song is None:
            self.set_log("请先在曲目列表里选一首", True)
            return
        if song.path is None:
            return
        self._halt()
        self.busy = True
        self.play_token += 1
        token = self.play_token
        self.btn_play.configure(state="disabled", text="准备中…")
        self.set_log("正在解码 / 分频 / 打开设备…")
        for m in self.machines:
            m.set_status("准备中…")
        jobs = []
        for m in self.machines:
            dev_id, dev_name = m.device_choice()
            jobs.append((m, dev_id, dev_name, m.get_delay()))
        threading.Thread(target=self._worker, args=(song, jobs, self.seek_frame, token),
                         daemon=True).start()

    @staticmethod
    def _open_device(machine: "Machine", data: "np.ndarray"):
        """(device, samples, sample_type, 是否回退到默认设备)。

        音质优先：首选 float32 输出（不动原采样），失败才退 int16。
        尽量用用户指定的设备，失败才退默认设备。
        """
        dev_id, _ = machine.device_choice()
        tries = [dev_id] if dev_id is not None else []
        tries.append(None)
        last_err = None
        for did in tries:
            for fmt, stype in ((F32, "f"), (S16, "h")):
                try:
                    dev = miniaudio.PlaybackDevice(
                        output_format=fmt, nchannels=NCH, sample_rate=SR,
                        buffersize_msec=100, device_id=did)
                    return dev, data, stype, (dev_id is not None and did is None)
                except Exception as e:
                    last_err = e
        raise RuntimeError(f"打开设备失败: {type(last_err).__name__}: {last_err}")

    def _worker(self, song: Song, jobs, start_frame: int, token: int):
        # 1) 一次解码 + 一次分频，两段都保留 float32
        try:
            data, lp, hp = self._get_bands(song.path)
        except Exception as e:
            self.ui(lambda e=e: self.set_log(f"解码失败: {e}", True))
            self.ui(lambda: self._after_start(0))
            return
        if data.shape[0] < 8:
            self.ui(lambda: self.set_log("音频太短，无法分频", True))
            self.ui(lambda: self._after_start(0))
            return
        total = data.shape[0]

        prepared = []
        for m, dev_id, dev_name, delay_ms in jobs:
            if token != self.play_token:
                return
            band = lp if m.band == "low" else hp
            try:
                dev, samples, stype, fallback = self._open_device(m, band)
            except Exception as e:
                self.ui(lambda m=m, e=e: m.set_status(str(e), ERR))
                continue
            # 线性相位 FIR 让两段都有固定群延迟 (taps-1)/2：起点整体后移，
            # 两机一起后移 -> 相互错位不变，而声音与 MV 画面仍对齐。
            offset = start_frame + self.xover_delay
            state = {"finished": False, "out": 0, "audio": start_frame, "offset": offset}
            gen = make_generator(samples, int(delay_ms / 1000 * SR), state, stype)
            next(gen)
            prepared.append((m, dev, gen, state, samples, dev_name, fallback, delay_ms))
        if token != self.play_token:
            for p in prepared:
                try:
                    p[1].close()
                except Exception:
                    pass
            return
        # 两台设备紧挨着启动，减小起始偏差
        for m, dev, gen, state, samples, dev_name, fallback, delay_ms in prepared:
            try:
                dev.start(gen)
            except Exception as e:
                self.ui(lambda m=m, e=e: m.set_status(f"启动失败: {e}", ERR))
                continue
            m.device = dev
            m.state = state
            m.total_frames = total
            m.playing = True
            m.used_device = f"{DEFAULT_LABEL}(回退)" if fallback else dev_name
        for m, dev, gen, state, samples, dev_name, fallback, delay_ms in prepared:
            if not m.playing:
                continue
            used_fmt = "float32" if samples.dtype == np.float32 else "int16"
            band_txt = f"< {self.cross_hz:g}Hz" if m.band == "low" else f"> {self.cross_hz:g}Hz"
            txt = f"播放中 · {band_txt} · {m.used_device} · {used_fmt}"
            if delay_ms:
                txt += f" · 延迟 {delay_ms}ms"
            self.ui(lambda m=m, t=txt: m.set_status(t, TEXT))
            self.ui(lambda m=m: m.set_playing_dot(True))
        self.ui(lambda n=len([p for p in prepared if p[0].playing]): self._after_start(n))

    def _after_start(self, started: int):
        self.busy = False
        self.btn_play.configure(state="normal")
        if started == 0:
            self.btn_play.configure(text="播 放")
            self.set_log("没有任何机器成功开始播放，请看右侧机器状态里的错误信息", True)
            return
        self.playing = True
        self.tick_perf = time.perf_counter()
        self.btn_play.configure(text="暂 停")
        parts = [f"{m.title} → {m.used_device}" for m in self.machines if m.playing]
        self.set_log(f"播放中（{self.cross_hz:g}Hz 分频，"
                     f"线性相位延迟 {self.xover_delay / SR * 1000:.0f}ms）：" + "　|　".join(parts))
        self._start_mv()
        self._tick()

    def pause(self):
        self._halt()
        self.btn_play.configure(text="播 放")
        self.set_log(f"已暂停于 {fmt_time(self.seek_frame)}，再点播放从此处继续")

    def stop(self, reset: bool = True):
        self._halt()
        self.btn_play.configure(text="播 放")
        if reset:
            self.seek_frame = 0
            self.slider.set(0)
            self.time_now.configure(text="00:00")
            for m in self.machines:
                m.set_status("待播放")
            self.set_log("已停止")
            self._load_mv_preview()

    def _halt(self):
        self.play_token += 1
        self.busy = False
        self.playing = False
        for m in self.machines:
            m.stop_play()
        self._stop_mv()
        self.btn_play.configure(state="normal")
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

    def _tick(self):
        playing = [m for m in self.machines if m.playing and m.state]
        if not playing:
            return
        pos = max(m.position_frames() for m in playing)
        total = max(m.total_frames for m in playing) or 1
        self.tick_perf = time.perf_counter()
        if not self.dragging:
            self.slider.set(min(total, pos))
            self.time_now.configure(text=fmt_time(pos))
        self.time_total.configure(text=fmt_time(total))
        self.seek_frame = min(total, pos)
        for m in playing:
            if m.state["finished"] and "已播完" not in m.status_text():
                m.set_status(m.status_text() + " · 已播完")
        if all(m.state and m.state["finished"] for m in playing):
            self._halt()
            self.seek_frame = 0
            self.slider.set(0)
            self.time_now.configure(text="00:00")
            self.set_log("播放结束")
            return
        self._tick_job = self.after(100, self._tick)

    # ------------------------------------------------------ 拖动跳转 ----

    def _slider_press(self, _e=None):
        self.dragging = True

    def _on_slider_move(self, value):
        if not self.dragging:
            return
        self.time_now.configure(text=fmt_time(int(float(value))))

    def _slider_release(self, _e=None):
        self.dragging = False
        self.seek_to(int(float(self.slider.get())))

    def seek_to(self, target: int):
        if self.total_frames <= 0:
            return
        target = max(0, min(self.total_frames, target))
        self.seek_frame = target
        playing = [m for m in self.machines if m.playing and m.state]
        if playing:
            # 用统一增量跳转：两台机器之间的延迟错位保持不变
            ref = max(m.state["offset"] + m.state["out"] for m in playing)
            delta = target - ref
            for m in playing:
                m.state["offset"] += delta
                m.state["finished"] = False
            self.tick_perf = time.perf_counter()
            if self.mv_active:           # MV 画面重新从该点解码
                self.video.start(self.mv_path, target / SR)
            self.set_log(f"已跳转到 {fmt_time(target)}")
        else:
            self.set_log(f"起点已设为 {fmt_time(target)}")
            if self.mv_path is not None and self.mv_var.get():
                self._load_mv_at(target)
        self.slider.set(target)
        self.time_now.configure(text=fmt_time(target))

    def _load_mv_at(self, target: int):
        """未播放时拖动：把 MV 静止画面切到该时间点（全屏开着就一起切）。"""
        self._grab_still(target / SR, self.preview_size)
        if self.fs_win is not None and self.fs_size:
            self._grab_still(target / SR, self.fs_size, to_fs=True)

    def _on_close(self):
        self._halt()
        if self.fs_win is not None:
            try:
                self.fs_win.destroy()
            except Exception:
                pass
            self.fs_win = None
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
