"""端到端测试：按已发布版本恢复未封存周期（restore，v1.9）。

覆盖：
- 成功恢复：锚点整体替换（含新增/误删）、声段补齐/按冻结内容改本地起止与
  备注/删除版本中不属于该周期的声段，恢复后定位与指纹；
- 恢复前后指纹、锚点与声段数量及 inserted/updated/removed 分类；
- 恢复后声段 UTC 投影与该版本冻结投影一致，按恢复后锚点重算；
- 已发布版本 / version_* 快照 / bundles 回执 / 其他设备周期保持原样；
- 版本不存在 / 设备不存在 / 周期不存在 / 指纹过期 / 周期已封存 /
  version_no 或 fingerprint 非法 -> 整次拒绝；
- 版本中既无目标周期锚点也无声段（含旧版非 default 周期）-> 拒绝，
  绝不按空周期清除当前数据；
- 版本声段采集编号现被其他设备或其他周期占用 -> 拒绝且无部分写入；
- 旧版（format_rev=1）未标周期记录按 default 恢复；
- 补录声段沿用冻结 created_at，更新声段保留现有 created_at；
- 仅锚点 / 仅声段冻结内容均可恢复；恢复后同内容重传仍幂等。

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
from app.logic import cycle_fingerprint  # noqa: E402


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


class RestoreApiTestCase(unittest.TestCase):
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

    # ------------------------------------------------------------ 场景搭建

    def _seed_v1(self, device="D1", note="鸟鸣", *, boot_cycle=None):
        """注册设备、录入两锚点与 C1/C2 两声段，发布 v1。

        boot_cycle 非空时额外登记该周期并录入同 local_ms 的锚点与声段 C3，
        用于验证恢复只改目标周期。
        """
        self.req("POST", "/devices", {"device_id": device, "name": "甲"})
        self.req("POST", f"/devices/{device}/anchors",
                 {"local_ms": 1000, "utc_ms": 1700000000000})
        self.req("POST", f"/devices/{device}/anchors",
                 {"local_ms": 61000, "utc_ms": 1700000060500})
        self.req("POST", "/segments",
                 {"capture_id": "C1", "device_id": device,
                  "local_start_ms": 5000, "local_end_ms": 20000, "note": note})
        self.req("POST", "/segments",
                 {"capture_id": "C2", "device_id": device,
                  "local_start_ms": 30000, "local_end_ms": 40000})
        if boot_cycle:
            self.req("POST", f"/devices/{device}/cycles",
                     {"cycle_id": boot_cycle, "note": "重启"})
            self.req("POST", f"/devices/{device}/anchors",
                     {"cycle_id": boot_cycle, "local_ms": 1000,
                      "utc_ms": 1700010000000})
            self.req("POST", f"/devices/{device}/anchors",
                     {"cycle_id": boot_cycle, "local_ms": 61000,
                      "utc_ms": 1700010060500})
            self.req("POST", "/segments",
                     {"capture_id": "C3", "device_id": device,
                      "cycle_id": boot_cycle,
                      "local_start_ms": 5000, "local_end_ms": 20000})
        status, body = self.req("POST", "/versions", {"note": "v1"})
        self.assertEqual(status, 201)
        self.assertEqual(body["version_no"], 1)

    def _tamper(self, device="D1", cycle="default"):
        """现场误改：锚点 +250ms、删 C2、新增 C9、改 C1 起止/备注。

        声段内容在常规 API 下不可直接编辑（同编号异内容 409），误改起止/备注
        的错误状态经直接写库模拟（与 reassign 测试同样的构造方式）。
        """
        self.req("PUT", f"/devices/{device}/anchors",
                 {"cycle_id": cycle, "anchors": [
                     {"local_ms": 1000, "utc_ms": 1700000000250},
                     {"local_ms": 61000, "utc_ms": 1700000060750}]})
        self.req("DELETE", "/segments/C2")
        self.req("POST", "/segments",
                 {"capture_id": "C9", "device_id": device, "cycle_id": cycle,
                  "local_start_ms": 1000, "local_end_ms": 2000})
        conn = sqlite3.connect(self.db_path)
        from app.logic import segment_content_hash
        tampered_hash = segment_content_hash(
            "C1", device, cycle, 6000, 21000, "被误改")
        conn.execute(
            "UPDATE segments SET local_start_ms=6000, local_end_ms=21000, "
            "note='被误改', content_hash=? WHERE capture_id='C1'",
            (tampered_hash,))
        conn.commit()
        conn.close()

    def _current_fp(self, device="D1", cycle="default"):
        st, body = self.req("GET", f"/devices/{device}/cycles/{cycle}")
        self.assertEqual(st, 200)
        return body["cycle"]["fingerprint"]

    def _restore(self, device, cycle, version_no, fingerprint):
        return self.req("POST",
                        f"/devices/{device}/cycles/{cycle}/restore",
                        {"version_no": version_no, "fingerprint": fingerprint})

    # ------------------------------------------------------------ 成功路径

    def test_restore_success_full(self):
        self._seed_v1()
        st, v1 = self.req("GET", "/versions/1")
        frozen_c1 = next(s for s in v1["segments"] if s["capture_id"] == "C1")
        self._tamper()
        # 误改后 C1 投影与 v1 冻结投影不同（锚点 +250ms 且本地起点也被改）
        st, seg = self.req("GET", "/segments/C1")
        self.assertEqual(st, 200)
        self.assertNotEqual(seg["segment"]["utc_start_ms"],
                            frozen_c1["utc_start_ms"])

        fp = self._current_fp()
        status, body = self._restore("D1", "default", 1, fp)
        self.assertEqual(status, 200, body)
        # 恢复前后指纹与数量
        self.assertEqual(body["before"]["anchor_count"], 2)
        self.assertEqual(body["before"]["segment_count"], 2)  # C1 + C9
        self.assertEqual(body["after"]["anchor_count"], 2)
        self.assertEqual(body["after"]["segment_count"], 2)  # C1 + C2
        self.assertNotEqual(body["before"]["fingerprint"], body["after"]["fingerprint"])
        self.assertEqual(body["before"]["fingerprint"], fp)
        # 分类：C2 补录、C1 按冻结内容更新、C9 删除
        self.assertEqual(body["restored"]["inserted_capture_ids"], ["C2"])
        self.assertEqual(body["restored"]["updated_capture_ids"], ["C1"])
        self.assertEqual(body["restored"]["removed_capture_ids"], ["C9"])
        self.assertEqual(body["restored"]["anchor_count"], 2)
        self.assertEqual(body["restored"]["segment_count"], 2)
        # 锚点恢复为冻结内容
        self.assertEqual(
            [(a["local_ms"], a["utc_ms"]) for a in body["anchors"]],
            [(1000, 1700000000000), (61000, 1700000060500)])
        self.assertTrue(all(a["changed"] for a in body["anchors"]))
        # 声段冻结内容
        c1 = next(s for s in body["segments"] if s["capture_id"] == "C1")
        self.assertEqual((c1["local_start_ms"], c1["local_end_ms"], c1["note"]),
                         (5000, 20000, "鸟鸣"))
        self.assertEqual(c1["change"], "updated")

    def test_restore_projections_match_frozen(self):
        self._seed_v1()
        # 记录 v1 冻结投影
        st, v1 = self.req("GET", "/versions/1")
        self.assertEqual(st, 200)
        frozen = {s["capture_id"]: s for s in v1["segments"]}
        self._tamper()
        status, body = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        # 恢复后定位结果与 v1 冻结投影逐项一致
        by_id = {p["capture_id"]: p for p in body["segment_projections"]}
        self.assertEqual(sorted(by_id), ["C1", "C2"])
        for cid, f in frozen.items():
            p = by_id[cid]
            self.assertEqual(p["status"], f["status"])
            self.assertEqual(p["utc_start_ms"], f["utc_start_ms"])
            self.assertEqual(p["utc_end_ms"], f["utc_end_ms"])
        # after 计数与排序（located 在前，按 UTC 起点）
        self.assertEqual(body["after"]["located_count"], 2)
        self.assertEqual(body["after"]["unlocatable_count"], 0)
        self.assertEqual([p["capture_id"] for p in body["segment_projections"]],
                         ["C1", "C2"])
        # 详情指纹与响应 after 指纹一致
        self.assertEqual(self._current_fp(), body["after"]["fingerprint"])

    def test_restore_after_fingerprint_equals_frozen_cycle_fingerprint(self):
        self._seed_v1()
        self._tamper()
        status, body = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, v1 = self.req("GET", "/versions/1")
        va = [{"local_ms": a["local_ms"], "utc_ms": a["utc_ms"]}
              for a in v1["anchors"] if a.get("cycle_id", "default") == "default"]
        vs = [{"capture_id": s["capture_id"],
               "local_start_ms": s["local_start_ms"],
               "local_end_ms": s["local_end_ms"], "note": s["note"]}
              for s in v1["segments"]
              if s.get("cycle_id", "default") == "default"]
        expect = cycle_fingerprint("D1", "default", va, vs)
        self.assertEqual(body["after"]["fingerprint"], expect)

    def test_restore_timeline_reflects_result(self):
        self._seed_v1()
        self._tamper()
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, tl = self.req("GET", "/timeline/current")
        self.assertEqual(st, 200)
        ids = {s["capture_id"] for s in tl["segments"]}
        self.assertIn("C2", ids)
        self.assertNotIn("C9", ids)
        # 列表/详情计数同步
        st, body = self.req("GET", "/segments?device_id=D1&cycle_id=default")
        self.assertEqual(body["count"], 2)
        st, det = self.req("GET", "/devices/D1/cycles/default")
        self.assertEqual(det["cycle"]["anchor_count"], 2)
        self.assertEqual(det["cycle"]["segment_count"], 2)

    def test_restore_replay_idempotent_afterwards(self):
        self._seed_v1()
        self._tamper()
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        # 恢复后同内容重传 -> 幂等；按误改内容重传 -> 冲突
        st, body = self.req("POST", "/segments",
                            {"capture_id": "C1", "device_id": "D1",
                             "local_start_ms": 5000, "local_end_ms": 20000,
                             "note": "鸟鸣"})
        self.assertEqual(st, 200)
        self.assertTrue(body["idempotent_replay"])
        st, body = self.req("POST", "/segments",
                            {"capture_id": "C1", "device_id": "D1",
                             "local_start_ms": 5000, "local_end_ms": 20001})
        self.assertEqual(st, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")

    def test_restore_created_at_semantics(self):
        self._seed_v1()
        # C2 被误删 -> 恢复时补录，沿用冻结 created_at
        st, v1 = self.req("GET", "/versions/1")
        c2_frozen = next(s for s in v1["segments"] if s["capture_id"] == "C2")
        self.req("DELETE", "/segments/C2")
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, seg = self.req("GET", "/segments/C2")
        self.assertEqual(seg["segment"]["created_at"], c2_frozen["created_at"])
        # C1 更新：现有 created_at 保持（发布前的录入时间）
        st, c1 = self.req("GET", "/segments/C1")
        st, v1c1 = (200, next(s for s in v1["segments"] if s["capture_id"] == "C1"))
        self.assertEqual(c1["segment"]["created_at"], v1c1["created_at"])

    # ------------------------------------------------------------ 原子性/隔离

    def test_restore_only_target_cycle(self):
        self._seed_v1(boot_cycle="boot-2")
        self._tamper()
        # 只恢复 default：boot-2 的锚点/声段原样
        status, body = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, c3 = self.req("GET", "/segments/C3")
        self.assertEqual(st, 200)
        self.assertEqual(c3["segment"]["cycle_id"], "boot-2")
        st, anc = self.req("GET", "/devices/D1/anchors?cycle_id=boot-2")
        self.assertEqual(
            [(a["local_ms"], a["utc_ms"]) for a in anc["anchors"]],
            [(1000, 1700010000000), (61000, 1700010060500)])
        st, det = self.req("GET", "/devices/D1/cycles/boot-2")
        self.assertEqual(det["cycle"]["anchor_count"], 2)
        self.assertEqual(det["cycle"]["segment_count"], 1)
        # 其他设备不受影响（D2 的锚点/声段）
        self.req("POST", "/devices", {"device_id": "D2"})
        self.req("POST", "/devices/D2/anchors",
                 {"local_ms": 0, "utc_ms": 1699999997000})
        self.req("POST", "/segments",
                 {"capture_id": "D2S", "device_id": "D2",
                  "local_start_ms": 1, "local_end_ms": 2})
        # 再恢复一次 default 不应触碰 D2
        self._tamper()
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, seg = self.req("GET", "/segments/D2S")
        self.assertEqual(st, 200)

    def test_restore_published_versions_unchanged(self):
        self._seed_v1()
        self._tamper()
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        # v1 快照逐字段不变
        st, v1a = self.req("GET", "/versions/1")
        self.assertEqual(st, 200)
        self.assertEqual(
            [(a["local_ms"], a["utc_ms"]) for a in v1a["anchors"]],
            [(1000, 1700000000000), (61000, 1700000060500)])
        ids = sorted(s["capture_id"] for s in v1a["segments"])
        self.assertEqual(ids, ["C1", "C2"])
        # 恢复本身不新建版本
        st, versions = self.req("GET", "/versions")
        self.assertEqual([v["version_no"] for v in versions["versions"]], [1])

    def test_restore_does_not_touch_bundle_receipts(self):
        self._seed_v1()
        bundle = {"bundle_id": "BUN-1",
                  "segments": [{"capture_id": "BZ1", "device_id": "D1",
                                "local_start_ms": 5000, "local_end_ms": 6000,
                                "note": "bundle"}]}
        st, first = self.req("POST", "/bundles", bundle)
        self.assertEqual(st, 201)
        self._tamper()
        status, _ = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        # bundle 回执不变：同编号同内容重传仍返回首次结果
        st, again = self.req("POST", "/bundles", bundle)
        self.assertEqual(st, 200)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["segments"], first["segments"])

    def test_restore_rejected_leaves_no_partial_write(self):
        self._seed_v1()
        self._tamper()
        # 占用触发整体拒绝：把版本声段 C1 先移到 boot-2，再尝试恢复 default
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-9"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-9", "local_ms": 1000, "utc_ms": 1800000000000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-9", "local_ms": 61000, "utc_ms": 1800000060000})
        # C1 当前已是误改内容（仍在 default）；直接在 DB 层把 C1 挪到 boot-9
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE segments SET cycle_id='boot-9' WHERE capture_id='C1'")
        conn.commit()
        conn.close()
        fp = self._current_fp()  # C1 移出后 default 内容已变，须取最新指纹
        status, body = self._restore("D1", "default", 1, fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_capture_occupied")
        occ = {o["capture_id"]: o for o in body["error"]["details"]["occupied"]}
        self.assertEqual(occ["C1"]["cycle_id"], "boot-9")
        # 无部分写入：锚点仍是误改后的 +250ms、C2 仍缺失、C9 仍存在、C1 仍在 boot-9
        st, anc = self.req("GET", "/devices/D1/anchors?cycle_id=default")
        self.assertEqual(
            [a["utc_ms"] for a in anc["anchors"]],
            [1700000000250, 1700000060750])
        self.assertEqual(self.req("GET", "/segments/C2")[0], 404)
        st, c9 = self.req("GET", "/segments/C9")
        self.assertEqual(st, 200)
        st, c1 = self.req("GET", "/segments/C1")
        self.assertEqual(c1["segment"]["cycle_id"], "boot-9")

    # ------------------------------------------------------------ 拒绝场景

    def test_restore_fingerprint_stale(self):
        self._seed_v1()
        self._tamper()
        stale = "0" * 64
        status, body = self._restore("D1", "default", 1, stale)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_fingerprint_stale")
        self.assertEqual(body["error"]["details"]["current_fingerprint"],
                         self._current_fp())
        # 数据维持误改状态
        st, anc = self.req("GET", "/devices/D1/anchors?cycle_id=default")
        self.assertEqual(anc["anchors"][0]["utc_ms"], 1700000000250)

    def test_restore_version_not_found(self):
        self._seed_v1()
        status, body = self._restore("D1", "default", 99, self._current_fp())
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "version_not_found")

    def test_restore_device_not_found(self):
        self._seed_v1()
        status, body = self._restore("NOPE", "default", 1, "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    def test_restore_cycle_not_found(self):
        self._seed_v1()
        status, body = self._restore("D1", "no-cycle", 1, "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")

    def test_restore_sealed_cycle_rejected(self):
        self._seed_v1()
        fp = self._current_fp()
        status, _ = self.req("POST", "/devices/D1/cycles/default/seal",
                             {"fingerprint": fp})
        self.assertEqual(status, 201)
        status, body = self._restore("D1", "default", 1, fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 封存内容未被恢复操作触碰
        self.assertEqual(self._current_fp(), fp)

    def test_restore_version_cycle_empty_rejected(self):
        self._seed_v1()
        # boot-2 是 v1 发布之后才登记的周期：v1 中既无其锚点也无声段
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-later"})
        fp = self._current_fp("D1", "boot-later")
        status, body = self._restore("D1", "boot-later", 1, fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_version_cycle_empty")
        # 关键语义：不能按空周期清除——给该周期写入锚点+声段后仍拒绝且数据保留
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-later", "local_ms": 0, "utc_ms": 1})
        self.req("POST", "/segments",
                 {"capture_id": "LATER1", "device_id": "D1",
                  "cycle_id": "boot-later", "local_start_ms": 0, "local_end_ms": 1})
        fp2 = self._current_fp("D1", "boot-later")
        status, body = self._restore("D1", "boot-later", 1, fp2)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_version_cycle_empty")
        st, seg = self.req("GET", "/segments/LATER1")
        self.assertEqual(st, 200)
        st, anc = self.req("GET", "/devices/D1/anchors?cycle_id=boot-later")
        self.assertEqual(len(anc["anchors"]), 1)

    def test_restore_occupied_by_other_cycle(self):
        self._seed_v1()
        # 把 C1 移到 boot-2（同设备其他周期），default 当前指纹随之变化
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 1000,
                  "utc_ms": 1700010000000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 61000,
                  "utc_ms": 1700010060500})
        st, body = self.req("POST", "/segments/reassign-cycle",
                            {"items": [{"capture_id": "C1",
                                        "from_cycle_id": "default",
                                        "to_cycle_id": "boot-2"}]})
        self.assertEqual(st, 200)
        status, body = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_capture_occupied")
        self.assertEqual(body["error"]["details"]["occupied"],
                         [{"capture_id": "C1", "device_id": "D1",
                           "cycle_id": "boot-2"}])

    def test_restore_occupied_by_other_device(self):
        self._seed_v1()
        # 发布 v2 含 D3/CX；之后 CX 被 D2 占用；恢复 D3/default -> 拒绝
        self.req("POST", "/devices", {"device_id": "D3"})
        self.req("POST", "/devices/D3/anchors",
                 {"local_ms": 0, "utc_ms": 100000})
        self.req("POST", "/devices/D3/anchors",
                 {"local_ms": 10000, "utc_ms": 110000})
        self.req("POST", "/segments",
                 {"capture_id": "CX", "device_id": "D3",
                  "local_start_ms": 1000, "local_end_ms": 2000})
        self.assertEqual(self.req("POST", "/versions")[0], 201)
        self.assertEqual(self.req("DELETE", "/segments/CX")[0], 200)
        self.req("POST", "/devices", {"device_id": "D2"})
        self.req("POST", "/devices/D2/anchors",
                 {"local_ms": 0, "utc_ms": 200000})
        self.req("POST", "/devices/D2/anchors",
                 {"local_ms": 10, "utc_ms": 200010})
        st, body = self.req("POST", "/segments",
                            {"capture_id": "CX", "device_id": "D2",
                             "local_start_ms": 1, "local_end_ms": 2})
        self.assertEqual(st, 201)
        status, body = self._restore("D3", "default", 2, self._current_fp("D3"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_capture_occupied")
        self.assertEqual(body["error"]["details"]["occupied"][0]["device_id"], "D2")
        # 未把 CX 抢到 D3
        st, seg = self.req("GET", "/segments/CX")
        self.assertEqual(seg["segment"]["device_id"], "D2")

    def test_restore_other_device_capture_not_conflicting(self):
        """版本目标周期声段与其他设备声段编号互不相同 -> 不构成占用。"""
        self._seed_v1()
        self.req("POST", "/devices", {"device_id": "D2"})
        self.req("POST", "/devices/D2/anchors", {"local_ms": 0, "utc_ms": 1})
        self.req("POST", "/segments",
                 {"capture_id": "OTHER", "device_id": "D2",
                  "local_start_ms": 0, "local_end_ms": 1})
        self._tamper()
        status, body = self._restore("D1", "default", 1, self._current_fp())
        self.assertEqual(status, 200)
        st, seg = self.req("GET", "/segments/OTHER")
        self.assertEqual(st, 200)

    # ------------------------------------------------------------ 输入校验

    def test_restore_invalid_version_no(self):
        self._seed_v1()
        for bad in (0, -1, "1", 1.5, True, None):
            status, body = self._restore_resp(bad, "0" * 64)
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error", bad)

    def _restore_resp(self, version_no, fingerprint):
        return self.req("POST", "/devices/D1/cycles/default/restore",
                        {"version_no": version_no, "fingerprint": fingerprint})

    def test_restore_invalid_fingerprint(self):
        self._seed_v1()
        for bad in ("abc", "0" * 63, "A" * 64, 123, None):
            status, body = self._restore_resp(1, bad)
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error", bad)

    def test_restore_missing_fields(self):
        self._seed_v1()
        status, body = self.req("POST", "/devices/D1/cycles/default/restore",
                                {"version_no": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        status, body = self.req("POST", "/devices/D1/cycles/default/restore",
                                {"fingerprint": "0" * 64})
        self.assertEqual(status, 400)
        status, body = self.req("POST", "/devices/D1/cycles/default/restore",
                                None)
        self.assertEqual(status, 400)

    def test_restore_non_default_cycle(self):
        """恢复非 default 周期：冻结内容按该周期取，定位只用该周期锚点。"""
        self.req("POST", "/devices", {"device_id": "D9"})
        self.req("POST", "/devices/D9/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D9/anchors",
                 {"cycle_id": "boot-2", "local_ms": 0, "utc_ms": 500000})
        self.req("POST", "/devices/D9/anchors",
                 {"cycle_id": "boot-2", "local_ms": 10000, "utc_ms": 510000})
        self.req("POST", "/segments",
                 {"capture_id": "K1", "device_id": "D9", "cycle_id": "boot-2",
                  "local_start_ms": 1000, "local_end_ms": 2000})
        self.assertEqual(self.req("POST", "/versions")[0], 201)
        # 误改锚点
        self.req("PUT", "/devices/D9/anchors",
                 {"cycle_id": "boot-2", "anchors": [
                     {"local_ms": 0, "utc_ms": 600000},
                     {"local_ms": 10000, "utc_ms": 610000}]})
        fp = self._current_fp("D9", "boot-2")
        status, body = self._restore("D9", "boot-2", 1, fp)
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "boot-2")
        k1 = body["segment_projections"][0]
        self.assertEqual(k1["capture_id"], "K1")
        self.assertEqual((k1["utc_start_ms"], k1["utc_end_ms"]),
                         (501000, 502000))

    # ------------------------------------------------------------ 特殊内容

    def test_restore_anchor_only_version(self):
        self.req("POST", "/devices", {"device_id": "A"})
        self.req("POST", "/devices/A/anchors", {"local_ms": 0, "utc_ms": 0})
        self.req("POST", "/devices/A/anchors",
                 {"local_ms": 100000, "utc_ms": 100000})
        self.assertEqual(self.req("POST", "/versions")[0], 201)
        self.req("DELETE", "/devices/A/anchors/100000?cycle_id=default")
        status, body = self._restore("A", "default", 1, self._current_fp("A"))
        self.assertEqual(status, 200)
        self.assertEqual(body["after"]["anchor_count"], 2)
        self.assertEqual(body["after"]["segment_count"], 0)
        self.assertEqual(body["restored"]["segment_count"], 0)

    def test_restore_segment_only_cycle(self):
        """版本中该周期只有声段（无锚点，冻结为 unlocatable）也可恢复。"""
        self.req("POST", "/devices", {"device_id": "S"})
        self.req("POST", "/segments",
                 {"capture_id": "U1", "device_id": "S",
                  "local_start_ms": 5, "local_end_ms": 9})
        self.assertEqual(self.req("POST", "/versions")[0], 201)
        self.assertEqual(self.req("DELETE", "/segments/U1")[0], 200)
        status, body = self._restore("S", "default", 1, self._current_fp("S"))
        self.assertEqual(status, 200)
        self.assertEqual(body["after"]["anchor_count"], 0)
        self.assertEqual(body["after"]["unlocatable_count"], 1)
        self.assertEqual(body["segment_projections"][0]["status"], "unlocatable")

    # ------------------------------------------------------------ 旧版快照

    def test_restore_legacy_rev1_default(self):
        """旧版 format_rev=1 冻结记录 cycle_id 为 NULL，按 default 恢复。"""
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
INSERT INTO devices VALUES ('L1','legacy','2026-09-01T00:00:00.000Z');
INSERT INTO cycles VALUES ('L1','default',NULL,NULL,NULL,'2026-09-01T00:00:00.000Z');
INSERT INTO versions VALUES (1,'旧版','2026-09-02T00:00:00.000Z',1);
INSERT INTO version_anchors VALUES (1,'L1',NULL,1000,100000);
INSERT INTO version_anchors VALUES (1,'L1',NULL,2000,101000);
INSERT INTO version_segments VALUES (1,'OLD1','L1',NULL,1100,1200,'旧备注',
       'located',100100,100200,'2026-09-01T01:00:00.000Z');
""")
        conn.commit()
        conn.close()
        # 当前 default 被误改：错误锚点 + 新声段 NEW9
        self.req("POST", "/devices/L1/anchors",
                 {"local_ms": 1000, "utc_ms": 999999})
        self.req("POST", "/devices/L1/anchors",
                 {"local_ms": 2000, "utc_ms": 1000001})
        self.req("POST", "/segments",
                 {"capture_id": "NEW9", "device_id": "L1",
                  "local_start_ms": 1, "local_end_ms": 2})
        fp = self._current_fp("L1")
        status, body = self._restore("L1", "default", 1, fp)
        self.assertEqual(status, 200)
        self.assertEqual(body["format_rev"], 1)
        self.assertEqual(
            [(a["local_ms"], a["utc_ms"]) for a in body["anchors"]],
            [(1000, 100000), (2000, 101000)])
        old = body["segment_projections"][0]
        self.assertEqual((old["capture_id"], old["note"],
                          old["utc_start_ms"], old["utc_end_ms"]),
                         ("OLD1", "旧备注", 100100, 100200))
        self.assertEqual(body["restored"]["removed_capture_ids"], ["NEW9"])
        self.assertEqual(body["restored"]["inserted_capture_ids"], ["OLD1"])
        st, seg = self.req("GET", "/segments/OLD1")
        self.assertEqual(seg["segment"]["created_at"],
                         "2026-09-01T01:00:00.000Z")
        # 旧版恢复到非 default 周期 -> 空周期拒绝
        self.req("POST", "/devices/L1/cycles", {"cycle_id": "boot-2"})
        status, body = self._restore(
            "L1", "boot-2", 1, self._current_fp("L1", "boot-2"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_version_cycle_empty")


if __name__ == "__main__":
    unittest.main()
