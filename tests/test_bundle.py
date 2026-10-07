"""端到端测试：采集包导入（bundle，v1.8）。

覆盖：
- 基本成功：多设备的设备/周期/锚点/声段整包导入，返回各类新增与复用数量；
  包内顺序不限，记录可引用同包登记的设备与周期（含隐式 default 周期）；
  当前时间轴立即反映入库结果，已发布版本保持原快照；
- 包级幂等：相同包编号 + 相同记录集合（乱序）重传返回首次结果且不重复
  写入；首次导入后状态变化（周期封存）重传仍返回首次结果；
  包编号复用但内容不同 -> 409 bundle_conflict；
- 整包原子拒绝（不留下任何部分数据，且包编号可修正后重用）：
  同标识记录与库中冲突（device/cycle/capture_conflict）、包内自相冲突、
  引用缺失（device/cycle_not_found）、锚点最终序列非法
  （anchors_not_increasing）、试图变更封存周期（cycle_sealed）；
- 与库中已有记录混合时的新增/复用计数；封存周期上完全一致记录的复用；
- 并发导入与发布：每个版本仍是完整自洽快照，同包并发重传只写一次。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class BundleApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def import_bundle(self, bundle_id, devices=None, cycles=None,
                      anchors=None, segments=None):
        payload = {"bundle_id": bundle_id}
        if devices is not None:
            payload["devices"] = devices
        if cycles is not None:
            payload["cycles"] = cycles
        if anchors is not None:
            payload["anchors"] = anchors
        if segments is not None:
            payload["segments"] = segments
        return self.req("POST", "/bundles", payload)

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def reg_cycle(self, device_id, cycle_id, note=None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def fingerprint(self, device_id, cycle_id):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def seal(self, device_id, cycle_id):
        fp = self.fingerprint(device_id, cycle_id)
        status, body = self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                                {"fingerprint": fp})
        self.assertEqual(status, 201)
        return body

    def counts(self):
        """库中各类记录总数（用于验证拒绝后不留下部分数据）。"""
        devices = self.req("GET", "/devices")[1]["devices"]
        segments = self.req("GET", "/segments")[1]["count"]
        anchors = 0
        cycles = 0
        for d in devices:
            cyc = self.req("GET", f"/devices/{d['device_id']}/cycles")[1]["cycles"]
            cycles += len(cyc)
            anchors += sum(c["anchor_count"] for c in cyc)
        return {"devices": len(devices), "cycles": cycles,
                "anchors": anchors, "segments": segments}

    def sample_bundle(self, bundle_id="B-001"):
        """两台设备的完整采集包：设备/周期/锚点/声段齐全且刻意乱序，
        声段与锚点引用同包登记的设备与周期。"""
        return {
            "bundle_id": bundle_id,
            "segments": [
                {"capture_id": "CAP-2", "device_id": "D2",
                 "local_start_ms": 500, "local_end_ms": 900},
                {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "boot-2",
                 "local_start_ms": 1500, "local_end_ms": 1800, "note": "鸟鸣"},
            ],
            "anchors": [
                {"device_id": "D2", "local_ms": 10000, "utc_ms": 210000},
                {"device_id": "D1", "cycle_id": "boot-2",
                 "local_ms": 2000, "utc_ms": 102000},
                {"device_id": "D2", "local_ms": 0, "utc_ms": 200000},
                {"device_id": "D1", "cycle_id": "boot-2",
                 "local_ms": 1000, "utc_ms": 101000},
            ],
            "cycles": [
                {"device_id": "D1", "cycle_id": "boot-2", "note": "第二次开机"},
            ],
            "devices": [
                {"device_id": "D2"},
                {"device_id": "D1", "name": "录音笔-甲"},
            ],
        }

    # ------------------------------------------------------------ 成功场景

    def test_import_bundle_success_counts_and_timeline(self):
        status, body = self.import_bundle(**self.sample_bundle())
        self.assertEqual(status, 201)
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["bundle_id"], "B-001")
        # 各类新增数量（D1 的 default 周期为隐式创建，不计入 cycles）
        self.assertEqual(body["devices"], {"added": 2, "reused": 0})
        self.assertEqual(body["cycles"], {"added": 1, "reused": 0})
        self.assertEqual(body["anchors"], {"added": 4, "reused": 0})
        self.assertEqual(body["segments"], {"added": 2, "reused": 0})
        # 当前时间轴立即反映入库结果（按所属周期锚点定位）
        _, tl = self.req("GET", "/timeline/current")
        by_id = {s["capture_id"]: s for s in tl["segments"]}
        self.assertEqual(by_id["CAP-1"]["cycle_id"], "boot-2")
        self.assertEqual(by_id["CAP-1"]["status"], "located")
        self.assertEqual(by_id["CAP-1"]["utc_start_ms"], 101500)
        self.assertEqual(by_id["CAP-1"]["utc_end_ms"], 101800)
        self.assertEqual(by_id["CAP-2"]["cycle_id"], "default")
        self.assertEqual(by_id["CAP-2"]["utc_start_ms"], 200500)
        # 设备详情可见隐式 default 周期与同包登记的 boot-2 周期
        _, dev = self.req("GET", "/devices/D1")
        self.assertEqual({c["cycle_id"] for c in dev["device"]["cycles"]},
                         {"default", "boot-2"})

    def test_import_out_of_order_and_in_bundle_references(self):
        """包内顺序不限：声段/锚点引用同包靠后登记的设备与周期，不因顺序误拒。"""
        bundle = {
            "bundle_id": "B-ORDER",
            # 声段在最前，引用的设备与周期记录在最后
            "segments": [
                {"capture_id": "CAP-LATE", "device_id": "DX", "cycle_id": "boot-9",
                 "local_start_ms": 100, "local_end_ms": 200},
            ],
            "anchors": [
                {"device_id": "DX", "cycle_id": "boot-9",
                 "local_ms": 1000, "utc_ms": 5000},
                {"device_id": "DX", "cycle_id": "boot-9",
                 "local_ms": 0, "utc_ms": 1000},
            ],
            "cycles": [{"device_id": "DX", "cycle_id": "boot-9"}],
            "devices": [{"device_id": "DX"}],
        }
        status, body = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        self.assertEqual(body["segments"], {"added": 1, "reused": 0})
        seg = self.req("GET", "/segments/CAP-LATE")[1]["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 1400)  # 100 -> 1000 + 100*4

    def test_import_default_cycle_implicit_for_new_device(self):
        """新登记设备自动带 default 周期：省略 cycle_id 的锚点/声段直接可用。"""
        status, body = self.import_bundle(
            "B-DEF",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 1000, "utc_ms": 2000}],
            segments=[{"capture_id": "CAP-D", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        self.assertEqual(body["cycles"], {"added": 0, "reused": 0})
        seg = self.req("GET", "/segments/CAP-D")[1]["segment"]
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["utc_start_ms"], 1100)

    def test_import_mixed_add_and_reuse_counts(self):
        """与库中已有记录混合：完全一致的记录复用，其余新增，数量分别统计。"""
        self.make_device("D1", "录音笔-甲")
        self.reg_cycle("D1", "boot-2", "第二次开机")
        self.add_anchor("D1", 1000, 101000, "boot-2")
        self.add_anchor("D1", 2000, 102000, "boot-2")
        self.submit("CAP-1", "D1", 1500, 1800, "boot-2", note="鸟鸣")
        status, body = self.import_bundle(
            "B-MIX",
            devices=[{"device_id": "D1", "name": "录音笔-甲"},  # 复用
                     {"device_id": "D2"}],                       # 新增
            cycles=[{"device_id": "D1", "cycle_id": "boot-2",
                     "note": "第二次开机"}],                       # 复用
            anchors=[{"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 1000, "utc_ms": 101000},       # 复用
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 3000, "utc_ms": 103000}],      # 新增
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "cycle_id": "boot-2", "local_start_ms": 1500,
                       "local_end_ms": 1800, "note": "鸟鸣"},    # 复用
                      {"capture_id": "CAP-2", "device_id": "D1",
                       "cycle_id": "boot-2", "local_start_ms": 2100,
                       "local_end_ms": 2200}])                   # 新增
        self.assertEqual(status, 201)
        self.assertEqual(body["devices"], {"added": 1, "reused": 1})
        self.assertEqual(body["cycles"], {"added": 0, "reused": 1})
        self.assertEqual(body["anchors"], {"added": 1, "reused": 1})
        self.assertEqual(body["segments"], {"added": 1, "reused": 1})
        self.assertEqual(self.counts(),
                         {"devices": 2, "cycles": 3, "anchors": 3, "segments": 2})

    def test_import_empty_bundle(self):
        """只含包编号的空包合法：全部计数为 0，重传仍返回首次结果。"""
        status, body = self.import_bundle("B-EMPTY")
        self.assertEqual(status, 201)
        for key in ("devices", "cycles", "anchors", "segments"):
            self.assertEqual(body[key], {"added": 0, "reused": 0})
        status, body = self.import_bundle("B-EMPTY")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])

    # ------------------------------------------------------------ 包级幂等

    def test_retry_same_bundle_returns_first_result_no_duplicate_writes(self):
        bundle = self.sample_bundle("B-RETRY")
        status, first = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        before = self.counts()
        # 相同记录集合、不同顺序重传 -> 返回首次结果，不重复写入
        shuffled = {"bundle_id": "B-RETRY",
                    "anchors": list(reversed(bundle["anchors"])),
                    "segments": list(reversed(bundle["segments"])),
                    "cycles": bundle["cycles"],
                    "devices": list(reversed(bundle["devices"]))}
        status, replay = self.req("POST", "/bundles", shuffled)
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual({k: v for k, v in replay.items() if k != "idempotent_replay"},
                         {k: v for k, v in first.items() if k != "idempotent_replay"})
        self.assertEqual(self.counts(), before)
        # 逐字节相同重传亦然
        status, replay2 = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 200)
        self.assertEqual(replay2, replay)
        self.assertEqual(self.counts(), before)

    def test_retry_same_bundle_after_state_change(self):
        """首次导入后周期被封存，相同包重传仍返回首次结果（不重新校验库状态）。"""
        status, first = self.import_bundle(
            "B-SEALED-RETRY",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 1000, "utc_ms": 2000}])
        self.assertEqual(status, 201)
        self.seal("D1", "default")
        status, replay = self.import_bundle(
            "B-SEALED-RETRY",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 1000, "utc_ms": 2000}])
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["anchors"], first["anchors"])

    def test_bundle_id_reuse_with_different_content_rejected(self):
        status, _ = self.import_bundle("B-DUP", devices=[{"device_id": "D1"}])
        self.assertEqual(status, 201)
        before = self.counts()
        # 同编号但内容不同（哪怕只是多一条记录）-> 409，不写任何数据
        status, body = self.import_bundle(
            "B-DUP", devices=[{"device_id": "D1"}, {"device_id": "D2"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_conflict")
        self.assertEqual(body["error"]["details"]["bundle_id"], "B-DUP")
        self.assertEqual(self.counts(), before)
        # 原内容重传仍返回首次结果
        status, body = self.import_bundle("B-DUP", devices=[{"device_id": "D1"}])
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])

    def test_rejected_bundle_leaves_no_trace_and_id_reusable(self):
        """被拒绝的包不留下任何数据（包括包记录本身），修正后同编号可重新导入。"""
        before = self.counts()
        status, body = self.import_bundle(
            "B-FIX",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "GHOST", "local_ms": 0, "utc_ms": 1000}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        self.assertEqual(self.counts(), before)
        # 修正内容后同编号重新导入 -> 成功（拒绝不留痕，编号可重用）
        status, body = self.import_bundle(
            "B-FIX",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000}])
        self.assertEqual(status, 201)
        self.assertEqual(body["anchors"], {"added": 1, "reused": 0})

    # ------------------------------------------------------------ 冲突拒绝（整包原子）

    def test_device_conflict_with_db_rejected_atomically(self):
        self.make_device("D1", "录音笔-甲")
        before = self.counts()
        status, body = self.import_bundle(
            "B-C1",
            devices=[{"device_id": "D1", "name": "另一名字"},
                     {"device_id": "D2"}],
            anchors=[{"device_id": "D2", "local_ms": 0, "utc_ms": 1000}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["name"], "录音笔-甲")
        # 整包拒绝：D2 与其锚点都未写入
        self.assertEqual(self.counts(), before)

    def test_device_conflict_within_bundle_rejected(self):
        before = self.counts()
        status, body = self.import_bundle(
            "B-C2",
            devices=[{"device_id": "D1", "name": "甲"},
                     {"device_id": "D1", "name": "乙"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_conflict")
        self.assertEqual(self.counts(), before)
        # 完全相同的重复记录按集合语义去重，不构成冲突
        status, body = self.import_bundle(
            "B-C3",
            devices=[{"device_id": "D1", "name": "甲"},
                     {"device_id": "D1", "name": "甲"}])
        self.assertEqual(status, 201)
        self.assertEqual(body["devices"], {"added": 1, "reused": 0})

    def test_cycle_conflict_rejected(self):
        self.make_device("D1")
        self.reg_cycle("D1", "boot-2", "第二次开机")
        before = self.counts()
        # 与库中 note 不一致 -> 409
        status, body = self.import_bundle(
            "B-C4", cycles=[{"device_id": "D1", "cycle_id": "boot-2",
                             "note": "另一个备注"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["note"], "第二次开机")
        # 包内自相冲突 -> 409
        status, body = self.import_bundle(
            "B-C5", cycles=[{"device_id": "D1", "cycle_id": "boot-9", "note": "x"},
                            {"device_id": "D1", "cycle_id": "boot-9", "note": "y"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_conflict")
        self.assertEqual(self.counts(), before)

    def test_capture_conflict_with_db_rejected_atomically(self):
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 1000, 2000)
        self.submit("CAP-1", "D1", 100, 200, note="鸟鸣")
        before = self.counts()
        status, body = self.import_bundle(
            "B-C6",
            devices=[{"device_id": "D2"}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 201, "note": "鸟鸣"},
                      {"capture_id": "CAP-2", "device_id": "D2",
                       "local_start_ms": 0, "local_end_ms": 1}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["local_end_ms"], 200)
        # 整包拒绝：D2 与 CAP-2 都未写入
        self.assertEqual(self.counts(), before)

    def test_capture_conflict_within_bundle_rejected(self):
        before = self.counts()
        status, body = self.import_bundle(
            "B-C7",
            devices=[{"device_id": "D1"}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 1, "local_end_ms": 2},
                      {"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 1, "local_end_ms": 3}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(self.counts(), before)

    # ------------------------------------------------------------ 引用缺失

    def test_missing_device_reference_rejected(self):
        before = self.counts()
        status, body = self.import_bundle(
            "B-R1",
            anchors=[{"device_id": "GHOST", "local_ms": 0, "utc_ms": 1000}],
            segments=[{"capture_id": "CAP-1", "device_id": "GHOST",
                       "local_start_ms": 0, "local_end_ms": 1}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        self.assertEqual(body["error"]["details"]["device_ids"], ["GHOST"])
        self.assertEqual(self.counts(), before)

    def test_missing_cycle_reference_rejected(self):
        self.make_device("D1")
        # 周期既不在库中也不在本包 -> 404（含只在别的设备上登记的同名周期）
        self.make_device("D2")
        self.reg_cycle("D2", "boot-2")
        before = self.counts()
        status, body = self.import_bundle(
            "B-R2",
            anchors=[{"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 0, "utc_ms": 1000}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(body["error"]["details"]["cycles"],
                         [{"device_id": "D1", "cycle_id": "boot-2"}])
        self.assertEqual(self.counts(), before)

    # ------------------------------------------------------------ 锚点最终序列

    def test_anchor_final_sequence_validated_against_merged_chain(self):
        """按每周期最终锚点序列校验：库中现有 + 包内新增合并后必须严格递增。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 3000, 300000)
        # 包内锚点插入库中两锚点之间（乱序给出），合并序列合法 -> 接受
        status, body = self.import_bundle(
            "B-A1",
            anchors=[{"device_id": "D1", "local_ms": 2500, "utc_ms": 250000},
                     {"device_id": "D1", "local_ms": 2000, "utc_ms": 200000}])
        self.assertEqual(status, 201)
        self.assertEqual(body["anchors"], {"added": 2, "reused": 0})
        anchors = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors],
                         [(1000, 100000), (2000, 200000),
                          (2500, 250000), (3000, 300000)])

    def test_anchor_sequence_violations_rejected(self):
        self.make_device("D1")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 3000, 300000)
        before = self.counts()
        # 1) 与库中锚点 local_ms 重复但 UTC 不同 -> 合并序列非法
        status, body = self.import_bundle(
            "B-A2",
            anchors=[{"device_id": "D1", "local_ms": 1000, "utc_ms": 999999}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # 2) 包内两条同 local_ms 不同 UTC -> 非法
        status, body = self.import_bundle(
            "B-A3",
            anchors=[{"device_id": "D1", "local_ms": 5000, "utc_ms": 500000},
                     {"device_id": "D1", "local_ms": 5000, "utc_ms": 500001}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # 3) 合并后 UTC 不严格递增（插入点 UTC 越过库中后一锚点）
        status, body = self.import_bundle(
            "B-A4",
            anchors=[{"device_id": "D1", "local_ms": 2000, "utc_ms": 400000}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # 三次拒绝均未写入任何数据
        self.assertEqual(self.counts(), before)
        # 与库中锚点完全一致的记录是复用而非冲突
        status, body = self.import_bundle(
            "B-A5",
            anchors=[{"device_id": "D1", "local_ms": 1000, "utc_ms": 100000}])
        self.assertEqual(status, 201)
        self.assertEqual(body["anchors"], {"added": 0, "reused": 1})

    # ------------------------------------------------------------ 封存周期

    def test_sealed_cycle_new_records_rejected_but_identical_reuse_ok(self):
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 1000, 2000)
        self.submit("CAP-1", "D1", 100, 200, note="鸟鸣")
        self.seal("D1", "default")
        before = self.counts()
        # 新增锚点到封存周期 -> 409 cycle_sealed
        status, body = self.import_bundle(
            "B-S1", anchors=[{"device_id": "D1", "local_ms": 2000, "utc_ms": 3000}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycles"][0]["cycle_id"], "default")
        # 新增声段到封存周期 -> 409 cycle_sealed
        status, body = self.import_bundle(
            "B-S2", segments=[{"capture_id": "CAP-2", "device_id": "D1",
                               "local_start_ms": 300, "local_end_ms": 400}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(self.counts(), before)
        # 完全一致的记录只是复用，不构成变更 -> 成功
        status, body = self.import_bundle(
            "B-S3",
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200, "note": "鸟鸣"}])
        self.assertEqual(status, 201)
        self.assertEqual(body["anchors"], {"added": 0, "reused": 1})
        self.assertEqual(body["segments"], {"added": 0, "reused": 1})
        self.assertEqual(self.counts(), before)

    # ------------------------------------------------------------ 字段校验

    def test_validation_errors(self):
        before = self.counts()
        # 缺 bundle_id / 请求体非对象 / 记录列表非数组
        for bad in ({}, {"devices": []}, {"bundle_id": "B-V", "devices": "x"},
                    {"bundle_id": "B-V", "anchors": {"local_ms": 1}}):
            status, body = self.req("POST", "/bundles", bad)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
        # 非法包编号
        status, body = self.import_bundle("bad id")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        # 记录缺字段 / 记录非对象 / 非法时间 / 起止倒置
        bad_bundles = [
            {"bundle_id": "B-V", "devices": [{"name": "x"}]},
            {"bundle_id": "B-V", "devices": ["not-an-object"]},
            {"bundle_id": "B-V", "cycles": [{"device_id": "D1"}]},
            {"bundle_id": "B-V", "anchors": [{"device_id": "D1", "local_ms": 1}]},
            {"bundle_id": "B-V", "anchors": [{"device_id": "D1", "local_ms": -1,
                                              "utc_ms": 1}]},
            {"bundle_id": "B-V", "anchors": [{"device_id": "D1", "local_ms": 1.5,
                                              "utc_ms": 1}]},
            {"bundle_id": "B-V", "segments": [{"capture_id": "C1",
                                               "device_id": "D1",
                                               "local_start_ms": 5,
                                               "local_end_ms": 1}]},
        ]
        for bad in bad_bundles:
            status, body = self.req("POST", "/bundles", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn(body["error"]["code"],
                          ("validation_error", "invalid_time", "segment_inverted"))
        self.assertEqual(self.counts(), before)

    # ------------------------------------------------------------ 与版本发布共存

    def test_published_versions_immutable_new_versions_reflect_import(self):
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.submit("CAP-OLD", "D1", 100, 200)
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        # 采集包导入
        status, _ = self.import_bundle(
            "B-PUB",
            devices=[{"device_id": "D2"}],
            anchors=[{"device_id": "D2", "local_ms": 0, "utc_ms": 50000},
                     {"device_id": "D2", "local_ms": 10000, "utc_ms": 60000}],
            segments=[{"capture_id": "CAP-NEW", "device_id": "D2",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        # 已发布版本仍是原快照
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again, v1)
        self.assertEqual(v1_again["segment_count"], 1)
        # 之后发布的版本反映导入结果
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["segment_count"], 2)
        self.assertEqual([s["capture_id"] for s in v2["changes"]["added"]],
                         ["CAP-NEW"])
        self.assertEqual(v2["changes"]["removed"], [])

    # ------------------------------------------------------------ 并发

    def test_concurrent_imports_and_publish(self):
        """并发导入多个采集包 + 发布：每个版本都是完整自洽快照，无半包状态。"""
        results, errors = [], []

        def import_one(k):
            try:
                results.append(self.import_bundle(
                    f"B-K{k}",
                    devices=[{"device_id": f"DEV-{k}"}],
                    anchors=[{"device_id": f"DEV-{k}", "local_ms": 0,
                              "utc_ms": k * 100000},
                             {"device_id": f"DEV-{k}", "local_ms": 10000,
                              "utc_ms": k * 100000 + 10000}],
                    segments=[{"capture_id": f"CAP-K{k}", "device_id": f"DEV-{k}",
                               "local_start_ms": 100, "local_end_ms": 200}])[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(6):
            threads.append(threading.Thread(target=import_one, args=(k,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results))
        # 全部包完整入库
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual({s["capture_id"] for s in tl["segments"]},
                         {f"CAP-K{k}" for k in range(6)})
        self.assertEqual(len(tl["devices"]), 6)
        # 版本号连续无空洞；每个版本的声段投影与快照内锚点自洽（无半包）
        versions = self.req("GET", "/versions")[1]["versions"]
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            devices_in_doc = {s["device_id"] for s in doc["segments"]}
            for dev in devices_in_doc:
                # 设备出现在版本中 <=> 其锚点与声段完整出现（不观察半包）
                self.assertEqual(
                    sorted(a["local_ms"] for a in doc["anchors"]
                           if a["device_id"] == dev), [0, 10000])
                self.assertEqual(
                    [s["capture_id"] for s in doc["segments"]
                     if s["device_id"] == dev],
                    [f"CAP-K{dev.split('-')[1]}"])
            for seg in doc["segments"]:
                self.assertEqual(seg["status"], "located")

    def test_concurrent_same_bundle_retry_writes_once(self):
        """同一采集包并发重传：只写入一次，其余返回首次结果。"""
        bundle = self.sample_bundle("B-RACE")
        outcomes, errors = [], []

        def send():
            try:
                outcomes.append(self.req("POST", "/bundles", bundle))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=send) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        statuses = sorted(s for s, _ in outcomes)
        self.assertEqual(statuses, [200, 200, 200, 201])
        bodies = []
        for status, body in outcomes:
            if status == 201:
                self.assertFalse(body["idempotent_replay"])
            else:
                self.assertTrue(body["idempotent_replay"])
            bodies.append({k: v for k, v in body.items()
                           if k != "idempotent_replay"})
        self.assertTrue(all(b == bodies[0] for b in bodies))
        self.assertEqual(self.counts(),
                         {"devices": 2, "cycles": 3, "anchors": 4, "segments": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
