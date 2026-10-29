# TradePilot · Futures Lab

Ứng dụng web mô phỏng bot Futures Long/Short bằng Python + FastAPI, giao diện tiếng Việt. Bản đầu triển khai giai đoạn **kiểm thử mô phỏng** đã thống nhất: tiền và giá đều giả lập, **chưa tích hợp API giao dịch Binance**. Không cần API key.

## Dành cho người dùng Windows không biết lập trình

Tải mã nguồn từ nhánh [`codex/tradepilot-windows`](https://github.com/hunghughun/testtcodexx/tree/codex/tradepilot-windows): chọn **Code → Download ZIP**, rồi giải nén. Nếu kho riêng tư, đăng nhập GitHub bằng tài khoản có quyền truy cập. Cách này không dùng chức năng tải artifact của Codex.

Mở [HUONG-DAN.html](HUONG-DAN.html) bằng trình duyệt để xem hướng dẫn chi tiết. Giải nén gói ứng dụng, cài Python 3.12 trở lên một lần, rồi nhấp đúp `Mo-bot-Windows.bat`. Tệp mở bot tự chuẩn bị thư viện và mở giao diện trong trình duyệt; không tự bắt đầu giao dịch. Giữ cửa sổ chương trình mở trong khi chạy. Không mở trực tiếp tệp trong ZIP.

Giao diện dùng nền sáng, có nút **Hướng dẫn sử dụng**. Đây là ứng dụng chạy trên chính máy của bạn; không có website công khai hay bản cài `.exe`. Nút Publish trong Codex không cài chương trình vào Windows. Tệp `.bat` chưa được kiểm thử trực tiếp trên Windows; cơ chế mở bằng Python được xác minh trong môi trường Linux.

## Cấu hình theo yêu cầu

| Thông số | Giá trị |
| --- | --- |
| Vốn đầu phiên | 500 USDT |
| Mục tiêu tổng tài sản | 1.000 USDT |
| Mức lỗ tối đa | 300 USDT; ngưỡng tài sản dừng là 200 USDT |
| Cặp được phép | BTCUSDT, ETHUSDT |
| Kiểu vị thế | Long / Short, một vị thế trên mỗi cặp |
| Ký quỹ / đòn bẩy | Isolated / 5x |

Giá trị mẫu bổ sung, có thể chỉnh trước khi chạy: ký quỹ 50 USDT/lệnh (giá trị danh nghĩa 250 USDT), SL 2% và TP 4% **theo biến động giá**, phí 5 bps mỗi chiều, trượt giá 5 bps, funding 1 bps mỗi 8 giờ giả lập. Đây không phải thông số được tối ưu hay khuyến nghị lợi nhuận. 1 bps = 0,01%.

## Chạy ứng dụng

Python 3.12 trở lên. Từ thư mục dự án:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn bot.web:create_app --factory --host 127.0.0.1 --port 8000
```

Mở trình duyệt trên máy đang chạy ứng dụng, địa chỉ `127.0.0.1:8000`. Trong môi trường Codex hiện tại, `.venv` đã được tạo với `--system-site-packages` để sử dụng các phiên bản thư viện có sẵn; máy chủ đã được kiểm tra tại cổng 8000. Không cần `npm install` hay bước build giao diện.

1. Kiểm tra vốn, mục tiêu, ngưỡng lỗ và danh sách cặp. Nếu chỉnh cấu hình, chọn **Lưu thành phiên mới**.
2. Chọn **Chạy mô phỏng**. Bộ mô phỏng tạo một giá đóng cửa giả lập mỗi 2 giây, tượng trưng cho một phút; tối thiểu 16 mẫu mới xác định được giao cắt SMA 5/15.
3. Theo dõi vị thế, tài sản và nhật ký. **Dừng & đóng vị thế** đóng toàn bộ vị thế mô phỏng bằng giá gần nhất đã biết.
4. Xuất JSON để lấy lịch sử tất cả các phiên, gồm cấu hình, lệnh và funding. Tạo phiên mới không xóa phiên cũ.

Chỉ chạy **một tiến trình / một worker** cho mỗi cơ sở dữ liệu; không dùng `--workers` lớn hơn 1 hoặc nhiều máy chủ cùng mở một DB. Dữ liệu ở `.runtime/bot.sqlite3`, được bỏ qua bởi Git. Có thể đặt đường dẫn khác bằng biến `BOT_DB_PATH`. Không có giao diện đăng nhập; máy chủ chỉ phục vụ loopback và không được thiết kế để công khai ra Internet.

## Cách bot hoạt động

- SMA nhanh cắt lên SMA chậm: mở Long. Cắt xuống: mở Short. Chỉ đọc mẫu hiện tại và trước đó.
- Đóng vị thế khi giá chạm SL/TP, có tín hiệu đảo chiều, chạm ngưỡng tài sản hoặc người dùng dừng. Tối đa một vị thế cho mỗi cặp; sau SL/TP có thời gian nghỉ 5 nhịp. Đảo chiều có thể đóng và mở vị thế ngược ngay tại tín hiệu.
- Tài sản ước tính = tiền khả dụng + số tiền thu hồi từ các vị thế sau khi đóng, gồm phí và trượt giá. Funding được ghi nhận vào ký quỹ isolated, không tự nạp thêm ký quỹ từ tiền khả dụng.
- Khi tài sản ước tính ≥ mục tiêu hoặc ≤ vốn đầu − mức lỗ tối đa, đóng toàn bộ vị thế và kết thúc phiên. Phiên kết thúc không tự chạy lại.
- Lệnh và số dư được ghi trong cùng một giao dịch SQLite. Khởi động lại ứng dụng chuyển phiên đang chạy sang **tạm dừng**; phải tiếp tục thủ công. Trong thời gian máy chủ dừng, bộ mô phỏng không sinh thêm giá.
- Tất cả phép tính tiền dùng `Decimal`; định dạng làm tròn chỉ áp dụng ở giao diện.

## Kiểm thử

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Bộ kiểm thử bao phủ Long/Short, phí hai chiều, trượt giá, funding, thanh lý isolated, SL/TP, dừng theo mục tiêu/lỗ, cặp cho phép, kiểm tra dữ liệu giá, đối soát số dư, khôi phục phiên và các API. Kiểm tra trình duyệt tùy chọn dùng `tests/browser_smoke.py`, cần Playwright và Chromium; script tạo máy chủ với DB tạm riêng và tự dọn khi xong.

## Giới hạn của bản này

- Dữ liệu giá là chuỗi tổng hợp có tính lặp, không phải giá Binance hay backtest lịch sử. Lợi nhuận mô phỏng không chứng minh hiệu quả chiến lược.
- Mô hình thanh lý đơn giản dùng tỷ lệ duy trì cố định 0,5%; coi toàn bộ ký quỹ vị thế bị mất khi thanh lý. Chưa mô phỏng bậc notional, mark/index price, phí thanh lý thật, ADL hay bảo hiểm sàn.
- Chưa mô phỏng order book, tick/step size, min notional, partial fills, funding thực tế và gián đoạn sàn. SL/TP là ngưỡng do mô phỏng xử lý, chưa phải lệnh điều kiện đặt trên Binance.
- Ngưỡng 200 USDT là điều kiện kích hoạt đóng, không phải mức tiền còn lại được bảo đảm nếu giá nhảy. Mục tiêu tăng từ 500 lên 1.000 USDT cũng không được bảo đảm.

Các bước để nối Binance Futures Testnet và sau đó giao dịch thật được ghi ở [docs/binance-integration.md](docs/binance-integration.md).
