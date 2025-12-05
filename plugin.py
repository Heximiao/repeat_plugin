from typing import Tuple, Optional, List, Type, Dict
from collections import deque
import re
import http.client
import json
import random

from src.plugin_system import (
    BasePlugin,
    BaseEventHandler,
    register_plugin,
    ComponentInfo,
    ConfigField,
    EventType,
    MaiMessages,
)
from src.plugin_system.base.component_types import CustomEventHandlerResult
from src.common.logger import get_logger

logger = get_logger("repeat_plugin")

# ---------------- Napcat 配置 ----------------
NAPCAT_HOST = "127.0.0.1"
NAPCAT_PORT = 4999

def send_group_msg(group_id: int, text: str):
    """通过 Napcat HTTP API 发送群消息"""
    try:
        conn = http.client.HTTPConnection(NAPCAT_HOST, NAPCAT_PORT, timeout=5)
        payload = json.dumps({
            "group_id": int(group_id),
            "message": [
                {
                    "type": "text",
                    "data": {"text": text}
                }
            ]
        })
        headers = {"Content-Type": "application/json"}
        conn.request("POST", "/send_group_msg", body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        logger.info(f"[repeat_plugin] Napcat返回: {data}")
        conn.close()
    except Exception as e:
        logger.error(f"[repeat_plugin] 发送群消息失败: {e}")

# ---------------- 工具函数 ----------------
def _safe_str(x) -> str:
    return str(x) if x is not None else ""

def _dig(obj, path: str, default=None):
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return default
        if hasattr(cur, seg):
            cur = getattr(cur, seg)
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return default
    return cur

def _first_text(*vals) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""

# ---------------- 复读处理器 ----------------
class RepeatHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "repeat_handler"
    handler_description = "检测群聊中连续重复消息并进行复读"

    chat_history: Dict[str, deque] = {}
    last_repeated_message: Optional[str] = None

    async def execute(self, message: MaiMessages | None) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        # 获取 debug 模式和复读概率配置
        debug_mode: bool = self.get_config("repeat.debug_mode", False)
        repeat_probability: float = self.get_config("repeat.repeat_probability", 1.0)
        skip_probability: float = self.get_config("repeat.skip_probability", 0.0)

        if debug_mode:
            logger.info("[repeat_plugin][repeat_handler] execute 被调用")

        if message is None:
            if debug_mode:
                logger.info("[repeat_plugin][repeat_handler] message is None")
            return True, True, None, None, None

        # 获取群 ID
        group_id = _first_text(
            _dig(message, "message_base_info.group_id"),
            _dig(message, "group_id"),
            _dig(message, "ctx.group_id"),
            _dig(message, "context.group_id")
        )
        if not group_id:
            if debug_mode:
                logger.info("[repeat_plugin][repeat_handler] group_id 未获取到")
            return True, True, None, None, None
        group_id = _safe_str(group_id)

        # 获取消息文本
        text = _first_text(
            _dig(message, "processed_plain_text"),
            _dig(message, "message_content"),
            _dig(message, "content"),
            _dig(message, "message_base_info.content"),
            _dig(message, "raw_message"),
            _dig(message, "text"),
        )
        if not text:
            if debug_mode:
                logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 消息文本为空，跳过")
            return True, True, None, None, None
            
        # 使用正则表达式来匹配并丢弃通知消息
        notice_pattern = r'"post_type"\s*:\s*"notice"'
        if re.search(notice_pattern, text):
            if debug_mode:
                logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 消息为通知事件，已丢弃")
            return True, True, None, None, None
            
        # 丢弃特殊格式消息（如图片、表情等）
        if text.startswith("[CQ:"):
            if debug_mode:
                logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 消息为特殊格式，跳过")
            return True, True, None, None, None

        # 不复读机器人自己消息
        is_self = getattr(message, "is_self", False)
        if is_self:
            # 如果本次消息是机器人上次复读的消息，则清除记录，避免连续复读
            if text == self.last_repeated_message:
                self.last_repeated_message = None
            if debug_mode:
                logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 消息来自机器人自己，跳过")
            return True, True, None, None, None

        # 初始化队列
        if group_id not in self.chat_history:
            self.chat_history[group_id] = deque(maxlen=3)
        history = self.chat_history[group_id]

        if debug_mode:
            logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 当前消息队列={list(history)}")

        # 检测连续重复消息并进行概率判断
        reply_text = None
        if len(history) >= 2:
            if history[-1] == history[-2] == text:
                # 满足复读条件后，先进行不复读的概率判断
                if random.random() <= skip_probability:
                    if debug_mode:
                        logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 满足复读条件，但通过跳过概率检查，本次不复读。")
                    # 直接返回，不进行任何复读操作
                    history.append(text)
                    return True, True, None, None, None

                # 如果没有跳过，再进行复读概率判断
                if random.random() <= repeat_probability:
                    reply_text = text
                elif debug_mode:
                    logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 满足复读条件，但未通过复读概率检查。")

        # 准备复读，并避免复读与上次相同的消息
        if reply_text and reply_text != self.last_repeated_message:
            # 移除 @机器人 部分
            pattern = r'@<([^:]+?):\d+>'
            reply_text_cleaned = re.sub(pattern, r'@\1', reply_text)
            
            send_group_msg(int(group_id), reply_text_cleaned)
            
            if debug_mode:
                logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 复读消息: {reply_text_cleaned}")
            
            # 记录本次复读的消息，以避免下次复读同一条
            self.last_repeated_message = reply_text
        
        # 更新消息队列
        history.append(text)
        if debug_mode:
            logger.info(f"[repeat_plugin][repeat_handler] 群={group_id} 消息队列更新后={list(history)}")

        return True, True, None, None, None

# ---------------- 插件注册 ----------------
@register_plugin
class RepeatPlugin(BasePlugin):
    plugin_name: str = "repeat_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "repeat": "复读功能配置",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="repeat_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "repeat": {
            "debug_mode": ConfigField(type=bool, default=False, description="是否开启调试模式，开启后会打印详细日志"),
            "repeat_probability": ConfigField(type=float, default=0.7, description="动态复读，就是每次复读别人的话的顺序不一样，建议0.7"),
            "skip_probability": ConfigField(type=float, default=0.1, description="完全不复读的概率 (0~1)"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (RepeatHandler.get_handler_info(), RepeatHandler),
        ]
