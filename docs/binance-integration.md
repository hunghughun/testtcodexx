# Binance USDⓈ-M Futures Demo và tiền thật — trạng thái triển khai

Có hai adapter REST cố định: `BinanceDemo` → **https://demo-fapi.binance.com**, `BinanceLive` → **https://fapi.binance.com**. Giao diện `/demo` và `/live`, API `/api/demo/*` và `/api/live/*`, tệp DB và kết nối giữ khóa riêng. Không có URL tùy ý, tự fallback hoặc tái sử dụng khóa khi đổi môi trường. Không hỗ trợ RSA/Ed25519, Spot, COIN-M, Portfolio Margin, Binance.US hoặc Futures Testnet cũ.

Logic chung ở `bot/futures.py`; `bot/demo.py` giữ import tương thích. DB tiền thật có marker `environment=live`, không chấp nhận DB Demo cũ kể cả thiếu marker; client IDs bắt đầu `tpl_` thay vì `tpd_`. `ProcessLock` dùng `flock` trên Linux hoặc `msvcrt.locking` trên Windows để ngăn hai tiến trình mở cùng DB tiền thật trước khi đọc/sửa trạng thái. Không bảo vệ nhiều DB hoặc nhiều máy cùng giao dịch một tài khoản.

`POST /api/live/start` nhận `{config: {...}, confirm_live: "GIAO DICH TIEN THAT"}`; resume cũng cần `confirm_live`. Service xác minh câu này cả khi gọi trực tiếp, trước mọi thay đổi tài khoản. Connect chỉ đọc; không đọc khóa từ môi trường, không tự start/resume lúc mở ứng dụng. Stop không cần câu xác nhận để người dùng luôn có thể yêu cầu đóng khi đã kết nối. Mặc định thủ công, tùy chọn SMA cho phép mở lệnh tự động sau khi bắt đầu.

## Hợp đồng API được đối chiếu

