"""消息缓冲管理模块 - 负责消息的累积、同步和状态管理"""

import asyncio
import time
from collections import defaultdict
from typing import Dict, List


class MessageBuffer:
    """消息缓冲区管理器

    职责：
    - 管理每个群组的消息缓冲区
    - 提供线程安全的缓冲区访问（通过 asyncio 锁）
    - 支持消息的原子提取和缓冲区重置
    - 清理过期消息
    """

    def __init__(self):
        # 消息缓冲区：{group_id: {user_id: [{"message": str, "timestamp": int, "user_name": str}]}}
        self.buffer: Dict[str, Dict[str, List[Dict]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # 每个群组的最后检测时间
        self.last_check_time: Dict[str, float] = {}

        # 每个群组的并发锁（防止竞争）
        self.group_locks: Dict[str, asyncio.Lock] = {}

    def get_or_create_lock(self, group_id: str) -> asyncio.Lock:
        """获取或创建群组的锁"""
        if group_id not in self.group_locks:
            self.group_locks[group_id] = asyncio.Lock()
        return self.group_locks[group_id]

    def append_message(self, group_id: str, user_id: str, message: dict):
        """向缓冲区添加消息"""
        self.buffer[group_id][user_id].append(message)

    def get_total_messages(self, group_id: str) -> int:
        """获取群组的总消息数"""
        if group_id not in self.buffer:
            return 0
        return sum(len(msgs) for msgs in self.buffer[group_id].values())

    def snapshot_and_clear(self, group_id: str) -> Dict[str, List[Dict]]:
        """原子操作：创建快照并清空缓冲区

        这是解决竞态条件的关键：在锁保护下，
        创建消息副本并立即清空缓冲区，
        这样新消息可以在 LLM 处理期间继续写入新的缓冲区。
        """
        if group_id not in self.buffer:
            return {}

        # 创建快照
        snapshot = dict(self.buffer[group_id])

        # 立即清空缓冲区，允许新消息写入
        self.buffer[group_id].clear()

        return snapshot

    async def cleanup_old_messages(self, max_age_seconds: int = 3600):
        """清理超过 max_age_seconds 的旧消息"""
        current_time = time.time()
        cutoff_time = current_time - max_age_seconds

        for group_id in list(self.buffer.keys()):
            for user_id in list(self.buffer[group_id].keys()):
                # 过滤掉旧消息
                self.buffer[group_id][user_id] = [
                    msg for msg in self.buffer[group_id][user_id]
                    if msg.get("timestamp", 0) >= cutoff_time
                ]

                # 如果该用户没有消息了，删除该用户
                if not self.buffer[group_id][user_id]:
                    del self.buffer[group_id][user_id]

            # 如果该群没有消息了，删除该群
            if not self.buffer[group_id]:
                del self.buffer[group_id]

    def trim_recent_messages(self, group_id: str, limit: int):
        """仅保留某群最近 N 条消息（跨用户全局窗口）。limit<=0 时不限制。"""
        if limit <= 0 or group_id not in self.buffer:
            return

        # 统计总消息数
        total_count = sum(len(msgs) for msgs in self.buffer[group_id].values())
        if total_count <= limit:
            return

        # 扁平化并排序
        flattened = []
        for uid, messages in self.buffer[group_id].items():
            for msg in messages:
                flattened.append((msg.get("timestamp", 0), uid, msg))

        flattened.sort(key=lambda item: item[0])
        recent_items = flattened[-limit:]

        # 重建缓冲区
        rebuilt = defaultdict(list)
        for _, uid, msg in recent_items:
            rebuilt[uid].append(msg)

        self.buffer[group_id] = rebuilt

    def update_check_time(self, group_id: str, timestamp: float = None):
        """更新群组的最后检测时间"""
        if timestamp is None:
            timestamp = time.time()
        self.last_check_time[group_id] = timestamp

    def get_check_time(self, group_id: str) -> float:
        """获取群组的最后检测时间"""
        return self.last_check_time.get(group_id, 0)

    def ensure_check_time_initialized(self, group_id: str):
        """确保群组的检测时间已初始化"""
        if group_id not in self.last_check_time:
            self.last_check_time[group_id] = time.time()
