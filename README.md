# Capri Dashboard

Capri Dashboard là bảng điều khiển chạy trên Raspberry Pi 5. Bạn có thể theo dõi máy, quản lý tệp và dịch vụ, điều khiển màn TFT qua trình duyệt. Dashboard dùng HTML, CSS và JavaScript thuần, với máy chủ Python chạy bằng `systemd --user`.

## Tính năng

| Tab | Bạn có thể làm gì |
| --- | --- |
| **Tổng quan** | Xem CPU, RAM, nhiệt độ, thời gian hoạt động, dung lượng ổ gốc, lượng ghi microSD và thông tin kết nối. Đăng nhập quản trị và quản lý Wi-Fi từ đây. |
| **Màn hình** | Xem trước, cắt khung và xoay GIF trước khi phát trên TFT. Chọn màn ngày giờ, hệ thống, Wi-Fi hoặc trạng thái Capri Anthem. |
| **Hoạt động** | Xem biểu đồ tài nguyên và sắp xếp danh sách tiến trình theo CPU hoặc RAM. Máy chủ lấy mẫu khoảng 2 giây một lần. |
| **Tệp** | Duyệt từ thư mục gốc hoặc thư mục nhà. Tạo thư mục, gửi tệp, tải xuống, đổi tên, cắt và dán, đưa mục vào Thùng rác. |
| **Dự án** | Xem trạng thái và mức dùng RAM của Capri Anthem. Bật, dừng hoặc khởi động lại dịch vụ nếu đã cài riêng. |

Để xem thử giao diện bằng số liệu mô phỏng, mở `index.html` trực tiếp trong trình duyệt hoặc thêm `?demo=1` vào URL dashboard.

## Cài dashboard trên Raspberry Pi

Bản này dùng Raspberry Pi 5 với Raspberry Pi OS 64-bit dựa trên Debian Trixie và `systemd`. Phần cứng TFT, NetworkManager và dịch vụ Capri Anthem là các phần tích hợp tùy chọn. Để đổi tên và di chuyển tệp, lệnh `mv` của GNU coreutils cần hỗ trợ `--update=none-fail`. Lệnh `gio` cung cấp chức năng Thùng rác.

1. Trên trang GitHub, bấm **Code** để sao chép URL HTTPS, rồi clone về Pi hoặc tải ZIP và giải nén.
2. Mở terminal trong thư mục vừa tải. Chạy các lệnh sau bằng tài khoản Linux dùng để vận hành dashboard.

```sh
sudo apt update
sudo apt install python3 libglib2.0-bin

install -d "$HOME/.local/share/capri-dashboard" "$HOME/.config/systemd/user"
install -m 0644 server.py index.html "$HOME/.local/share/capri-dashboard/"
install -m 0644 capri-dashboard.service "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable --now capri-dashboard.service
```

Từ máy cùng mạng, mở `http://DIA_CHI_PI:8765/`. Thay `DIA_CHI_PI` bằng IPv4 nội bộ của Pi hoặc tên mDNS của chính máy đó nếu mạng hỗ trợ. Để xem địa chỉ từ Pi, chạy `hostname -I`. Kiểm tra máy chủ ngay trên Pi bằng các lệnh sau.

```sh
systemctl --user status capri-dashboard.service
curl -fsS http://127.0.0.1:8765/api/admin
```

Nếu cần máy chủ tự chạy ngay khi Pi khởi động, kể cả khi tài khoản chưa đăng nhập, bật chế độ linger cho tài khoản đó bằng `sudo loginctl enable-linger "$(id -un)"`.

## Đăng nhập quản trị

Tạo mã từ terminal trên Pi bằng cùng tài khoản đã cài dashboard.

```sh
python3 "$HOME/.local/share/capri-dashboard/server.py" --admin-code
```

Dán mã vào mục **Quản trị** ở tab **Tổng quan**. Mã có hiệu lực 5 phút và dùng một lần. Phiên đăng nhập dùng chung cho **Tệp**, **Màn hình**, **Dự án** và quản lý Wi-Fi. Sau khi dịch vụ dashboard khởi động lại, tạo mã mới để đăng nhập lại từ máy khác.

Dashboard phục vụ qua HTTP và dành cho mạng nội bộ tin cậy. Số liệu tổng quan có thể xem trong LAN trước khi đăng nhập. Địa chỉ loopback `127.0.0.1:8765` được cấp quyền quản trị khi truy cập từ chính máy Pi. Hãy giữ cổng này trong mạng nội bộ và giữ riêng mã quản trị.

## Quản lý tệp và dịch vụ

- Trong **Tệp**, chọn **Toàn bộ Pi** để bắt đầu từ `/`. **Thư mục nhà** và **Pictures** là các vị trí truy cập nhanh.
- Chọn một mục để đổi tên hoặc bấm **Cắt**, chuyển tới thư mục đích và bấm **Dán vào đây**. Nếu tên đã tồn tại, Capri giữ nguyên mục đích.
- Có thể gửi nhiều tệp trong một lượt, tối đa **1 GiB mỗi tệp**. **Đưa vào Thùng rác** cần xác nhận và phụ thuộc vào khả năng hỗ trợ Thùng rác của ổ lưu trữ.
- Các thao tác tệp chịu quyền Linux của tài khoản chạy dashboard. Để mở thư mục được trỏ tới bởi liên kết tượng trưng, duyệt đến đường dẫn thật.
- Trong **Dự án**, dashboard điều khiển user unit `capri-anthem.service`. Bot được cài riêng và cần thuộc cùng tài khoản Linux. Mức RAM lấy theo thông tin tiến trình hoặc nhóm tiến trình mà hệ thống cung cấp.