Đối chiếu mã nguồn SDK chính thức [binance/binance-connector-python](https://github.com/binance/binance-connector-python/tree/91a2100258a1eaf8e5a0e0e6393ff3fafa4336a8) ngày 30/10/2026: `common/src/binance_common/constants.py` và `clients/derivatives_trading_usds_futures/.../rest_api/api/`. Adapter dùng Python standard library, không chép mã SDK hay phụ thuộc hành vi retry của SDK.

| Chức năng | REST |
| --- | --- |
| Đồng bộ giờ, metadata, mark và nến đóng | GET `/fapi/v1/time`, `/exchangeInfo`, `/premiumIndex`, `/klines` |
| Tài khoản, vị thế | GET `/fapi/v3/account`, `/fapi/v3/positionRisk` |
| Kiểm tra mode, phí, cấu hình cặp | GET `/fapi/v1/accountConfig`, `/feeBurn`, `/commissionRate`, `/symbolConfig` |
| Isolated và đòn bẩy | POST `/fapi/v1/marginType`, `/fapi/v1/leverage` |
| Lệnh market, đóng reduce-only, query/cancel | POST/GET/DELETE `/fapi/v1/order` |
| SL/TP native, query/cancel | POST/GET/DELETE `/fapi/v1/algoOrder` |
| Lệnh chờ thường / điều kiện | GET `/fapi/v1/openOrders`, `/fapi/v1/openAlgoOrders` |
| Phí và realized PnL / funding | GET `/fapi/v1/userTrades`, `/fapi/v1/income` |

`/fapi/v3/account` chỉ lấy số dư/vị thế; cờ `canTrade`, `dualSidePosition`, `multiAssetsMargin` lấy từ `/fapi/v1/accountConfig`. `/feeBurn` phải trả `feeBurn=false` để hạch toán phí USDT. Không tự đổi chế độ toàn tài khoản hoặc tắt BNB thay người dùng. `canTrade` là quyền giao dịch của tài khoản, không phải bằng chứng mọi quyền của API Key hợp lệ; người dùng kiểm tra quyền Futures, IP và tắt quyền rút tiền tại Binance.

SL/TP dùng `algoType=CONDITIONAL`, `STOP_MARKET`/`TAKE_PROFIT_MARKET`, `triggerPrice`, `workingType=MARK_PRICE`, `quantity` theo phần đã khớp, `reduceOnly=true`. Không trộn `closePosition=true` với quantity/reduceOnly. Mỗi phía được gửi riêng; đây không phải giao dịch nguyên tử hay OCO phía sàn. Bot hủy lệnh bảo vệ còn lại sau khi xác nhận vị thế đóng.

## Giới hạn và quản lý rủi ro

- Dùng tài khoản Futures riêng, ban đầu không có vị thế hoặc lệnh chờ trên bất kỳ cặp nào; yêu cầu one-way, single-asset USDT. Không tự đổi các mode toàn tài khoản.
- Bắt đầu phiên mới chỉ cài isolated/đòn bẩy cho cặp đã chọn khi tài khoản đang trống. Kiểm tra lại cấu hình trước mỗi lệnh mở; Auto-add Margin phải tắt. Ký quỹ mỗi lệnh và tổng ký quỹ bị chặn theo ngân sách phiên và số dư khả dụng.
- Xác minh hợp đồng USDT perpetual đang giao dịch, tick size, LOT_SIZE/MARKET_LOT_SIZE và min notional. SL/TP làm tròn theo bước giá.
- Tiền nạp vào tài khoản không được tính là lãi phiên. Net PnL dùng các fill có order ID thuộc bot, phí USDT và funding. Cộng unrealized PnL theo mark mới, trừ phí taker và trượt giá đóng ước tính. Nếu lịch sử fill chưa đồng bộ, chưa xét đạt mục tiêu hoặc mở thêm lệnh; vẫn yêu cầu đóng nếu tài sản đã chạm ngưỡng lỗ, sau đó chờ đối soát phí. Phần ký quỹ còn dùng không giảm theo giá nếu sàn vẫn giữ isolated wallet; lãi chưa chốt không cho phép dùng tiền ngoài vốn được cấp.
- Kiểm tra tài sản sau đóng thực tế; nếu thấp hơn mục tiêu, trạng thái là đã dừng, không tuyên bố đạt mục tiêu. Ngưỡng lỗ không bảo đảm giá khớp.
- Tín hiệu SMA 5/15 chỉ dùng nến 1 phút đã đóng, có kiểm tra liên tục và độ mới. Nến đầu là baseline, không vào lệnh dựa trên tín hiệu cũ. REST polling khoảng 10 giây, chưa có user data stream/WebSocket.

## Lỗi và khôi phục

Mỗi lệnh có client ID duy nhất được commit vào SQLite **trước** khi gửi. Timeout được query theo ID; không retry POST mù. Khớp một phần được hủy phần còn lại và chỉ bảo vệ lượng đã khớp. Nếu không đặt đủ SL/TP, bot cố đóng giảm vị thế, giữ trạng thái tạm dừng nếu chưa xác minh hết rủi ro.

Khi stop, hủy phần entry còn mở trước, đóng reduce-only, xác minh vị thế bằng không rồi hủy tất cả algo thuộc phiên (kể cả intent chưa liên kết vào bracket). Không hủy toàn bộ lệnh tài khoản. Phát hiện vị thế/lệnh/fill ngoài bot thì tạm dừng để tránh xử lý nhầm.

Khởi động lại chuyển phiên đang chạy thành paused; nhập lại cùng khóa rồi đối soát/tiếp tục hoặc đóng. Connect là chỉ đọc. Lệnh UNKNOWN vẫn chưa xác minh được sẽ chặn phiên mới; cần kiểm tra trên Binance, không xóa DB để né bước này. Mạng/sàn lỗi có thể làm việc đóng vị thế thất bại. Trong trường hợp đó không được coi bot đã đóng được vị thế.

Nếu cần đối soát >=1.000 fill cho một cặp hoặc >=1.000 bản ghi funding một lần, phiên dừng để kiểm tra thay vì bỏ sót dữ liệu. Chưa hỗ trợ nhiều tiến trình cùng tài khoản, multi-account, đổi API Key giữa phiên hay thanh toán phí bằng coin khác USDT. Khóa DB tiền thật chặn tiến trình thứ hai; không có điều phối giữa nhiều máy. Thanh lý/ADL hoặc thao tác thủ công có thể làm đối soát không khớp và cần can thiệp trên Binance.

## Khóa và mạng

API Key/Secret là `SecretStr` tại biên API và chỉ giữ trong RAM; không nằm trong DB, export, localStorage, log hoặc Git. Lưu fingerprint SHA-256 của API Key để tránh khôi phục phiên bằng tài khoản khác. Không tự đọc khóa từ biến môi trường; khóa được gửi tới môi trường cố định tương ứng với trang người dùng chọn. URL đích cố định HTTPS, kiểm tra TLS, không theo redirect, không in URL có chữ ký. Lỗi validation bỏ trường input để tránh echo khóa. Khi bị rate limit, giữ thời gian chờ Retry-After và không retry write. Các mã Binance -1001/-1006/-1007 được coi là chưa rõ kết quả ghi, kể cả khi HTTP không phải 5xx.

Cloud hiện chưa có khóa Binance. Proxy cloud trả CONNECT 403 cho host Demo tại lúc triển khai; đã thêm `demo-fapi.binance.com` và `developers.binance.com` vào **bản nháp** mạng, chưa chứng minh policy runtime đã đổi. Windows chạy độc lập với chính sách cloud. Host production `fapi.binance.com` cũng chưa được runtime cho phép. Người dùng nhập khóa HMAC đúng môi trường vào ứng dụng trên máy mình; proxy secret placeholder không phải secret thực dùng để ký HMAC.

## Bằng chứng và phần chưa xác minh

Kiểm thử tự động và browser smoke sử dụng fake exchange, bao gồm timeout sau fill, timeout chưa rõ, partial fill, SL/TP lỗi, thanh lý/đóng, chống lệnh trùng, mục tiêu/lỗ, phí/funding, khóa không lộ và khôi phục trạng thái. Đây là kiểm tra hành vi phần mềm với phản hồi mô phỏng.

**Chưa xác minh end-to-end trên tài khoản Binance Demo hoặc production**, do không có khóa và mạng cloud đang chặn. Chưa xác minh hiệu quả chiến lược, chạy lâu dài, sàn thực tế hay tệp .bat trực tiếp trên Windows. Chế độ tiền thật đã triển khai và người dùng có thể tự kích hoạt, nhưng chưa được chứng nhận bằng giao dịch thật. Mở ứng dụng chỉ tạo các phiên idle, không thực hiện giao dịch.

Kiểm thử bổ sung cho tiền thật kiểm tra host/HMAC cố định, dạng phản hồi account V3/accountConfig theo SDK, xác nhận start/resume, khóa/DB/mã lệnh tách Demo, khóa tiến trình, phát hiện lệch tài khoản, SL/TP/timeout, phí chậm và giới hạn tài sản. Browser smoke dùng hai FakeBinance riêng, không dùng adapter mạng để gửi lệnh. Dữ liệu kiểm thử không phải bằng chứng giao dịch Binance thật.
