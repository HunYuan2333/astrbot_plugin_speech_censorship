"""混合检索服务：关键词 + 语义（可插拔）"""

from typing import Dict, List

from ..models.slang_entry import SlangHit
from ..repositories.slang_repository import SlangRepository
from .rule_retriever import RuleRetriever
from .vector_retriever import VectorRetriever


class HybridRetriever:
    """混合检索聚合器"""

    def __init__(self, repository: SlangRepository):
        self.repository = repository
        self.rule_retriever = RuleRetriever()
        self.vector_retriever = VectorRetriever()

    async def retrieve(
        self,
        group_id: str,
        messages_dict: Dict[str, List[Dict]],
        case_sensitive: bool = False,
        max_hits: int = 20,
    ) -> List[SlangHit]:
        entries = await self.repository.get_effective_entries(group_id)
        if not entries:
            return []

        keyword_hits = await self.rule_retriever.retrieve(
            group_id=group_id,
            messages_dict=messages_dict,
            entries=entries,
            case_sensitive=case_sensitive,
            max_hits=max_hits,
        )

        remaining = max(0, max_hits - len(keyword_hits))
        if remaining <= 0:
            return keyword_hits

        vector_hits = await self.vector_retriever.retrieve(
            group_id=group_id,
            messages_dict=messages_dict,
            entries=entries,
            max_hits=remaining,
        )

        combined: List[SlangHit] = []
        seen = set()

        for hit in keyword_hits + vector_hits:
            key = (hit.user_id, hit.term_id, hit.match_type, hit.matched_text)
            if key in seen:
                continue
            seen.add(key)
            combined.append(hit)
            if len(combined) >= max_hits:
                break

        return combined
