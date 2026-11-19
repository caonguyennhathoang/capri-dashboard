"""Capri's 320 x 240 ILI9341 display test on Raspberry Pi 5."""
import argparse
import array
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import struct
import sys
import time

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
except ImportError:
    np = None

WIDTH, HEIGHT = 320, 240
DC, RESET = 25, 27  # BCM numbers, physical pins 22 and 13


def notify_ready():
    endpoint = os.environ.get("NOTIFY_SOCKET")
    if endpoint:
        address = "\0" + endpoint[1:] if endpoint.startswith("@") else endpoint
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.sendto(b"READY=1", address)


def test_image():
    image = Image.new("RGB", (WIDTH, HEIGHT), "#111820")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 307, 227), radius=12, outline="#23e1ab", width=2)
    draw.text((160, 56), "CAPRI", font=ImageFont.load_default(size=44),
              anchor="mm", fill="#23e1ab")
    draw.text((160, 101), "LCD OK", font=ImageFont.load_default(size=22),
              anchor="mm", fill="white")
    for index, color in enumerate(("red", "lime", "blue", "white")):
        x = 28 + 66 * index
        draw.rectangle((x, 133, x + 63, 187), fill=color)
    draw.text((160, 208), "ILI9341  |  320 x 240", anchor="mm", fill="white")
    return image


def rgb565(image):
    if np is not None:
        rgb = np.asarray(image, dtype=np.uint16)
        red, green, blue = (rgb[:, :, channel] for channel in range(3))
        words = ((red & 248) << 8) | ((green & 252) << 3) | (blue >> 3)
        return words.astype(">u2", copy=False).tobytes()
    words = array.array("H", (((r & 248) << 8) | ((g & 252) << 3) | (b >> 3)
                              for r, g, b in image.getdata()))
    if sys.byteorder == "little":
        words.byteswap()
    return words.tobytes()


