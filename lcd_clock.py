"""Render Capri's glanceable TFT screens and publish the latest preview."""
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event
from urllib.request import urlopen

from PIL import Image, ImageChops, ImageDraw, ImageFont

from lcd_player import SPI_HZ
from lcd_test import HEIGHT, WIDTH, notify_ready, open_display

BG, LINE = "#171120", "#44314e"
INK, MUTED = "#fbf2ff", "#c0adca"
PINK, LIME = "#ff73bc", "#cbf376"
SURFACE = "#261a30"
WEEKDAYS = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật")


@lru_cache
def font(size, bold=False):
    suffix = "-Bold" if bold else ""
    for path in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf",
                 f"DejaVuSans{suffix}.ttf", "arialbd.ttf" if bold else "arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def label(draw, at, text, size, color=INK, bold=False, anchor="lt", width=284):
    face = font(size, bold)
    if draw.textlength(text, font=face) > width:
        while text and draw.textlength(text + "…", font=face) > width:
            text = text[:-1]
        text += "…"
    draw.text(at, text, fill=color, font=face, anchor=anchor)


def screen_image(title, now):
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.polygon([(0, 0), (64, 0), (43, 5), (0, 5)], fill=PINK)
    label(draw, (18, 17), title, 18, bold=True)
    draw.rounded_rectangle((247, 14, 302, 36), radius=5, fill=SURFACE)
    label(draw, (274, 25), now.strftime("%H:%M"), 12, LIME, anchor="mm")
    draw.line((18, 45, 302, 45), fill=LINE)
    return image, draw


def clock_image(now):
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.polygon([(0, 0), (82, 0), (54, 6), (0, 6)], fill=PINK)
    label(draw, (18, 23), "Capri", 20, PINK, bold=True)
    draw.rounded_rectangle((190, 15, 302, 45), radius=6, fill=SURFACE)
    label(draw, (246, 30), WEEKDAYS[now.weekday()], 14, LIME, bold=True, anchor="mm")
    draw.line((18, 59, 302, 59), fill=LINE)
    label(draw, (160, 116), now.strftime("%H:%M"), 78, bold=True, anchor="mm")
    draw.polygon([(18, 176), (302, 176), (302, 207), (284, 223), (18, 223)], fill=SURFACE)
    draw.line((31, 188, 31, 211), fill=PINK, width=3)
    label(draw, (45, 199), now.strftime("%d/%m/%Y"), 24, PINK, bold=True, anchor="lm")
    return image


def storage_name():
    device = Path("/").stat().st_dev
    block = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}").resolve().name
    if block.startswith("mmcblk"):
        return "microSD"
    if block.startswith("nvme"):
        return "SSD"
    return "Ổ hệ thống"


def system_image(now, data, disk):
    image, draw = screen_image("Hệ thống", now)
    metrics = data.get("metrics") or {}
    health = data.get("health") or {}
    cpu, ram, total, temp = (metrics.get(key) for key in ("cpu", "ram", "ram_total", "temp"))
    free, capacity = health.get("free"), health.get("total")
    entries = (
        ("CPU", f"{cpu:.1f}%" if cpu is not None else "—", cpu, PINK),
        ("RAM", f"{ram / 1024:.0f} MiB" if ram is not None else "—", 100 * ram / total if total and ram is not None else None, LIME),
        ("Nhiệt độ CPU", f"{temp:.1f} °C" if temp is not None else "—", temp, "#ffc18e"),
        (f"{disk} còn trống", f"{free / 2**30:.1f} GiB" if free is not None else "—",
         100 * free / capacity if capacity and free is not None else None, LIME),
    )
    for row, (name, value, percent, color) in enumerate(entries):
        y = 57 + 43 * row
        label(draw, (18, y), name, 14, MUTED, width=158)
        label(draw, (302, y - 2), value, 18, bold=True, anchor="rt", width=121)
        draw.line((18, y + 25, 302, y + 25), fill=LINE, width=2)
        if percent is not None and percent > 0:
            draw.line((18, y + 25, 18 + int(284 * min(percent, 100) / 100), y + 25),
                      fill=color, width=2)
    return image


def wifi_image(now, data):
    image, draw = screen_image("Wi-Fi", now)
    wifi = data.get("wifi") or {}
    ssid = wifi.get("ssid")
    draw.rounded_rectangle((18, 59, 302, 134), radius=8, fill=SURFACE)
    label(draw, (31, 71), "Đang kết nối" if ssid else "Chưa kết nối", 14, LIME if ssid else PINK)
    label(draw, (31, 97), ssid or "—", 25, bold=True, width=258)
    draw.line((18, 150, 302, 150), fill=LINE)
    label(draw, (18, 166), "Địa chỉ IPv4", 14, MUTED)
    label(draw, (18, 191), wifi.get("ip") or "—", 24)
    return image


def anthem_image(now, data):
    image, draw = screen_image("Capri Anthem", now)
    state = data.get("state")
    labels = {"active": "Đang chạy", "inactive": "Đã dừng", "failed": "Gặp lỗi",
              "activating": "Đang bật", "deactivating": "Đang dừng"}
    accent = LIME if state == "active" else PINK
    draw.rounded_rectangle((18, 59, 302, 134), radius=8, fill=SURFACE)
    draw.ellipse((31, 89, 41, 99), fill=accent)
    label(draw, (54, 95), labels.get(state, "Chưa xác định"), 26,
          accent, bold=True, anchor="lm", width=236)
    draw.line((18, 150, 302, 150), fill=LINE)
    label(draw, (18, 166), "Thời gian chạy", 14, MUTED)
    minutes = data["uptime"] // 60 if data.get("uptime") is not None else None
    elapsed = f"{minutes // 60} giờ {minutes % 60:02d} phút" if minutes is not None else "—"
    label(draw, (18, 191), elapsed, 24)
    return image


def dashboard_data(route):
    try:
        with urlopen(f"http://127.0.0.1:8765/api/{route}", timeout=2.5) as response:
            return json.load(response)
    except (OSError, ValueError):
        return {}


def run(config_path):
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        settings = config_path.read_text().strip()
    except FileNotFoundError:
        settings = "clock:180"
    screen, _, rotation = settings.partition(":")
    if screen in ("0", "180"):
        screen, rotation = "clock", screen
    disk = storage_name()
    preview = config_path.with_suffix(".png")
    pending = preview.with_suffix(".tmp")
    previous = None
    with open_display(SPI_HZ, False, "rgb565", False, wide_window=True,
                      stream_pixels=True) as (prepare, send):
        while not stopped.is_set():
            now = datetime.now()
            if screen == "system":
                image = system_image(now, dashboard_data("display/telemetry"), disk)
            elif screen == "wifi":
                image = wifi_image(now, dashboard_data("display/telemetry"))
            elif screen == "anthem":
                image = anthem_image(now, dashboard_data("anthem"))
            else:
                image = clock_image(now)
            physical = image if rotation == "0" else image.transpose(Image.Transpose.ROTATE_180)
            region = ImageChops.difference(previous, physical).getbbox() if previous is not None else None
            if previous is None or region:
                send(*prepare(physical, region))
                image.save(pending, format="PNG")
                pending.replace(preview)
                if previous is None:
                    notify_ready()
                    print(f"LCD screen ready: {screen}.", flush=True)
                previous = physical
            if screen == "clock":
                current = datetime.now()
                if current.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                    stopped.wait(60 - current.second - current.microsecond / 1_000_000)
            else:
                stopped.wait(2 if screen == "system" else 5)


if __name__ == "__main__":
    run(Path(sys.argv[1]))
