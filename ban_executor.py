"""禁言执行模块 - 负责禁言 API 调用和警告消息"""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Type

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class BanExecutor:
    """禁言执行器

    职责：
    - 调用禁言 API
    - 发送警告消息
    - 验证和防误杀护栏
    """

    def __init__(
        self,
        get_aiocqhttp_event_class: Callable[[], Optional[Type[Any]]],
        get_config: Callable[[str, Any], Any],
    ):
        """
        Args:
            get_aiocqhttp_event_class: 获取 AiocqhttpMessageEvent 类的方法
            get_config: 获取配置的方法
        """
        self.get_aiocqhttp_event_class = get_aiocqhttp_event_class
        self.get_config = get_config

    @staticmethod
    def is_ban_api_success(ret: Any) -> bool:
        """统一禁言 API 成功判定口径。"""
        return isinstance(ret, dict) and ret.get('retcode') == 0

    async def ban_user(self, event: AstrMessageEvent, group_id: str, user_id: str, reason: str) -> bool:
        """禁言用户并发送警告消息

        Args:
            event: 事件对象
            group_id: 群组 ID
            user_id: 用户 ID
            reason: 禁言原因

        Returns:
            True 表示禁言成功，False 表示失败
        """
        # 检查基本参数
        if not event:
            logger.warning(f"无法获取群 {group_id} 的 event 对象")
            return False

        if event.get_platform_name() != "aiocqhttp":
            logger.warning("仅支持 QQ 平台（aiocqhttp）")
            return False

        # 获取并验证 AiocqhttpMessageEvent 类（必须成功，否则不能继续）
        AiocqhttpMessageEvent = self.get_aiocqhttp_event_class()
        if AiocqhttpMessageEvent is None:
            logger.error(f"无法加载 AiocqhttpMessageEvent 类，禁言无法执行")
            return False

        if not isinstance(event, AiocqhttpMessageEvent):
            logger.error("Event 类型验证失败，不是有效的 AiocqhttpMessageEvent 实例")
            return False

        # 获取 bot 客户端
        client = event.bot
        if not client:
            logger.error("无法获取 bot 客户端对象")
            return False

        # 参数类型转换（必须成功）
        try:
            group_id_int = int(group_id)
            user_id_int = int(user_id)
            # 修复：添加 ban_duration 类型转换，防止配置为字符串时出错
            ban_duration = int(self.get_config("ban_duration", 600))
        except (ValueError, TypeError) as ve:
            logger.error(f"禁言参数类型转换失败 - group_id={group_id}, user_id={user_id}, ban_duration={self.get_config('ban_duration', 600)}: {ve}")
            return False

        logger.info(f"禁言用户 {user_id}（群: {group_id}，原因: {reason}，时长: {ban_duration} 秒）")

        # 调用禁言 API（明确的单层异常捕获）
        api_timeout_seconds = float(self.get_config("api_timeout_seconds", 60))
        try:
            ret = await asyncio.wait_for(
                client.api.call_action(
                    'set_group_ban',
                    group_id=group_id_int,
                    user_id=user_id_int,
                    duration=ban_duration
                ),
                timeout=api_timeout_seconds,
            )

            # 检查禁言是否成功
            if self.is_ban_api_success(ret):
                logger.info(f"禁言成功: 用户 {user_id}")

                # 发送警告消息
                if self.get_config("send_warning", True):
                    await self.send_warning_message(event, group_id, user_id, reason, ban_duration)

                return True
            else:
                logger.error(f"禁言失败: 无效返回 {ret}")
                return False

        except asyncio.TimeoutError:
            logger.error(f"调用禁言 API 超时（>{api_timeout_seconds}s）")
            return False
        except TypeError as te:
            # 处理参数类型相关错误
            logger.error(f"API 调用参数错误: {te}")
            return False
        except Exception as e:
            # 处理其他API调用异常
            logger.error(f"调用禁言 API 失败: {type(e).__name__}: {e}")
            return False

    async def send_warning_message(self, event: AstrMessageEvent, group_id: str,
                                  user_id: str, reason: str, duration: int):
        """发送警告消息到群聊"""
        try:
            # 获取警告消息模板
            warning_template = self.get_config(
                "warning_template",
                "⚠️ 用户 {user} 因 {reason} 已被禁言 {duration} 秒。请注意文明发言。"
            )

            # 格式化警告消息
            warning_message = warning_template.format(
                user=user_id,
                reason=reason,
                duration=duration
            )

            # 发送消息到群聊
            await event.send(event.plain_result(warning_message))

            logger.info(f"已发送警告消息到群 {group_id}")

        except Exception as e:
            logger.error(f"发送警告消息失败: {e}", exc_info=True)

    def validate_and_should_ban(self, user_id: str,
                               messages_dict: Dict[str, List[Dict]],
                               reason: str) -> bool:
        """验证用户和应用分层防误杀护栏

        Args:
            user_id: 用户 ID
            messages_dict: 本次分析的消息字典
            reason: 违规原因

        Returns:
            True 则执行禁言，False 则跳过
        """
        # 层级1：用户集合约束（必须）
        # 检验 user_id 是否在本次 messages_dict 中出现
        if user_id not in messages_dict:
            logger.warning(f"[护栏L1-用户存在性] 用户 {user_id} 不在本次消息记录中，疑似 LLM 幻觉，阻止禁言")
            return False

        user_messages = messages_dict.get(user_id, [])
        if not user_messages:
            logger.warning(f"[护栏L1-消息存在性] 用户 {user_id} 检索结果为空，阻止禁言")
            return False

        # 层级2：消息数量检查（推荐）
        # 确保至少有 2 条消息，防止单条消息误杀
        message_count = len(user_messages)
        if message_count < 2:
            logger.warning(f"[护栏L2-消息数量] 用户 {user_id} 仅有 {message_count} 条消息（需≥2），按警告处理")
            return False

        # 层级3：时间跨度检查（推荐）
        # 确保违规消息跨越一定时间，避免短时间内的孤立事件被过度惩罚
        if len(user_messages) >= 2:
            timestamps = [msg.get("timestamp", 0) for msg in user_messages]
            valid_timestamps = [ts for ts in timestamps if ts > 0]

            if len(valid_timestamps) >= 2:
                time_span = max(valid_timestamps) - min(valid_timestamps)
                min_time_span = 30  # 最小30秒跨度

                if time_span < min_time_span:
                    logger.warning(
                        f"[护栏L3-时间跨度] 用户 {user_id} 的消息时间跨度仅 {time_span}s（需>30s），按警告处理"
                    )
                    return False
                else:
                    logger.debug(f"[护栏L3-时间跨度] 用户 {user_id} 通过（时间跨度: {time_span}s）")

        logger.info(f"[护栏验证通过] 用户 {user_id} 将被禁言（消息数: {message_count}，原因: {reason}）")
        return True
