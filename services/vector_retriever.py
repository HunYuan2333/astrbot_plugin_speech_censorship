"""向量检索器占位实现（可替换为 Chroma/FAISS/云向量库）"""

from typing import Dict, List

from ..models.slang_entry import SlangEntry, SlangHit


class VectorRetriever:
    """语义检索接口占位。

    当前返回空结果，避免引入额外依赖影响稳定性。
    后续可在不修改主流程的前提下替换实现。
    """

    async def retrieve(
        self,
        group_id: str,
        messages_dict: Dict[str, List[Dict]],
        entries: List[SlangEntry],
        max_hits: int = 20,
    ) -> List[SlangHit]:
        del group_id, messages_dict, entries, max_hits
        return []
