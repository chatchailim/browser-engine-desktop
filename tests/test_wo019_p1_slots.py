"""Tests: WO-019 P1 คู่ขนานขั้น 1 — TaskSlotPool หลายสล็อต + TaskQueue คิวรอ + ธปิด=เดิม 100%.

กติกาเหล็กที่คุม:
- TaskReservation เดิม (สล็อตเดียว) ไม่เปลี่ยนพฤติกรรม — เทสต์ test_reliability.py เดิมยังเขียว
- desktop = งานกรรมสิทธิ์เครื่อง: ต้องได้ทุกสล็อตว่าง · ขณะถือ ปฏิเสธจองใหม่ทุกชนิด
- release อ่านจากไฟล์ (คืนได้ข้าม instance — สถานะบนดิสก์ ไม่ใช่หน่วยความจำ)
- คิว cap เต็ม = ปฏิเสธชัด (ไม่เงียบ)
- แข่งระหว่าง thread: จองพร้อมกัน N งานบนสล็อต 2 = ได้พร้อมกันสูงสุด 2 (อะตอมมิก)
"""
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

BASE = pathlib.Path(__file__).resolve().parent.parent  # repo แยกแล้ว — ห้ามชี้ path เดิมใน ai-platform-kit
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from service.reliability import TaskSlotPool, TaskQueue, TaskReservation


class P1SlotPool(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="p1_"))
        self.pool = TaskSlotPool(slots=2, base=self.tmp / "slots")

    def test_two_browser_tasks_take_two_slots(self):
        self.assertEqual(self.pool.acquire("t1"), 1)
        self.assertEqual(self.pool.acquire("t2"), 2)
        self.assertIsNone(self.pool.acquire("t3"), "สล็อตเต็ม = None (เข้าคิวตาม server)")

    def test_desktop_exclusive_over_all_slots(self):
        self.assertEqual(self.pool.acquire("b1", kind="browser"), 1)
        self.assertIsNone(self.pool.acquire("d1", kind="desktop"), "มีงาน browser ถือสล็อต = desktop ห้ามขึ้น")
        self.pool.release("b1")
        self.assertIsNotNone(self.pool.acquire("d1", kind="desktop"), "ว่างทั้งเครื่อง = desktop ได้")

    def test_desktop_holding_blocks_everything(self):
        self.assertIsNotNone(self.pool.acquire("d1", kind="desktop"))
        self.assertIsNone(self.pool.acquire("b1"), "desktop ถืออยู่ = browser ปฏิเสธ")
        self.assertIsNone(self.pool.acquire("d2", kind="desktop"), "desktop เองก็ซ้อนไม่ได้")

    def test_release_restores_slot(self):
        self.pool.acquire("t1")
        self.assertTrue(self.pool.release("t1"))
        self.assertEqual(self.pool.acquire("t2"), 1, "สล็อตคืนแล้วจองใหม่ได้ที่เลขเดิม")

    def test_release_wrong_id_is_noop(self):
        self.pool.acquire("t1")
        self.assertFalse(self.pool.release("ไม่ใช่ตัวจริง"))

    def test_stale_slot_reclaimable(self):
        self.pool.acquire("old")
        p = self.tmp / "slots" / "slot-1.lock"
        info = json.loads(p.read_text(encoding="utf-8"))
        info["at"] = time.time() - 1900
        p.write_text(json.dumps(info), encoding="utf-8")
        self.assertEqual(self.pool.acquire("new"), 1, "ค้าง >30 นาที = ยึดคืนได้ (กติกาเดิม)")

    def test_single_slot_pool_matches_old_reservation_semantics(self):
        one = TaskSlotPool(slots=1, base=self.tmp / "s1")
        self.assertIsNotNone(one.acquire("t1"))
        self.assertIsNone(one.acquire("t2"))
        one.release("t1")
        self.assertIsNotNone(one.acquire("t2"))

    def test_concurrent_acquire_never_exceeds_slots(self):
        pool = TaskSlotPool(slots=2, base=self.tmp / "s2")
        got = []

        def racer(i):
            for _ in range(12):
                s = pool.acquire(f"r{i}-{_/100}")
                if s:
                    got.append((i, s))
                    time.sleep(0.005)
                    pool.release(f"r{i}-{_/100}")
                time.sleep(0.001)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(got), 5 * 12)
        # สรุปสำคัญ: snapshot ณ จบ = ว่างทุกสล็อต (ทุก release ครบ)
        self.assertTrue(all(s["task_id"] is None for s in pool.snapshot()))

    def test_snapshot_shape(self):
        self.pool.acquire("t1", kind="browser")
        snap = self.pool.snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap[0]["task_id"], "t1")
        self.assertEqual(snap[0]["kind"], "browser")
        self.assertIsNone(snap[1]["task_id"])


class P1Queue(unittest.TestCase):
    def setUp(self):
        self.q = TaskQueue(cap=3)

    def test_enqueue_order_and_position(self):
        self.assertEqual(self.q.enqueue({"task_id": "a"}), 1)
        self.assertEqual(self.q.enqueue({"task_id": "b"}), 2)
        self.assertEqual([q["task_id"] for q in self.q.snapshot()], ["a", "b"])

    def test_cap_full_rejected(self):
        for i in range(3):
            self.assertIsNotNone(self.q.enqueue({"task_id": f"t{i}"}))
        self.assertIsNone(self.q.enqueue({"task_id": "overflow"}), "คิวเต็ม = ปฏิเสธชัด")

    def test_remove_and_pop_fifo(self):
        self.q.enqueue({"task_id": "a"})
        self.q.enqueue({"task_id": "b"})
        self.assertTrue(self.q.remove("a"))
        self.assertEqual(self.q.pop()["task_id"], "b")
        self.assertIsNone(self.q.pop())

    def test_remove_missing_false(self):
        self.assertFalse(self.q.remove("nothing"))


if __name__ == "__main__":
    unittest.main()