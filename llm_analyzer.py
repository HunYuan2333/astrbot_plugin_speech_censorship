"""LLM 分析器模块 - 负责 LLM 调用和响应解析"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger


class LLMAnalyzer:
    """LLM 分析器

    职责：
    - 构建审核提示词
    - 调用 LLM 进行消息分析
    - 解析和验证 LLM 响应
    """

    REQUIRED_JSON_FORMAT = (
        '{"violations":[{"user_id":"123456","reason":"阴阳怪气/争吵/敏感话题"}]}'
    )

    def __init__(self, context: Any):
        """
        Args:
            context: AstrBot Context 对象（用于调用 LLM）
        """
        self.context = context

    def build_review_prompt(self, messages_text: str, default_rules: str = "", custom_rules: str = "") -> str:
        """构建审核提示词：默认规则 + 用户自定义规则"""
        rules_block = default_rules.strip()
        if custom_rules.strip():
            rules_block = f"{rules_block}\n\n额外自定义规则：\n{custom_rules.strip()}"

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

    def format_messages_for_llm(self, messages_dict: Dict[str, List[Dict]]) -> str:
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

    async def analyze_messages(self, group_id: str, messages_dict: Dict[str, List[Dict]],
                              llm_provider_name: str, default_rules: str = "",
                              custom_rules: str = "") -> List[Dict]:
        """使用 LLM 分析消息，返回违规用户列表"""
        try:
            # 构造消息文本
            messages_text = self.format_messages_for_llm(messages_dict)

            # 构造提示词
            prompt = self.build_review_prompt(messages_text, default_rules, custom_rules)

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
            violations = self.parse_llm_response(result)

            return violations

        except asyncio.TimeoutError:
            logger.error("LLM 调用超时")
            return []
        except Exception as e:
            logger.error(f"LLM 分析出错: {e}", exc_info=True)
            return []

    def parse_llm_response(self, response: str) -> List[Dict]:
        """解析 LLM 响应，提取违规用户列表（包含 Schema 验证）"""
        try:
            # 尝试直接解析 JSON
            data = json.loads(response)
            violations = data.get("violations", [])

            # Schema 验证：确保 violations 是列表
            if not isinstance(violations, list):
                logger.error(f"Schema 校验失败：violations 应为列表，实际类型: {type(violations).__name__}")
                return []

            # 验证每个元素都有必要字段
            valid_violations = []
            for v in violations:
                if isinstance(v, dict) and "user_id" in v:
                    valid_violations.append(v)
                else:
                    logger.warning(f"违规项目格式不正确，跳过: {v}")

            return valid_violations
        except json.JSONDecodeError:
            # 如果不是完整的 JSON，尝试提取 JSON 部分
            try:
                json_str = self._extract_json_string(response)
                data = json.loads(json_str)
                violations = data.get("violations", [])

                # Schema 验证
                if not isinstance(violations, list):
                    logger.error(f"Schema 校验失败：violations 应为列表，实际类型: {type(violations).__name__}")
                    return []

                valid_violations = []
                for v in violations:
                    if isinstance(v, dict) and "user_id" in v:
                        valid_violations.append(v)
                    else:
                        logger.warning(f"违规项目格式不正确，跳过: {v}")

                return valid_violations
            except Exception as e:
                logger.error(f"解析 LLM 响应失败: {e}\n响应内容: {response}")
                return []

    @staticmethod
    def _extract_json_string(response: str) -> str:
        """从响应中提取 JSON 字符串"""
        # 查找 JSON 代码块
        if "```json" in response:
            json_start = response.find("```json") + 7
            json_end = response.find("```", json_start)
            return response[json_start:json_end].strip()
        elif "```" in response:
            json_start = response.find("```") + 3
            json_end = response.find("```", json_start)
            return response[json_start:json_end].strip()
        elif "{" in response and "}" in response:
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            return response[json_start:json_end]
        else:
            raise ValueError("无法找到 JSON 数据")
