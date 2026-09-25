# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống điều tra khiếu nại thương mại điện tử đa tác tử (Multi-Agent A2A) tuân thủ mô hình phân rã nhiệm vụ (Task Decomposition) và kiểm định nghiêm ngặt (Strict Verification).

```text
                                 ┌──────────────────────────┐
                                 │   Coordinator / Router   │
                                 └─────────────┬────────────┘
                                               │ (Handoff & Task Assign)
                ┌──────────────────────────────┼──────────────────────────────┐
                ▼                              ▼                              ▼
       ┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
       │ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
       └────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
                │                              │                              │
                │ (Entity & Order Evidence)    │ (Payment & Refund Evidence)  │ (Tracking Evidence)
                └──────────────────────────────┼──────────────────────────────┘
                                               │ (Evidence Aggregation)
                                               ▼
                                      ┌──────────────────┐
                                      │   Policy Agent   │
                                      └────────┬─────────┘
                                               │ (Proposed Resolution & Conflicts)
                                               ▼
                                      ┌──────────────────┐
                                      │  Verifier Agent  │
                                      └────────┬─────────┘
                                               │ (Schema & Consistency Invariants Check)
                                               ▼
                                          [END OUTPUT]
```

Luồng thực thi:
1. **Case Ingestion**: `Coordinator` tiếp nhận raw case từ runner, phát sinh sự kiện trace `case_received`.
2. **Entity Resolution & Delegation**: Nếu case chứa candidate orders hoặc thông tin chưa rõ ràng, `Coordinator` ủy quyền cho `Order/Item Agent` định danh `resolved_order_ids` và `rejected_candidates`.
3. **Specialist Investigation (A2A Parallel/Sequential Delegation)**:
   - `Order/Item Agent`: Thu thập thông tin giỏ hàng, người bán, sản phẩm.
   - `Payment Agent`: Đối soát dòng tiền, installments, vouchers, tình trạng charge/refund.
   - `Shipment Agent`: Phân tích hành trình vận chuyển, SLA người bán và đơn vị vận chuyển.
4. **Policy & Conflict Synthesis**: `Policy Agent` tổng hợp các bằng chứng, đối chiếu chính sách nền tảng, phát hiện data conflict (khách hàng vs hệ thống, logistics vs seller) và đề xuất phương án bồi hoàn/xử lý.
5. **Independent Verification**: `Verifier Agent` đóng vai trò chốt chặn kiểm thử độc lập: thẩm định tính toàn vẹn của JSON Schema, kiểm tra cross-field consistency invariants, và cô lập bằng chứng trước khi phát `verification_completed` và tạo output cuối cùng.

---

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** | `case: dict` (Raw case) | Phân tích yêu cầu, khởi tạo context, điều phối handoff đến các specialist, giám sát luồng thực thi | *Không gọi MCP trực tiếp* | Task assigned sang Specialist Agents |
| **Order/Item Agent** | `case_id`, candidate order IDs, customer info | Entity resolution (chọn order đúng, loại bỏ candidate sai), truy xuất đơn hàng, mặt hàng, người bán | `get_order_details`, `get_customer_history`, `get_order_items`, `get_product_details` | `entity_resolution`, `customer_context`, `affected_entities` |
| **Shipment Agent** | `case_id`, `order_ids`, `shipping_limit_date` | Kiểm tra hành trình giao nhận, đối chiếu SLA người giao vs người bán, xác định trách nhiệm giao trễ | `get_shipment_tracking`, `get_carrier_status`, `get_seller_sla` | `shipment_analysis` (`verdict`, `late_seller_ids`, `timeline_complete`) |
| **Payment Agent** | `case_id`, `order_ids`, `payment_references` | Đối soát giao dịch, phát hiện trùng lặp/thiếu hụt thanh toán, kiểm tra trạng thái hoàn tiền | `get_payment_transactions`, `get_refund_status`, `get_payment_reconciliation` | `payment_analysis` (`verdict`, `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`) |
| **Policy & Conflict Agent** | Tổng hợp evidence & phân tích từ 3 specialist agents | Đối chiếu điều khoản hoàn tiền, giải quyết mâu thuẫn dữ liệu (data conflicts), xác định nguyên nhân gốc và phương án tài chính | `get_policy_rule`, `get_compensation_matrix` | `root_cause_analysis`, `data_conflicts`, `financial_resolution`, `resolution_actions` |
| **Verifier** | Dự thảo output toàn diện từ Policy Agent | Kiểm tra schema invariants, kiểm tra tính nhất quán logic (consistency invariants), bảo đảm không rò rỉ evidence chéo case | *Không gọi MCP* | Final validated output, emit `verification_completed` |

---

## 3. Entity resolution và A2A protocol

- **Nguyên tắc định danh (Entity Resolution)**:
  - Khi case không cung cấp `order_id` chính xác mà chỉ có danh sách candidates hoặc customer context, Agent sẽ truy vấn lịch sử khách hàng và chi tiết các candidate orders.
  - Candidate phù hợp nhất về thời gian khiếu nại, mã sản phẩm hoặc giá trị đơn hàng được xếp vào `resolved_order_ids` (status: `"resolved"`).
  - Các candidate không khớp bị đưa vào `rejected_candidates`. Nếu không tìm thấy hoặc có nhiều hơn 1 candidate tương đương không thể phân biệt, chuyển status sang `"ambiguous"` hoặc `"not_found"` với confidence tương ứng.
- **A2A Message Envelope & Correlation**:
  - Mọi giao tiếp giữa các agent đều mang `case_id` làm correlation ID bất biến.
  - Các agent trao đổi thông qua cấu trúc dữ liệu tường minh (dataclass / typed dict), không truyền tải prompt tự do hay chain-of-thought vào trace log.
