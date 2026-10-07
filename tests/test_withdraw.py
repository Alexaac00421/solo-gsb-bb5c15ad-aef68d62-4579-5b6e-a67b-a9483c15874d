"""端到端测试：采集包撤回（bundle withdraw，v1.10）。

覆盖：
- 基本成功：按包编号撤回，只删除该包新增的设备/周期/锚点/声段，
  复用的原有记录绝不可删；返回各类撤回数量与状态；当前时间轴立即
  反映撤回结果；
- 幂等：重复撤回返回首次结果（idempotent_replay）；撤回后同编号
  重传仍返回首次导入回执且不重新写入；同编号异内容仍 bundle_conflict；
- 明确拒绝（整包拒绝、不改动任何数据）：包编号不存在（404）、旧包
  缺少新增明细（409 bundle_withdraw_unavailable，含旧库迁移补列）、
  新增记录内容已变化或缺失（409 bundle_content_changed：声段删除/
  改归属、锚点更正/删除）、相关周期已封存（409 cycle_sealed）、
  被其他未撤回包复用（409 bundle_record_reused，先撤回对方后可撤回）、
  新增设备/周期已有包外内容（409 bundle_external_content）、
  删除会改变保留声段可定位状态或 UTC 位置（409 bundle_projection_changed）；
- 与版本发布共存：已发布版本保持原快照，之后发布的版本反映撤回；
- 并发：同包并发撤回只执行一次，并发撤回/导入/发布串行判定无半包状态。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
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


class WithdrawApiTestCase(unittest.TestCase):
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

    def withdraw(self, bundle_id):
        return self.req("POST", f"/bundles/{bundle_id}/withdraw")

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
        """库中各类记录总数（用于验证拒绝后不留下任何改动）。"""
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
        """两台设备的完整采集包：设备/周期/锚点/声段齐全。"""
        return {
            "bundle_id": bundle_id,
            "devices": [{"device_id": "D1", "name": "录音笔-甲"},
                        {"device_id": "D2"}],
            "cycles": [{"device_id": "D1", "cycle_id": "boot-2", "note": "第二次开机"}],
            "anchors": [
                {"device_id": "D1", "cycle_id": "boot-2",
                 "local_ms": 1000, "utc_ms": 101000},
                {"device_id": "D1", "cycle_id": "boot-2",
                 "local_ms": 2000, "utc_ms": 102000},
                {"device_id": "D2", "local_ms": 0, "utc_ms": 200000},
                {"device_id": "D2", "local_ms": 10000, "utc_ms": 210000},
            ],
            "segments": [
                {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "boot-2",
                 "local_start_ms": 1200, "local_end_ms": 1500, "note": "鸟鸣"},
                {"capture_id": "CAP-2", "device_id": "D2",
                 "local_start_ms": 100, "local_end_ms": 200},
            ],
        }

    # ------------------------------------------------------------ 成功场景

    def test_withdraw_success_removes_only_added_records(self):
        """撤回只删除本包新增记录；复用的原有记录（设备/周期/锚点/声段）保留。"""
        # 库中既有记录
        self.make_device("D1", "录音笔-甲")
        self.reg_cycle("D1", "boot-2", "第二次开机")
        self.add_anchor("D1", 1000, 101000, "boot-2")
        self.add_anchor("D1", 9000, 109000, "boot-2")
        self.submit("CAP-OLD", "D1", 2000, 3000, "boot-2", note="原有声段")
        before = self.counts()
        # 采集包：复用 D1/boot-2/锚点1000/CAP-OLD 语义外的既有记录，新增其余
        status, body = self.import_bundle(
            "B-W1",
            devices=[{"device_id": "D1", "name": "录音笔-甲"},  # 复用
                     {"device_id": "D2"}],                       # 新增
            cycles=[{"device_id": "D1", "cycle_id": "boot-2",
                     "note": "第二次开机"}],                       # 复用
            anchors=[{"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 1000, "utc_ms": 101000},       # 复用
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 5000, "utc_ms": 105000},       # 新增（在原直线上）
                     {"device_id": "D2", "local_ms": 0, "utc_ms": 200000},
                     {"device_id": "D2", "local_ms": 10000, "utc_ms": 210000}],
            segments=[{"capture_id": "CAP-NEW", "device_id": "D1",
                       "cycle_id": "boot-2", "local_start_ms": 4000,
                       "local_end_ms": 4500},
                      {"capture_id": "CAP-2", "device_id": "D2",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        self.assertEqual(body["devices"], {"added": 1, "reused": 1})
        self.assertEqual(body["anchors"], {"added": 3, "reused": 1})

        status, body = self.withdraw("B-W1")
        self.assertEqual(status, 200)
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["bundle_id"], "B-W1")
        self.assertEqual(body["status"], "withdrawn")
        self.assertTrue(body["withdrawn_at"])
        # 各类撤回数量：只含新增记录
        self.assertEqual(body["devices"], {"removed": 1, "device_ids": ["D2"]})
        self.assertEqual(body["cycles"], {"removed": 0, "cycles": []})
        self.assertEqual(body["anchors"]["removed"], 3)
        self.assertEqual(body["segments"],
                         {"removed": 2, "capture_ids": ["CAP-2", "CAP-NEW"]})
        # 库中记录回到导入前：复用记录全部保留
        self.assertEqual(self.counts(), before)
        # 原有声段仍在且定位不变；新增声段已删除
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 102000)
        status, _ = self.req("GET", "/segments/CAP-NEW")
        self.assertEqual(status, 404)
        # 新增设备 D2 已删除；复用设备 D1 与其周期/锚点保留
        status, _ = self.req("GET", "/devices/D2")
        self.assertEqual(status, 404)
        anchors = self.req("GET", "/devices/D1/anchors?cycle_id=boot-2")[1]["anchors"]
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors],
                         [(1000, 101000), (9000, 109000)])

    def test_withdraw_full_bundle_removes_devices_cycles_anchors_segments(self):
        bundle = self.sample_bundle("B-W2")
        status, _ = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        self.assertEqual(self.counts(),
                         {"devices": 2, "cycles": 3, "anchors": 4, "segments": 2})
        status, body = self.withdraw("B-W2")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"]["removed"], 2)
        self.assertEqual(body["cycles"]["removed"], 1)  # 隐式 default 随设备删除，不计入
        self.assertEqual(body["anchors"]["removed"], 4)
        self.assertEqual(body["segments"]["removed"], 2)
        # 当前时间轴已无任何设备与声段
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual(tl["devices"], [])
        self.assertEqual(tl["segment_count"], 0)

    def test_withdraw_empty_bundle_succeeds_with_zero_counts(self):
        status, body = self.import_bundle("B-EMPTY")
        self.assertEqual(status, 201)
        status, body = self.withdraw("B-EMPTY")
        self.assertEqual(status, 200)
        for key in ("devices", "cycles", "anchors", "segments"):
            self.assertEqual(body[key]["removed"], 0)
        self.assertEqual(body["status"], "withdrawn")

    # ------------------------------------------------------------ 幂等

    def test_repeated_withdraw_returns_first_result(self):
        status, _ = self.req("POST", "/bundles", self.sample_bundle("B-REP"))
        self.assertEqual(status, 201)
        status, first = self.withdraw("B-REP")
        self.assertEqual(status, 200)
        self.assertFalse(first["idempotent_replay"])
        after_first = self.counts()
        # 重复撤回 -> 200 返回首次结果（撤回时间不变），不重复删除
        status, replay = self.withdraw("B-REP")
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual({k: v for k, v in replay.items() if k != "idempotent_replay"},
                         {k: v for k, v in first.items() if k != "idempotent_replay"})
        self.assertEqual(replay["withdrawn_at"], first["withdrawn_at"])
        self.assertEqual(self.counts(), after_first)

    def test_reimport_after_withdraw_returns_first_receipt_without_rewrite(self):
        """撤回后同编号同内容重传：仍返回首次导入回执，且不重新写入任何记录。"""
        bundle = self.sample_bundle("B-REIMP")
        status, first = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        status, _ = self.withdraw("B-REIMP")
        self.assertEqual(status, 200)
        empty = self.counts()
        self.assertEqual(empty, {"devices": 0, "cycles": 0,
                                 "anchors": 0, "segments": 0})
        # 同编号同内容重传 -> 首次导入回执（idempotent_replay），不重新写入
        status, replay = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual({k: v for k, v in replay.items() if k != "idempotent_replay"},
                         {k: v for k, v in first.items() if k != "idempotent_replay"})
        self.assertEqual(self.counts(), empty)
        # 同编号异内容仍按既有规则拒绝
        status, body = self.import_bundle("B-REIMP", devices=[{"device_id": "D9"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_conflict")
        self.assertEqual(self.counts(), empty)

    # ------------------------------------------------------------ 明确拒绝：编号与旧包

    def test_withdraw_unknown_bundle_rejected(self):
        before = self.counts()
        status, body = self.withdraw("NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "bundle_not_found")
        self.assertEqual(body["error"]["details"]["bundle_id"], "NOPE")
        # 被拒绝而未留痕的包编号同样不存在（导入拒绝不登记包记录）
        status, body = self.import_bundle(
            "B-REJ", anchors=[{"device_id": "GHOST", "local_ms": 0, "utc_ms": 1}])
        self.assertEqual(status, 404)
        status, body = self.withdraw("B-REJ")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "bundle_not_found")
        self.assertEqual(self.counts(), before)
        # 非法包编号 -> 400
        status, body = self.req("POST", "/bundles/bad%20id/withdraw")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_withdraw_legacy_bundle_without_detail_rejected(self):
        """旧库迁移：bundles 表补齐撤回列；旧包 added_detail 为 NULL -> 明确拒绝。"""
        # 模拟 v1.8 旧库：bundles 表无撤回列，且有一条已导入包记录
        legacy_db = os.path.join(self.tmpdir.name, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            "CREATE TABLE bundles (bundle_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,"
            " result TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO bundles(bundle_id, content_hash, result, created_at) "
            "VALUES ('B-OLD', 'hash', '{\"bundle_id\":\"B-OLD\"}', '2026-10-01T00:00:00.000Z')")
        conn.commit()
        conn.close()
        # 启动迁移：自动补列，旧包记录保留
        db.init_db(legacy_db)
        self.server.ctx.db_path = legacy_db
        try:
            status, body = self.withdraw("B-OLD")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "bundle_withdraw_unavailable")
            self.assertEqual(body["error"]["details"]["bundle_id"], "B-OLD")
            # 旧包重传回执不受撤回列影响：同内容重传仍返回首次结果
            conn = sqlite3.connect(legacy_db)
            row = conn.execute(
                "SELECT added_detail, withdrawn_at, withdraw_result FROM bundles "
                "WHERE bundle_id='B-OLD'").fetchone()
            conn.close()
            self.assertEqual(row, (None, None, None))
        finally:
            self.server.ctx.db_path = self.db_path

    # ------------------------------------------------------------ 明确拒绝：内容已变化

    def test_withdraw_segment_deleted_or_moved_rejected(self):
        self.import_bundle(
            "B-C1",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        before = self.counts()
        # 声段被删除 -> 新增记录已缺失
        self.req("DELETE", "/segments/CAP-1")
        status, body = self.withdraw("B-C1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_content_changed")
        changed = body["error"]["details"]["changed"]
        self.assertEqual(changed, [{"type": "segment", "capture_id": "CAP-1",
                                    "reason": "missing"}])
        # 拒绝不改动任何数据（其余新增记录仍在）
        self.assertEqual(self.counts(),
                         {"devices": 1, "cycles": 1, "anchors": 2, "segments": 0})
        self.assertNotEqual(self.counts(), before)

    def test_withdraw_segment_reassigned_rejected(self):
        self.import_bundle(
            "B-C2",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        # 声段被纠正到其他周期 -> 内容指纹变化
        self.reg_cycle("D1", "boot-2")
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-1",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)
        status, body = self.withdraw("B-C2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_content_changed")
        self.assertEqual(body["error"]["details"]["changed"][0]["capture_id"], "CAP-1")
        # 声段仍在新周期，未被删除
        seg = self.req("GET", "/segments/CAP-1")[1]["segment"]
        self.assertEqual(seg["cycle_id"], "boot-2")

    def test_withdraw_anchor_corrected_or_deleted_rejected(self):
        self.import_bundle(
            "B-C3",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}])
        # 锚点更正（整体替换）-> 新增锚点已缺失/变化
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 1000},
                              {"local_ms": 9000, "utc_ms": 10500}]})
        status, body = self.withdraw("B-C3")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_content_changed")
        reasons = {(c["type"], c["reason"]) for c in body["error"]["details"]["changed"]}
        self.assertEqual(reasons, {("anchor", "missing")})
        # 锚点仍在（更正后的内容），未被删除
        anchors = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors],
                         [(0, 1000), (9000, 10500)])

        # 另一个包：新增锚点被 DELETE 删除 -> 同样拒绝
        self.import_bundle(
            "B-C4",
            devices=[{"device_id": "D2"}],
            anchors=[{"device_id": "D2", "local_ms": 0, "utc_ms": 5000},
                     {"device_id": "D2", "local_ms": 1000, "utc_ms": 6000}])
        self.req("DELETE", "/devices/D2/anchors/1000")
        status, body = self.withdraw("B-C4")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_content_changed")

    # ------------------------------------------------------------ 明确拒绝：封存

    def test_withdraw_sealed_cycle_rejected(self):
        self.import_bundle(
            "B-S1",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.seal("D1", "default")
        before = self.counts()
        status, body = self.withdraw("B-S1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycles"][0]["cycle_id"], "default")
        self.assertEqual(self.counts(), before)

    def test_withdraw_sealed_implicit_default_cycle_of_added_device_rejected(self):
        """新增设备的隐式 default 周期被封存（空周期也可封存）-> 拒绝撤回设备。"""
        self.import_bundle("B-S2", devices=[{"device_id": "D1"}])
        self.seal("D1", "default")
        status, body = self.withdraw("B-S2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 设备仍在
        self.assertEqual(self.counts()["devices"], 1)

    # ------------------------------------------------------------ 明确拒绝：被其他包复用

    def test_withdraw_record_reused_by_other_bundle_rejected(self):
        self.import_bundle(
            "B-R1",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}])
        # B2 复用 B1 的设备与锚点，并新增自己的声段
        status, body = self.import_bundle(
            "B-R2",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000}],
            segments=[{"capture_id": "CAP-9", "device_id": "D1",
                       "local_start_ms": 10, "local_end_ms": 20}])
        self.assertEqual(status, 201)
        self.assertEqual(body["devices"], {"added": 0, "reused": 1})
        before = self.counts()
        # B1 的设备与锚点被 B2 复用 -> 拒绝
        status, body = self.withdraw("B-R1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_reused")
        reused = body["error"]["details"]["reused"]
        self.assertEqual({r["type"] for r in reused}, {"device", "anchor"})
        self.assertTrue(all(r["bundle_ids"] == ["B-R2"] for r in reused))
        self.assertEqual(self.counts(), before)
        # 先撤回 B2（只删其新增声段，复用记录保留），再撤回 B1 -> 成功
        status, body = self.withdraw("B-R2")
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"]["capture_ids"], ["CAP-9"])
        self.assertEqual(body["anchors"]["removed"], 0)  # 复用的锚点绝不删除
        status, body = self.withdraw("B-R1")
        self.assertEqual(status, 200)
        self.assertEqual(self.counts(), {"devices": 0, "cycles": 0,
                                         "anchors": 0, "segments": 0})

    # ------------------------------------------------------------ 明确拒绝：包外内容

    def test_withdraw_added_device_with_external_content_rejected(self):
        self.import_bundle("B-E1", devices=[{"device_id": "D1"}])
        # 包外向新设备登记周期
        self.reg_cycle("D1", "boot-2")
        before = self.counts()
        status, body = self.withdraw("B-E1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        ext = body["error"]["details"]["external"]
        self.assertEqual(ext[0]["type"], "cycle")
        self.assertEqual(ext[0]["cycle_id"], "boot-2")
        self.assertEqual(self.counts(), before)

    def test_withdraw_added_device_with_external_anchor_and_segment_rejected(self):
        self.import_bundle(
            "B-E2",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}])
        # 包外向新设备的隐式 default 周期加锚点与声段
        self.add_anchor("D1", 5000, 6000)
        self.submit("CAP-OUT", "D1", 100, 200)
        status, body = self.withdraw("B-E2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        types = sorted(e["type"] for e in body["error"]["details"]["external"])
        self.assertEqual(types, ["anchor", "segment"])
        # 包外记录与本包记录都未被改动
        self.assertEqual(self.counts(),
                         {"devices": 1, "cycles": 1, "anchors": 3, "segments": 1})

    def test_withdraw_added_cycle_with_external_content_rejected(self):
        """新增周期（设备为库中既有）内出现包外锚点/声段 -> 拒绝。"""
        self.make_device("D1")
        self.import_bundle(
            "B-E3",
            cycles=[{"device_id": "D1", "cycle_id": "boot-2"}],
            anchors=[{"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 10000, "utc_ms": 11000}])
        self.add_anchor("D1", 5000, 6000, "boot-2")
        self.submit("CAP-OUT", "D1", 100, 200, "boot-2")
        status, body = self.withdraw("B-E3")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        types = sorted(e["type"] for e in body["error"]["details"]["external"])
        self.assertEqual(types, ["anchor", "segment"])
        # 既有设备与包外内容保持不变
        self.assertEqual(self.counts()["devices"], 1)
        self.assertEqual(self.counts()["anchors"], 3)

    # ------------------------------------------------------------ 明确拒绝：定位保护

    def test_withdraw_projection_change_rejected(self):
        """新增锚点偏离原直线：删除它会改变保留声段的 UTC 位置 -> 拒绝。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.submit("CAP-OLD", "D1", 1000, 9000)
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertEqual(seg["utc_start_ms"], 2000)
        self.assertEqual(seg["utc_end_ms"], 10000)
        # 包新增锚点 (5000, 6200) 偏离原直线 y=1000+x -> CAP-OLD 投影已改变
        status, _ = self.import_bundle(
            "B-P1",
            anchors=[{"device_id": "D1", "local_ms": 5000, "utc_ms": 6200}])
        self.assertEqual(status, 201)
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertNotEqual(seg["utc_end_ms"], 10000)
        before = self.counts()
        status, body = self.withdraw("B-P1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_projection_changed")
        segs = body["error"]["details"]["segments"]
        self.assertEqual([s["capture_id"] for s in segs], ["CAP-OLD"])
        self.assertEqual(segs[0]["before"]["status"], "located")
        self.assertEqual(self.counts(), before)

    def test_withdraw_projection_to_unlocatable_rejected(self):
        """新增锚点是保留声段唯一的覆盖来源：删除会使其不可定位 -> 拒绝。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 1000, 2000)
        # CAP-OLD 终点 5000 超出 [0,1000] 覆盖 -> 当前不可定位
        self.submit("CAP-OLD", "D1", 100, 5000)
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertEqual(seg["status"], "unlocatable")
        # 包新增锚点 (10000, 11000) 使 CAP-OLD 变为可定位
        self.import_bundle(
            "B-P2", anchors=[{"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}])
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertEqual(seg["status"], "located")
        status, body = self.withdraw("B-P2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_projection_changed")
        change = body["error"]["details"]["segments"][0]
        self.assertEqual(change["before"]["status"], "located")
        self.assertEqual(change["after"]["status"], "unlocatable")

    def test_withdraw_collinear_anchor_keeps_projection_and_succeeds(self):
        """新增锚点在原直线上：删除不改变保留声段定位 -> 允许撤回。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.submit("CAP-OLD", "D1", 1000, 9000)
        self.import_bundle(
            "B-P3",
            anchors=[{"device_id": "D1", "local_ms": 5000, "utc_ms": 6000}],
            segments=[{"capture_id": "CAP-NEW", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        status, body = self.withdraw("B-P3")
        self.assertEqual(status, 200)
        self.assertEqual(body["anchors"]["removed"], 1)
        self.assertEqual(body["segments"]["capture_ids"], ["CAP-NEW"])
        # 保留声段定位与导入前一致
        seg = self.req("GET", "/segments/CAP-OLD")[1]["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual((seg["utc_start_ms"], seg["utc_end_ms"]), (2000, 10000))

    # ------------------------------------------------------------ 与版本发布共存

    def test_published_versions_immutable_withdraw_reflected_in_new_version(self):
        self.import_bundle(
            "B-V1",
            devices=[{"device_id": "D1"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v1["segment_count"], 1)
        status, body = self.withdraw("B-V1")
        self.assertEqual(status, 200)
        # 已发布版本保持原快照
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again, v1)
        self.assertEqual(v1_again["segment_count"], 1)
        # 之后发布的版本反映撤回：CAP-1 进入 removed
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["segment_count"], 0)
        self.assertEqual([s["capture_id"] for s in v2["changes"]["removed"]],
                         ["CAP-1"])
        self.assertEqual(v2["changes"]["added"], [])

    # ------------------------------------------------------------ 并发

    def test_concurrent_withdraw_same_bundle_executes_once(self):
        """同包并发撤回：只执行一次删除，其余返回首次结果。"""
        status, _ = self.req("POST", "/bundles", self.sample_bundle("B-RACE"))
        self.assertEqual(status, 201)
        outcomes, errors = [], []

        def send():
            try:
                outcomes.append(self.withdraw("B-RACE"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=send) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual([s for s, _ in outcomes], [200, 200, 200, 200])
        replays = sorted(b["idempotent_replay"] for _, b in outcomes)
        self.assertEqual(replays, [False, True, True, True])
        bodies = [{k: v for k, v in b.items() if k != "idempotent_replay"}
                  for _, b in outcomes]
        self.assertTrue(all(b == bodies[0] for b in bodies))
        # 只删除一次：库中为空
        self.assertEqual(self.counts(), {"devices": 0, "cycles": 0,
                                         "anchors": 0, "segments": 0})

    def test_concurrent_withdraw_import_and_publish(self):
        """并发撤回/导入/发布：每个版本仍是完整自洽快照，无半包状态。"""
        # 预先导入两个包，随后并发撤回
        for k in range(2):
            status, _ = self.import_bundle(
                f"B-PRE-{k}",
                devices=[{"device_id": f"OLD-{k}"}],
                anchors=[{"device_id": f"OLD-{k}", "local_ms": 0,
                          "utc_ms": 900000 + k * 100000},
                         {"device_id": f"OLD-{k}", "local_ms": 10000,
                          "utc_ms": 910000 + k * 100000}],
                segments=[{"capture_id": f"CAP-OLD-{k}", "device_id": f"OLD-{k}",
                           "local_start_ms": 100, "local_end_ms": 200}])
            self.assertEqual(status, 201)
        results, errors = [], []

        def withdraw_one(k):
            try:
                results.append(self.withdraw(f"B-PRE-{k}")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def import_one(k):
            try:
                results.append(self.import_bundle(
                    f"B-NEW-{k}",
                    devices=[{"device_id": f"NEW-{k}"}],
                    anchors=[{"device_id": f"NEW-{k}", "local_ms": 0,
                              "utc_ms": k * 100000},
                             {"device_id": f"NEW-{k}", "local_ms": 10000,
                              "utc_ms": k * 100000 + 10000}],
                    segments=[{"capture_id": f"CAP-NEW-{k}", "device_id": f"NEW-{k}",
                               "local_start_ms": 100, "local_end_ms": 200}])[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(2):
            threads.append(threading.Thread(target=withdraw_one, args=(k,)))
        for k in range(3):
            threads.append(threading.Thread(target=import_one, args=(k,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results))
        # 最终状态：旧包已撤回，新包完整入库
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual({s["capture_id"] for s in tl["segments"]},
                         {f"CAP-NEW-{k}" for k in range(3)})
        self.assertEqual({d["device_id"] for d in tl["devices"]},
                         {f"NEW-{k}" for k in range(3)})
        # 版本号连续无空洞；每个版本内同一设备的锚点与声段成完整集合（无半包）
        versions = self.req("GET", "/versions")[1]["versions"]
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            for dev in {s["device_id"] for s in doc["segments"]}:
                self.assertEqual(
                    sorted(a["local_ms"] for a in doc["anchors"]
                           if a["device_id"] == dev), [0, 10000])
            for a in doc["anchors"]:
                dev = a["device_id"]
                self.assertTrue(
                    dev.startswith("OLD-") or dev.startswith("NEW-"))
                # 设备要么完整出现（锚点 2 条），要么不出现（旧包已整体撤回）
                self.assertEqual(
                    len([x for x in doc["anchors"] if x["device_id"] == dev]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
