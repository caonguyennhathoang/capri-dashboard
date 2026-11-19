"""Play GIF frames on Capri's 320 × 240 SPI panel."""
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

from PIL import Image, ImageChops, ImageOps

from lcd_test import HEIGHT, WIDTH, notify_ready, open_display

# 40 MHz is an experimental setting above the ILI9341's specified write clock.
SPI_HZ = 40_000_000
FULL_FRAME_PERIOD = 1 / 24
CACHE_RAM_BYTES = 16 * 1024 * 1024
REGION_SIZE = (176, 132)
POSITIONS = {
    "center": (1, 1),
    "top-left": (0, 0),
    "top-right": (2, 0),
    "bottom-left": (0, 2),
    "bottom-right": (2, 2),
}
GIF_ROTATIONS = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def fit_frame(image, size, crop):
    if crop is None:
        return ImageOps.fit(image, size)
    x, y, zoom = crop
    width, height = image.size
    aspect = size[0] / size[1]
    crop_width = min(width, height * aspect) / zoom
    crop_height = crop_width / aspect
    left = (width - crop_width) * x
    top = (height - crop_height) * y
    return image.resize(size, Image.Resampling.BICUBIC,
                        box=(left, top, min(width, left + crop_width),
                             min(height, top + crop_height)))


def play(path):
    mode_path = path.with_suffix(".mode")
    settings = mode_path.read_text().strip() if mode_path.is_file() else "full"
    mode, _, options = settings.partition(":")
    position, _, display_options = options.partition(":")
    rotation, _, gif_options = display_options.partition(":")
    crop_option, _, angle_option = gif_options.partition(":")
    crop = tuple(map(float, crop_option.split(","))) if crop_option else None
    gif_rotation = GIF_ROTATIONS.get(int(angle_option or "0"))
    upside_down = rotation != "0"
    size = REGION_SIZE if mode == "region" else (WIDTH, HEIGHT)
    minimum_period = 0.02 if mode == "region" else FULL_FRAME_PERIOD
    x, y = POSITIONS.get(position, POSITIONS["center"])
    offset = ((WIDTH - size[0]) * x // 2, (HEIGHT - size[1]) * y // 2)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    sent = 0
    first_sent = last_sent = None
    frames = []
    first = previous = None
    cache_dir = Path.home() / ".cache" / "capri-display"
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.SpooledTemporaryFile(max_size=CACHE_RAM_BYTES, dir=cache_dir) as cache, \
            open_display(SPI_HZ, False, "rgb565", False, wide_window=True,
                         stream_pixels=True) as (prepare, send):
        deadline = time.monotonic()

        def present(period, region, pixels):
            nonlocal deadline, sent, first_sent, last_sent
            start = time.monotonic()
            if region:
                send(region, pixels)
                sent += 1
                last_sent = time.monotonic()
                if first_sent is None:
                    first_sent = last_sent
                    notify_ready()
            deadline = max(deadline + period, start)
            stopped.wait(deadline - time.monotonic())

        try:
            with Image.open(path, formats=("GIF",)) as gif:
                frame = 0
                while not stopped.is_set():
                    try:
                        gif.seek(frame)
                    except EOFError:
                        break
                    period = max(minimum_period, gif.info.get("duration", 100) / 1000)
                    frame_image = gif.convert("RGB")
                    if gif_rotation is not None:
                        frame_image = frame_image.transpose(gif_rotation)
                    content = fit_frame(frame_image, size, crop)
                    if mode == "region":
                        image = Image.new("RGB", (WIDTH, HEIGHT))
                        image.paste(content, offset)
                    else:
                        image = content
                    if upside_down:
                        image = image.transpose(Image.Transpose.ROTATE_180)
                    if first is None:
                        first = image
                        region, pixels = prepare(image)
                    else:
                        change = ImageChops.difference(previous, image).getbbox()
                        region, pixels = prepare(image, change) if change else (None, b"")
                    frames.append((period, region, cache.tell(), len(pixels)))
                    cache.write(pixels)
                    present(period, region, pixels)
                    previous = image
                    frame += 1
            if stopped.is_set():
                return
            # Once through the GIF, only the transition from the last frame to
            # the first differs from the data already sent and cached.
            change = ImageChops.difference(previous, first).getbbox()
            region, pixels = prepare(first, change) if change else (None, b"")
            frames[0] = (frames[0][0], region, cache.tell(), len(pixels))
            cache.write(pixels)
            print(f"Prepared {len(frames)} frames ({cache.tell()} RGB565 bytes).", flush=True)
            del first, previous, frame_image, image, content, pixels
            if not any(entry[1] for entry in frames):
                stopped.wait()
                return
            while not stopped.is_set():
                for period, region, offset, length in frames:
                    if stopped.is_set():
                        break
                    pixels = b""
                    if region:
                        cache.seek(offset)
                        pixels = cache.read(length)
                    present(period, region, pixels)
        finally:
            rate = (sent - 1) / (last_sent - first_sent) if sent > 1 else 0
            print(f"Sent {sent} GIF frames over SPI at {rate:.1f} updates/s.", flush=True)


if __name__ == "__main__":
    play(Path(sys.argv[1]))
