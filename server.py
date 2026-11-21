from contextlib import contextmanager
import errno
import hashlib
import hmac
import io
import ipaddress
import json
import os
import shutil
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".local/state/capri-dashboard/writes.json"
BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
DEVICE = Path("/sys/class/block/mmcblk0/stat")
HISTORY = deque(maxlen=900)
DETAILS = {}
PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024
CPU_COUNT = os.cpu_count() or 1
LOCK = threading.Lock()
STOP = threading.Event()
FAILED = threading.Event()
LOCAL = tuple(map(ipaddress.ip_network, ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
PAGE = (HERE / "index.html").read_bytes()
DISPLAY_UNIT = "capri-display.service"
DISPLAY_CLOCK_UNIT = "capri-display-clock.service"
DISPLAY_SCREENS = {"clock", "system", "wifi", "anthem"}
DISPLAY_MAX = 8 * 1024 * 1024
DISPLAY_LOCK = threading.Lock()
DISPLAY_STATE = {}
UPLOAD_LIMIT = 1024 ** 3
UPLOAD_LOCK = threading.Lock()


def file_name(name):
    if (not name or name in (".", "..") or "/" in name or "\0" in name
            or len(os.fsencode(name)) > 255 or name.startswith(".capri-upload-")):
        raise ValueError("Tên tệp hoặc thư mục không hợp lệ.")
    return name


@contextmanager
def file_folder(path):
    if not path.startswith("/"):
        raise ValueError("Cần đường dẫn tuyệt đối.")
    parts = [file_name(part) for part in path[1:].split("/")] if path != "/" else []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def file_entries(fd, show_hidden):
    with os.scandir(fd) as entries:
        items = []
        for entry in entries:
            if entry.name.startswith(".capri-upload-") or (entry.name.startswith(".") and not show_hidden):
                continue
            try:
                entry.name.encode("utf-8")
                info = entry.stat(follow_symlinks=False)
            except (FileNotFoundError, PermissionError, UnicodeEncodeError):
                continue
            mode = info.st_mode
            if stat.S_ISDIR(mode):
                kind = "folder"
            elif stat.S_ISREG(mode):
                kind = "file"
            elif stat.S_ISLNK(mode):
                kind = "link"
            else:
                kind = "special"
            items.append(dict(name=entry.name, kind=kind, modified=info.st_mtime,
                              size=info.st_size if kind == "file" else None))
    return sorted(items, key=lambda item: (item["kind"] != "folder", item["name"].casefold()))


DISPLAY_POSITIONS = {"center", "top-left", "top-right", "bottom-left", "bottom-right"}
ANTHEM = "capri-anthem.service"
PROJECTS = {"anthem": ("Capri Anthem", ANTHEM)}
BOT_ACTIONS = {"start", "stop", "restart"}
CONTROL_HOST = "127.0.0.1:8765"
CONTROL_LOCK = threading.Lock()
WIFI_SOCKET = Path("/run/capri-wifi.sock")
WIFI_RESULT = Path("/run/capri-wifi-result.json")
ADMIN_PAIR = Path.home() / ".config/capri-dashboard/anthem-pairing.json"
ADMIN_LOCK = threading.Lock()
ADMIN_SESSIONS = {}
ADMIN_COOKIE = "capri_dashboard_admin"


def code_hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def issue_admin_code():
    code = secrets.token_urlsafe(24)
    ADMIN_PAIR.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = ADMIN_PAIR.with_name(".anthem-pair-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(dict(digest=code_hash(code), expires=time.time() + 300), stream)
        temporary.replace(ADMIN_PAIR)
    finally:
        temporary.unlink(missing_ok=True)
    print(code)


def health():
    total, _, free = shutil.disk_usage("/")
    flags = None
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                text=True, timeout=1, check=True)
        key, value = result.stdout.strip().split("=", 1)
        if key == "throttled":
            flags = int(value, 16)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return dict(total=total, free=free, flags=flags)


def project_memory(fields):
    current = fields.get("MemoryCurrent", "")
    if current.isdecimal():
        return int(current)
    group = fields.get("ControlGroup")
    if not group:
        return 0 if fields["ActiveState"] in ("inactive", "failed") else None
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    pids = set()
    try:
        pids.update((root / "cgroup.procs").read_text().split())
        for path in root.glob("*/**/cgroup.procs"):
            try:
                pids.update(path.read_text().split())
            except FileNotFoundError:
                continue
    except OSError:
        return None
    total = 0
    for pid in pids:
        try:
            with Path(f"/proc/{pid}/smaps_rollup").open() as rollup:
                total += next(int(line.split()[1]) * 1024 for line in rollup
                              if line.startswith("Pss:"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, StopIteration, ValueError):
            return None
    return total


def service_status(unit, include_memory=False):
    properties = ["ActiveState", "MainPID", "ActiveEnterTimestampMonotonic"]
    if include_memory:
        properties.extend(["LoadState", "MemoryCurrent", "ControlGroup"])
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=" + ",".join(properties)],
            capture_output=True, text=True, timeout=2, check=True,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
        start = int(fields["ActiveEnterTimestampMonotonic"])
        state = fields["ActiveState"]
        status = dict(state=state, pid=int(fields["MainPID"]),
                      uptime=max(0, int(time.monotonic() - start / 1_000_000))
                      if state == "active" and start else None)
        if include_memory:
            if fields["LoadState"] != "loaded":
                status["state"] = "unknown"
            status["ram"] = project_memory(fields) if status["state"] != "unknown" else None
        return status
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return dict(state="unknown", pid=0, uptime=None)

def counters():
    fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(fields), fields[3] + fields[4]

def sectors():
    return int(DEVICE.read_text().split()[6])

def processes(previous, elapsed):
    current, rows = {}, []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = path.read_text(errors="replace")
            end = raw.rindex(")")
            fields = raw[end + 2:].split()
            if fields[0] == "Z" or int(fields[21]) <= 0:
                continue
            pid = int(path.parent.name)
            key = (pid, fields[19])
            ticks = int(fields[11]) + int(fields[12])
            current[key] = ticks
            cpu = max(0, ticks - previous.get(key, ticks)) * CPU_COUNT * 100 / max(1, elapsed)
            rows.append(dict(pid=pid, name=raw[raw.index("(") + 1:end],
                             cpu=round(cpu, 1), ram=int(fields[21]) * PAGE_KIB))
        except (OSError, ValueError, IndexError):
            continue
    return current, {
        metric: sorted(rows, key=lambda row: row[metric], reverse=True)[:12]
        for metric in ("cpu", "ram")
    }

def network():
    try:
        result = subprocess.run(
            ["nmcli", "-g", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", "wlan0"],
            capture_output=True, text=True, timeout=1.5, check=True,
        )
        lines = result.stdout.splitlines()
        profile = lines[0]
        ssid = ""
        if profile and profile != "--":
            try:
                ssid = subprocess.run(
                    ["nmcli", "-t", "-e", "no", "-g", "802-11-wireless.ssid", "connection", "show", "id", profile],
                    capture_output=True, text=True, timeout=1.5, check=True,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                ssid = profile.removeprefix("netplan-wlan0-")
        return dict(profile=profile, ssid=ssid, ip=lines[1].split("/")[0] if len(lines) > 1 else "")
    except (OSError, subprocess.SubprocessError, IndexError):
        return dict(profile="", ip="")

class Writes:
    def __init__(self):
        current = sectors()
        try:
            self.saved = json.loads(STATE.read_text())
        except FileNotFoundError:
            self.saved = dict(boot=BOOT, sectors=current, total=0)
        self.last = current
        self.total = self.saved["total"] + (
            max(0, current - self.saved["sectors"]) if self.saved["boot"] == BOOT else current
        )

    def update(self):
        current = sectors()
        self.total += max(0, current - self.last)
        self.last = current
        return self.total * 512

    def save(self):
        self.update()
        record = dict(boot=BOOT, sectors=self.last, total=self.total)
        if record == self.saved and STATE.exists():
            return
        STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        with tmp.open("w") as file:
            json.dump(record, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, STATE)
        fd = os.open(STATE.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.saved = record

def sample(writes):
    previous = counters()
    previous_processes, _ = processes({}, 1)
    last_written = writes.update()
    last_time = time.monotonic()
    next_network = 0
    next_save = time.monotonic() + 60
    while not STOP.wait(2):
        current = counters()
        elapsed = current[0] - previous[0]
        cpu = 100 * (1 - (current[1] - previous[1]) / elapsed) if elapsed > 0 else 0
        previous = current
        mem = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        now = time.monotonic()
        written = writes.update()
        rate = max(0, written - last_written) / max(0.001, now - last_time)
        last_written, last_time = written, now
        previous_processes, top = processes(previous_processes, elapsed)
        if now >= next_network:
            wifi = network()
            pi_health = health()
            next_network = now + 10
        with LOCK:
            HISTORY.append(dict(time=time.time(), cpu=round(cpu, 1), ram=mem["MemTotal"] - mem["MemAvailable"],
                                ram_total=mem["MemTotal"], temp=temp, written=written, write_rate=round(rate)))
            DETAILS.update(processes=top, wifi=wifi, health=pi_health,
                           uptime=int(float(Path("/proc/uptime").read_text().split()[0])))
        if time.monotonic() >= next_save:
            writes.save()
            next_save = time.monotonic() + 60

class Handler(BaseHTTPRequestHandler):
    def can_control(self):
        return self.client_address[0] == "127.0.0.1" and self.headers.get("Host") == CONTROL_HOST

    def admin_session(self):
        try:
            return SimpleCookie(self.headers.get("Cookie", ""))[ADMIN_COOKIE].value
        except (KeyError, CookieError):
            return ""

    def is_admin(self):
        if self.can_control():
            return True
        if not self.wifi_origin():
            return False
        token = self.admin_session()
        with ADMIN_LOCK:
            return ADMIN_SESSIONS.get(token) == (self.client_address[0], self.headers.get("Host"))

    def admin_same_origin(self):
        return (self.wifi_origin() and not self.headers.get("Transfer-Encoding")
                and self.headers.get("Origin") == "http://" + self.headers.get("Host", ""))

    def admin_auth(self, *, logout=False):
        if not self.admin_same_origin():
            self.send_json(dict(error="Mở dashboard qua địa chỉ Capri trong mạng nội bộ."), 403)
            return
        if logout:
            with ADMIN_LOCK:
                ADMIN_SESSIONS.pop(self.admin_session(), None)
            self.send_admin_session("")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256 or self.headers.get_content_type() != "application/json":
                raise ValueError
            self.connection.settimeout(5)
            code = json.loads(self.rfile.read(length))["code"]
            if not isinstance(code, str) or len(code) != 32:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(dict(error="Mã đăng nhập không hợp lệ."), 400)
            return
        token = None
        with ADMIN_LOCK:
            try:
                pairing = json.loads(ADMIN_PAIR.read_bytes())
                if time.time() < pairing["expires"] and hmac.compare_digest(pairing["digest"], code_hash(code)):
                    ADMIN_PAIR.unlink()
                    token = secrets.token_urlsafe(32)
                    ADMIN_SESSIONS.clear()
                    ADMIN_SESSIONS[token] = (self.client_address[0], self.headers.get("Host"))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if token is None:
            self.send_json(dict(error="Mã sai, đã dùng hoặc đã hết hạn. Hãy lấy mã mới."), 401)
        else:
            self.send_admin_session(token)

    def send_admin_session(self, token):
        cookie = f"{ADMIN_COOKIE}={token}; Path=/api; HttpOnly; SameSite=Strict"
        if not token:
            cookie += "; Max-Age=0"
        self.send_data(b'{"ok":true}', "application/json", cookie=cookie)

    def wifi_origin(self):
        hosts = {self.connection.getsockname()[0], "localhost", socket.gethostname().lower()}
        hosts.add(socket.gethostname().lower() + ".local")
        host = self.headers.get("Host", "").lower()
        return host in {name + ":8765" for name in hosts} and any(
            ipaddress.ip_address(self.client_address[0]) in net for net in LOCAL
        )

    def send_json(self, data, status=200):
        self.send_data(json.dumps(data).encode(), "application/json", status)

    def files_request(self, url, action):
        query = parse_qs(url.query, max_num_fields=4, errors="strict")
        folder = query.get("folder", [str(Path.home())])[0]
        if action == "list":
            with file_folder(folder) as fd:
                items = file_entries(fd, query.get("hidden") == ["1"])
                writable = os.access(".", os.W_OK | os.X_OK, dir_fd=fd)
            self.send_json(dict(folder=folder, home=str(Path.home()), items=items, writable=writable))
            return

        name = file_name(query.get("name", [""])[0])
        if action == "upload":
            self.receive_file(folder, name)
            return
        with file_folder(folder) as fd:
            if action == "download":
                self.download_file(fd, name)
                return
            if action == "folder":
                os.mkdir(name, mode=0o700, dir_fd=fd)
            elif action == "trash":
                if os.readlink(f"/proc/self/fd/{fd}") != folder:
                    raise FileNotFoundError(folder)
                subprocess.run(["gio", "trash", "--", str(Path(folder) / name)],
                               check=True, capture_output=True, timeout=15)
            elif action == "rename":
                target = file_name(query.get("target", [""])[0])
                if target != name:
                    self.move_file(fd, name, fd, target)
            elif action == "move":
                destination = query.get("destination", [""])[0]
                with file_folder(destination) as destination_fd:
                    self.move_file(fd, name, destination_fd, name)
        self.send_json(dict(ok=True), 201 if action == "folder" else 200)

    def handle_files(self, url, action):
        if (not self.is_admin() or self.headers.get("Sec-Fetch-Site") == "cross-site"
                or (self.command == "POST" and not self.admin_same_origin())):
            self.send_json(dict(error="Đăng nhập quản trị trong tab Tổng quan để quản lý tệp."), 403)
            return
        try:
            self.files_request(url, action)
        except FileExistsError:
            self.send_json(dict(error="Tên này đã tồn tại trong thư mục đích."), 409)
        except (FileNotFoundError, NotADirectoryError):
            self.send_json(dict(error="Tệp hoặc thư mục không tồn tại hoặc không thể mở."), 404)
        except PermissionError:
            self.send_json(dict(error="Tài khoản trên Capri không có quyền thực hiện thao tác này."), 403)
        except ValueError:
            self.send_json(dict(error="Tên tệp, thư mục hoặc đường dẫn không hợp lệ."), 400)
        except subprocess.SubprocessError:
            message = ("Không thể đưa mục này vào Thùng rác trên ổ hiện tại."
                       if action == "trash" else "Không thể di chuyển mục này. Hãy kiểm tra quyền và thư mục đích.")
            self.send_json(dict(error=message), 409)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                self.send_json(dict(error="Ổ lưu trữ không còn đủ dung lượng."), 507)
            elif error.errno == errno.EROFS:
                self.send_json(dict(error="Ổ lưu trữ này chỉ cho phép đọc."), 403)
            elif error.errno == errno.ELOOP:
                self.send_json(dict(error="Mục này là liên kết. Hãy mở đường dẫn thật."), 400)
            else:
                self.send_json(dict(error="Capri không hoàn tất được thao tác với tệp."), 503)

    def move_file(self, source_fd, name, destination_fd, target):
        os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if os.path.lexists(f"/proc/self/fd/{destination_fd}/{target}"):
            raise FileExistsError(target)
        command = ["mv", "-T", "--update=none-fail", "--",
                   f"/proc/self/fd/{source_fd}/{name}", f"/proc/self/fd/{destination_fd}/{target}"]
        subprocess.run(command, pass_fds=(source_fd, destination_fd), check=True, capture_output=True)

    def download_file(self, folder, name):
        if not stat.S_ISREG(os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode):
            raise ValueError("Chỉ tải được tệp thường.")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Chỉ tải được tệp thường.")
            self.connection.settimeout(20)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name, safe=""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            try:
                self.end_headers()
                remaining = info.st_size
                while remaining:
                    chunk = stream.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                self.close_connection = True

    def receive_file(self, folder, name):
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= UPLOAD_LIMIT:
            self.send_json(dict(error="Tệp phải có dung lượng tối đa 1 GiB."), 413)
            return
        if not UPLOAD_LOCK.acquire(blocking=False):
            self.send_json(dict(error="Capri đang nhận một tệp khác. Hãy thử lại."), 409)
            return
        try:
            with file_folder(folder) as fd:
                self.upload_file(fd, name, length)
        finally:
            UPLOAD_LOCK.release()
        self.send_json(dict(ok=True), 201)

    def upload_file(self, folder, name, remaining):
        temporary = ".capri-upload-" + secrets.token_hex(16)
        self.connection.settimeout(20)
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=folder)
        try:
            with os.fdopen(handle, "wb") as output:
                while remaining:
                    data = self.rfile.read(min(remaining, 256 * 1024))
                    if not data:
                        raise OSError("Incomplete upload")
                    output.write(data)
                    remaining -= len(data)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=folder)
        os.fsync(folder)

    def control_display(self):
        global DISPLAY_STATE
        if not self.admin_same_origin() or not self.is_admin():
            self.send_json(dict(error="Đăng nhập quản trị trong tab Tổng quan để điều khiển màn hình."), 403)
            return
        action = self.path.rsplit("/", 1)[-1]
        screen = ("clock" if action == "stop" else
                  self.headers.get("X-Capri-Display-Screen", "") if action == "show" else "gif")
        mode = self.headers.get("X-Capri-Display-Mode", "full")
        rotation = self.headers.get("X-Capri-Display-Rotation", "180")
        position = self.headers.get("X-Capri-Display-Position", "center") if mode == "region" else None
        if action != "stop" and (mode not in ("region", "full") or rotation not in ("0", "180")
                                 or (action == "show" and screen not in DISPLAY_SCREENS)
                                 or (mode == "region" and position not in DISPLAY_POSITIONS)):
            self.send_json(dict(error="Chế độ hiển thị không hợp lệ."), 400)
            return
        crop_header = self.headers.get("X-Capri-Display-Crop") if action == "play" else None
        gif_angle = self.headers.get("X-Capri-Gif-Angle", "0") if action == "play" else "0"
        if gif_angle not in ("0", "90", "180", "270"):
            self.send_json(dict(error="Góc xoay GIF không hợp lệ."), 400)
            return
        crop = None
        if crop_header is not None:
            try:
                x, y, zoom = map(float, crop_header.split(","))
                if not (0 <= x <= 1 and 0 <= y <= 1 and 1 <= zoom <= 4):
                    raise ValueError
                crop = (x, y, zoom)
            except ValueError:
                self.send_json(dict(error="Khung cắt GIF không hợp lệ."), 400)
                return
        if not DISPLAY_LOCK.acquire(blocking=False):
            self.send_json(dict(error="Màn hình đang xử lý yêu cầu khác."), 409)
            return
        try:
            if screen != "gif":
                if action == "stop":
                    rotation = str(DISPLAY_STATE.get("rotation", 180))
                next_state = dict(screen=screen, rotation=int(rotation))
            else:
                gif = self.read_display_gif()
                next_state = dict(revision=secrets.token_hex(8), gif=gif,
                                  source="upload",
                                  mode=mode, position=position, rotation=int(rotation), screen=screen,
                                  crop=crop, gif_angle=int(gif_angle))
            runtime = Path(os.environ["XDG_RUNTIME_DIR"])
            playback = runtime / "capri-display.gif"
            mode_path = playback.with_suffix(".mode")
            if screen != "gif":
                config = runtime / "capri-display-clock.rotation"
                config.write_text(f"{screen}:{rotation}", encoding="ascii")
            else:
                pending = runtime / "capri-display.gif.next"
                pending.write_bytes(gif)
                pending.replace(playback)
                options = ",".join(map(str, crop)) if crop else ""
                mode_path.write_text(f"{mode}:{position or ''}:{rotation}:{options}:{gif_angle}",
                                     encoding="ascii")
            try:
                subprocess.run(
                    ["systemctl", "--user", "--no-ask-password", "restart",
                     DISPLAY_UNIT if screen == "gif" else DISPLAY_CLOCK_UNIT],
                    capture_output=True, check=True, timeout=8,
                )
            except (OSError, subprocess.SubprocessError):
                DISPLAY_STATE = {}
                self.send_json(dict(error="Không điều khiển được màn TFT. Kiểm tra kết nối SPI rồi thử lại."), 503)
                return
            finally:
                playback.unlink(missing_ok=True)
                mode_path.unlink(missing_ok=True)
            DISPLAY_STATE = next_state
            self.display_status()
        except ValueError as error:
            self.send_json(dict(error=str(error)), 400)
        except (KeyError, OSError):
            self.send_json(dict(error="Không đọc được dữ liệu GIF."), 503)
        finally:
            DISPLAY_LOCK.release()

    def display_status(self):
        state = DISPLAY_STATE
        status = {key: state.get(key)
                  for key in ("revision", "source", "mode", "position", "rotation", "screen", "crop", "gif_angle")}
        if status["screen"] in DISPLAY_SCREENS:
            try:
                stamp = self.display_preview_path().stat().st_mtime_ns
                status["preview"] = f"/api/display/preview?frame={stamp}"
            except OSError:
                pass
        self.send_json(status)

    @staticmethod
    def display_preview_path():
        return Path(os.environ["XDG_RUNTIME_DIR"]) / "capri-display-clock.png"

    def read_display_gif(self):
        from PIL import Image
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= DISPLAY_MAX or self.headers.get_content_type() != "image/gif":
            raise ValueError("Chọn tệp GIF tối đa 8 MiB.")
        self.connection.settimeout(6)
        gif = self.rfile.read(length)
        if len(gif) != length or gif[:6] not in (b"GIF87a", b"GIF89a"):
            raise ValueError("Tệp GIF không hợp lệ.")
        width = int.from_bytes(gif[6:8], "little")
        height = int.from_bytes(gif[8:10], "little")
        if not (0 < width <= 1920 and 0 < height <= 1080):
            raise ValueError("GIF phải có kích thước tối đa 1920 × 1080.")
        try:
            with Image.open(io.BytesIO(gif), formats=("GIF",)) as image:
                image.load()
        except (OSError, EOFError, SyntaxError):
            raise ValueError("Không đọc được khung hình GIF.") from None
        return gif

    def control_projects(self):
        if (not self.admin_same_origin() or not self.is_admin()
                or self.headers.get_content_type() != "application/json"):
            self.send_json(dict(error="Đăng nhập quản trị trong tab Tổng quan để điều khiển dự án."), 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError
            self.connection.settimeout(5)
            request = json.loads(self.rfile.read(length))
            action, ids = request["action"], request["ids"]
            if (not isinstance(action, str) or action not in BOT_ACTIONS or not isinstance(ids, list) or not ids
                    or any(not isinstance(key, str) or key not in PROJECTS for key in ids)
                    or len(set(ids)) != len(ids) or (action != "stop" and len(ids) != 1)):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(dict(error="Yêu cầu dự án không hợp lệ."), 400)
            return
        self.run_service_action(action, [PROJECTS[key][1] for key in ids])

    def run_service_action(self, action, units):
        if not CONTROL_LOCK.acquire(blocking=False):
            self.send_json(dict(error="Đang xử lý một yêu cầu khác."), 409)
            return
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", action, *units],
                           capture_output=True, timeout=4, check=True)
        except (OSError, subprocess.SubprocessError):
            self.send_json(dict(error="Hãy kiểm tra trạng thái dịch vụ trước khi thử lại."), 503)
            return
        finally:
            CONTROL_LOCK.release()
        self.send_json(dict(accepted=True), 202)

    def wifi_request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as worker:
            worker.settimeout(10)
            worker.connect(str(WIFI_SOCKET))
            worker.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            with worker.makefile("rb") as response:
                result = json.loads(response.readline(32768))
        status = result.pop("status")
        self.send_json(result, status)

    def change_wifi(self):
        if (not self.admin_same_origin() or not self.is_admin()
                or self.headers.get_content_type() != "application/json"):
            self.send_json(dict(error="Đăng nhập quản trị trong tab Tổng quan để đổi Wi-Fi."), 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError
            self.connection.settimeout(5)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError
        except (ValueError, TypeError, OSError):
            self.send_json(dict(error="Yêu cầu Wi-Fi không hợp lệ."), 400)
            return
        try:
            action = request.get("action", "add")
            keys = {"add": ("ssid", "password"), "update": ("profile", "password"), "delete": ("profile",)}
            self.wifi_request(dict(action=action, **{key: request[key] for key in keys[action]}))
        except (KeyError, TypeError):
            self.send_json(dict(error="Thiếu tên hoặc mật khẩu Wi-Fi."), 400)
            return
        except (OSError, ValueError):
            self.send_json(dict(error="Dịch vụ đổi Wi-Fi chưa sẵn sàng."), 503)
            return

    def do_GET(self):
        url = urlsplit(self.path)
        action = url.path.removeprefix("/api/files/")
        if url.path.startswith("/api/files/") and action in ("list", "download"):
            self.handle_files(url, action)
            return
        client = ipaddress.ip_address(self.client_address[0])
        if not any(client in net for net in LOCAL):
            self.send_error(403)
            return
        if self.path == "/":
            data, kind = PAGE, "text/html; charset=utf-8"
        elif self.path == "/api/display":
            self.display_status()
            return
        elif url.path == "/api/display/preview":
            try:
                data = self.display_preview_path().read_bytes()
            except OSError:
                self.send_error(404)
                return
            kind = "image/png"
        elif self.path == "/api/display/telemetry":
            if not self.can_control():
                self.send_error(403)
                return
            with LOCK:
                data = dict(metrics=HISTORY[-1] if HISTORY else None,
                            wifi=DETAILS.get("wifi"), health=DETAILS.get("health"))
            self.send_json(data)
            return
        elif self.path.startswith("/api/display/gif/"):
            state = DISPLAY_STATE
            data = (state["gif"] if self.path.removeprefix("/api/display/gif/")
                    == state.get("revision") else None)
            if data is None:
                self.send_error(404)
                return
            kind = "image/gif"
        elif self.path == "/api/metrics":
            with LOCK:
                data = json.dumps(list(HISTORY)).encode()
            kind = "application/json"
        elif self.path == "/api/dashboard":
            with LOCK:
                data = json.dumps(dict(history=list(HISTORY), server_time=time.time(), **DETAILS)).encode()
            kind = "application/json"
        elif self.path == "/api/admin":
            self.send_json(dict(can_control=self.is_admin(),
                                can_sign_in=self.wifi_origin() and not self.can_control()))
            return
        elif url.path == "/api/anthem":
            data = json.dumps(dict(**service_status(ANTHEM, include_memory=url.query == "memory=1"),
                                   can_control=self.is_admin(),
                                   can_sign_in=self.wifi_origin() and not self.can_control())).encode()
            kind = "application/json"
        elif self.path == "/api/projects":
            projects = [dict(id=key, name=name, **service_status(unit, include_memory=True))
                        for key, (name, unit) in PROJECTS.items()]
            self.send_json(dict(projects=projects, can_control=self.is_admin()))
            return
        elif self.path == "/api/wifi/profiles":
            if not self.is_admin():
                self.send_error(403)
                return
            try:
                self.wifi_request(dict(action="list"))
            except (OSError, ValueError):
                self.send_json(dict(error="Chưa đọc được Wi-Fi đã lưu."), 503)
            return
        elif self.path == "/api/wifi":
            if not self.is_admin():
                self.send_error(403)
                return
            try:
                result = json.loads(WIFI_RESULT.read_bytes())
            except (OSError, ValueError):
                result = None
            self.send_json(dict(available=WIFI_SOCKET.is_socket(), result=result))
            return
        else:
            self.send_error(404)
            return
        self.send_data(data, kind)

    def do_POST(self):
        url = urlsplit(self.path)
        if self.path in ("/api/admin/login", "/api/admin/logout", "/api/anthem/login", "/api/anthem/logout"):
            self.admin_auth(logout=self.path.endswith("/logout"))
            return
        action = url.path.removeprefix("/api/files/")
        if url.path.startswith("/api/files/") and action in ("folder", "upload", "rename", "move", "trash"):
            self.handle_files(url, action)
            return
        if self.path in ("/api/display/play", "/api/display/stop", "/api/display/show"):
            self.control_display()
            return
        if self.path == "/api/projects":
            self.control_projects()
            return
        if self.path == "/api/wifi":
            self.change_wifi()
            return
        prefix = "/api/anthem/"
        action = self.path.removeprefix(prefix)
        if not self.path.startswith(prefix) or action not in BOT_ACTIONS:
            self.send_error(404)
            return
        if (not self.admin_same_origin() or not self.is_admin()
                or self.headers.get("Content-Length") not in (None, "0")):
            self.send_error(403)
            return
        self.run_service_action(action, [ANTHEM])

    def send_data(self, data, kind, status=200, *, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

if __name__ == "__main__":
    if sys.argv[1:] in (["--admin-code"], ["--anthem-admin-code"]):
        issue_admin_code()
        raise SystemExit(0)
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    writes = Writes()
    writes.save()
    def stop(*_):
        STOP.set()
    def run_sampler():
        try:
            sample(writes)
        except Exception:
            FAILED.set()
            raise
        finally:
            STOP.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker = threading.Thread(target=run_sampler)
    worker.start()
    server.timeout = 0.5
    try:
        while not STOP.is_set():
            server.handle_request()
    finally:
        STOP.set()
        worker.join()
        server.server_close()
        writes.save()
    # A sampler failure should let systemd restart the process.
    if FAILED.is_set():
        raise SystemExit(1)
