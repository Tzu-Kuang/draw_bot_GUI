"""一番くじ ONLINE SelectBox 頁面辨識：可點的 a.openModalButton 與售罄卡片。

只讀取頁面 DOM，不執行 JS 點擊；所有動作經由 Selenium 原生 click 完成。
Box 排序包含販売終了卡片，避免以可點按鈕次序取代視覺上第 N 盒。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from monitor_core import BoxInfo

BOX_RE = re.compile(r'^BOX-[A-Z0-9]+$', flags=re.IGNORECASE)
ACTION_TEXT = 'このくじ箱を見る'
ACTION_CSS = 'a[id*="rBoxList_"][id$="_lbQueue"], a.openModalButton'

# 由可見 BOX-* 文字節點向上找只包含 1 個 BOX 的最小商品卡片；
# 同時辨識無按鈕的「販売終了」卡，確保第 N 盒是頁面視覺順位。
# 不依賴按鈕的顏色：黃色、紫色、橘色都應按照動作文字判斷。
READ_BOXES_JS = r"""
const ACTION_CSS = 'a[id*="rBoxList_"][id$="_lbQueue"], a.openModalButton';
const boxPattern = /BOX-[A-Z0-9]+/gi;
function visible(el) {
  if (!el || !el.isConnected || !el.getClientRects().length) return false;
  let node = el;
  while (node && node.nodeType === 1) {
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    node = node.parentElement;
  }
  return true;
}
function findCard(labelNode, identity) {
  let parent = labelNode.parentElement;
  for (let depth = 0; parent && depth < 14; depth++, parent = parent.parentElement) {
    if (!visible(parent)) return null;
    const text = (parent.innerText || '').trim();
    const names = [...new Set((text.match(boxPattern) || []).map(x => x.toUpperCase()))];
    if (names.length !== 1 || names[0] !== identity) continue;
    const actions = [...parent.querySelectorAll(ACTION_CSS)].filter(visible);
    // One card should contain one corresponding action OR one sold-out indicator.
    if (actions.length === 1 || (actions.length === 0 && text.includes('販売終了'))) {
      return {card: parent, action: actions[0] || null, preview: text.slice(0, 240)};
    }
  }
  return null;
}
const base = document.querySelector('form') || document.body;
if (!base) return [];
const walker = document.createTreeWalker(base, NodeFilter.SHOW_TEXT);
const seen = new Set();
const results = [];
while (walker.nextNode()) {
  const node = walker.currentNode;
  const text = (node.nodeValue || '').trim();
  // Prevent matching incidental references inside long scripts/attributes.
  if (!text || text.length > 100) continue;
  const matches = text.match(boxPattern) || [];
  if (matches.length !== 1) continue;
  const id = matches[0].toUpperCase();
  // Exact label or short label with adjacent information; avoid full page text.
  if (text.length > 45 && text !== id) continue;
  if (!node.parentElement || !visible(node.parentElement)) continue;
  const found = findCard(node, id);
  if (found && !seen.has(found.card)) {
    results.push({id, button: found.action, preview: found.preview});
    seen.add(found.card);
  }
}
return results;
"""


def normalized(text: str) -> str:
    return ''.join((text or '').split())


def valid_link_id(button_id: str, classes: str) -> bool:
    """只允許截圖確認的選盒連結，不對任意 role=link 操作。"""
    return (bool(re.search(r'rBoxList_ctl\d+_lbQueue$', button_id or '', re.IGNORECASE))
            or 'openModalButton' in (classes or '').split())


def actionable_kuji_button(button: Any) -> bool:
    """紫色排隊、橘色、黃色都能點；販売終了／disabled 不點。"""
    if button is None:
        return False
    try:
        if (button.tag_name or '').lower() != 'a':
            return False
        if not valid_link_id(button.get_attribute('id') or '', button.get_attribute('class') or ''):
            return False
        if normalized(button.text or button.get_attribute('aria-label') or '') != ACTION_TEXT:
            return False
        if not button.is_displayed() or not button.is_enabled():
            return False
        if (button.get_attribute('aria-disabled') or '').lower() == 'true':
            return False
        if button.get_attribute('disabled') is not None:
            return False
        classes = (button.get_attribute('class') or '').lower().split()
        if 'disabled' in classes or 'is-disabled' in classes:
            return False
        if (button.value_of_css_property('pointer-events') or '').lower() == 'none':
            return False
        return True
    except Exception:
        return False


@dataclass
class KujiRow:
    info: BoxInfo
    button: Any


def read_kuji_rows(driver: Any) -> list[KujiRow]:
    """回傳 DOM 順序的盒子資訊及其相應連結，不會點擊。"""
    raw_rows = driver.execute_script(READ_BOXES_JS)
    if not isinstance(raw_rows, list):
        return []
    result: list[KujiRow] = []
    for record in raw_rows[:300]:
        if not isinstance(record, dict):
            continue
        box_id = str(record.get('id', '')).upper()
        if not BOX_RE.fullmatch(box_id):
            continue
        button = record.get('button')
        is_ready = actionable_kuji_button(button)
        preview = ' '.join(str(record.get('preview') or '').split())[:120]
        status = '可點：このくじ箱を見る' if is_ready else ('已售完' if '販売終了' in preview else '不可點')
        result.append(KujiRow(BoxInfo(len(result) + 1, box_id, preview, status, '官方 BOX 編號'),
                              button if is_ready else None))
    return result
