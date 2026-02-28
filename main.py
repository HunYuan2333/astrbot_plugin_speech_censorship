import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register


class SpeechCensorshipPlugin(Star):
    """监听群聊消息，使用 LLM 识别违规内容并自动禁言"""

    REQUIRED_JSON_FORMAT = (
        '{"violations":[{"user_id":"123456","reason":"阴阳怪气/争吵/敏感话题"}]}'
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 消息缓冲区：{group_id: {user_id: [{"message": str, "timestamp": int, "user_name": str}]}}
        self.message_buffer: Dict[str, Dict[str, List[Dict]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # 每个群组的最后检测时间
        self.last_check_time: Dict[str, float] = {}

        # 每个群组的并发锁（防止竞争）
        self.group_locks: Dict[str, asyncio.Lock] = {}

        # 定时检测任务
        self.timer_task: Optional[asyncio.Task] = None

        # 保存最新的 event 对象（用于发送消息和调用 API）
        self.latest_events: Dict[str, AstrMessageEvent] = {}

        # 用户违规记录（用于防误杀护栏）
        self.user_violation_records: Dict[str, Dict[str, float]] = defaultdict(dict)

        logger.info("群聊消息审核插件已加载")

    def _get_config(self, key: str, default: Any = None) -> Any:
        """获取插件配置"""
        return self.config.get(key, default)

    async def initialize(self):
        """插件初始化：启动定时检测任务"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        batch_size = self._get_config("batch_size", 10)
        llm_provider = self._get_config("llm_provider", "")

        # 如果触发模式包含时间触发，启动定时器
        if trigger_mode in ["time_only", "hybrid", "strict_hybrid"]:
            self.timer_task = asyncio.create_task(self._periodic_check())
            check_interval = self._get_config("check_interval", 60)
            logger.info(f"定时检测任务已启动（间隔: {check_interval} 秒，模式: {trigger_mode}）")

        logger.info(
            f"当前配置：trigger_mode={trigger_mode}, batch_size={batch_size}, llm_provider={llm_provider or '未配置'}"
        )
        logger.info("群聊消息审核插件初始化完成")

    @filter.command("censor_status")
    async def censor_status(self, event: AstrMessageEvent):
        """查看当前审核配置状态"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        check_interval = self._get_config("check_interval", 60)
        batch_size = self._get_config("batch_size", 10)
        recent_message_limit = self._get_config("recent_message_limit", 50)
        llm_provider = self._get_config("llm_provider", "")
        total_groups = len(self.message_buffer)
        total_messages = sum(
            sum(len(msgs) for msgs in users.values())
            for users in self.message_buffer.values()
        )
        yield event.plain_result(
            "审核状态:\n"
            f"- trigger_mode: {trigger_mode}\n"
            f"- check_interval: {check_interval}\n"
            f"- batch_size: {batch_size}\n"
            f"- recent_message_limit: {recent_message_limit}\n"
            f"- llm_provider: {llm_provider or '未配置'}\n"
            f"- buffer_groups: {total_groups}\n"
            f"- buffer_messages: {total_messages}"
        )

    @filter.command("censor_prompt_help")
    async def censor_prompt_help(self, event: AstrMessageEvent):
        """查看自定义提示词和JSON返回格式说明"""
        default_rules = self._get_config("default_review_rules", "")
        custom_rules = self._get_config("custom_review_rules", "")
        yield event.plain_result(
            "提示词说明:\n"
            "- 审核提示词由插件固定生成（含默认规则 + 你的自定义规则）\n"
            f"- default_review_rules: {'已配置' if default_rules.strip() else '未配置'}\n"
            f"- custom_review_rules: {'已配置' if custom_rules.strip() else '未配置'}\n"
            "- 你只需要填写“额外禁止什么”，不需要写提示词模板\n"
            "- 你不需要写 JSON 返回格式，插件会自动附加\n"
            "- LLM 必须严格返回 JSON，不要返回额外文字\n"
            f"- JSON 格式: {self.REQUIRED_JSON_FORMAT}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("censor_force_check")
    async def censor_force_check(self, event: AstrMessageEvent):
        """管理员命令：立刻执行一次当前群的 LLM 审查并按规则禁言"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("❌ 此命令仅支持 QQ 平台")
                return

            message_obj = event.message_obj
            group_id = str(message_obj.group_id) if message_obj and message_obj.group_id else ""
            if not group_id:
                yield event.plain_result("❌ 此命令仅支持群聊使用")
                return

            # 刷新该群最近事件引用，确保后续禁言/告警发送使用当前上下文
            self.latest_events[group_id] = event

            total_messages = sum(
                len(msgs) for msgs in self.message_buffer.get(group_id, {}).values()
            )
            if total_messages == 0:
                yield event.plain_result("ℹ️ 当前群缓冲区暂无可审查消息。")
                return

            yield event.plain_result(
                f"🧪 管理员强制审查已启动，当前缓冲消息 {total_messages} 条。"
            )

            await self._process_group_messages(group_id)

            yield event.plain_result("✅ 强制审查执行完成。")
        except Exception as e:
            logger.error(f"强制审查命令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 强制审查失败：{e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息"""
        try:
            # 检查是否为 QQ 平台
            if event.get_platform_name() != "aiocqhttp":
                return

            # 获取消息信息
            message_obj = event.message_obj
            group_id = str(message_obj.group_id) if message_obj.group_id else None
            user_id = str(message_obj.sender.user_id) if message_obj.sender else None
            self_id = str(message_obj.self_id) if getattr(message_obj, "self_id", None) else None
            message_str = event.message_str
            timestamp = message_obj.timestamp
            user_name = event.get_sender_name()

            if not group_id or not user_id or not message_str.strip():
                return

            # 保存最新的 event 对象
            self.latest_events[group_id] = event

            # 不缓冲机器人自身消息，避免自触发
            if self_id and user_id == self_id:
                return

            # 首次收到该群消息时初始化时间，避免 hybrid 模式首条消息就触发检测
            if group_id not in self.last_check_time:
                self.last_check_time[group_id] = time.time()

            # 白名单检查
            whitelist_users = self._get_config("whitelist_users", [])
            if user_id in [str(u) for u in whitelist_users]:
                return

            # 群组过滤
            enabled_groups = self._get_config("enabled_groups", [])
            if enabled_groups and group_id not in [str(g) for g in enabled_groups]:
                return

            # 累积消息到缓冲区
            self.message_buffer[group_id][user_id].append({
                "message": message_str,
                "timestamp": timestamp,
                "user_name": user_name
            })

            current_count = sum(len(msgs) for msgs in self.message_buffer[group_id].values())
            batch_size = self._get_config("batch_size", 10)
            trigger_mode = self._get_config("trigger_mode", "hybrid")
            logger.info(
                f"群 {group_id} 消息累积: {current_count}/{batch_size}（mode={trigger_mode}）"
            )

            # 检查是否需要触发检测
            if await self._should_trigger_check(group_id):
                await self._process_group_messages(group_id)

        except Exception as e:
            logger.error(f"处理群消息时出错: {e}", exc_info=True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("test_ban")
    async def test_ban_command(self, event: AstrMessageEvent):
        """测试禁言功能 - 禁言发送者1分钟（仅管理员可用）"""
        try:
            # 检查是否为 QQ 平台的群消息
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("❌ 此命令仅支持 QQ 平台")
                return

            message_obj = event.message_obj
            if not message_obj.group_id:
                yield event.plain_result("❌ 此命令仅支持群聊使用")
                return

            group_id = str(message_obj.group_id)
            user_id = str(message_obj.sender.user_id)
            user_name = event.get_sender_name()

            # 导入平台特定的事件类型
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

            if not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("❌ Event 类型不匹配")
                return

            # 获取 bot 客户端
            client = event.bot

            # 测试禁言：1分钟
            test_duration = 60

            logger.info(f"执行测试禁言：群 {group_id}，用户 {user_id}（{user_name}），时长 {test_duration} 秒")

            try:
                ret = await client.api.call_action(
                    'set_group_ban',
                    group_id=int(group_id),
                    user_id=int(user_id),
                    duration=test_duration
                )

                if ret is None:
                    logger.info(f"测试禁言成功（返回为空，按成功处理）：用户 {user_id}")
                    yield event.plain_result(
                        f"✅ 测试成功！用户 {user_name}（{user_id}）已被禁言 {test_duration} 秒。\n"
                        f"这是一次测试，用于验证禁言功能是否正常工作。"
                    )
                elif isinstance(ret, dict) and ret.get('retcode') == 0:
                    logger.info(f"测试禁言成功：用户 {user_id}")
                    # 注意：被禁言后用户看不到这条消息，但其他人可以看到
                    yield event.plain_result(
                        f"✅ 测试成功！用户 {user_name}（{user_id}）已被禁言 {test_duration} 秒。\n"
                        f"这是一次测试，用于验证禁言功能是否正常工作。"
                    )
                else:
                    error_msg = ret.get('message', '未知错误') if isinstance(ret, dict) else f"未知返回: {ret}"
                    logger.error(f"测试禁言失败: {ret}")
                    yield event.plain_result(
                        f"❌ 禁言失败：{error_msg}\n"
                        f"可能原因：Bot 不是管理员、权限不足、或 API 调用失败。"
                    )

            except Exception as e:
                logger.error(f"调用禁言 API 失败: {e}", exc_info=True)
                yield event.plain_result(
                    f"❌ API 调用异常：{str(e)}\n"
                    f"请检查 Bot 配置和权限。"
                )

        except Exception as e:
            logger.error(f"测试禁言命令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 命令执行失败：{str(e)}")

    async def _should_trigger_check(self, group_id: str) -> bool:
        """判断是否应该触发检测"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")

        # 计算当前群组的总消息数
        total_messages = sum(len(msgs) for msgs in self.message_buffer[group_id].values())

        # 获取配置
        check_interval = self._get_config("check_interval", 60)
        batch_size = self._get_config("batch_size", 10)

        # 获取上次检测时间
        last_check = self.last_check_time.get(group_id, 0)
        current_time = time.time()
        time_elapsed = current_time - last_check

        # 根据模式判断
        if trigger_mode == "time_only":
            # 仅时间触发（由定时器处理）
            return False
        elif trigger_mode == "count_only":
            # 仅消息数量触发
            if total_messages < batch_size:
                logger.info(
                    f"count_only 未触发：群 {group_id} 当前 {total_messages}/{batch_size}"
                )
            return total_messages >= batch_size
        elif trigger_mode == "hybrid":
            # 时间或数量达标都触发
            time_triggered = time_elapsed >= check_interval
            count_triggered = total_messages >= batch_size
            return time_triggered or count_triggered
        elif trigger_mode == "strict_hybrid":
            # 必须同时满足时间与数量（更省 token）
            time_triggered = time_elapsed >= check_interval
            count_triggered = total_messages >= batch_size
            if not (time_triggered and count_triggered):
                logger.info(
                    f"strict_hybrid 未触发：群 {group_id} time_ok={time_triggered}, count={total_messages}/{batch_size}"
                )
            return time_triggered and count_triggered

        return False

    async def _periodic_check(self):
        """定时检测任务（用于包含时间条件的模式）"""
        while True:
            try:
                check_interval = self._get_config("check_interval", 60)
                await asyncio.sleep(check_interval)

                logger.debug("执行定时检测...")

                # 遍历所有群组
                for group_id in list(self.message_buffer.keys()):
                    total_messages = sum(len(msgs) for msgs in self.message_buffer[group_id].values())

                    # 如果有消息，则进行检测
                    if total_messages > 0:
                        trigger_mode = self._get_config("trigger_mode", "hybrid")
                        last_check = self.last_check_time.get(group_id, 0)
                        time_elapsed = time.time() - last_check

                        if trigger_mode == "time_only":
                            # 仅时间模式：超时即触发
                            if time_elapsed >= check_interval:
                                logger.info(f"群 {group_id} 定时触发检测（消息数: {total_messages}）")
                                await self._process_group_messages(group_id)
                        else:
                            # 其余模式统一按触发条件判断（含 strict_hybrid）
                            if await self._should_trigger_check(group_id):
                                logger.info(f"群 {group_id} 定时轮询触发检测（消息数: {total_messages}）")
                                await self._process_group_messages(group_id)

                # 清理过期消息（超过 1 小时）
                await self._cleanup_old_messages()

            except asyncio.CancelledError:
                logger.info("定时检测任务被取消")
                break
            except Exception as e:
                logger.error(f"定时检测出错: {e}", exc_info=True)

    def _trim_group_buffer_recent(self, group_id: str, limit: int):
        """仅保留某群最近 N 条消息（跨用户全局窗口）。limit<=0 时不限制。"""
        if limit <= 0 or group_id not in self.message_buffer:
            return

        # 统计总消息数
        total_count = sum(len(msgs) for msgs in self.message_buffer[group_id].values())
        if total_count <= limit:
            return

        # 扁平化并排序（仅在必要时执行）
        flattened = []
        for uid, messages in self.message_buffer[group_id].items():
            for msg in messages:
                flattened.append((msg.get("timestamp", 0), uid, msg))

        flattened.sort(key=lambda item: item[0])
        recent_items = flattened[-limit:]

        # 重建缓冲区
        rebuilt = defaultdict(list)
        for _, uid, msg in recent_items:
            rebuilt[uid].append(msg)

        self.message_buffer[group_id] = rebuilt
        logger.info(f"群 {group_id} 缓冲窗口裁剪：{total_count} -> {limit} 条消息")

    async def _cleanup_old_messages(self):
        """清理超过 1 小时的旧消息"""
        current_time = time.time()
        one_hour_ago = current_time - 3600

        for group_id in list(self.message_buffer.keys()):
            for user_id in list(self.message_buffer[group_id].keys()):
                # 过滤掉旧消息
                self.message_buffer[group_id][user_id] = [
                    msg for msg in self.message_buffer[group_id][user_id]
                    if msg["timestamp"] >= one_hour_ago
                ]

                # 如果该用户没有消息了，删除该用户
                if not self.message_buffer[group_id][user_id]:
                    del self.message_buffer[group_id][user_id]

            # 如果该群没有消息了，删除该群
            if not self.message_buffer[group_id]:
                del self.message_buffer[group_id]

    async def _process_group_messages(self, group_id: str):
        """处理群组的累积消息（带并发锁和状态收敛保证）"""
        # 获取或创建该群的锁
        if group_id not in self.group_locks:
            self.group_locks[group_id] = asyncio.Lock()

        async with self.group_locks[group_id]:
            try:
                # 获取该群的所有消息（快照）
                messages_dict = dict(self.message_buffer[group_id])  # 深拷贝，防止后续修改

                if not messages_dict:
                    return

                total_count = sum(len(msgs) for msgs in messages_dict.values())
                logger.info(f"开始分析群 {group_id} 的 {total_count} 条消息...")

                # 调用 LLM 分析
                violations = await self._analyze_messages(group_id, messages_dict)

                if violations:
                    logger.info(f"检测到 {len(violations)} 个违规用户")

                    # 对每个违规用户执行禁言（包含验证和护栏）
                    for violation in violations:
                        user_id = violation.get("user_id")
                        reason = violation.get("reason", "违规内容")

                        if user_id and self._validate_and_apply_guardrails(group_id, user_id, messages_dict, reason):
                            await self._ban_user(group_id, user_id, reason)
                else:
                    logger.info(f"群 {group_id} 未检测到违规内容")

            except Exception as e:
                logger.error(f"处理群组消息时出错: {e}", exc_info=True)
            finally:
                # 确保状态收敛：无论成功失败都要更新时间和清理消息
                try:
                    self.message_buffer[group_id].clear()
                    self.last_check_time[group_id] = time.time()
                    logger.debug(f"群 {group_id} 消息清理完成")
                except Exception as e:
                    logger.error(f"状态收敛失败: {e}", exc_info=True)

    async def _analyze_messages(self, group_id: str, messages_dict: Dict[str, List[Dict]]) -> List[Dict]:
        """使用 LLM 分析消息，返回违规用户列表"""
        try:
            # 构造消息文本
            messages_text = self._format_messages_for_llm(messages_dict)

            # 构造 prompt（支持标准提示词 + 自定义提示词）
            prompt = self._build_review_prompt(messages_text)

            # 获取 LLM 提供商
            llm_provider_name = self._get_config("llm_provider")
            if not llm_provider_name:
                logger.warning("未配置 LLM 提供商，跳过检测")
                return []

            logger.info(f"开始调用 LLM（provider={llm_provider_name}）分析群 {group_id} 消息")

            # 按 AstrBot 文档使用 llm_generate
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=llm_provider_name,
                    prompt=prompt,
                ),
                timeout=30.0,
            )

            result = llm_resp.completion_text if llm_resp else ""

            logger.debug(f"LLM 响应: {result}")

            # 解析 JSON 响应
            violations = self._parse_llm_response(result)

            return violations

        except asyncio.TimeoutError:
            logger.error("LLM 调用超时")
            return []
        except Exception as e:
            logger.error(f"LLM 分析出错: {e}", exc_info=True)
            return []

    def _build_review_prompt(self, messages_text: str) -> str:
        """构建审核提示词：默认规则 + 用户自定义规则（固定 JSON 输出要求）"""
        default_rules = self._get_config("default_review_rules", "").strip()
        custom_rules = self._get_config("custom_review_rules", "").strip()

        rules_block = default_rules
        if custom_rules:
            rules_block = f"{rules_block}\n\n额外自定义规则：\n{custom_rules}"

        if not rules_block:
            rules_block = (
                "1. 阴阳怪气、讽刺挖苦、挑衅行为\n"
                "2. 争吵辱骂、人身攻击、恶意诋毁\n"
                "3. 敏感话题（政治、宗教、色情、暴力等）\n"
                "4. 恶意刷屏、广告骚扰"
            )

        final_prompt = (
            "你是一个群聊消息审核助手。请按以下规则分析消息并识别违规：\n"
            f"{rules_block}\n\n"
            f"消息记录：\n{messages_text}"
        )

        output_format_requirements = (
            "\n\n请以 JSON 格式返回违规用户列表，格式如下：\n"
            "{\"violations\": [{\"user_id\": \"用户QQ号\", \"reason\": \"具体违规原因\"}]}\n\n"
            "如果没有违规内容，返回：\n"
            "{\"violations\": []}\n\n"
            "注意：只返回 JSON 数据，不要有任何其他文字。"
        )

        return f"{final_prompt}{output_format_requirements}"

    def _format_messages_for_llm(self, messages_dict: Dict[str, List[Dict]]) -> str:
        """格式化消息用于 LLM 分析（按全局时间排序）"""
        # 扁平化所有消息
        flattened = []
        for user_id, messages in messages_dict.items():
            for msg in messages:
                flattened.append({
                    "user_id": user_id,
                    "timestamp": msg.get("timestamp", 0),
                    "user_name": msg.get("user_name", "未知用户"),
                    "message": msg["message"]
                })

        # 按全局时间排序
        flattened.sort(key=lambda m: m["timestamp"])

        # 格式化输出
        lines = []
        for msg in flattened:
            timestamp = time.strftime("%H:%M:%S", time.localtime(msg["timestamp"]))
            lines.append(f"[{msg['user_id']}|{msg['user_name']}] {timestamp}: {msg['message']}")

        return "\n".join(lines)

    def _parse_llm_response(self, response: str) -> List[Dict]:
        """解析 LLM 响应，提取违规用户列表（不做用户集合验证，由 _validate_and_apply_guardrails 负责）"""
        try:
            # 尝试直接解析 JSON
            data = json.loads(response)
            violations = data.get("violations", [])
            return violations
        except json.JSONDecodeError:
            # 如果不是完整的 JSON，尝试提取 JSON 部分
            try:
                # 查找 JSON 代码块
                if "```json" in response:
                    json_start = response.find("```json") + 7
                    json_end = response.find("```", json_start)
                    json_str = response[json_start:json_end].strip()
                elif "```" in response:
                    json_start = response.find("```") + 3
                    json_end = response.find("```", json_start)
                    json_str = response[json_start:json_end].strip()
                elif "{" in response and "}" in response:
                    json_start = response.find("{")
                    json_end = response.rfind("}") + 1
                    json_str = response[json_start:json_end]
                else:
                    raise ValueError("无法找到 JSON 数据")

                data = json.loads(json_str)
                violations = data.get("violations", [])
                return violations
            except Exception as e:
                logger.error(f"解析 LLM 响应失败: {e}\n响应内容: {response}")
                return []

    def _validate_and_apply_guardrails(self, group_id: str, user_id: str, messages_dict: Dict[str, List[Dict]], reason: str) -> bool:
        """验证用户和应用防误杀护栏。返回 True 则执行禁言，False 则跳过"""
        # 1. 用户集合约束：检验 user_id 是否在本次 messages_dict 中出现
        if user_id not in messages_dict:
            logger.warning(f"[护栏] 用户 {user_id} 不在本次消息记录中，疑似 LLM 幻觉，跳过禁言")
            return False

        # 2. 重复违规检查：同一周期内同一用户不连续禁言
        violation_key = f"{group_id}_{user_id}"
        if self.user_violation_records[violation_key].get("count", 0) > 0:
            last_violation_time = self.user_violation_records[violation_key].get("last_time", 0)
            if time.time() - last_violation_time < 3600:  # 1小时内
                logger.warning(f"[护栏] 用户 {user_id} 在 1 小时内已被处罚，跳过本次禁言")
                return False

        # 3. 消息数量检查：确保至少有 1 条以上的违规消息
        user_messages = messages_dict.get(user_id, [])
        if not user_messages or len(user_messages) == 0:
            logger.warning(f"[护栏] 用户 {user_id} 没有对应消息，跳过禁言")
            return False

        # 4. 可选：关键词二阶检查（可扩展为更多规则）
        # 这里可以根据 reason 和 messages 做额外校验

        logger.info(f"[护栏验证通过] 用户 {user_id} 将被禁言，原因：{reason}")
        return True

    async def _ban_user(self, group_id: str, user_id: str, reason: str):
        """禁言用户并发送警告消息"""
        try:
            # 获取该群的最新 event 对象
            event = self.latest_events.get(group_id)
            if not event:
                logger.warning(f"无法获取群 {group_id} 的 event 对象")
                return

            # 确保是 aiocqhttp 平台
            if event.get_platform_name() != "aiocqhttp":
                logger.warning("仅支持 QQ 平台（aiocqhttp）")
                return

            # 导入平台特定的事件类型
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

            if not isinstance(event, AiocqhttpMessageEvent):
                logger.warning("Event 类型不匹配")
                return

            # 获取 bot 客户端
            client = event.bot

            # 获取禁言时长
            ban_duration = self._get_config("ban_duration", 600)

            # 调用禁言 API
            logger.info(f"禁言用户 {user_id}（群: {group_id}，原因: {reason}，时长: {ban_duration} 秒）")

            try:
                ret = await client.api.call_action(
                    'set_group_ban',
                    group_id=int(group_id),
                    user_id=int(user_id),
                    duration=ban_duration
                )

                if ret is None or (isinstance(ret, dict) and ret.get('retcode') == 0):
                    logger.info(f"禁言成功: 用户 {user_id}")

                    # 记录违规历史
                    violation_key = f"{group_id}_{user_id}"
                    self.user_violation_records[violation_key]["count"] = self.user_violation_records[violation_key].get("count", 0) + 1
                    self.user_violation_records[violation_key]["last_time"] = time.time()

                    # 发送警告消息
                    if self._get_config("send_warning", True):
                        await self._send_warning_message(event, group_id, user_id, reason, ban_duration)
                else:
                    logger.error(f"禁言失败: {ret}")

            except Exception as e:
                logger.error(f"调用禁言 API 失败: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"禁言用户时出错: {e}", exc_info=True)

    async def _send_warning_message(self, event: AstrMessageEvent, group_id: str, user_id: str, reason: str, duration: int):
        """发送警告消息到群聊"""
        try:
            # 获取警告消息模板
            warning_template = self._get_config(
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

    async def terminate(self):
        """插件卸载时取消定时任务"""
        if self.timer_task:
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
            logger.info("定时检测任务已停止")

        logger.info("群聊消息审核插件已卸载")
