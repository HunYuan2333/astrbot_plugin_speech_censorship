"""审核上下文构建器"""

from typing import List

from ..models.slang_entry import SlangHit


class ReviewContextBuilder:
    """将检索结果构造成可解释、可复核的提示词上下文"""

    @staticmethod
    def build_slang_context(hits: List[SlangHit], max_items: int = 10) -> str:
        if not hits:
            return ""

        lines = [
            "检索增强证据（仅供复核参考，必须结合上下文判断，不得仅凭关键词直接处罚）：",
        ]

        for idx, hit in enumerate(hits[:max_items], start=1):
            lines.append(
                f"{idx}. user_id={hit.user_id}, canonical={hit.canonical_term}, "
                f"matched={hit.matched_term}, severity={hit.severity_level}, "
                f"source={hit.source}, scope={hit.group_scope}, version={hit.entry_version}, "
                f"match_type={hit.match_type}, text={hit.matched_text}"
            )

        return "\n".join(lines)