@contextmanager
def open_display(speed, flip, pixel_format, native, mark_bottom=False, wide_window=False,
                 stream_pixels=False):
    import lgpio
    import spidev

    bus = spidev.SpiDev()
    bus.open(0, 0)
    try:
        bus.mode = 0
        bus.max_speed_hz = speed
        for path in sorted(Path("/dev").glob("gpiochip*")):
            chip = lgpio.gpiochip_open(int(path.name.removeprefix("gpiochip")))
            if "rp1" in str(lgpio.gpio_get_chip_info(chip)).lower():
                break
            lgpio.gpiochip_close(chip)
        else:
            raise RuntimeError("RP1 GPIO chip not found")

        try:
            lgpio.gpio_claim_output(chip, DC, 0)
            lgpio.gpio_claim_output(chip, RESET, 1)

            def command(opcode, data=b""):
                lgpio.gpio_write(chip, DC, 0)
                bus.writebytes([opcode])
                if data:
                    lgpio.gpio_write(chip, DC, 1)
                    bus.writebytes2(data)

            lgpio.gpio_write(chip, RESET, 0)
            time.sleep(0.02)
            lgpio.gpio_write(chip, RESET, 1)
            time.sleep(0.15)
            command(0x01)  # Software reset
            time.sleep(0.15)
            # ILI9341 power, timing, and panel setup (Adafruit ILI9341 driver).
            for opcode, payload in (
                (0xEF, "03 80 02"),
                (0xCF, "00 C1 30"),
                (0xED, "64 03 12 81"),
                (0xE8, "85 00 78"),
                (0xCB, "39 2C 00 34 02"),
                (0xF7, "20"),
                (0xEA, "00 00"),
                (0xC0, "23"),
                (0xC1, "10"),
                (0xC5, "3E 28"),
                (0xC7, "86"),
                (0xB1, "00 18"),
                (0xB6, "08 82 27"),
                (0xF2, "00"),
                (0x26, "01"),
            ):
                command(opcode, bytes.fromhex(payload))
            command(0x11)  # Sleep out
            time.sleep(0.15)
            command(0x3A, bytes([0x66 if pixel_format == "rgb666" else 0x55]))
            madctl = 0x48 if native or wide_window else (0xE8 if flip else 0x28)
            command(0x36, bytes([madctl]))
            command(0x29)  # Display on
            time.sleep(0.12)

            def prepare(image, region=None):
                if native:
                    image = image.transpose(Image.Transpose.ROTATE_90 if flip else
                                            Image.Transpose.ROTATE_270)
                if wide_window:
                    red, green, blue = image.split()
                    image = Image.merge("RGB", (blue, green, red))
                    if flip:
                        image = image.transpose(Image.Transpose.ROTATE_180)
                if mark_bottom:
                    draw = ImageDraw.Draw(image)
                    marker = (240, 0) if wide_window else (0, image.height * 3 // 4)
                    draw.rectangle((*marker, image.width - 1, image.height - 1), fill=(255, 0, 255))
                left, y, right, limit = region or (0, 0, image.width, image.height)
                crop = image.crop((left, y, right, limit))
                pixels = crop.tobytes() if pixel_format == "rgb666" else rgb565(crop)
                return (left, y, right, limit), pixels

            def send(region, pixels):
                left, y, right, limit = region
                if stream_pixels:
                    # The controller retains its write pointer between spidev chunks.
                    command(0x2A, struct.pack(">HH", left, right - 1))
                    command(0x2B, struct.pack(">HH", y, limit - 1))
                    command(0x2C, pixels)
                    return
                # Keep each transfer below the usual 4096-byte spidev buffer.
                bytes_per_pixel = 3 if pixel_format == "rgb666" else 2
                row_bytes = (right - left) * bytes_per_pixel
                rows_per_band = 1 if mark_bottom else 4096 // row_bytes
                for top in range(y, limit, rows_per_band):
                    bottom = min(top + rows_per_band, limit)
                    command(0x2A, struct.pack(">HH", left, right - 1))
                    command(0x2B, struct.pack(">HH", top, bottom - 1))
                    command(0x2C, pixels[(top - y) * row_bytes:(bottom - y) * row_bytes])

            yield prepare, send
        finally:
            lgpio.gpiochip_close(chip)
    finally:
        bus.close()


def show(image, speed, flip, pixel_format, native, mark_bottom=False, wide_window=False):
    with open_display(speed, flip, pixel_format, native, mark_bottom, wide_window) as (prepare, send):
        send(*prepare(image))
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, help="Save a PNG without using GPIO")
    parser.add_argument("--speed", type=int, default=4_000_000, help="SPI clock in Hz")
    parser.add_argument("--flip", action="store_true", help="Rotate LCD output 180 degrees")
    parser.add_argument("--pixel-format", choices=("rgb565", "rgb666"), default="rgb565")
    parser.add_argument("--native", action="store_true", help="Use native 240 x 320 addressing")
    parser.add_argument("--mark-bottom", action="store_true",
                        help="Write one row at a time and mark the final quarter magenta")
    parser.add_argument("--wide-window", action="store_true",
                        help="Test a 320 x 240 window with the previous panel scan mode")
    args = parser.parse_args()
    if args.native and args.wide_window:
        parser.error("--native and --wide-window are mutually exclusive")
    image = test_image()
    if args.preview:
        image.save(args.preview)
    elif not Path("/dev/spidev0.0").exists():
        parser.exit(1, "SPI0 is disabled. Enable SPI with sudo raspi-config, then reboot.\n")
    else:
        show(image, args.speed, args.flip, args.pixel_format, args.native,
             args.mark_bottom, args.wide_window)
        addressing = "240 x 320" if args.native else "320 x 240"
        print(f"Sent test image using {addressing} addressing ({args.pixel_format}) on SPI0 CE0.")


if __name__ == "__main__":
    main()
