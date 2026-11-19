import fcntl
import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

IFACE = "wlan0"
FALLBACK = "Capri-Fallback"
RESULT = Path("/run/capri-wifi-result.json")
PROFILES = Path("/etc/NetworkManager/system-connections")


def nm(*args, wait=5):
    return subprocess.run(
        ["/usr/bin/nmcli", "--wait", str(wait), *args], text=True,
        capture_output=True, check=True, timeout=wait + 3,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def validate(request):
    ssid, password = request["ssid"], request["password"]
    if (not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32
            or not ssid.isprintable() or not ssid.strip() or ssid == FALLBACK):
        raise ValueError("Tên Wi-Fi phải có từ 1 đến 32 byte và khác Capri-Fallback.")
    if (not isinstance(password, str) or not 8 <= len(password) <= 63
            or not password.isascii() or not password.isprintable()):
        raise ValueError("Mật khẩu WPA phải dài 8–63 ký tự ASCII in được.")
    return ssid, password


def active_uuid():
    value = nm("-g", "GENERAL.CON-UUID", "device", "show", IFACE).strip()
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def activate(profile, selector="uuid", wait=25):
    nm("connection", "up", selector, profile, "ifname", IFACE, wait=wait)
    expected = profile if selector == "uuid" else nm(
        "-g", "connection.uuid", "connection", "show", "id", profile
    ).strip()
    if active_uuid() != expected:
        raise RuntimeError("Wifi profile is not active")
    state, *addresses = nm(
        "-g", "GENERAL.STATE,IP4.ADDRESS", "device", "show", IFACE
    ).splitlines()
    for value in addresses:
        address = ipaddress.IPv4Interface(value).ip
        if state.startswith("100 ") and not address.is_link_local:
            return str(address)
    raise RuntimeError("Wifi has no usable IPv4 address")


def restore(previous):
    if previous:
        try:
            state = nm("-g", "GENERAL.STATE,IP4.ADDRESS", "device", "show", IFACE).splitlines()
            if active_uuid() != previous or len(state) < 2 or not state[0].startswith("100 "):
                activate(previous, wait=15)
            return True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    try:
        activate(FALLBACK, "id", wait=15)
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def derive_psk(ssid, password):
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


def keyfile(ssid, password, profile):
    psk = derive_psk(ssid, password)
    label = ssid.replace("\\", "\\\\").replace(" ", "\\s")
    octets = "".join(f"{byte};" for byte in ssid.encode())
    return (
        f"[connection]\nid={label}\nuuid={profile}\ntype=wifi\n"
        f"interface-name={IFACE}\nautoconnect=false\nautoconnect-priority=-10\n"
        f"[wifi]\nmode=infrastructure\nssid={octets}\n"
        f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={psk}\npsk-flags=0\n"
        "[ipv4]\nmethod=auto\n[ipv6]\nmethod=auto\n"
    )


def publish(profile, state, **details):
    temporary = RESULT.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(id=profile, state=state, **details)))
    temporary.chmod(0o644)
    temporary.replace(RESULT)


def reply(status, **details):
    print(json.dumps(dict(status=status, **details)), flush=True)


def profiles():
    active = active_uuid()
    listing = nm("-t", "--escape", "no", "-f", "UUID,TYPE,FILENAME", "connection", "show")
    for line in listing.splitlines():
        profile, kind, path = line.split(":", 2)
        if kind not in ("wifi", "802-11-wireless"):
            continue
        fields = nm(
            "--escape", "no", "-g",
            "802-11-wireless.mode,connection.interface-name,802-11-wireless-security.key-mgmt,connection.id,802-11-wireless.ssid",
            "connection", "show", "uuid", profile
        ).rstrip("\n").split("\n", 4)
        if len(fields) != 5:
            continue
        mode, interface, security, name, ssid = fields
        if mode not in ("", "--", "infrastructure") or interface not in ("", "--", IFACE):
            continue
        if not ssid or name == FALLBACK or ssid == FALLBACK:
            continue
        filename = Path(path)
        editable = security == "wpa-psk" and filename.parent == PROFILES
        yield dict(id=profile, name=name, ssid=ssid, active=profile == active, editable=editable), filename


def replace_file(path, contents):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capri-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def replace_password(contents, psk):
    sections = re.split(rb"(?m)(?=^\[)", contents)
    for index, section in enumerate(sections):
        if section.splitlines()[0:1] not in ([b"[wifi-security]"], [b"[802-11-wireless-security]"]):
            continue
        lines = [line for line in section.splitlines()
                 if not re.match(rb"^\s*(psk|psk-flags)\s*=", line)]
        sections[index] = b"\n".join(lines + [b"psk=" + psk.encode(), b"psk-flags=0", b""])
        return b"".join(sections)
    raise ValueError("Capri chưa hỗ trợ kiểu bảo mật của mạng này.")


