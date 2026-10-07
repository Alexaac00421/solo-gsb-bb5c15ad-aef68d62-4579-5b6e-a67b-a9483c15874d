#!/usr/bin/env python3
"""采集包导入（POST /bundles，v1.8）最小示例：断网汇聚 -> 整包上传 -> 重试。

现场设备断网期间，多台设备的登记信息、时钟周期、同步锚点与声段被汇成
一个采集包；恢复网络后整包上传。包内记录顺序不限，可引用同包登记的
设备与周期；锚点按"每周期最终序列"校验，不因上传顺序误拒。相同包编号
+ 相同记录集合重传返回首次结果且不重复写入；编号复用但内容不同、
同标识记录与库中冲突、引用缺失、锚点序列非法或试图变更封存周期时
整包拒绝，不留下部分数据。

本脚本演示：

  1) 整包导入：两台设备的设备/周期/锚点/声段（刻意乱序，声段引用同包
     登记的设备与周期）-> 201，返回各类新增与复用数量；
  2) 重试：相同包编号 + 相同记录集合（不同顺序）-> 200 返回首次结果，
     不重复写入；
  3) 编号复用但内容不同 -> 409 bundle_conflict；
  4) 引用缺失 -> 404 device_not_found（整包拒绝，不留部分数据）；
  5) 锚点最终序列非法 -> 400 anchors_not_increasing；
  6) 封存周期后：新增记录 -> 409 cycle_sealed；完全一致的记录复用 -> 201；
     首次导入的包在封存后重传 -> 仍返回首次结果；
  7) 当前时间轴反映入库结果，已发布版本保持原快照。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/bundle_example.py [base_url]
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
    # 1. 整包导入：断网期间汇聚的采集包（记录刻意乱序；声段/锚点引用同包
    #    登记的设备与周期；省略 cycle_id 的记录归入 default 周期）
    # ------------------------------------------------------------------
    bundle = {
        "bundle_id": "FIELD-2026-10-06-001",
        "segments": [
            {"capture_id": "CAP-0002", "device_id": "D2",
             "local_start_ms": 3000, "local_end_ms": 9000},
            {"capture_id": "CAP-0001", "device_id": "D1", "cycle_id": "boot-2",
             "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"},
        ],
        "anchors": [
            {"device_id": "D2", "local_ms": 60000, "utc_ms": 1700000057000},
            {"device_id": "D1", "cycle_id": "boot-2",
             "local_ms": 61000, "utc_ms": 1700010060500},
            {"device_id": "D2", "local_ms": 0, "utc_ms": 1699999997000},
            {"device_id": "D1", "cycle_id": "boot-2",
             "local_ms": 1000, "utc_ms": 1700010000000},
        ],
        "cycles": [
            {"device_id": "D1", "cycle_id": "boot-2", "note": "第二次开机，本地毫秒归零"},
        ],
        "devices": [
            {"device_id": "D2", "name": "录音笔-乙"},
            {"device_id": "D1", "name": "录音笔-甲"},
        ],
    }
    status, first = call(base, "POST", "/bundles", bundle)
    show("1. 整包导入采集包（201，各类新增数量）", status, first)
    assert status == 201 and first["devices"]["added"] == 2

    # 当前时间轴立即反映入库结果（CAP-0001 按 boot-2 周期锚点线性映射）
    status, tl = call(base, "GET", "/timeline/current")
    located = {s["capture_id"]: s["utc_start_ms"] for s in tl["segments"]}
    print("\n   当前时间轴：", located)
    assert located["CAP-0001"] == 1700010004033
    assert located["CAP-0002"] == 1700000000000

    # ------------------------------------------------------------------
    # 2. 重试：相同包编号 + 相同记录集合（不同顺序）-> 返回首次结果
    # ------------------------------------------------------------------
    retry = dict(bundle)
    retry["anchors"] = list(reversed(bundle["anchors"]))
    retry["segments"] = list(reversed(bundle["segments"]))
    status, replay = call(base, "POST", "/bundles", retry)
    show("2. 相同包重传（乱序）-> 200 返回首次结果，不重复写入", status, replay)
    assert status == 200 and replay["idempotent_replay"] is True
    assert replay["anchors"] == first["anchors"]

    # ------------------------------------------------------------------
    # 3. 编号复用但内容不同 -> 409 bundle_conflict
    # ------------------------------------------------------------------
    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-001",
        "devices": [{"device_id": "D9"}]})
    show("3. 包编号复用但内容不同 -> 409 bundle_conflict", status, body)
    assert status == 409

    # ------------------------------------------------------------------
    # 4. 引用缺失 -> 404，整包拒绝不留部分数据（修正后同编号可重新导入）
    # ------------------------------------------------------------------
    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-002",
        "anchors": [{"device_id": "GHOST", "local_ms": 0, "utc_ms": 1}]})
    show("4. 引用未登记设备 -> 404 device_not_found（整包拒绝）", status, body)
    assert status == 404
    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-002",
        "devices": [{"device_id": "D3"}],
        "anchors": [{"device_id": "D3", "local_ms": 0, "utc_ms": 1}]})
    show("   修正后同编号重新导入 -> 201", status, body)
    assert status == 201

    # ------------------------------------------------------------------
    # 5. 锚点最终序列非法：与库中锚点本地时间重复但 UTC 不同 -> 400
    # ------------------------------------------------------------------
    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-003",
        "anchors": [{"device_id": "D1", "cycle_id": "boot-2",
                     "local_ms": 1000, "utc_ms": 1799999999999}]})
    show("5. 合并后锚点序列非法 -> 400 anchors_not_increasing", status, body)
    assert status == 400

    # ------------------------------------------------------------------
    # 6. 封存周期：新增记录 -> 409 cycle_sealed；完全一致记录复用 -> 201；
    #    首次导入的包在封存后重传 -> 仍返回首次结果
    # ------------------------------------------------------------------
    _, detail = call(base, "GET", "/devices/D1/cycles/boot-2")
    fp = detail["cycle"]["fingerprint"]
    status, sealed = call(base, "POST", "/devices/D1/cycles/boot-2/seal",
                          {"fingerprint": fp})
    show("6. 先封存 D1/boot-2 周期", status, sealed)
    assert status == 201

    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-004",
        "anchors": [{"device_id": "D1", "cycle_id": "boot-2",
                     "local_ms": 121000, "utc_ms": 1700010120000}]})
    show("   向封存周期写新锚点 -> 409 cycle_sealed", status, body)
    assert status == 409

    status, body = call(base, "POST", "/bundles", {
        "bundle_id": "FIELD-2026-10-06-005",
        "anchors": [{"device_id": "D1", "cycle_id": "boot-2",
                     "local_ms": 1000, "utc_ms": 1700010000000}],
        "segments": [{"capture_id": "CAP-0001", "device_id": "D1",
                      "cycle_id": "boot-2", "local_start_ms": 5000,
                      "local_end_ms": 20000, "note": "鸟鸣"}]})
    show("   完全一致记录复用（封存周期无变更）-> 201", status, body)
    assert status == 201 and body["anchors"]["reused"] == 1

    status, replay2 = call(base, "POST", "/bundles", bundle)
    show("   首次导入的包在封存后重传 -> 仍返回首次结果", status, replay2)
    assert status == 200 and replay2["idempotent_replay"] is True

    # ------------------------------------------------------------------
    # 7. 发布：已发布版本保持原快照，新版本反映导入结果
    # ------------------------------------------------------------------
    status, v1 = call(base, "POST", "/versions", {"note": "导入后首次发布"})
    show("7. 发布版本（反映采集包入库结果）", status,
         {"version_no": v1["version_no"], "segment_count": v1["segment_count"],
          "added": [s["capture_id"] for s in v1["changes"]["added"]]})
    status, again = call(base, "GET", f"/versions/{v1['version_no']}")
    assert status == 200 and again["segment_count"] == v1["segment_count"]

    print("\n全部断言通过：采集包导入 / 重试幂等 / 整包拒绝 / 封存保护均符合预期。")


if __name__ == "__main__":
    main()
