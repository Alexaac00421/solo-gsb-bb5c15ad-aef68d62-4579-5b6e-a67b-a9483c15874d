#!/usr/bin/env python3
"""采集包撤回（POST /bundles/{bundle_id}/withdraw，v1.10）最小示例。

现场发现已导入的采集包有误时，按包编号撤回其对当前时间轴的贡献：
只删除该包首次导入**新增**的设备/周期（含新设备隐式 default 周期）/
锚点/声段，导入时**复用**的原有记录绝不删除。新增记录当前内容已变化、
被其他未撤回包复用、新设备/周期已有包外内容、相关周期已封存，或删除
会改变任何保留声段的可定位状态/UTC 位置时，指出阻碍并整包拒绝，不改动
任何数据。撤回成功后包行保留：重复撤回返回首次撤回结果；同编号导入
重传仍返回首次导入回执且不重新写入；已发布版本保持原快照。

本脚本演示：

  1) 整包导入两台设备 -> 发布 v1；
  2) 撤回有误的包 -> 200，各类撤回数量；当前时间轴移除，v1 保持原快照；
  3) 重复撤回 -> 200 返回首次撤回结果（幂等）；
  4) 撤回后同编号重传 -> 仍返回首次导入回执，不重新写入；
  5) 各类整包拒绝：内容已变化 / 被其他包复用 / 包外内容 / 周期已封存 /
     保留声段定位会改变 / 包编号不存在；
  6) 混合新增/复用的包：撤回只删新增，复用记录保留。

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
    # 1. 整包导入：两台设备的采集包（设备/锚点/声段全部新增）
    # ------------------------------------------------------------------
    bundle = {
        "bundle_id": "FIELD-2026-10-07-001",
        "devices": [{"device_id": "D1", "name": "录音笔-甲"},
                    {"device_id": "D2", "name": "录音笔-乙"}],
        "anchors": [
            {"device_id": "D1", "local_ms": 0, "utc_ms": 1700000000000},
            {"device_id": "D1", "local_ms": 60000, "utc_ms": 1700000060000},
            {"device_id": "D2", "local_ms": 0, "utc_ms": 1699999997000},
            {"device_id": "D2", "local_ms": 60000, "utc_ms": 1700000057000},
        ],
        "segments": [
            {"capture_id": "CAP-0001", "device_id": "D1",
             "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"},
            {"capture_id": "CAP-0002", "device_id": "D2",
             "local_start_ms": 3000, "local_end_ms": 9000},
        ],
    }
    status, first = call(base, "POST", "/bundles", bundle)
    show("1. 整包导入采集包（201）", status, first)
    assert status == 201 and first["devices"]["added"] == 2

    status, v1 = call(base, "POST", "/versions", {"note": "导入后发布"})
    print(f"\n   已发布 v{v1['version_no']}：segment_count={v1['segment_count']}")

    # ------------------------------------------------------------------
    # 2. 现场发现包有误 -> 按包编号撤回：只删除该包新增的记录
    # ------------------------------------------------------------------
    status, withdrawn = call(base, "POST", "/bundles/FIELD-2026-10-07-001/withdraw")
    show("2. 撤回采集包（200，各类撤回数量与状态）", status, withdrawn)
    assert status == 200 and withdrawn["status"] == "withdrawn"
    # 周期计数含两台新设备隐式创建的 default 周期
    assert withdrawn["devices"] == {"removed": 2}
    assert withdrawn["cycles"] == {"removed": 2}
    assert withdrawn["anchors"] == {"removed": 4}
    assert withdrawn["segments"] == {"removed": 2}

    status, tl = call(base, "GET", "/timeline/current")
    print(f"\n   撤回后当前时间轴：devices={len(tl['devices'])}, "
          f"segments={tl['segment_count']}")
    assert tl["segment_count"] == 0

    status, doc = call(base, "GET", f"/versions/{v1['version_no']}")
    print(f"   已发布 v{v1['version_no']} 保持原快照："
          f"segment_count={doc['segment_count']}")
    assert doc["segment_count"] == 2

    # ------------------------------------------------------------------
    # 3. 重复撤回 -> 返回首次撤回结果（撤回时间不变）
    # ------------------------------------------------------------------
    status, replay = call(base, "POST", "/bundles/FIELD-2026-10-07-001/withdraw")
    show("3. 重复撤回 -> 200 返回首次撤回结果（幂等）", status, replay)
    assert status == 200 and replay["idempotent_replay"] is True
    assert replay["withdrawn_at"] == withdrawn["withdrawn_at"]

    # ------------------------------------------------------------------
    # 4. 撤回后同编号重传 -> 仍返回首次导入回执，不重新写入
    # ------------------------------------------------------------------
    status, reimport = call(base, "POST", "/bundles", bundle)
    show("4. 撤回后同编号重传 -> 首次导入回执，不重新写入", status, reimport)
    assert status == 200 and reimport["idempotent_replay"] is True
    status, tl = call(base, "GET", "/timeline/current")
    assert tl["segment_count"] == 0  # 未重新写入

    # ------------------------------------------------------------------
    # 5. 各类整包拒绝（指出阻碍，不改动任何数据）
    # ------------------------------------------------------------------
    # 5a. 新增记录当前内容已变化：导入后锚点被更正
    call(base, "POST", "/bundles", {
        "bundle_id": "B-CHANGED",
        "devices": [{"device_id": "D3"}],
        "anchors": [{"device_id": "D3", "local_ms": 0, "utc_ms": 1000},
                    {"device_id": "D3", "local_ms": 10000, "utc_ms": 11000}]})
    call(base, "PUT", "/devices/D3/anchors",
         {"anchors": [{"local_ms": 0, "utc_ms": 1000},
                      {"local_ms": 10000, "utc_ms": 11500}]})
    status, body = call(base, "POST", "/bundles/B-CHANGED/withdraw")
    show("5a. 新增锚点已被更正 -> 409 bundle_record_changed", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_record_changed"

    # 5b. 被其他未撤回包复用
    call(base, "POST", "/bundles", {
        "bundle_id": "B-BASE",
        "devices": [{"device_id": "D4"}],
        "anchors": [{"device_id": "D4", "local_ms": 0, "utc_ms": 1000},
                    {"device_id": "D4", "local_ms": 10000, "utc_ms": 11000}]})
    call(base, "POST", "/bundles", {
        "bundle_id": "B-DEP",
        "devices": [{"device_id": "D4"}],  # 复用 B-BASE 的设备
        "anchors": [{"device_id": "D4", "local_ms": 20000, "utc_ms": 21000}]})
    status, body = call(base, "POST", "/bundles/B-BASE/withdraw")
    show("5b. 新增设备被其他包复用 -> 409 bundle_record_reused", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_record_reused"
    # 先撤回依赖方，本包即可撤回
    status, _ = call(base, "POST", "/bundles/B-DEP/withdraw")
    assert status == 200
    status, _ = call(base, "POST", "/bundles/B-BASE/withdraw")
    assert status == 200

    # 5c. 新设备已有包外内容（直接录入追加）
    call(base, "POST", "/bundles", {
        "bundle_id": "B-EXT",
        "devices": [{"device_id": "D5"}],
        "anchors": [{"device_id": "D5", "local_ms": 0, "utc_ms": 1000},
                    {"device_id": "D5", "local_ms": 10000, "utc_ms": 11000}]})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-EXT", "device_id": "D5",
          "local_start_ms": 100, "local_end_ms": 200})
    status, body = call(base, "POST", "/bundles/B-EXT/withdraw")
    show("5c. 新设备已有包外声段 -> 409 bundle_external_content", status, body)
    assert status == 409 and body["error"]["code"] == "bundle_external_content"

    # 5d. 相关周期已封存
    call(base, "POST", "/bundles", {
        "bundle_id": "B-SEALED",
        "devices": [{"device_id": "D6"}],
        "anchors": [{"device_id": "D6", "local_ms": 0, "utc_ms": 1000},
                    {"device_id": "D6", "local_ms": 10000, "utc_ms": 11000}]})
    _, detail = call(base, "GET", "/devices/D6/cycles/default")
    call(base, "POST", "/devices/D6/cycles/default/seal",
         {"fingerprint": detail["cycle"]["fingerprint"]})
    status, body = call(base, "POST", "/bundles/B-SEALED/withdraw")
    show("5d. 相关周期已封存 -> 409 cycle_sealed", status, body)
    assert status == 409 and body["error"]["code"] == "cycle_sealed"

    # 5e. 删除会改变保留声段的可定位状态/UTC 位置
    call(base, "POST", "/devices", {"device_id": "D7"})
    call(base, "POST", "/devices/D7/anchors",
         {"local_ms": 0, "utc_ms": 1000})
    call(base, "POST", "/devices/D7/anchors",
         {"local_ms": 10000, "utc_ms": 11000})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-KEEP", "device_id": "D7",
          "local_start_ms": 2000, "local_end_ms": 4000})
    call(base, "POST", "/bundles", {
        "bundle_id": "B-SHIFT",
        "anchors": [{"device_id": "D7", "local_ms": 5000, "utc_ms": 5400}]})
    status, body = call(base, "POST", "/bundles/B-SHIFT/withdraw")
    show("5e. 保留声段 UTC 位置将改变 -> 409 bundle_projection_shift",
         status, body)
    assert status == 409 and body["error"]["code"] == "bundle_projection_shift"

    # 5f. 包编号不存在
    status, body = call(base, "POST", "/bundles/NO-SUCH-BUNDLE/withdraw")
    show("5f. 包编号不存在 -> 404 bundle_not_found", status, body)
    assert status == 404 and body["error"]["code"] == "bundle_not_found"

    # ------------------------------------------------------------------
    # 6. 混合新增/复用：撤回只删新增，复用的原有记录保留
    # ------------------------------------------------------------------
    call(base, "POST", "/devices", {"device_id": "D8", "name": "录音笔-辛"})
    call(base, "POST", "/devices/D8/anchors", {"local_ms": 0, "utc_ms": 1000})
    call(base, "POST", "/devices/D8/anchors",
         {"local_ms": 10000, "utc_ms": 11000})
    call(base, "POST", "/bundles", {
        "bundle_id": "B-MIXED",
        "devices": [{"device_id": "D8", "name": "录音笔-辛"}],      # 复用
        "anchors": [{"device_id": "D8", "local_ms": 0, "utc_ms": 1000},  # 复用
                    {"device_id": "D8", "local_ms": 20000, "utc_ms": 21000}],  # 新增
        "segments": [{"capture_id": "CAP-NEW", "device_id": "D8",
                      "local_start_ms": 15000, "local_end_ms": 16000}]})  # 新增
    status, body = call(base, "POST", "/bundles/B-MIXED/withdraw")
    show("6. 混合包撤回：只删新增，复用记录保留", status, body)
    assert status == 200
    assert body["devices"] == {"removed": 0}      # 复用的设备不删
    assert body["anchors"] == {"removed": 1}      # 只删新增锚点
    assert body["segments"] == {"removed": 1}
    status, dev = call(base, "GET", "/devices/D8")
    assert status == 200  # 复用设备仍在

    print("\n全部断言通过：采集包撤回 / 幂等重放 / 重传回执 / 各类整包拒绝"
          " / 复用保护均符合预期。")


if __name__ == "__main__":
    main()
