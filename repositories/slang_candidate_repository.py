"""候选新黑话仓储（并发安全）"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List

from astrbot.api import logger


class SlangCandidateRepository:
    """候选新黑话仓储。用于保存 LLM 自动发现的疑似新黑话。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._candidates: Dict[str, Dict] = {}

    @property
    def candidates_file(self) -> Path:
        return self.data_dir / "slang_candidates.json"

    async def load(self):
        try:
            if not self.candidates_file.exists():
                await self._persist()
                return

            payload = await asyncio.to_thread(self._read_file)
            items = payload.get("candidates", []) if isinstance(payload, dict) else []
            loaded: Dict[str, Dict] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("term", "")).strip().lower()
                if not key:
                    continue
                loaded[key] = item

            async with self._lock:
                self._candidates = loaded

            logger.info(f"已加载候选新黑话：{len(loaded)} 条")
        except Exception as exc:
            logger.error(f"加载候选新黑话失败: {exc}", exc_info=True)

    async def add_candidates(self, group_id: str, candidates: List[Dict]):
        if not candidates:
            return

        now_ts = time.time()
        async with self._lock:
            for candidate in candidates:
                term = str(candidate.get("term", "")).strip()
                if not term:
                    continue

                key = term.lower()
                confidence = float(candidate.get("confidence", 0.0) or 0.0)
                reason = str(candidate.get("reason", "")).strip()
                category = str(candidate.get("category", "general")).strip() or "general"
                hint = str(candidate.get("hint", "")).strip()
                examples = [str(item).strip() for item in candidate.get("examples", []) if str(item).strip()]

                existing = self._candidates.get(key)
                if existing:
                    existing["count"] = int(existing.get("count", 1)) + 1
                    existing["last_seen"] = now_ts
                    existing["max_confidence"] = max(float(existing.get("max_confidence", 0.0)), confidence)
                    if reason:
                        existing["reason"] = reason
                    if hint:
                        existing["hint"] = hint
                    if examples:
                        existing_examples = [
                            str(item).strip() for item in existing.get("examples", []) if str(item).strip()
                        ]
                        merged = existing_examples + examples
                        deduped = []
                        seen = set()
                        for item in merged:
                            lower_item = item.lower()
                            if lower_item in seen:
                                continue
                            seen.add(lower_item)
                            deduped.append(item)
                        existing["examples"] = deduped[:10]
                    existing["category"] = category
                    source_groups = set(str(item) for item in existing.get("source_groups", []))
                    source_groups.add(str(group_id))
                    existing["source_groups"] = sorted(source_groups)
                else:
                    self._candidates[key] = {
                        "term": term,
                        "category": category,
                        "hint": hint,
                        "reason": reason,
                        "examples": examples[:10],
                        "max_confidence": confidence,
                        "count": 1,
                        "first_seen": now_ts,
                        "last_seen": now_ts,
                        "status": "pending",
                        "source_groups": [str(group_id)],
                    }

        await self._persist()

    async def list_top_candidates(self, limit: int = 10) -> List[Dict]:
        async with self._lock:
            candidates = list(self._candidates.values())

        candidates.sort(
            key=lambda item: (
                float(item.get("max_confidence", 0.0)),
                int(item.get("count", 0)),
                float(item.get("last_seen", 0.0)),
            ),
            reverse=True,
        )
        return candidates[: max(1, int(limit))]

    async def get_candidate(self, term: str) -> Dict:
        key = str(term or "").strip().lower()
        if not key:
            return {}

        async with self._lock:
            item = self._candidates.get(key)
            return dict(item) if item else {}

    async def mark_candidate_status(self, term: str, status: str, operator: str = "system") -> bool:
        key = str(term or "").strip().lower()
        if not key:
            return False

        normalized_status = str(status or "pending").strip() or "pending"
        now_ts = time.time()

        updated = False
        async with self._lock:
            item = self._candidates.get(key)
            if not item:
                return False
            item["status"] = normalized_status
            item["updated_by"] = operator
            item["updated_at"] = now_ts
            updated = True

        if updated:
            await self._persist()
        return updated

    async def get_stats(self) -> Dict[str, int]:
        async with self._lock:
            total = len(self._candidates)
            pending = sum(1 for item in self._candidates.values() if str(item.get("status", "pending")) == "pending")
        return {"total_candidates": total, "pending_candidates": pending}

    async def _persist(self):
        async with self._lock:
            payload = {
                "candidates": list(self._candidates.values()),
                "updated_at": time.time(),
            }
        await asyncio.to_thread(self._write_file, payload)

    def _read_file(self) -> Dict:
        with open(self.candidates_file, "r", encoding="utf-8") as file:
            return json.load(file)

    def _write_file(self, payload: Dict):
        with open(self.candidates_file, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
