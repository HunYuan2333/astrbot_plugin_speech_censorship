"""黑话词条仓储（并发安全 + 可审计）"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger

from ..models.slang_entry import GLOBAL_SCOPE, SlangEntry


class SlangRepository:
    """词条仓储：负责持久化、版本控制与作用域解析"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._entries: Dict[str, SlangEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def entries_file(self) -> Path:
        return self.data_dir / "slang_entries.json"

    async def load(self):
        """加载词条文件"""
        try:
            if not self.entries_file.exists():
                await self._persist_entries()
                return

            data = await asyncio.to_thread(self._read_file)
            entries: Dict[str, SlangEntry] = {}
            for item in data.get("entries", []):
                entry = SlangEntry.from_dict(item)
                if entry.term_id and entry.canonical_term:
                    entries[entry.term_id] = entry

            async with self._lock:
                self._entries = entries

            logger.info(f"已加载黑话词条：{len(entries)} 条")
        except Exception as exc:
            logger.error(f"加载黑话词条失败: {exc}", exc_info=True)

    def _read_file(self) -> Dict:
        with open(self.entries_file, "r", encoding="utf-8") as file:
            return json.load(file)

    async def _persist_entries(self):
        async with self._lock:
            payload = {
                "entries": [entry.to_dict() for entry in self._entries.values()],
                "updated_at": time.time(),
            }
        await asyncio.to_thread(self._write_file, payload)

    def _write_file(self, payload: Dict):
        with open(self.entries_file, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    async def list_entries(self) -> List[SlangEntry]:
        async with self._lock:
            return list(self._entries.values())

    async def get_effective_entries(self, group_id: str, now_ts: Optional[float] = None) -> List[SlangEntry]:
        now_ts = time.time() if now_ts is None else float(now_ts)

        async with self._lock:
            entries = list(self._entries.values())

        global_entries: Dict[str, SlangEntry] = {}
        group_entries: Dict[str, SlangEntry] = {}

        for entry in entries:
            if not entry.is_effective_for_group(group_id, now_ts):
                continue

            key = entry.canonical_term.strip().lower()
            if not key:
                continue

            if entry.group_scope == GLOBAL_SCOPE:
                global_entries[key] = entry
            elif entry.group_scope == str(group_id):
                group_entries[key] = entry

        merged = dict(global_entries)
        merged.update(group_entries)
        return list(merged.values())

    async def upsert_entry(
        self,
        canonical_term: str,
        aliases: Optional[List[str]] = None,
        category: str = "general",
        metaphor_hint: str = "",
        severity_level: str = "medium",
        action_hint: str = "review",
        group_scope: str = GLOBAL_SCOPE,
        source: str = "manual",
        context_examples: Optional[List[str]] = None,
        effective_from: Optional[float] = None,
        expire_at: Optional[float] = None,
        is_active: bool = True,
        risk_tags: Optional[List[str]] = None,
        operator: str = "system",
        expected_version: Optional[int] = None,
        term_id: Optional[str] = None,
    ) -> SlangEntry:
        """新增或更新词条；支持 expected_version 乐观并发控制"""
        current_ts = time.time()
        normalized_term = canonical_term.strip()
        normalized_scope = str(group_scope or GLOBAL_SCOPE).strip()
        normalized_aliases = [item.strip() for item in (aliases or []) if item and item.strip()]
        normalized_category = (category or "general").strip() or "general"
        normalized_hint = (metaphor_hint or "").strip()
        normalized_examples = [item.strip() for item in (context_examples or []) if item and item.strip()]
        normalized_tags = [item.strip() for item in (risk_tags or []) if item and item.strip()]

        async with self._lock:
            target_id = (term_id or "").strip()
            existing: Optional[SlangEntry] = self._entries.get(target_id) if target_id else None

            if existing is None and not target_id:
                for item in self._entries.values():
                    if (
                        item.canonical_term.strip().lower() == normalized_term.lower()
                        and item.group_scope == normalized_scope
                    ):
                        existing = item
                        target_id = item.term_id
                        break

            if existing is not None:
                if expected_version is not None and existing.version != int(expected_version):
                    raise ValueError(
                        f"词条版本冲突: term_id={existing.term_id}, "
                        f"expected={expected_version}, actual={existing.version}"
                    )

                updated_entry = SlangEntry(
                    term_id=existing.term_id,
                    canonical_term=normalized_term or existing.canonical_term,
                    aliases=normalized_aliases,
                    category=normalized_category,
                    metaphor_hint=normalized_hint,
                    severity_level=severity_level,
                    action_hint=action_hint,
                    group_scope=normalized_scope,
                    source=source,
                    context_examples=normalized_examples,
                    effective_from=effective_from,
                    expire_at=expire_at,
                    is_active=is_active,
                    version=existing.version + 1,
                    created_by=existing.created_by,
                    updated_by=operator,
                    created_at=existing.created_at,
                    updated_at=current_ts,
                    risk_tags=normalized_tags,
                )
                self._entries[updated_entry.term_id] = updated_entry
                result = updated_entry
            else:
                new_entry = SlangEntry(
                    term_id=str(uuid.uuid4()),
                    canonical_term=normalized_term,
                    aliases=normalized_aliases,
                    category=normalized_category,
                    metaphor_hint=normalized_hint,
                    severity_level=severity_level,
                    action_hint=action_hint,
                    group_scope=normalized_scope,
                    source=source,
                    context_examples=normalized_examples,
                    effective_from=effective_from,
                    expire_at=expire_at,
                    is_active=is_active,
                    version=1,
                    created_by=operator,
                    updated_by=operator,
                    created_at=current_ts,
                    updated_at=current_ts,
                    risk_tags=normalized_tags,
                )
                self._entries[new_entry.term_id] = new_entry
                result = new_entry

        await self._persist_entries()
        return result

    async def get_stats(self) -> Dict[str, int]:
        async with self._lock:
            total_entries = len(self._entries)
            active_entries = sum(1 for item in self._entries.values() if item.is_active)
            global_entries = sum(1 for item in self._entries.values() if item.group_scope == GLOBAL_SCOPE)

        return {
            "total_entries": total_entries,
            "active_entries": active_entries,
            "global_entries": global_entries,
            "group_entries": max(0, total_entries - global_entries),
        }
