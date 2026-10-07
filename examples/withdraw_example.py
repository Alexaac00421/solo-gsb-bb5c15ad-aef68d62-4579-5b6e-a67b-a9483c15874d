#!/usr/bin/env python3
"""采集包撤回（POST /bundles/{bundle_id}/withdraw，v1.10）最小示例。

现场发现已导入的采集包有误时，按包编号撤回其对当前时间轴的贡献：
只删除该包首次导入新增的设备/周期/锚点/声段，复用的原有记录绝不可删。
新增记录内容已变化、被其他包复用、相关周期已封存、新增设备/周期已有
包外内容，或删除会改变保留声段的可定位状态/UTC 位置时，指出阻碍并
整包拒绝；重复撤回返回首次结果；撤回后同编号重传仍返回首次导入回执
且不重新写入；已发布版本保持不变。

本脚本演示：

  1) 整包导入 -> 撤回 -> 新增记录全部删除、复用记录保留；
  2) 重复撤回 -> 200 返回首次结果（幂等）；
  3) 撤回后同编号重传 -> 返回首次导入回执，不重新写入；
  4) 内容已变化（声段被删）-> 409 bundle_content_changed；
  5) 被其他包复用 -> 409 bundle_record_reused（先撤回对方后可撤回）；
  6) 新增设备已有包外内容 -> 409 bundle_external_content；
  7) 删除会改变保留声段 UTC 位置 -> 409 bundle_projection_changed；
  8) 相关周期已封存 -> 409 cycle_sealed；旧包/未知编号 -> 409/404；
  9) 已发布版本保持原快照，新版本反映撤回。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/withdraw_example.py [base_url]
默认连接 http://127.0.0.1:8000，仅依赖标准库。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    # ------------------------------------------------------------------
    # 0. 前置：库中已有一台现场设备 D0 与其声段（非包内容，演示"复用保留"）
    # ------------------------------------------------------------------
    call(base, "POST", "/devices", {"device_id": "D0", "name": "基准录音笔"})
    call(base, "POST", "/devices/D0/anchors",
         {"local_ms": 0, "utc_ms": 1700000000000})
    call(base, "POST", "/devices/D0/anchors",
         {"local_ms": 60000, "utc_ms": 1700000060000})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-BASE", "device_id": "D0",
          "local_start_ms": 5000, "local_end_ms": 50000})

    # ------------------------------------------------------------------
    # 1. 整包导入（复用 D0，新增 D1 与其周期/锚点/声段）-> 撤回
    # ------------------------------------------------------------------
    bundle = {
        "bundle_id": "FIELD-2026-10-07-001",
        "devices": [{"device_id": "D0", "name": "基准录音笔"},   # 复用
                    {"device_id": "D1", "name": "录音笔-丙"}],   # 新增
        "anchors": [
            {"device_id": "D1", "local_ms": 0, "utc_ms": 1700000100000},
            {"device_id": "D1", "local_ms": 60000, "utc_ms": 1700000160000},
        ],
        "segments": [
            {"capture_id": "CAP-9001", "device_id": "D1",
             "local_start_ms": 5000, "local_end_ms": 20000, "note": "误录包"},
        ],
    }
    status, first = call(base, "POST", "/bundles", bundle)
    show("1. 整包导入（D0 复用，D1 新增）", status, first)
    assert status == 201 and first["devices"] == {"added": 1, "reused": 1}

    status, result = call(base, "POST", "/bundles/FIELD-2026-10-07-001/withdraw")
    show("   按包编号撤回 -> 只删新增记录", status, result)
    assert status == 200 and result["status"] == "withdrawn"
    assert result["devices"]["removed"] == 1
    assert result["anchors"]["removed"] == 2
    assert result["segments"]["capture_ids"] == ["CAP-9001"]

    # 复用的 D0 与其声段原样保留；新增的 D1/CAP-9001 已删除
    status, seg = call(base, "GET", "/segments/CAP-BASE")
    assert status == 200 and seg["segment"]["status"] == "located"
    status, _ = call(base, "GET", "/devices/D1")
    assert status == 404
    status, _ = call(base, "GET", "/segments/CAP-9001")
    assert status == 404
    print("   复用记录保留：CAP-BASE 仍可定位；新增 D1/CAP-9001 已删除")

    # ------------------------------------------------------------------
    # 2. 重复撤回 -> 200 返回首次结果（幂等，不重复删除）
    # ------------------------------------------------------------------
    status, replay = call(base, "POST", "/bundles/FIELD-2026-10-07-001/withdraw")
    show("2. 重复撤回 -> 返回首次结果", status, replay)
    assert status == 200 and replay["idempotent_replay"] is True
    assert replay["withdrawn_at"] == result["withdrawn_at"]

    # ------------------------------------------------------------------
    # 3. 撤回后同编号重传 -> 返回首次导入回执，不重新写入
    # ------------------------------------------------------------------
    status, receipt = call(base, "POST", "/bundles", bundle)
    show("3. 撤回后同编号重传 -> 首次导入回执，不重新写入", status, receipt)
    assert status == 200 and receipt["idempotent_replay"] is True
    status, _ = call(base, "GET", "/devices/D1")
    assert status == 404  # 数据保持撤回状态

    # ------------------------------------------------------------------
    # 4. 新增记录内容已变化 -> 409 bundle_content_changed
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-002",
        "devices": [{"device_id": "D2"}],
        "anchors": [{"device_id": "D2", "local_ms": 0, "utc_ms": 1700000200000},
                    {"device_id": "D2", "local_ms": 60000, "utc_ms": 1700000260000}],
        "segments": [{"capture_id": "CAP-9002", "device_id": "D2",
                      "local_start_ms": 100, "local_end_ms": 200}]})
    call(base, "DELETE", "/segments/CAP-9002")  # 包外删除了包内声段
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-002/withdraw")
    show("4. 新增声段已被删除 -> 409 bundle_content_changed", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_content_changed"

    # ------------------------------------------------------------------
    # 5. 被其他包复用 -> 409 bundle_record_reused（先撤回对方后可撤回）
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-003",
        "devices": [{"device_id": "D3"}],
        "anchors": [{"device_id": "D3", "local_ms": 0, "utc_ms": 1700000300000},
                    {"device_id": "D3", "local_ms": 60000, "utc_ms": 1700000360000}]})
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-004",
        "anchors": [{"device_id": "D3", "local_ms": 0, "utc_ms": 1700000300000}],
        "segments": [{"capture_id": "CAP-9004", "device_id": "D3",
                      "local_start_ms": 10, "local_end_ms": 20}]})
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-003/withdraw")
    show("5. 锚点被其他包复用 -> 409 bundle_record_reused", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_record_reused"
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-004/withdraw")
    assert status == 200  # 先撤回复用方（只删其新增声段）
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-003/withdraw")
    show("   先撤回复用方后再撤回 -> 200", status, body)
    assert status == 200

    # ------------------------------------------------------------------
    # 6. 新增设备已有包外内容 -> 409 bundle_external_content
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-005", "devices": [{"device_id": "D4"}]})
    call(base, "POST", "/devices/D4/anchors",
         {"local_ms": 0, "utc_ms": 1700000400000})  # 包外写入
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-005/withdraw")
    show("6. 新增设备已有包外锚点 -> 409 bundle_external_content", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_external_content"

    # ------------------------------------------------------------------
    # 7. 删除会改变保留声段 UTC 位置 -> 409 bundle_projection_changed
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-006",
        "anchors": [{"device_id": "D0", "local_ms": 30000,
                     "utc_ms": 1700000031000}]})  # 偏离 D0 原直线的锚点
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-006/withdraw")
    show("7. 删除将改变保留声段 UTC 位置 -> 409 bundle_projection_changed",
         status, body)
    assert status == 409 and body["error"]["code"] == "bundle_projection_changed"

    # ------------------------------------------------------------------
    # 8. 相关周期已封存 -> 409 cycle_sealed；未知编号 -> 404
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-007",
        "devices": [{"device_id": "D5"}],
        "anchors": [{"device_id": "D5", "local_ms": 0, "utc_ms": 1700000500000},
                    {"device_id": "D5", "local_ms": 60000, "utc_ms": 1700000560000}]})
    _, detail = call(base, "GET", "/devices/D5/cycles/default")
    call(base, "POST", "/devices/D5/cycles/default/seal",
         {"fingerprint": detail["cycle"]["fingerprint"]})
    status, body = call(base, "POST", "/bundles/FIELD-2026-10-07-007/withdraw")
    show("8. 相关周期已封存 -> 409 cycle_sealed", status, body)
    assert status == 409 and body["error"]["code"] == "cycle_sealed"
    status, body = call(base, "POST", "/bundles/NOPE/withdraw")
    show("   包编号不存在 -> 404 bundle_not_found", status, body)
    assert status == 404 and body["error"]["code"] == "bundle_not_found"

    # ------------------------------------------------------------------
    # 9. 已发布版本保持原快照；之后发布的版本反映撤回
    # ------------------------------------------------------------------
    call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-07-008",
        "devices": [{"device_id": "D6"}],
        "anchors": [{"device_id": "D6", "local_ms": 0, "utc_ms": 1700000600000},
                    {"device_id": "D6", "local_ms": 60000, "utc_ms": 1700000660000}],
        "segments": [{"capture_id": "CAP-9008", "device_id": "D6",
                      "local_start_ms": 100, "local_end_ms": 200}]})
    status, v1 = call(base, "POST", "/versions", {"note": "撤回前"})
    assert status == 201
    call(base, "POST", "/bundles/FIELD-2026-10-07-008/withdraw")
    status, v1_again = call(base, "GET", f"/versions/{v1['version_no']}")
    assert v1_again == v1  # 已发布版本逐字节不变
    status, v2 = call(base, "POST", "/versions", {"note": "撤回后"})
    show("9. 撤回后发布新版本：CAP-9008 进入 removed", status,
         {"version_no": v2["version_no"],
          "removed": [s["capture_id"] for s in v2["changes"]["removed"]]})
    assert "CAP-9008" in [s["capture_id"] for s in v2["changes"]["removed"]]

    print("\n全部断言通过：采集包撤回 / 幂等 / 整包拒绝 / 版本共存均符合预期。")


if __name__ == "__main__":
    main()
