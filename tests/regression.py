r"""岔音 Xover —— 无窗口回归测试：检查布局 / 配色 / 分频 / 播放。

运行（在项目根目录）:
    python tests/regression.py
"""
import importlib.util, re, sys, time, tkinter as tk
from importlib.machinery import SourceFileLoader
from pathlib import Path

# 屏蔽全屏窗口，免得测试时弹出来抢焦点
_real = tk.Toplevel.attributes
tk.Toplevel.attributes = lambda s, *a, **k: None if (a and a[0] == "-fullscreen") else _real(s, *a, **k)
tk.Toplevel.focus_force = lambda s: None

# 主程序是 .pyw，标准加载器认不出扩展名，这里显式用 SourceFileLoader
MAIN = Path(__file__).resolve().parent.parent / "xover.pyw"
spec = importlib.util.spec_from_loader("xover", SourceFileLoader("xover", str(MAIN)))
P = importlib.util.module_from_spec(spec)
sys.modules["xover"] = P
spec.loader.exec_module(P)
app = P.App()
for _ in range(40):
    app.update(); time.sleep(0.03)

ok = []


def chk(n, c, e=""):
    ok.append(bool(c))
    print(("  [OK] " if c else "  [!!] ") + n, e)


def settle(sec):
    """跑 mainloop 若干秒（用于等重绘/几何生效）。"""
    t0 = time.time()
    while time.time() - t0 < sec:
        app.update(); time.sleep(0.03)


def wait_for(cond, timeout):
    """轮询等待条件成立，返回是否成立。比固定 sleep 稳。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        app.update(); time.sleep(0.05)
    return cond()


def scale():
    try:
        s = P.ctk.ScalingTracker.get_window_scaling(app)
        return s if s and s > 0 else 1.0
    except Exception:
        return 1.0


def hexes(t):
    """主题里除 name/appearance 外的所有 #RRGGBB 颜色值。"""
    return {k: v for k, v in t.items()
            if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v)}


print("A. 布局")
sc = scale()
w_logical, h_logical = app.winfo_width() / sc, app.winfo_height() / sc
chk("默认窗口为 800x600", abs(w_logical - 800) <= 2 and abs(h_logical - 600) <= 2,
    f"{w_logical:.0f}x{h_logical:.0f} 逻辑px (缩放 {sc:.2f})")
chk("列表高度充足(>=250)", app.list_area.winfo_height() / sc >= 250,
    f"{app.list_area.winfo_height()/sc:.0f}px")
chk("MV 框按 16:9 铺满", abs(app.preview_size[0] / app.preview_size[1] - 16 / 9) < 0.02,
    f"{app.preview_size}")
chk("MV 解码尺寸不超过 960", app.preview_size[0] <= 960, str(app.preview_size))

print("B. 配色（黑白双主题）")
chk("启动主题合法", P.THEME_NAME in ("light", "dark"), P.THEME_NAME)
bad = []
for name in ("dark", "light"):
    for k, v in hexes(P.THEMES[name]).items():
        if k in ("WARN", "ERR"):      # 警告/报错故意留一点色相，好被一眼看到
            continue
        r, g, b = int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)
        if not (r == g == b):
            bad.append(f"{name}.{k}={v}")
chk("调色板全为纯灰阶(R==G==B)", not bad, ", ".join(bad) or "全部通过")
old_bg = P.BG
app.toggle_theme()
settle(0.6)
chk("切换后主题变了", P.BG != old_bg, f"{old_bg} -> {P.BG}")
chk("切换后窗口根色同步", str(app.cget("fg_color")) == P.BG, str(app.cget("fg_color")))
chk("切换后列表仍完整", len(app.rows) == len(app.songs) and len(app.songs) > 0,
    f"{len(app.rows)}/{len(app.songs)}")
chk("切换后分频徽章正确", app.rows[0].b_split.cget("text") == f"{app.cross_hz:g}Hz 分频",
    app.rows[0].b_split.cget("text"))
