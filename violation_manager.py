"""违规记录管理模块 - 负责违规数据的持久化、查询和清理"""

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

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
        # 结构: {group_id: {user_id: {"count": int, "last_time": float, "created_time": float}}}
        # 嵌套结构更直观，避免键冲突问题
        self.records: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

    @property
    def records_file(self) -> Path:
        """违规记录文件路径"""
        return self.data_dir / "violation_records.json"

    async def load_records(self):
        """从文件中加载违规记录"""
        try:
            if self.records_file.exists():
                data = await asyncio.to_thread(self._read_records_file)

                # 支持两种格式：新的嵌套结构和旧的扁平结构（向后兼容）
                if isinstance(data, dict):
                    if self._looks_like_flat_records(data):
                        logger.info("检测到旧格式违规记录，正在迁移...")
                        migrated = self._migrate_flat_records(data)
                        self.records = self._normalize_nested_records(migrated)
                    else:
                        self.records = self._normalize_nested_records(data)

                logger.info("已加载违规记录")
            else:
                logger.debug("未找到违规记录文件，使用空记录")
        except Exception as e:
            logger.error(f"加载违规记录失败: {e}", exc_info=True)

    async def save_records(self):
        """将违规记录保存到文件"""
        try:
            # 转换为可序列化的格式
            serializable_records = {}
            for group_id, users in self.records.items():
                serializable_records[group_id] = {}
                for user_id, record in users.items():
                    serializable_records[group_id][user_id] = {
                        "count": int(record.get("count", 0)),
                        "last_time": float(record.get("last_time", 0)),
                        "created_time": float(record.get("created_time", 0))
                    }

            await asyncio.to_thread(self._write_records_file, serializable_records)
            logger.debug("已保存违规记录")
        except Exception as e:
            logger.error(f"保存违规记录失败: {e}", exc_info=True)

    def _read_records_file(self) -> dict:
        """同步读取记录文件（供 to_thread 调用）"""
        with open(self.records_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_records_file(self, records: Dict[str, Dict[str, Dict[str, float]]]):
        """同步写入记录文件（供 to_thread 调用）"""
        with open(self.records_file, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _looks_like_flat_records(data: Dict[str, Any]) -> bool:
        """判断是否为旧版扁平结构：{group_user: {count,...}}"""
        if not data:
            return False

        flat_like = 0
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            if "_" in key and ("count" in value or "last_time" in value or "created_time" in value):
                flat_like += 1

        return flat_like > 0 and flat_like == len(data)

    @staticmethod
    def _normalize_nested_records(data: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """标准化嵌套结构，确保 records 外层/中层类型一致"""
        normalized: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

        for group_id, users in data.items():
            if not isinstance(group_id, str) or not isinstance(users, dict):
                continue

            for user_id, record in users.items():
                if not isinstance(user_id, str) or not isinstance(record, dict):
                    continue

                normalized[group_id][user_id] = {
                    "count": int(record.get("count", 0)),
                    "last_time": float(record.get("last_time", 0)),
                    "created_time": float(record.get("created_time", 0)),
                }

        return normalized

    @staticmethod
    def _migrate_flat_records(flat_records: Dict[str, Dict]) -> Dict[str, Dict[str, Dict]]:
        """从扁平格式迁移到嵌套格式的违规记录"""
        nested = defaultdict(dict)
        for key, record in flat_records.items():
            if "_" in key:
                parts = key.rsplit("_", 1)
                if len(parts) == 2:
                    group_id, user_id = parts
                    nested[group_id][user_id] = record
        return nested

    def record_violation(self, group_id: str, user_id: str) -> dict:
        """记录一次违规行为"""
        record = self.records[group_id].get(user_id, {})

        # 更新违规次数和最后违规时间
        record["count"] = record.get("count", 0) + 1
        record["last_time"] = time.time()

        # 记录创建时间（用于后续过期清理）
        if "created_time" not in record:
            record["created_time"] = time.time()

        self.records[group_id][user_id] = record
        return record

    def get_violation_record(self, group_id: str, user_id: str) -> Optional[dict]:
        """获取用户的违规记录"""
        return self.records.get(group_id, {}).get(user_id)

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

        expired_count = 0
        for group_id in list(self.records.keys()):
            for user_id in list(self.records[group_id].keys()):
                record = self.records[group_id][user_id]
                created_time = record.get("created_time", 0)
                if created_time < (current_time - max_age_seconds):
                    del self.records[group_id][user_id]
                    expired_count += 1

            # 清理空的群组
            if not self.records[group_id]:
                del self.records[group_id]

        if expired_count > 0:
            logger.info(f"清理了 {expired_count} 条过期的违规记录")
            await self.save_records()

    def get_user_violation_count(self, group_id: str, user_id: str) -> int:
        """获取用户的总违规次数"""
        record = self.get_violation_record(group_id, user_id)
        return record.get("count", 0) if record else 0

    def get_stats(self) -> dict:
        """获取违规统计信息"""
        total_records = sum(len(users) for users in self.records.values())
        total_violations = sum(
            record.get("count", 0)
            for users in self.records.values()
            for record in users.values()
        )

        return {
            "total_records": total_records,
            "total_violations": total_violations,
            "file_size": self.records_file.stat().st_size if self.records_file.exists() else 0,
        }