## Màn TFT ILI9341

Phần màn hình dùng mô-đun ILI9341 **320 × 240** qua SPI0. Bật SPI trong `sudo raspi-config` rồi khởi động lại Pi nếu được yêu cầu. Bảng dưới ghi các chân tín hiệu theo cấu hình trong mã. Dùng tài liệu của mô-đun để xác định cách cấp nguồn và nối đèn nền.

| Tín hiệu | BCM | Chân vật lý trên Pi |
| --- | --- | --- |
| MOSI / SDI | GPIO 10 | 19 |
| SCLK | GPIO 11 | 23 |
| CS / CE0 | GPIO 8 | 24 |
| DC | GPIO 25 | 22 |
| RESET | GPIO 27 | 13 |

Cài phần phụ thuộc và user units từ thư mục mã nguồn.

```sh
sudo apt install python3-pil python3-lgpio python3-spidev python3-numpy fonts-dejavu-core

install -m 0644 lcd_clock.py lcd_player.py lcd_test.py "$HOME/.local/share/capri-dashboard/"
install -m 0644 capri-display.service capri-display-clock.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
ls -l /dev/spidev0.0
```

Tài khoản chạy dashboard cần quyền truy cập thiết bị SPI và GPIO. Mở tab **Màn hình**, chọn một chế độ thông tin hoặc chọn GIF, kéo để lấy khung và phát. GIF giới hạn **8 MiB** và **1920 × 1080** ở đầu vào. Trình phát chuẩn bị khung RGB565 trước khi lặp và đặt mục tiêu đến **24 fps** ở chế độ toàn màn hình. Tốc độ thực tế còn tùy tệp và kết nối SPI.

## Quản lý Wi-Fi qua dashboard

Wi-Fi dùng NetworkManager trên `wlan0` và một dịch vụ hệ thống riêng. Sau khi phần dashboard đã chạy và `nmcli` hoạt động, chạy trình cài từ thư mục mã nguồn bằng tài khoản vận hành dashboard.

```sh
./install-wifi.sh
systemctl status capri-wifi.socket
```

Trình cài cần `sudo` để đặt dịch vụ và cấp quyền dùng socket cho tài khoản hiện tại. Tab **Tổng quan** hỗ trợ chọn mạng đã lưu, thêm mạng WPA/WPA2, cập nhật mật khẩu và xóa cấu hình. Khi đổi sang mạng Wi-Fi khác, kết nối trình duyệt có thể ngắt trong lúc Pi chuyển mạng. Nếu một hotspot dự phòng đã được cấu hình trên Pi, giao diện có thể hướng dẫn quay lại hotspot đó.

## Bố cục mã nguồn

| Tệp | Vai trò |
| --- | --- |
| `index.html` | Giao diện trình duyệt, biểu đồ, phần xem thử và các thao tác điều khiển. |
| `server.py` | HTTP API, thu thập số liệu, phiên quản trị, quản lý tệp và gọi user units. |
| `capri-dashboard.service` | Khởi chạy máy chủ dưới tài khoản người dùng. |
| `lcd_clock.py`, `lcd_player.py`, `lcd_test.py` | Các màn thông tin TFT, trình phát GIF và driver SPI dùng chung. |
| `capri-display*.service` | Điều phối các chế độ TFT qua systemd. |
| `wifi-switch.py`, `capri-wifi.socket`, `capri-wifi@.service` | Kênh Unix socket và các thao tác Wi-Fi qua NetworkManager. |
| `install-wifi.sh` | Cài tích hợp Wi-Fi và cập nhật máy chủ dashboard. |

## Kiểm tra khi có lỗi

| Hiện tượng | Cách kiểm tra |
| --- | --- |
| Trang không mở | Chạy `systemctl --user status capri-dashboard.service`. Kiểm tra địa chỉ nội bộ và cổng `8765`. |
| Mã quản trị hết hạn | Tạo mã mới trên Pi. Dùng cùng địa chỉ dashboard trên trình duyệt trong suốt phiên làm việc. |
| Không thể sửa tệp | Kiểm tra quyền ghi của tài khoản Linux, ổ có đang chỉ đọc và thư mục đích có mục trùng tên hay không. |
| Không có điều khiển Wi-Fi | Kiểm tra `systemctl status capri-wifi.socket` và `nmcli device status`. |
| TFT chưa hiển thị | Kiểm tra `/dev/spidev0.0` và xem nhật ký của dịch vụ màn hình. |

```sh
journalctl --user -u capri-dashboard.service -n 50 --no-pager
journalctl --user -u capri-display.service -u capri-display-clock.service -n 50 --no-pager
```

Dữ liệu vận hành được tạo trên Pi. Các tệp mã quản trị, cấu hình mạng thật, nội dung bot, tệp tải lên và ảnh chụp màn hình nên được lưu riêng khỏi repository.
