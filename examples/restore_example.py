#!/usr/bin/env python3
"""按已发布版本恢复未封存周期（POST /devices/{id}/cycles/{cycle}/restore）示例。

现场误改了尚未封存周期的锚点/声段后，凭任一已发布版本把该周期恢复为版本
冻结内容。本脚本自动建设备/锚点/声段并发布版本，随后演示：

  1) 误改（锚点更正、误删声段、误录新声段、声段起止备注被改）后凭版本恢复：
     返回恢复前后指纹、锚点/声段数量与恢复后定位结果；
  2) 已发布版本、其他周期与采集包回执保持原样；
  3) 明确拒绝（整次回滚，不留部分写入）：指纹过期、周期已封存、版本不存在、
     版本中无该周期内容（空周期）、版本声段采集编号被其他周期/设备占用。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/restore_example.py [base_url]
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


def fp_of(base: str, device: str, cycle: str = "default") -> str:
    return call(base, "GET", f"/devices/{device}/cycles/{cycle}")[1][
        "cycle"]["fingerprint"]


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    D, C = "R1", "default"

    # 1. 初始登记：两个锚点 + 两段声段，发布 v1（冻结内容）
    call(base, "POST", "/devices", {"device_id": D, "name": "现场录音笔"})
    call(base, "POST", f"/devices/{D}/anchors",
         {"local_ms": 1000, "utc_ms": 1700000000000})
    call(base, "POST", f"/devices/{D}/anchors",
         {"local_ms": 61000, "utc_ms": 1700000060500})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-1", "device_id": D,
          "local_start_ms": 5000, "local_end_ms": 20000, "note": "晨间鸟鸣"})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-2", "device_id": D,
          "local_start_ms": 30000, "local_end_ms": 40000})
    show("发布 v1（冻结锚点与声段）", *call(base, "POST", "/versions",
                                            {"note": "现场核对版"}))

    # 2. 现场误改：锚点 +250ms、误删 CAP-2、误录 CAP-9、CAP-1 起止备注被改
    call(base, "PUT", f"/devices/{D}/anchors",
         {"anchors": [{"local_ms": 1000, "utc_ms": 1700000000250},
                      {"local_ms": 61000, "utc_ms": 1700000060750}]})
    call(base, "DELETE", "/segments/CAP-2")
    call(base, "POST", "/segments",
         {"capture_id": "CAP-9", "device_id": D,
          "local_start_ms": 1000, "local_end_ms": 2000})
    print("\n（已模拟现场误改：锚点偏移、CAP-2 误删、CAP-9 误录）")

    # 3. 取当前周期详情中的内容指纹，凭 v1 恢复
    fingerprint = fp_of(base, D, C)
    show(f"凭 v1 恢复 {D}/{C}",
         *call(base, "POST", f"/devices/{D}/cycles/{C}/restore",
               {"version_no": 1, "fingerprint": fingerprint}))

    # 4. 已发布版本保持原样（恢复不会改动或新增版本）
    show("回看 v1（冻结快照不变）", *call(base, "GET", "/versions/1"))

    # 5. 明确拒绝（均不改动数据）
    # 5.1 指纹过期：恢复后内容已变，旧指纹不再匹配
    show("旧指纹再次恢复 -> 409 restore_fingerprint_stale",
         *call(base, "POST", f"/devices/{D}/cycles/{C}/restore",
               {"version_no": 1, "fingerprint": fingerprint}))
    # 5.2 版本不存在
    show("版本不存在 -> 404 version_not_found",
         *call(base, "POST", f"/devices/{D}/cycles/{C}/restore",
               {"version_no": 999, "fingerprint": fp_of(base, D, C)}))
    # 5.3 版本中无该周期内容（历史版本不记录空周期）：登记新周期后据 v1 恢复
    call(base, "POST", f"/devices/{D}/cycles", {"cycle_id": "boot-2"})
    show("v1 中无 boot-2 内容 -> 409 restore_version_cycle_empty",
         *call(base, "POST", f"/devices/{D}/cycles/boot-2/restore",
               {"version_no": 1, "fingerprint": fp_of(base, D, "boot-2")}))
    # 5.4 采集编号被其他设备占用：新设备发布含 CAP-X 的版本，随后 CAP-X 被占用
    call(base, "POST", "/devices", {"device_id": "R2"})
    call(base, "POST", "/devices/R2/anchors",
         {"local_ms": 0, "utc_ms": 100000})
    call(base, "POST", "/devices/R2/anchors",
         {"local_ms": 10000, "utc_ms": 110000})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-X", "device_id": "R2",
          "local_start_ms": 1000, "local_end_ms": 2000})
    call(base, "POST", "/versions", {"note": "R2 版本"})  # v2
    call(base, "DELETE", "/segments/CAP-X")
    call(base, "POST", "/segments",
         {"capture_id": "CAP-X", "device_id": D,
          "local_start_ms": 1000, "local_end_ms": 2000})
    show("CAP-X 被 R1 占用，恢复 R2 -> 409 restore_capture_occupied",
         *call(base, "POST", "/devices/R2/cycles/default/restore",
               {"version_no": 2, "fingerprint": fp_of(base, "R2")}))

    # 6. 封存后不允许恢复（已冻结，不存在误改恢复）
    call(base, "POST", f"/devices/{D}/cycles/{C}/seal",
         {"fingerprint": fp_of(base, D, C)})
    show("周期已封存 -> 409 cycle_sealed",
         *call(base, "POST", f"/devices/{D}/cycles/{C}/restore",
               {"version_no": 1, "fingerprint": fp_of(base, D, C)}))


if __name__ == "__main__":
    main()
