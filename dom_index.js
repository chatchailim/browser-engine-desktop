// dom_index.js — คืนรายการ element ที่โต้ตอบได้ + ติดหมายเลข data-bx-id (เฉพาะที่มองเห็น)
(maxN) => {
  document.querySelectorAll('[data-bx-id]').forEach(e => e.removeAttribute('data-bx-id'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=option],[role=combobox],[role=searchbox],[role=textbox],[contenteditable=true],[onclick]';
  const vh = window.innerHeight, vw = window.innerWidth;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (el.disabled) continue;
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'combobox' :
      (tag === 'input' || tag === 'textarea') ? ((el.type === 'search') ? 'searchbox' : (['checkbox','radio','submit','button'].includes(el.type) ? el.type : 'textbox')) : tag);
    const name = clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.value ||
      el.getAttribute('placeholder') || el.getAttribute('alt') || el.getAttribute('name'));
    let nm = name;
    if (!nm) {   // ปุ่มไอคอน (เช่น แว่นขยาย) ไม่มีข้อความ → ตั้งชื่อจาก class/id/type
      const hint = ((el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) + ' ' + (el.id || '')).toLowerCase();
      if (/search|ค้นหา|magnif/.test(hint)) nm = 'ค้นหา (ไอคอน)';
      else if (el.type === 'submit') nm = 'submit';
      else if (/close|dismiss/.test(hint)) nm = 'ปิด (ไอคอน)';
    }
    if (!nm && !['textbox','searchbox','combobox'].includes(role)) continue;
    n += 1; el.setAttribute('data-bx-id', String(n));
    let val, checked;
    if (tag === 'select') val = clean(el.options[el.selectedIndex]?.text);
    else if ((tag === 'input' || tag === 'textarea') && !['checkbox','radio','submit','button'].includes(el.type)) val = clean(el.value);
    if (el.type === 'checkbox' || el.type === 'radio') checked = el.checked;
    const lab = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
    const label = lab ? clean(lab.innerText) : '';
    out.push({id: n, role, name: (label && label !== nm && ['textbox','searchbox','combobox','checkbox','radio'].includes(role)) ? clean(label + ' | ' + nm) : nm,
              inView, ph: clean(el.getAttribute('placeholder')), val, checked,
              opts: tag === 'select' ? [...el.options].slice(0, 25).map(o => clean(o.text)) : undefined,
              nopts: tag === 'select' ? el.options.length : undefined});
  }
  // เรียง: ในจอก่อน, ตัดให้เหลือ maxN
  out.sort((a, b) => (b.inView - a.inView) || (a.id - b.id));
  return out.slice(0, maxN);
}