def queue(operation):
    publish(operation, "pending")
    reply(202, id=operation)
    time.sleep(2)


def change_saved(request):
    target = str(uuid.UUID(request["profile"]))
    entry = next(((profile, path) for profile, path in profiles() if profile["id"] == target), None)
    if entry is None:
        reply(404, error="Không tìm thấy cấu hình Wi-Fi có thể quản lý.")
        return
    profile, path = entry
    if request["action"] == "delete":
        previous = active_uuid()
        if previous != target:
            nm("connection", "delete", "uuid", target)
            reply(200, deleted=True)
            return
        operation = str(uuid.uuid4())
        queue(operation)
        try:
            ip = activate(FALLBACK, "id")
            nm("connection", "delete", "uuid", target)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            publish(operation, "restored" if restore(previous) else "failed")
        else:
            publish(operation, "deleted", ip=ip)
        return

    if not profile["editable"] or path.is_symlink():
        reply(409, error="Capri chỉ sửa được cấu hình WPA-PSK đã lưu trên đĩa.")
        return
    ssid, password = validate(dict(ssid=profile["ssid"], password=request["password"]))
    old = path.read_bytes()
    new = replace_password(old, derive_psk(ssid, password))
    previous = active_uuid()
    operation = str(uuid.uuid4())
    queue(operation)
    try:
        replace_file(path, new)
        nm("connection", "load", str(path))
        if previous == target:
            nm("device", "disconnect", IFACE)
        ip = activate(target)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        try:
            replace_file(path, old)
            nm("connection", "load", str(path))
        except (OSError, subprocess.SubprocessError):
            publish(operation, "failed")
            restore(None)
            return
        publish(operation, "restored" if restore(previous) else "failed")
    else:
        publish(operation, "connected", ip=ip)


def handle(request):
    action = request.get("action", "add")
    if action == "list":
        reply(200, profiles=[profile for profile, _ in profiles()])
        return
    if action in ("update", "delete"):
        change_saved(request)
        return
    if action != "add":
        raise ValueError("Thao tác Wi-Fi không hợp lệ.")
    ssid, password = validate(request)
    saved_profiles = nm("-t", "-f", "UUID,TYPE", "connection", "show").splitlines()
    for saved, kind in (line.split(":", 1) for line in saved_profiles):
        if kind not in ("wifi", "802-11-wireless"):
            continue
        if ssid == nm("-g", "802-11-wireless.ssid", "connection", "show", "uuid", saved).rstrip("\n"):
            reply(409, error="Wi-Fi này đã có cấu hình trên Capri.")
            return

    previous = active_uuid()
    profile = str(uuid.uuid4())
    path = PROFILES / f"capri-{profile}.nmconnection"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(keyfile(ssid, password, profile))
    try:
        nm("connection", "load", str(path))
        publish(profile, "pending")
        reply(202, id=profile)
    except (OSError, subprocess.SubprocessError):
        remove_profile(profile, path)
        raise

    time.sleep(2)
    try:
        ip = activate(profile)
        nm("connection", "modify", "uuid", profile, "connection.autoconnect", "yes")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        remove_profile(profile, path)
        publish(profile, "restored" if restore(previous) else "failed")
        return
    publish(profile, "connected", ip=ip)


def remove_profile(profile, path):
    try:
        nm("connection", "delete", "uuid", profile)
    except (OSError, subprocess.SubprocessError):
        pass
    path.unlink(missing_ok=True)


def main():
    def input_timeout(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, input_timeout)
    signal.alarm(6)
    try:
        request = json.loads(sys.stdin.buffer.readline(2048))
    except (ValueError, OSError):
        reply(400, error="Yêu cầu Wi-Fi không hợp lệ.")
        return
    finally:
        signal.alarm(0)
    with open("/run/capri-wifi-switch.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reply(409, error="Capri đang đổi Wi-Fi. Hãy chờ một lát.")
            return
        try:
            handle(request)
        except ValueError as exc:
            reply(400, error=str(exc) if str(exc).startswith(("Tên", "Mật", "Capri", "Thao")) else "Yêu cầu Wi-Fi không hợp lệ.")
        except (AttributeError, KeyError, TypeError):
            reply(400, error="Yêu cầu Wi-Fi không hợp lệ.")
        except (OSError, subprocess.SubprocessError):
            reply(503, error="Capri chưa lưu được cấu hình Wi-Fi.")


if __name__ == "__main__":
    main()
