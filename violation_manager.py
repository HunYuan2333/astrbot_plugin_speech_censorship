"""违规记录管理模块 - 负责违规数据的持久化、查询和清理"""

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

from astrbot.api import logger


class ViolationManager:
    """违规记录管理器

    职责：
    - 管理用户违规记录（防误杀护栏）
    - 实现数据持久化（JSON 文件）
    - 定期清理过期记录（防止内存泄漏）
    """

    def __init__(self, data_dir: Path):
        """
        Args:
            data_dir: 数据保存目录路径
        """
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 用户违规记录
        # 结构: {group_id_user_id: {"count": int, "last_time": float, "created_time": float}}
        self.records: Dict[str, Dict[str, float]] = defaultdict(dict)

    @property
    def records_file(self) -> Path:
        """违规记录文件路径"""
        return self.data_dir / "violation_records.json"

    async def load_records(self):
        """从文件中加载违规记录"""
        try:
            if self.records_file.exists():
                with open(self.records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.records = defaultdict(dict, data)
                    logger.info(f"已加载 {len(self.records)} 条违规记录")
            else:
                logger.debug("未找到违规记录文件，使用空记录")
        except Exception as e:
            logger.error(f"加载违规记录失败: {e}", exc_info=True)

    async def save_records(self):
        """将违规记录保存到文件"""
        try:
            with open(self.records_file, 'w', encoding='utf-8') as f:
                json.dump(dict(self.records), f, ensure_ascii=False, indent=2)
            logger.debug(f"已保存 {len(self.records)} 条违规记录")
        except Exception as e:
            logger.error(f"保存违规记录失败: {e}", exc_info=True)

    def record_violation(self, group_id: str, user_id: str) -> dict:
        """记录一次违规行为"""
        violation_key = f"{group_id}_{user_id}"
        record = self.records[violation_key]

        # 更新违规次数和最后违规时间
        record["count"] = record.get("count", 0) + 1
        record["last_time"] = time.time()

        # 记录创建时间（用于后续过期清理）
        if "created_time" not in record:
            record["created_time"] = time.time()

        self.records[violation_key] = record
        return record

    def get_violation_record(self, group_id: str, user_id: str) -> Optional[dict]:
        """获取用户的违规记录"""
        violation_key = f"{group_id}_{user_id}"
        return self.records.get(violation_key)

    def check_repeated_violation(self, group_id: str, user_id: str,
                                cooldown_seconds: int = 3600) -> bool:
        """检查用户在冷却时间内是否已违规（防止重复禁言）

        Args:
            group_id: 群组 ID
            user_id: 用户 ID
            cooldown_seconds: 冷却时间（秒）

        Returns:
            True 表示在冷却期内已有违规记录，应该跳过禁言；
            False 表示可以进行禁言。
        """
        record = self.get_violation_record(group_id, user_id)
        if not record or record.get("count", 0) == 0:
            return False

        last_violation_time = record.get("last_time", 0)
        if time.time() - last_violation_time < cooldown_seconds:
            logger.warning(f"[护栏] 用户 {user_id} 在 {cooldown_seconds}s 内已被处罚，跳过本次禁言")
            return True

        return False

    async def cleanup_expired_records(self, max_age_days: int = 7):
        """清理超过 max_age_days 天的违规记录

        Args:
            max_age_days: 记录最大保留天数
        """
        current_time = time.time()
        max_age_seconds = max_age_days * 24 * 3600

        expired_keys = []
        for key, record in self.records.items():
            created_time = record.get("created_time", 0)
            if created_time < (current_time - max_age_seconds):
                expired_keys.append(key)

        for key in expired_keys:
            del self.records[key]

        if expired_keys:
            logger.info(f"清理了 {len(expired_keys)} 条过期的违规记录")
            await self.save_records()

    def get_user_violation_count(self, group_id: str, user_id: str) -> int:
        """获取用户的总违规次数"""
        record = self.get_violation_record(group_id, user_id)
        return record.get("count", 0) if record else 0

    def get_stats(self) -> dict:
        """获取违规统计信息"""
        total_records = len(self.records)
        total_violations = sum(r.get("count", 0) for r in self.records.values())

        return {
            "total_records": total_records,
            "total_violations": total_violations,
            "file_size": self.records_file.stat().st_size if self.records_file.exists() else 0,
        }