chk("切换后进度条新配色", str(app.slider.cget("progress_color")) == P.ACCENT,
    f"{app.slider.cget('progress_color')} vs {P.ACCENT}")
app.toggle_theme()
settle(0.6)
chk("切回原主题", P.BG == old_bg, P.BG)

print("C. MV 配对（模糊匹配）")
n_mv = sum(1 for s in app.songs if s.mv is not None)
chk("全部配上 MV", n_mv == len(app.songs) and len(app.songs) > 0,
    f"{n_mv}/{len(app.songs)}")
chk("无中生有不误配", app._match_mv("完全不相关的曲名xyz") is None)
first = app.songs[0].stem
chk("精确/包含匹配可用", app._match_mv(first) is not None, first)

print("D. 预览铺满")
app._select_index(0)


def preview_ok():
    """解码尺寸与当前预览尺寸一致且都已落地。"""
    return (app.preview_raw is not None and app.preview_raw[0] == app.preview_size
            and app.mv_photo is not None)


# 窗口尺寸在切换主题后会短暂过渡，等到真正稳定再判定
if not wait_for(preview_ok, 25):
    settle(1.5)
chk("静帧解码尺寸 == 预览尺寸", preview_ok(),
    f"{app.preview_raw[0] if app.preview_raw else None} vs {app.preview_size}")
ph = app.mv_photo
chk("贴图尺寸 == 预览尺寸",
    ph is not None and (ph.width(), ph.height()) == app.preview_size,
    f"{(ph.width(), ph.height()) if ph else None}")

print("E. 缩放窗口后重适配")
old_size = app.preview_size
app.geometry("1240x840")
settle(1.5)
wait_for(lambda: app.preview_size != old_size, 10)
chk("预览尺寸随窗口变大", app.preview_size != old_size, f"{old_size} -> {app.preview_size}")
chk("画面确实变大", app.preview_size[0] > old_size[0], f"{old_size[0]} -> {app.preview_size[0]}")
wait_for(lambda: app.preview_raw is not None and app.preview_raw[0] == app.preview_size, 20)
chk("重解码尺寸与预览对齐",
    app.preview_raw is not None and app.preview_raw[0] == app.preview_size,
    f"{app.preview_raw[0] if app.preview_raw else None} vs {app.preview_size}")
chk("列表仍足够高", app.list_area.winfo_height() / sc >= 250,
    f"{app.list_area.winfo_height()/sc:.0f}px")

print("F. 分频/播放未回归")
chk("默认 200Hz", app.cross_hz == 200.0)
app.cross_entry.delete(0, "end"); app.cross_entry.insert(0, "1000")
app.apply_crossover()
chk("改 1000Hz", app.cross_hz == 1000.0 and app.xover_delay == P.xover_delay_for(1000))
chk("列表徽章同步 1000Hz", app.rows[0].b_split.cget("text") == "1000Hz 分频",
    app.rows[0].b_split.cget("text"))
chk("机器卡同步 1000Hz", app.m_low.sub_label.cget("text") == "< 1000Hz",
    app.m_low.sub_label.cget("text"))
app.cross_entry.delete(0, "end"); app.cross_entry.insert(0, "200")
app.apply_crossover()
wait_for(lambda: app.total_frames > 0, 40)
chk("预载完成", app.total_frames > 0, P.fmt_time(app.total_frames))
app.toggle_play()
wait_for(lambda: app.playing, 30)
chk("起播", app.playing)
chk("MV 已同步起播", app.mv_active)
settle(1.0)
ph2 = app.mv_photo
chk("播放中贴图仍铺满",
    ph2 is not None and (ph2.width(), ph2.height()) == app.preview_size,
    f"{(ph2.width(), ph2.height()) if ph2 else None} vs {app.preview_size}")
app.stop()
settle(0.4)
chk("停止正常", not app.playing)
app._on_close()
print(f"\n通过 {sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
