"""黑话词条与检索命中模型"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


GLOBAL_SCOPE = "*"


@dataclass(frozen=True)
class SlangEntry:
    """黑话/自定义屏蔽词词条"""

    term_id: str
    canonical_term: str
    aliases: List[str] = field(default_factory=list)
    category: str = "general"
    metaphor_hint: str = ""
    severity_level: str = "medium"
    action_hint: str = "review"
    group_scope: str = GLOBAL_SCOPE
    source: str = "manual"
    context_examples: List[str] = field(default_factory=list)
    effective_from: Optional[float] = None
    expire_at: Optional[float] = None
    is_active: bool = True
    version: int = 1
    created_by: str = "system"
    updated_by: str = "system"
    created_at: float = 0.0
    updated_at: float = 0.0
    risk_tags: List[str] = field(default_factory=list)

    def is_effective_for_group(self, group_id: str, now_ts: float) -> bool:
        if not self.is_active:
            return False
        if self.group_scope != GLOBAL_SCOPE and self.group_scope != str(group_id):
            return False
        if self.effective_from is not None and now_ts < self.effective_from:
            return False
        if self.expire_at is not None and now_ts > self.expire_at:
            return False
        return True

    @property
    def normalized_terms(self) -> List[str]:
        terms = [self.canonical_term] + list(self.aliases)
        unique_terms: List[str] = []
        seen = set()
        for term in terms:
            normalized = (term or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique_terms.append(normalized)
        return unique_terms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "term_id": self.term_id,
            "canonical_term": self.canonical_term,
            "aliases": list(self.aliases),
            "category": self.category,
            "metaphor_hint": self.metaphor_hint,
            "severity_level": self.severity_level,
            "action_hint": self.action_hint,
            "group_scope": self.group_scope,
            "source": self.source,
            "context_examples": list(self.context_examples),
            "effective_from": self.effective_from,
            "expire_at": self.expire_at,
            "is_active": self.is_active,
            "version": int(self.version),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "risk_tags": list(self.risk_tags),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlangEntry":
        return cls(
            term_id=str(data.get("term_id", "")).strip(),
            canonical_term=str(data.get("canonical_term", "")).strip(),
            aliases=[str(v).strip() for v in data.get("aliases", []) if str(v).strip()],
            category=str(data.get("category", "general") or "general").strip(),
            metaphor_hint=str(data.get("metaphor_hint", "") or "").strip(),
            severity_level=str(data.get("severity_level", "medium") or "medium").strip(),
            action_hint=str(data.get("action_hint", "review") or "review").strip(),
            group_scope=str(data.get("group_scope", GLOBAL_SCOPE) or GLOBAL_SCOPE).strip(),
            source=str(data.get("source", "manual") or "manual").strip(),
            context_examples=[str(v).strip() for v in data.get("context_examples", []) if str(v).strip()],
            effective_from=(float(data["effective_from"]) if data.get("effective_from") is not None else None),
            expire_at=(float(data["expire_at"]) if data.get("expire_at") is not None else None),
            is_active=bool(data.get("is_active", True)),
            version=max(1, int(data.get("version", 1))),
            created_by=str(data.get("created_by", "system") or "system").strip(),
            updated_by=str(data.get("updated_by", "system") or "system").strip(),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            risk_tags=[str(v).strip() for v in data.get("risk_tags", []) if str(v).strip()],
        )


@dataclass(frozen=True)
class SlangHit:
    """检索命中结果"""

    term_id: str
    canonical_term: str
    matched_term: str
    matched_text: str
    user_id: str
    severity_level: str
    action_hint: str
    score: float
    match_type: str
    group_scope: str
    source: str
    entry_version: int
