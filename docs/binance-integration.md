# Binance USDⓈ-M Futures Demo — trạng thái triển khai

Đã có adapter REST và giao diện `/demo` để kết nối **https://demo-fapi.binance.com** bằng khóa HMAC Demo. Không hỗ trợ URL tùy ý, khóa RSA/Ed25519, Spot Testnet, Futures Testnet cũ hoặc tiền thật. Không có fallback sang `fapi.binance.com`.

## Hợp đồng API được đối chiếu

Đối chiếu mã nguồn SDK chính thức [binance/binance-connector-python](https://github.com/binance/binance-connector-python/tree/91a2100258a1eaf8e5a0e0e6393ff3fafa4336a8) ngày 29/10/2026: `common/src/binance_common/constants.py` và `clients/derivatives_trading_usds_futures/.../rest_api/api/`. Adapter dùng Python standard library, không chép mã SDK hay phụ thuộc hành vi retry của SDK.

| Chức năng | REST |
| --- | --- |
| Đồng bộ giờ, metadata, mark và nến đóng | GET `/fapi/v1/time`, `/exchangeInfo`, `/premiumIndex`, `/klines` |
| Tài khoản, vị thế | GET `/fapi/v3/account`, `/fapi/v3/positionRisk` |
| Kiểm tra mode, phí, cấu hình cặp | GET `/fapi/v1/positionSide/dual`, `/multiAssetsMargin`, `/commissionRate`, `/symbolConfig` |
| Isolated và đòn bẩy | POST `/fapi/v1/marginType`, `/fapi/v1/leverage` |
| Lệnh market, đóng reduce-only, query/cancel | POST/GET/DELETE `/fapi/v1/order` |
| SL/TP native, query/cancel | POST/GET/DELETE `/fapi/v1/algoOrder` |
| Lệnh chờ thường / điều kiện | GET `/fapi/v1/openOrders`, `/fapi/v1/openAlgoOrders` |
| Phí và realized PnL / funding | GET `/fapi/v1/userTrades`, `/fapi/v1/income` |

SL/TP dùng `algoType=CONDITIONAL`, `STOP_MARKET`/`TAKE_PROFIT_MARKET`, `triggerPrice`, `workingType=MARK_PRICE`, `quantity` theo phần đã khớp, `reduceOnly=true`. Không trộn `closePosition=true` với quantity/reduceOnly. Mỗi phía được gửi riêng; đây không phải giao dịch nguyên tử hay OCO phía sàn. Bot hủy lệnh bảo vệ còn lại sau khi xác nhận vị thế đóng.

## Giới hạn và quản lý rủi ro

- Dùng tài khoản Demo riêng, ban đầu không có vị thế hoặc lệnh chờ trên bất kỳ cặp nào; yêu cầu one-way, single-asset USDT. Không tự đổi các mode toàn tài khoản.
- Bắt đầu phiên mới chỉ cài isolated/đòn bẩy cho cặp đã chọn khi tài khoản đang trống. Kiểm tra lại cấu hình trước mỗi lệnh mở; Auto-add Margin phải tắt. Ký quỹ mỗi lệnh và tổng ký quỹ bị chặn theo ngân sách phiên và số dư khả dụng.
- Xác minh hợp đồng USDT perpetual đang giao dịch, tick size, LOT_SIZE/MARKET_LOT_SIZE và min notional. SL/TP làm tròn theo bước giá.
- Tiền nạp vào tài khoản không được tính là lãi phiên. Net PnL dùng các fill có order ID thuộc bot, phí USDT và funding. Cộng unrealized PnL theo mark mới, trừ phí taker và trượt giá đóng ước tính. Nếu lịch sử fill chưa đồng bộ, chưa xét đạt mục tiêu hoặc mở thêm lệnh.
- Kiểm tra tài sản sau đóng thực tế; nếu thấp hơn mục tiêu, trạng thái là đã dừng, không tuyên bố đạt mục tiêu. Ngưỡng lỗ không bảo đảm giá khớp.
- Tín hiệu SMA 5/15 chỉ dùng nến 1 phút đã đóng, có kiểm tra liên tục và độ mới. Nến đầu là baseline, không vào lệnh dựa trên tín hiệu cũ. REST polling khoảng 10 giây, chưa có user data stream/WebSocket.

## Lỗi và khôi phục

Mỗi lệnh có client ID duy nhất được commit vào SQLite **trước** khi gửi. Timeout được query theo ID; không retry POST mù. Khớp một phần được hủy phần còn lại và chỉ bảo vệ lượng đã khớp. Nếu không đặt đủ SL/TP, bot cố đóng giảm vị thế, giữ trạng thái tạm dừng nếu chưa xác minh hết rủi ro.

Khi stop, hủy phần entry còn mở trước, đóng reduce-only, xác minh vị thế bằng không rồi hủy tất cả algo thuộc phiên (kể cả intent chưa liên kết vào bracket). Không hủy toàn bộ lệnh tài khoản. Phát hiện vị thế/lệnh/fill ngoài bot thì tạm dừng để tránh xử lý nhầm.

Khởi động lại chuyển phiên đang chạy thành paused; nhập lại cùng khóa rồi đối soát/tiếp tục hoặc đóng. Connect là chỉ đọc. Lệnh UNKNOWN vẫn chưa xác minh được sẽ chặn phiên mới; cần kiểm tra trên Binance, không xóa DB để né bước này. Mạng/sàn lỗi có thể làm việc đóng vị thế thất bại. Trong trường hợp đó không được coi bot đã đóng được vị thế.

Nếu cần đối soát >=1.000 fill cho một cặp hoặc >=1.000 bản ghi funding một lần, phiên dừng để kiểm tra thay vì bỏ sót dữ liệu. Chưa hỗ trợ nhiều tiến trình cùng DB, multi-account, đổi API Key giữa phiên hay thanh toán phí bằng coin khác USDT.

## Khóa và mạng

API Key/Secret là `SecretStr` tại biên API và chỉ giữ trong RAM; không nằm trong DB, export, localStorage, log hoặc Git. Lưu fingerprint SHA-256 của API Key để tránh khôi phục phiên bằng tài khoản khác. Không tự đọc khóa từ biến môi trường hoặc gửi khóa cho môi trường thật. URL đích cố định HTTPS, kiểm tra TLS, không theo redirect, không in URL có chữ ký. Lỗi validation bỏ trường input để tránh echo khóa. Khi bị rate limit, giữ thời gian chờ Retry-After và không retry write.

Cloud hiện chưa có khóa Binance. Proxy cloud trả CONNECT 403 cho host Demo tại lúc triển khai; đã thêm `demo-fapi.binance.com` và `developers.binance.com` vào **bản nháp** mạng, chưa chứng minh policy runtime đã đổi. Windows chạy độc lập với chính sách cloud. Người dùng nhập khóa HMAC Demo vào ứng dụng trên máy mình; proxy secret placeholder không phải secret thực dùng để ký HMAC.

## Bằng chứng và phần chưa xác minh

Kiểm thử tự động và browser smoke sử dụng fake exchange, bao gồm timeout sau fill, timeout chưa rõ, partial fill, SL/TP lỗi, thanh lý/đóng, chống lệnh trùng, mục tiêu/lỗ, phí/funding, khóa không lộ và khôi phục trạng thái. Đây là kiểm tra hành vi phần mềm với phản hồi mô phỏng.

**Chưa xác minh end-to-end trên tài khoản Binance Demo thật**, do không có khóa và mạng cloud đang chặn. Chưa xác minh hiệu quả chiến lược, chạy lâu dài, sàn thực tế hoặc giao dịch tiền thật. Chế độ tiền thật vẫn bị khóa; phải hoàn thành kiểm tra thực tế và bổ sung các năng lực vận hành trước khi xem xét giai đoạn đó.
