"""ชื่อเดิมของ service.chain_sign — คงไว้ให้โค้ดและสคริปต์เก่า import ได้ช่วงเปลี่ยนผ่าน (T1 2026-11-23).

`import service.keysign` ได้โมดูล service.chain_sign ตัวเดียวกันทุกประการ (ไม่ใช่สำเนา)
ใช้ service.chain_sign แทน · คำว่า KeySign สงวนไว้เรียกอุปกรณ์ SPADA KeySign (service.spada_keysign)
"""
import sys

from service import chain_sign as _chain_sign

sys.modules[__name__] = _chain_sign
