"""Shell Sandbox (WO-018 step-2): สแนปช็อต+คลังสำรอง+กู้คืนไฟล์รอบคำสั่ง — sandbox/rollback ระดับไฟล์จริง.

ขอบเขต (ตรงไปตรงมา):
- ครอบ "ไฟล์ภายใน sandbox dir" — สร้าง/แก้/ลบในโฟลเดอร์นี้กู้คืนได้
- ไม่ครอบ: โปรแกรมติดตั้งระดับระบบ (winget/registry) — คำสั่งติดตั้งบันทึก requires_manual_uninstall ใน manifest
"""
import hashlib
import os
import pathlib
import shutil
import time

MAX_FILES = 2000


class ShellSandbox:
    def __init__(self, dir_path):
        self.dir = pathlib.Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, max_files=MAX_FILES):
        out = {}
        count = 0
        for base, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if d not in ("stash",)]
            for f in files:
                if count >= max_files:
                    raise RuntimeError("sandbox_files_overflow")
                p = pathlib.Path(base) / f
                rel = str(p.relative_to(self.dir)).replace("\\", "/")
                h = hashlib.sha256()
                size = p.stat().st_size
                try:
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    out[rel] = {"sha256": h.hexdigest(), "bytes": size}
                except OSError:
                    out[rel] = {"sha256": "UNREADABLE", "bytes": size}
                count += 1
        return out

    def stash(self, snapshot, max_mb=200):
        """สำรองไฟล์ทั้งสแนปช็อตลง stash (ต่อรอบ) — ใช้กู้คืนภายหลัง. คืน (stash_dir, total_mb) หรือ (None,0)."""
        total = sum(v["bytes"] for v in snapshot.values())
        if total > max_mb * 1000 * 1000:
            return None, round(total / 1e6, 2)
        stash = self.dir / "stash" / time.strftime("%Y%m%dT%H%M%S")
        stash.mkdir(parents=True, exist_ok=True)
        for rel, meta in snapshot.items():
            dst = stash / rel.replace("/", "__")
            src = self.dir / rel
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
        return stash, round(total / 1e6, 2)

    @staticmethod
    def diff(before, after):
        created = sorted(set(after) - set(before))
        deleted = sorted(set(before) - set(after))
        modified = sorted(k for k in (set(before) & set(after)) if before[k]["sha256"] != after[k]["sha256"])
        return {"created": created, "modified": modified, "deleted": deleted}

    def restore(self, stash_dir, before, after_diff):
        """กู้คืนจาก stash → ไฟล์แก้/ลบกลับเนื้อหาเดิม + ไฟล์ที่สร้างใหม่ถูกลบ. คืนสถิติ."""
        restored, removed = 0, 0
        for rel, meta in before.items():
            src = pathlib.Path(stash_dir) / rel.replace("/", "__")
            dst = self.dir / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
        for rel in after_diff["created"]:
            p = self.dir / rel
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        return {"restored": restored, "removed_created": removed}