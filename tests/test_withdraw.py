"""端到端测试：采集包撤回（bundle withdrawal，v1.10）。

覆盖：
- 基本成功：按包编号撤回，只删除该包新增的设备/周期（含隐式 default
  周期）/锚点/声段，复用的原有记录绝不删除；返回各类撤回数量与状态；
- 幂等：重复撤回返回首次撤回结果（撤回时间不变）；
- 撤回后同编号导入重传仍返回首次导入回执且不重新写入；异内容仍
  bundle_conflict；已发布版本保持原快照；
- 整包拒绝（指出阻碍，不改动任何数据，包保持未撤回）：
  新增记录内容已变化（锚点被更正/删除、声段被删除/改归属）、
  被其他未撤回包复用、新设备/周期已有包外内容（直接录入或其他包追加）、
  相关周期已封存、删除会改变保留声段的可定位状态或 UTC 位置；
  旧包缺少新增明细（含旧库 bundles 表自动补列迁移）；
- 依赖方先撤回后本包可撤回；空新增包（全部复用）可撤回且不动复用记录；
- 并发：撤回与导入/发布串行，同包并发撤回只生效一次。

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
        """库中各类记录总数（用于验证拒绝后不留下任何变化）。"""
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

    def device_bundle(self, bundle_id, dev, cap, name=None):
        """单设备完整采集包：设备 + default 周期 2 锚点 + 1 声段。"""
        return {
            "bundle_id": bundle_id,
            "devices": [{"device_id": dev, "name": name}],
            "anchors": [{"device_id": dev, "local_ms": 0, "utc_ms": 1000},
                        {"device_id": dev, "local_ms": 10000, "utc_ms": 11000}],
            "segments": [{"capture_id": cap, "device_id": dev,
                          "local_start_ms": 100, "local_end_ms": 200}],
        }

    # ------------------------------------------------------------ 成功场景

    def test_withdraw_success_full_bundle(self):
        """整包新增（设备+显式周期+两周期锚点+声段）-> 全部删除，计数正确。"""
        status, body = self.import_bundle(
            "B-FULL",
            devices=[{"device_id": "D1", "name": "甲"}],
            cycles=[{"device_id": "D1", "cycle_id": "boot-2", "note": "第二次开机"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 0, "utc_ms": 2000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 10000, "utc_ms": 12000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200},
                      {"capture_id": "CAP-2", "device_id": "D1", "cycle_id": "boot-2",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        self.assertEqual(self.counts(),
                         {"devices": 1, "cycles": 2, "anchors": 4, "segments": 2})

        status, body = self.withdraw("B-FULL")
        self.assertEqual(status, 200)
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["bundle_id"], "B-FULL")
        self.assertEqual(body["status"], "withdrawn")
        self.assertIsNotNone(body["withdrawn_at"])
        # 周期计数含新设备隐式 default 周期
        self.assertEqual(body["devices"], {"removed": 1})
        self.assertEqual(body["cycles"], {"removed": 2})
        self.assertEqual(body["anchors"], {"removed": 4})
        self.assertEqual(body["segments"], {"removed": 2})
        # 当前时间轴已清空
        self.assertEqual(self.counts(),
                         {"devices": 0, "cycles": 0, "anchors": 0, "segments": 0})
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual(tl["devices"], [])
        self.assertEqual(tl["segments"], [])

    def test_withdraw_only_added_records_reused_survive(self):
        """混合新增/复用：只删新增，复用的原有记录绝不删除。"""
        self.make_device("D1", "录音笔-甲")
        self.reg_cycle("D1", "boot-2", "第二次开机")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.add_anchor("D1", 0, 2000, "boot-2")
        self.add_anchor("D1", 10000, 12000, "boot-2")
        self.submit("CAP-KEEP", "D1", 100, 200)
        before = self.counts()

        status, body = self.import_bundle(
            "B-MIX",
            devices=[{"device_id": "D1", "name": "录音笔-甲"}],      # 复用
            cycles=[{"device_id": "D1", "cycle_id": "boot-2",
                     "note": "第二次开机"}],                            # 复用
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},  # 复用
                     {"device_id": "D1", "local_ms": 20000, "utc_ms": 21000}],  # 新增
            segments=[{"capture_id": "CAP-KEEP", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200},   # 复用
                      {"capture_id": "CAP-NEW", "device_id": "D1",
                       "local_start_ms": 15000, "local_end_ms": 16000}])  # 新增
        self.assertEqual(status, 201)
        self.assertEqual(body["anchors"], {"added": 1, "reused": 1})
        self.assertEqual(body["segments"], {"added": 1, "reused": 1})

        status, body = self.withdraw("B-MIX")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], {"removed": 0})
        self.assertEqual(body["cycles"], {"removed": 0})
        self.assertEqual(body["anchors"], {"removed": 1})
        self.assertEqual(body["segments"], {"removed": 1})
        # 复用记录全部保留，库状态回到导入前
        self.assertEqual(self.counts(), before)
        seg = self.req("GET", "/segments/CAP-KEEP")[1]["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual(self.req("GET", "/segments/CAP-NEW")[0], 404)
        # 新增锚点已删，原有锚点仍在
        _, anchors = self.req("GET", "/devices/D1/anchors")
        self.assertEqual({a["local_ms"] for a in anchors["anchors"]},
                         {0, 10000})

    def test_withdraw_replay_returns_first_result(self):
        """重复撤回 -> 200 返回首次撤回结果（撤回时间不变），不重复删除。"""
        bundle = self.device_bundle("B-REPLAY", "D1", "CAP-1")
        self.assertEqual(self.req("POST", "/bundles", bundle)[0], 201)
        status, first = self.withdraw("B-REPLAY")
        self.assertEqual(status, 200)
        after_first = self.counts()

        status, replay = self.withdraw("B-REPLAY")
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["withdrawn_at"], first["withdrawn_at"])
        self.assertEqual({k: v for k, v in replay.items() if k != "idempotent_replay"},
                         {k: v for k, v in first.items() if k != "idempotent_replay"})
        self.assertEqual(self.counts(), after_first)

    def test_reimport_after_withdraw_returns_first_receipt_no_rewrite(self):
        """撤回后同编号同内容重传 -> 返回首次导入回执且不重新写入；
        同编号异内容 -> 409 bundle_conflict。"""
        bundle = self.device_bundle("B-REIMPORT", "D1", "CAP-1")
        status, first = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        self.assertEqual(self.withdraw("B-REIMPORT")[0], 200)
        empty = self.counts()

        status, replay = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["anchors"], first["anchors"])
        # 不重新写入：库仍为空
        self.assertEqual(self.counts(), empty)
        # 同编号异内容仍拒绝
        other = self.device_bundle("B-REIMPORT", "D1", "CAP-1")
        other["segments"] = [{"capture_id": "CAP-2", "device_id": "D1",
                              "local_start_ms": 1, "local_end_ms": 2}]
        status, body = self.req("POST", "/bundles", other)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_conflict")
        self.assertEqual(self.counts(), empty)

    def test_withdraw_empty_added_bundle(self):
        """全部复用的包（新增明细为空）也可撤回：删除 0 条，复用记录不动。"""
        self.make_device("D1", "甲")
        self.add_anchor("D1", 0, 1000)
        status, body = self.import_bundle(
            "B-ALL-REUSED",
            devices=[{"device_id": "D1", "name": "甲"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000}])
        self.assertEqual(status, 201)
        self.assertEqual(body["devices"], {"added": 0, "reused": 1})
        before = self.counts()

        status, body = self.withdraw("B-ALL-REUSED")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], {"removed": 0})
        self.assertEqual(body["cycles"], {"removed": 0})
        self.assertEqual(body["anchors"], {"removed": 0})
        self.assertEqual(body["segments"], {"removed": 0})
        self.assertEqual(self.counts(), before)

    def test_withdraw_published_versions_unchanged(self):
        """已发布版本保持原快照；撤回后发布的版本体现移除。"""
        bundle = self.device_bundle("B-VER", "D1", "CAP-1")
        self.assertEqual(self.req("POST", "/bundles", bundle)[0], 201)
        status, v1 = self.req("POST", "/versions", {"note": "撤回前"})
        self.assertEqual(status, 201)
        self.assertEqual(v1["segment_count"], 1)

        self.assertEqual(self.withdraw("B-VER")[0], 200)
        # 已发布版本不变：CAP-1 仍在 v1 快照中且可定位
        _, doc = self.req("GET", "/versions/1")
        self.assertEqual(doc["segment_count"], 1)
        self.assertEqual(doc["segments"][0]["capture_id"], "CAP-1")
        self.assertEqual(doc["segments"][0]["status"], "located")
        self.assertEqual(len(doc["anchors"]), 2)
        # 当前时间轴已移除；新版本 changes.removed 体现
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual(tl["segments"], [])
        status, v2 = self.req("POST", "/versions", {"note": "撤回后"})
        self.assertEqual(status, 201)
        self.assertEqual(v2["segment_count"], 0)
        self.assertEqual([s["capture_id"] for s in v2["changes"]["removed"]],
                         ["CAP-1"])
        # v1 再次查询仍不变
        _, doc = self.req("GET", "/versions/1")
        self.assertEqual(doc["segment_count"], 1)

    def test_withdraw_leaves_other_records_untouched(self):
        """撤回只影响本包新增记录：其他包与直接录入的记录不变。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-A", "DA", "CAP-A"))[0], 201)
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-B", "DB", "CAP-B"))[0], 201)
        self.make_device("DC")
        self.add_anchor("DC", 0, 1000)
        self.add_anchor("DC", 10000, 11000)
        self.submit("CAP-C", "DC", 100, 200)

        status, body = self.withdraw("B-A")
        self.assertEqual(status, 200)
        self.assertEqual(self.counts(),
                         {"devices": 2, "cycles": 2, "anchors": 4, "segments": 2})
        self.assertEqual(self.req("GET", "/devices/DA")[0], 404)
        self.assertEqual(self.req("GET", "/devices/DB")[0], 200)
        self.assertEqual(self.req("GET", "/segments/CAP-B")[0], 200)
        self.assertEqual(self.req("GET", "/segments/CAP-C")[0], 200)
        # B-B 随后可正常撤回
        self.assertEqual(self.withdraw("B-B")[0], 200)
        self.assertEqual(self.counts(),
                         {"devices": 1, "cycles": 1, "anchors": 2, "segments": 1})

    # ------------------------------------------------------------ 明确拒绝

    def test_withdraw_bundle_not_found(self):
        status, body = self.withdraw("B-NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "bundle_not_found")
        self.assertEqual(body["error"]["details"]["bundle_id"], "B-NOPE")

    def test_withdraw_invalid_bundle_id(self):
        status, body = self.req("POST", "/bundles/BAD%20ID/withdraw")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_withdraw_legacy_bundle_without_details(self):
        """旧包（added_details 为 NULL）缺少首次导入新增明细 -> 明确拒绝。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-NEW", "D1", "CAP-1"))[0], 201)
        # 模拟 v1.10 之前导入的包：只有内容指纹与首次结果，无新增明细
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO bundles(bundle_id, content_hash, result, created_at) "
                "VALUES (?,?,?,?)",
                ("B-OLD", "0" * 64,
                 json.dumps({"bundle_id": "B-OLD",
                             "devices": {"added": 1, "reused": 0}},
                            ensure_ascii=False),
                 "2026-10-01T00:00:00.000Z"))
        status, body = self.withdraw("B-OLD")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_details_missing")
        # 新包不受影响，可正常撤回
        self.assertEqual(self.withdraw("B-NEW")[0], 200)

    def test_legacy_bundles_table_auto_migrated(self):
        """旧库 bundles 表（无撤回列）启动自动补列；旧行不可撤回，新包可撤回。"""
        legacy = os.path.join(self.tmpdir.name, "legacy_bundles.db")
        conn = sqlite3.connect(legacy)
        conn.executescript("""
            CREATE TABLE bundles (
                bundle_id    TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                result       TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO bundles VALUES (?,?,?,?)",
            ("B-LEGACY", "1" * 64, json.dumps({"bundle_id": "B-LEGACY"}),
             "2026-10-01T00:00:00.000Z"))
        conn.commit()
        conn.close()
        db.init_db(legacy)
        self.server.ctx.db_path = legacy
        # 新列已补齐
        cols = {r[1] for r in sqlite3.connect(legacy)
                .execute("PRAGMA table_info(bundles)")}
        self.assertTrue({"added_details", "record_keys", "withdrawn_at",
                         "withdrawal_result"} <= cols)
        # 旧行缺少新增明细 -> 明确拒绝
        status, body = self.withdraw("B-LEGACY")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_details_missing")
        # 新导入的包带新增明细，可撤回
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-NEW", "D1", "CAP-1"))[0], 201)
        self.assertEqual(self.withdraw("B-NEW")[0], 200)

    def test_withdraw_rejected_when_anchor_corrected(self):
        """新增锚点被更正（PUT 整体替换）-> 内容已变化，整包拒绝。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-CHG", "D1", "CAP-1"))[0], 201)
        status, _ = self.req("PUT", "/devices/D1/anchors",
                             {"anchors": [{"local_ms": 0, "utc_ms": 1000},
                                          {"local_ms": 10000, "utc_ms": 11500}]})
        self.assertEqual(status, 200)
        before = self.counts()
        status, body = self.withdraw("B-CHG")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_changed")
        changed = body["error"]["details"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["type"], "anchor")
        self.assertEqual(changed[0]["expected_utc_ms"], 11000)
        self.assertEqual(changed[0]["current_utc_ms"], 11500)
        # 不改动任何数据，包保持未撤回
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.withdraw("B-CHG")[0], 409)

    def test_withdraw_rejected_when_anchor_deleted(self):
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-DEL", "D1", "CAP-1"))[0], 201)
        self.assertEqual(self.req("DELETE", "/devices/D1/anchors/10000")[0], 200)
        status, body = self.withdraw("B-DEL")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_changed")
        changed = body["error"]["details"]["changed"]
        self.assertEqual([c["type"] for c in changed], ["anchor"])
        self.assertIsNone(changed[0]["current_utc_ms"])

    def test_withdraw_rejected_when_segment_deleted(self):
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-SDEL", "D1", "CAP-1"))[0], 201)
        self.assertEqual(self.req("DELETE", "/segments/CAP-1")[0], 200)
        status, body = self.withdraw("B-SDEL")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_changed")
        changed = body["error"]["details"]["changed"]
        self.assertEqual([c["type"] for c in changed], ["segment"])
        self.assertEqual(changed[0]["current"], "missing")

    def test_withdraw_rejected_when_segment_reassigned(self):
        """新增声段被改归属到其他周期 -> 内容指纹变化，整包拒绝。"""
        status, _ = self.import_bundle(
            "B-REAS",
            devices=[{"device_id": "D1"}],
            cycles=[{"device_id": "D1", "cycle_id": "boot-2"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 0, "utc_ms": 2000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 10000, "utc_ms": 12000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-1",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)
        status, body = self.withdraw("B-REAS")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_changed")
        self.assertEqual(body["error"]["details"]["changed"][0]["type"], "segment")

    def test_withdraw_rejected_when_reused_by_other_bundle(self):
        """新增记录被其他未撤回包复用 -> 拒绝；依赖方撤回后本包可撤回。"""
        status, _ = self.import_bundle(
            "B-BASE",
            devices=[{"device_id": "D1", "name": "甲"}],
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},
                     {"device_id": "D1", "local_ms": 10000, "utc_ms": 11000}],
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200}])
        self.assertEqual(status, 201)
        # B-DEP 复用 B-BASE 的设备/锚点/声段，并新增自己的锚点与声段
        status, body = self.import_bundle(
            "B-DEP",
            devices=[{"device_id": "D1", "name": "甲"}],                 # 复用
            anchors=[{"device_id": "D1", "local_ms": 0, "utc_ms": 1000},  # 复用
                     {"device_id": "D1", "local_ms": 20000, "utc_ms": 21000}],  # 新增
            segments=[{"capture_id": "CAP-1", "device_id": "D1",
                       "local_start_ms": 100, "local_end_ms": 200},      # 复用
                      {"capture_id": "CAP-2", "device_id": "D1",
                       "local_start_ms": 15000, "local_end_ms": 16000}])  # 新增
        self.assertEqual(status, 201)

        status, body = self.withdraw("B-BASE")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_record_reused")
        reused_by = body["error"]["details"]["reused_by"]
        self.assertEqual(len(reused_by), 1)
        self.assertEqual(reused_by[0]["bundle_id"], "B-DEP")
        self.assertEqual(reused_by[0]["devices"], ["D1"])
        self.assertEqual(reused_by[0]["anchors"], [["D1", "default", 0]])
        self.assertEqual(reused_by[0]["segments"], ["CAP-1"])
        # 不改动任何数据
        self.assertEqual(self.counts(),
                         {"devices": 1, "cycles": 1, "anchors": 3, "segments": 2})

        # 先撤回依赖方：只删 B-DEP 新增的锚点/声段，复用记录保留
        status, body = self.withdraw("B-DEP")
        self.assertEqual(status, 200)
        self.assertEqual(body["anchors"], {"removed": 1})
        self.assertEqual(body["segments"], {"removed": 1})
        self.assertEqual(self.req("GET", "/segments/CAP-1")[0], 200)
        # 本包随后可撤回
        status, body = self.withdraw("B-BASE")
        self.assertEqual(status, 200)
        self.assertEqual(self.counts(),
                         {"devices": 0, "cycles": 0, "anchors": 0, "segments": 0})

    def test_withdraw_rejected_when_cycle_sealed(self):
        """相关周期已封存 -> 409 cycle_sealed，状态不变。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-SEAL", "D1", "CAP-1"))[0], 201)
        self.seal("D1", "default")
        before = self.counts()
        status, body = self.withdraw("B-SEAL")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycles"][0]["cycle_id"], "default")
        self.assertEqual(self.counts(), before)

    def test_withdraw_rejected_when_projection_shifts(self):
        """删除新增锚点会改变保留声段的可定位状态或 UTC 位置 -> 拒绝。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.submit("CAP-KEEP", "D1", 2000, 4000)      # 可定位（rate 1.0）
        self.submit("CAP-FAR", "D1", 15000, 16000)     # 覆盖不足，不可定位
        # 包新增两个锚点：一个改变 CAP-KEEP 的插值，一个让 CAP-FAR 变为可定位
        status, _ = self.import_bundle(
            "B-SHIFT",
            anchors=[{"device_id": "D1", "local_ms": 5000, "utc_ms": 5400},
                     {"device_id": "D1", "local_ms": 20000, "utc_ms": 21000}])
        self.assertEqual(status, 201)
        seg = self.req("GET", "/segments/CAP-FAR")[1]["segment"]
        self.assertEqual(seg["status"], "located")

        status, body = self.withdraw("B-SHIFT")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_projection_shift")
        affected = {s["capture_id"] for s in body["error"]["details"]
                    ["affected_segments"]}
        self.assertEqual(affected, {"CAP-KEEP", "CAP-FAR"})
        # 两段保留声段投影均未受影响（撤回被拒绝）
        self.assertEqual(self.req("GET", "/segments/CAP-FAR")[1]["segment"]["status"],
                         "located")

    def test_withdraw_allowed_when_projection_unaffected(self):
        """新增锚点不影响任何保留声段定位 -> 允许撤回，保留声段投影不变。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 1000)
        self.add_anchor("D1", 10000, 11000)
        self.submit("CAP-KEEP", "D1", 500, 1500)
        before_seg = self.req("GET", "/segments/CAP-KEEP")[1]["segment"]
        # 包新增锚点（保留声段范围之外）与自有声段
        status, _ = self.import_bundle(
            "B-NOSHA",
            anchors=[{"device_id": "D1", "local_ms": 20000, "utc_ms": 21000}],
            segments=[{"capture_id": "CAP-NEW", "device_id": "D1",
                       "local_start_ms": 15000, "local_end_ms": 16000}])
        self.assertEqual(status, 201)

        status, body = self.withdraw("B-NOSHA")
        self.assertEqual(status, 200)
        self.assertEqual(body["anchors"], {"removed": 1})
        self.assertEqual(body["segments"], {"removed": 1})
        after_seg = self.req("GET", "/segments/CAP-KEEP")[1]["segment"]
        self.assertEqual(before_seg["status"], after_seg["status"])
        self.assertEqual(before_seg["utc_start_ms"], after_seg["utc_start_ms"])
        self.assertEqual(before_seg["utc_end_ms"], after_seg["utc_end_ms"])
        self.assertEqual(self.req("GET", "/segments/CAP-NEW")[0], 404)

    def test_withdraw_rejected_when_added_device_has_external_content(self):
        """新设备已有包外内容（直接录入的周期/锚点/声段）-> 拒绝。"""
        # 场景一：包外新增锚点
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-E1", "DA", "CAP-A"))[0], 201)
        self.assertEqual(self.add_anchor("DA", 20000, 21000)[0], 201)
        status, body = self.withdraw("B-E1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        external = body["error"]["details"]["external"]
        self.assertEqual(external, [{"type": "anchor", "device_id": "DA",
                                     "cycle_id": "default", "local_ms": 20000}])

        # 场景二：包外新增声段
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-E2", "DB", "CAP-B"))[0], 201)
        self.assertEqual(self.submit("CAP-X", "DB", 300, 400)[0], 201)
        status, body = self.withdraw("B-E2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        self.assertEqual(body["error"]["details"]["external"][0]["type"], "segment")

        # 场景三：包外登记新周期
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-E3", "DC", "CAP-C"))[0], 201)
        self.assertEqual(self.reg_cycle("DC", "boot-9")[0], 201)
        status, body = self.withdraw("B-E3")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        self.assertEqual(body["error"]["details"]["external"][0],
                         {"type": "cycle", "device_id": "DC", "cycle_id": "boot-9"})

    def test_withdraw_rejected_when_added_cycle_has_external_content(self):
        """新增周期（在既有设备上）已有包外锚点/声段 -> 拒绝。"""
        self.make_device("D1")
        status, _ = self.import_bundle(
            "B-CYC",
            cycles=[{"device_id": "D1", "cycle_id": "boot-2"}],
            anchors=[{"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 0, "utc_ms": 2000},
                     {"device_id": "D1", "cycle_id": "boot-2",
                      "local_ms": 10000, "utc_ms": 12000}])
        self.assertEqual(status, 201)
        # 包外向该周期追加锚点与声段
        self.assertEqual(self.add_anchor("D1", 20000, 22000, "boot-2")[0], 201)
        self.assertEqual(self.submit("CAP-X", "D1", 100, 200, "boot-2")[0], 201)
        status, body = self.withdraw("B-CYC")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        types = {e["type"] for e in body["error"]["details"]["external"]}
        self.assertEqual(types, {"anchor", "segment"})

    def test_withdraw_rejected_when_other_bundle_adds_content_to_device(self):
        """其他包在本包新设备下追加内容 -> 拒绝；该包撤回后本包可撤回。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-OWN", "D1", "CAP-1"))[0], 201)
        status, _ = self.import_bundle(
            "B-EXT",
            anchors=[{"device_id": "D1", "local_ms": 20000, "utc_ms": 21000}])
        self.assertEqual(status, 201)
        status, body = self.withdraw("B-OWN")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "bundle_external_content")
        # 另一包撤回后（其新增锚点不影响 CAP-1 定位），本包可撤回
        self.assertEqual(self.withdraw("B-EXT")[0], 200)
        self.assertEqual(self.withdraw("B-OWN")[0], 200)
        self.assertEqual(self.counts(),
                         {"devices": 0, "cycles": 0, "anchors": 0, "segments": 0})

    # ------------------------------------------------------------ 并发

    def test_concurrent_same_bundle_withdraw_once(self):
        """同一包并发撤回：只生效一次，其余返回首次撤回结果。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-RACE", "D1", "CAP-1"))[0], 201)
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
        flags = sorted(body["idempotent_replay"] for _, body in outcomes)
        self.assertEqual(flags, [False, True, True, True])
        bodies = [{k: v for k, v in body.items() if k != "idempotent_replay"}
                  for _, body in outcomes]
        self.assertTrue(all(b == bodies[0] for b in bodies))
        self.assertEqual(self.counts(),
                         {"devices": 0, "cycles": 0, "anchors": 0, "segments": 0})

    def test_concurrent_withdraw_import_and_publish(self):
        """撤回与导入、发布并发：串行判定，版本快照自洽，无半包状态。"""
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-W1", "DW1", "CAP-W1"))[0], 201)
        self.assertEqual(self.req("POST", "/bundles",
                                  self.device_bundle("B-W2", "DW2", "CAP-W2"))[0], 201)
        results, errors = [], []

        def run(fn):
            try:
                results.append(fn())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=run, args=(lambda: self.withdraw("B-W1"),)),
            threading.Thread(target=run, args=(lambda: self.withdraw("B-W2"),)),
            threading.Thread(target=run, args=(lambda: self.req(
                "POST", "/bundles", self.device_bundle("B-N1", "DN1", "CAP-N1")),)),
            threading.Thread(target=run, args=(lambda: self.req("POST", "/versions"),)),
            threading.Thread(target=run, args=(lambda: self.req("POST", "/versions"),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 两个撤回都成功（各 200）
        withdraws = [body for (s, body) in (r for r in results)
                     if s == 200 and body.get("status") == "withdrawn"]
        self.assertEqual(len(withdraws), 2)
        # 最终状态：B-W1/B-W2 的记录已删，B-N1 完整入库
        self.assertEqual(self.req("GET", "/devices/DW1")[0], 404)
        self.assertEqual(self.req("GET", "/devices/DW2")[0], 404)
        self.assertEqual(self.req("GET", "/devices/DN1")[0], 200)
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual([s["capture_id"] for s in tl["segments"]], ["CAP-N1"])
        # 版本号连续；每个版本快照自洽（版本中出现的设备其锚点完整）
        versions = self.req("GET", "/versions")[1]["versions"]
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            for seg in doc["segments"]:
                dev = seg["device_id"]
                self.assertEqual(
                    sorted(a["local_ms"] for a in doc["anchors"]
                           if a["device_id"] == dev), [0, 10000])
                self.assertEqual(seg["status"], "located")


if __name__ == "__main__":
    unittest.main(verbosity=2)
