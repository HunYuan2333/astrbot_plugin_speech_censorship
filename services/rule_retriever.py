"""关键词规则检索器"""

from typing import Dict, List

from ..models.slang_entry import SlangEntry, SlangHit


class RuleRetriever:
    """基于关键词与别名的精确检索"""

    async def retrieve(
        self,
        group_id: str,
        messages_dict: Dict[str, List[Dict]],
        entries: List[SlangEntry],
        case_sensitive: bool = False,
        max_hits: int = 20,
    ) -> List[SlangHit]:
        del group_id

        hits: List[SlangHit] = []
        seen = set()

        for user_id, messages in messages_dict.items():
            for message in messages:
                raw_text = str(message.get("message", "") or "")
                if not raw_text:
                    continue

                text_for_match = raw_text if case_sensitive else raw_text.lower()

                for entry in entries:
                    for term in entry.normalized_terms:
                        term_for_match = term if case_sensitive else term.lower()
                        if not term_for_match or term_for_match not in text_for_match:
                            continue

                        dedupe_key = (user_id, entry.term_id, term_for_match, raw_text)
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)

                        hit = SlangHit(
                            term_id=entry.term_id,
                            canonical_term=entry.canonical_term,
                            matched_term=term,
                            matched_text=raw_text,
                            user_id=str(user_id),
                            severity_level=entry.severity_level,
                            action_hint=entry.action_hint,
                            score=1.0,
                            match_type="keyword",
                            group_scope=entry.group_scope,
                            source=entry.source,
                            entry_version=entry.version,
                        )
                        hits.append(hit)

                        if len(hits) >= max_hits:
                            return hits

        return hits
