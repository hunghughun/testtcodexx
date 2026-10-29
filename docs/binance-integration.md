# Giai đoạn tiếp theo: Binance USDⓈ-M Futures

Hiện tại chỉ có mô phỏng cục bộ. Chưa có adapter gọi Binance, khóa API, lệnh sàn, dữ liệu thị trường thật hoặc kết quả Testnet. Không đổi nhãn môi trường để coi mô phỏng là giao dịch thật.

## Cần triển khai trước khi thử trên Testnet

1. Đọc tài liệu Binance Futures hiện hành để xác định API lệnh thường và lệnh điều kiện SL/TP, URL Testnet và dữ liệu user stream. Không giả định API Spot dùng được cho Futures.
2. Thêm adapter thị trường và sàn tách biệt với engine: candle đóng thực tế, mark price, metadata `exchangeInfo`, tick/step size, min notional, mức đòn bẩy và ký quỹ duy trì theo bậc.
3. Cài isolated 5x và one-way position mode, xác minh cấu hình hiệu lực. Chỉ quản lý BTCUSDT và ETHUSDT trong phạm vi vốn được cấp cho bot; tài khoản phụ dành riêng giúp tránh xung đột với lệnh tay hay bot khác.
4. Định danh lệnh duy nhất, lưu ý định trước khi gửi; đối soát trạng thái sàn sau timeout/restart. Chờ xác nhận khớp, xử lý partial fills, đặt SL/TP phía sàn bằng cơ chế giảm/đóng vị thế phù hợp. Nếu không bảo vệ được vị thế, ngừng mở mới và xử lý đóng theo quy tắc đã kiểm thử.
5. Dừng khẩn cấp phải hủy lệnh chờ do bot tạo, đóng vị thế do bot quản lý, xác minh khớp và xác nhận không còn rủi ro mở. Không đánh dấu đã dừng chỉ dựa vào HTTP 200 khi chưa biết trạng thái lệnh.
6. Tính tài sản sau phí/funding thực tế và chi phí đóng ước tính, nhận diện nạp/rút tiền ngoài phiên. Khi mất kết nối hoặc dữ liệu cũ, ngừng mở lệnh; không dùng giá cũ để tuyên bố đạt mục tiêu.
7. Thử reconnect, rate limit, đồng bộ thời gian, clock skew, lỗi ký yêu cầu, mất tiến trình, lệnh trùng, giá gap và đối soát số dư trên Testnet. Chạy backtest bằng dữ liệu thật với phí/slippage và thử ngoài mẫu trước khi đánh giá chiến lược.

## Thông tin cần trước khi kết nối

Khóa API Testnet phải được nhập qua cấu hình bí mật phía máy chủ, không qua chat, mã nguồn, URL hoặc trình duyệt. Việc ký HMAC cần một secret thực sự tại tiến trình ký; không dùng chuỗi placeholder của proxy như secret thật. Cần kiểm tra cơ chế cấp bí mật được nền tảng hỗ trợ trước khi triển khai.

Khi chuyển sang tiền thật, dùng quyền giao dịch cần thiết và không cấp quyền rút tiền; xác nhận tài khoản, phạm vi vốn, chiến lược và cấu hình rủi ro thực tế. Bản hiện tại chưa có công tắc giao dịch thật nên không thể vô tình gửi lệnh Binance.
