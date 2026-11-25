"""瀏覽器無關的新盒與狀態變化追蹤；只處理可辨識的盒子 ID。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Sequence

MODE_NEW_ID = 'new_id'                # 僅指定位置出現新 ID
MODE_AVAILABLE = 'available_again'    # 指定位置新 ID 或原盒不可點 -> 可點
MODE_ANY_NEW = 'any_new_id'           # 不限位置的新 ID
MODES = {MODE_NEW_ID, MODE_AVAILABLE, MODE_ANY_NEW}
MODE_LABELS = {
    MODE_NEW_ID: '新 ID（指定順位）',
    MODE_AVAILABLE: '新 ID ＋ 恢復可點（指定順位）',
    MODE_ANY_NEW: '任意順位新 ID',
}

@dataclass(frozen=True)
class BoxInfo:
    index: int
    identity: str
    preview: str
    button_status: str
    identity_source: str = ''

    @property
    def actionable(self) -> bool:
        # 「不可點」也包含「可點」兩字，必須從開頭嚴格判斷。
        return self.button_status.startswith('可點')

@dataclass
class ScanDecision:
    baseline_created: bool = False
    new_identities: list[str] = field(default_factory=list)
    became_actionable: list[str] = field(default_factory=list)
    selected_identity: str | None = None
    target_index: int | None = None
    trigger: str = ''
    should_click: bool = False
    duplicate_selected_id: bool = False
    retry_number: int = 0

class NewBoxTracker:
    """分帳號保存本次監控所有已看過的 ID、上次可用狀態與待點擊清單。"""
    def __init__(self, max_retries: int = 4):
        self.baselined = False
        self.seen: set[str] = set()
        self.last_actionable: dict[str, bool] = {}
        self.pending: dict[str, int] = {}
        self.pending_reason: dict[str, str] = {}
        self.completed: set[str] = set()
        self.max_retries = max_retries

    def observe(self, boxes: Sequence[BoxInfo], selected_index: int,
                mode: str = MODE_NEW_ID) -> ScanDecision:
        if mode not in MODES:
            raise ValueError('未知監控模式')
        result = ScanDecision()
        if not boxes:
            return result  # 網站暫時空白不清空基準與待辦

        if not self.baselined:
            for box in boxes:
                if box.identity:
                    self.seen.add(box.identity)
                    self.last_actionable[box.identity] = box.actionable
            self.baselined = True
            result.baseline_created = True
            return result

        before = set(self.seen)
        current_ids = [b.identity for b in boxes if b.identity]
        new_ids = set(current_ids) - before
        result.new_identities = list(dict.fromkeys(b.identity for b in boxes if b.identity in new_ids))
        result.became_actionable = list(dict.fromkeys(
            b.identity for b in boxes if b.identity in before
            and self.last_actionable.get(b.identity) is False and b.actionable
        ))
        for box in boxes:
            if box.identity:
                self.seen.add(box.identity)
                self.last_actionable[box.identity] = box.actionable

        if mode == MODE_ANY_NEW:
            # 不管出現在第 N 盒，先追蹤所有新 ID；不可點的盒子等之後變可點再處理。
            for key in result.new_identities:
                if key not in self.completed:
                    self.pending.setdefault(key, 0)
                    self.pending_reason.setdefault(key, 'new_id')
            for index, box in enumerate(boxes, 1):
                if box.identity not in self.pending or not box.actionable:
                    continue
                if sum(b.identity == box.identity for b in boxes) != 1:
                    result.duplicate_selected_id = True
                    continue
                result.selected_identity = box.identity
                result.target_index = index
                result.trigger = self.pending_reason.get(box.identity, 'new_id')
                result.should_click = True
                result.retry_number = self.pending[box.identity] + 1
                return result
            return result

        if not 1 <= selected_index <= len(boxes):
            return result
        selected = boxes[selected_index - 1]
        result.selected_identity = selected.identity
        result.target_index = selected_index
        result.duplicate_selected_id = bool(selected.identity) and sum(
            b.identity == selected.identity for b in boxes) > 1
        if not selected.identity or result.duplicate_selected_id:
            return result

        new_at_rank = selected.identity in new_ids
        recovered_at_rank = (mode == MODE_AVAILABLE
                             and selected.identity in result.became_actionable)
        if (new_at_rank or recovered_at_rank) and selected.identity not in self.completed:
            self.pending.setdefault(selected.identity, 0)
            self.pending_reason.setdefault(selected.identity,
                                           'new_id' if new_at_rank else 'became_available')
        if selected.identity in self.pending and selected.actionable:
            result.should_click = True
            result.trigger = self.pending_reason.get(selected.identity, 'new_id')
            result.retry_number = self.pending[selected.identity] + 1
        return result

    def clicked(self, identity: str, success: bool) -> bool:
        """完成或重試；回傳 True 表示已達最大次數。"""
        if success:
            self.pending.pop(identity, None)
            self.pending_reason.pop(identity, None)
            self.completed.add(identity)
            return False
        if identity not in self.pending:
            return False
        self.pending[identity] += 1
        if self.pending[identity] >= self.max_retries:
            self.pending.pop(identity, None)
            self.pending_reason.pop(identity, None)
            return True
        return False