- **Điều kiện Handoff & Chống vòng lặp (Loop Prevention)**:
  - Handoff là luồng đơn hướng (DAG: Coordinator → Specialists → Policy → Verifier).
  - Không có vòng lặp hồi tiếp vô tận: Nếu Verifier phát hiện lỗi không nhất quán không thể tự sửa, nó đưa ra cảnh báo hạ confidence và gán fallback có kiểm soát (`needs_investigation` hoặc `insufficient_evidence`) thay vì quay lại gọi thêm MCP.

---

## 4. Evidence và conflict lifecycle

- **Validation Envelope**:
  - Mọi phản hồi từ `EvidenceGateway` bắt buộc phải khớp với `mcp-evidence-response-v1.schema.json` (chứa `schema_version`, `evidence_ref`, `result_hash`, `domain`, `data`).
- **Lưu trữ & Cô lập Evidence Ref**:
  - Mỗi case duy trì một `evidence_ref_registry` cục bộ. Nghiêm cấm tái sử dụng `evidence_ref` giữa các case khác nhau để tránh dính hard-gate `cross_scope_evidence_ref`.
- **Phát hiện & Xử lý xung đột (Data Conflict Lifecycle)**:
  - Khi có mâu thuẫn giữa lời khai khách hàng (`customer_claim`) và bản ghi hệ thống (`carrier_tracking` hoặc `payment_gateway`):
    - Khởi tạo mục `data_conflicts` với `field`, `sources`, `selected_source`, và `resolution_code`.
    - Thứ tự ưu tiên nguồn tin cậy (Precedence Rule): `audit_log` / `gateway_log` > `carrier_official` > `seller_declared` > `customer_unverified`.
- **Observable Trace Linkage**:
  - Khi một Specialist Agent sử dụng dữ liệu từ MCP để đưa ra quyết định, bắt buộc phải phát sinh trace event `tool_result_consumed` gắn kèm đúng `evidence_refs` tương ứng.

---

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| **MCP timeout / connection error** | Tối đa 2 lần (exponential backoff 500ms, 1000ms) | Đánh dấu domain là `insufficient_evidence` | `tool_call_failed` |
| **Entity not found / ambiguous** | 0 retry (không quét vét ngẫu nhiên) | Gán `entity_resolution.status` = `ambiguous` hoặc `not_found`, confidence <= 0.5 | `task_assigned` / `decision_code: ENTITY_AMBIGUOUS` |
| **Source conflict** | 0 retry | Ghi nhận vào mảng `data_conflicts`, áp dụng thứ tự ưu tiên chuẩn | `policy_decided` / `decision_code: CONFLICT_RESOLVED` |
| **Invalid specialist result** | 1 retry nội bộ | Sử dụng safe default (`needs_investigation`, confidence = 0.3) | `verification_completed` / `decision_code: FALLBACK_APPLIED` |

- **Hiệu quả gọi MCP (Efficiency & Budget Policy)**:
  - Áp dụng **in-case caching**: Cùng một `(tool_name, case_id, args)` trong một case chỉ gọi MCP một lần duy nhất.
  - Giới hạn tổng số cuộc gọi MCP cho mỗi case (tối đa không quá 6-8 tool calls/case) để đạt điểm trọn vẹn ở tiêu chí `efficiency`.

---

## 6. Verification invariants

Trước khi hàm `solve_case` hoàn tất và trả về kết quả, `Verifier Agent` kiểm tra 100% các điều kiện tiên quyết:

1. **Schema Compliance**: Output phải pass 100% kiểm tra `l3b-output-v2.schema.json`. `additionalProperties: false` được bảo toàn tuyệt đối, không có trường thừa.
2. **Entity Consistency**:
   - Mọi `seller_id` xuất hiện trong `shipment_analysis.late_seller_ids` phải thuộc tập `affected_entities.seller_ids`.
   - `resolved_order_ids` và `rejected_candidates` phải rời nhau hoàn toàn (disjoint sets).
3. **Financial Invariants**:
   - `recommended_refund_brl` phải bằng tổng `amount_brl` trong tất cả `refund_lines` (sai số <= 0.01 BRL).
   - Nếu `recommended_refund_brl > 0`, thì `case_status` phải là `action_required`.
   - `recommended_refund_brl` không được vượt quá `payment_analysis.captured_total_brl` hoặc `refundable_total_brl`.
4. **Evidence Ownership**:
   - Toàn bộ các `evidence_refs` có mặt trong `output["evidence_refs"]` và `claim_assessments` phải là các mã `evidence_ref` thực sự được trả về từ MCP Gateway trong chính case này.
5. **Status & Action Consistency**:
   - Nếu `primary_issue == "unsupported_claim"`, `case_status` phải là `no_action`, `recommended_refund_brl` = 0.
   - Số lượng `resolution_actions` từ 0 đến tối đa 8 hành động, không trùng lặp (`uniqueItems: true`).
6. **Confidence Bounds**:
   - Điểm `confidence` của `assessment` và `entity_resolution` phải nằm chặt chẽ trong khoảng `[0.0, 1.0]`.

---

## 7. Reproducibility

- **Ngôn ngữ & Runtime**: Python 3.11 trở lên.
- **Dependencies chính**: `httpx2`, `jsonschema[format]`, `mcp`, `python-dotenv`, `pytest`.
- **Kiến trúc luồng**: Async State-Machine pipeline (Deterministic Directed Acyclic Graph).
- **Concurrency limit**: Xử lý tuần tự từng case hoặc async semaphore (giới hạn 5 concurrent cases khi chạy toàn bộ tập 100 cases).
- **Nguyên tắc bảo mật**: Không bao giờ commit `.env` hoặc Team API Key (`sk-team-...`) vào git, outputs hay traces.

