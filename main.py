"""
Agnes AI 图片生成插件
====================
基于 Agnes AI API (agnes-image-2.1-flash) 的文生图/图生图插件。
生成图片后自动发送到聊天，支持合并转发（QQ）和直接发送。
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import List, Optional

import aiohttp

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat import MessageChain
from core.chat.message_elements import Image
from core.provider import LLMRequest
from core.utils.path_utils import get_data_path


# ─── 风格提示词映射 ─────────────────────────────────────────────

STYLE_PROMPTS: dict[str, str] = {
    "anime": "anime illustration style, cel shading, soft lighting, highly detailed, vibrant colors",
    "realistic": "photorealistic, cinematic lighting, 8k resolution, highly detailed, sharp focus, professional photography",
    "oil_painting": "oil painting style, visible brush strokes, classical art composition, rich texture and depth",
    "watercolor": "watercolor painting, soft edges, flowing colors, artistic, delicate washes, paper texture",
}

# ─── 可用尺寸 ────────────────────────────────────────────────────

SIZE_OPTIONS: list[str] = [
    "1024x1024",
    "768x1024",
    "1024x768",
    "1024x576",
    "576x1024",
]

SIZE_LABELS: dict[str, str] = {
    "1024x1024": "正方形",
    "768x1024": "竖图",
    "1024x768": "横图",
    "1024x576": "宽屏",
    "576x1024": "手机竖屏",
}

STYLE_LABELS: dict[str, str] = {
    "anime": "动漫",
    "realistic": "写实",
    "oil_painting": "油画",
    "watercolor": "水彩",
}


class AgnesImageGenPlugin(BasePlugin):
    """Agnes AI 图片生成插件

    通过 Agnes AI API 生成高质量图片，支持：
    - 文生图：纯文本描述生成图片
    - 图生图：基于参考图片 URL 生成变体
    - 多种风格：动漫 / 写实 / 油画 / 水彩
    - 多种尺寸：正方形 / 竖图 / 横图 / 宽屏 / 手机竖屏
    - 发送模式：合并转发（QQ）/ 逐张直接发送
    """

    # ── 生命周期 ─────────────────────────────────────────────────

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # API 设置
        api_sec = cfg.get("section_api", {})
        self.api_key: str = api_sec.get("api_key", "")
        self.api_base: str = api_sec.get(
            "api_base", "https://apihub.agnes-ai.com/v1/images/generations"
        )
        self.model: str = api_sec.get("model", "agnes-image-2.1-flash")
        self.timeout: int = max(5, min(300, api_sec.get("timeout", 120)))

        # 生成设置
        gen_sec = cfg.get("section_generation", {})
        self.default_size: str = gen_sec.get("default_size", "1024x1024")
        if self.default_size not in SIZE_OPTIONS:
            self.default_size = "1024x1024"
        self.default_style: str = gen_sec.get("default_style", "anime")
        if self.default_style not in STYLE_PROMPTS:
            self.default_style = "anime"
        self.max_count: int = max(1, min(10, gen_sec.get("max_count", 4)))

        # 自我形象参考图：优先用插件配置，未配置则自动读取 KiraAI 系统设置
        self.selfie_image_path: str = gen_sec.get("selfie_image_path", "")
        if not self.selfie_image_path:
            try:
                kira_selfie = (
                    self.ctx.config.get("bot_config", {})
                    .get("selfie", {})
                    .get("path", "")
                )
                if kira_selfie and kira_selfie != "None":
                    self.selfie_image_path = str(kira_selfie)
                    logger.info(
                        "[agnes_image_gen] 自动读取 KiraAI 系统设置中的"
                        f" Bot 角色形象参考图: {self.selfie_image_path}"
                    )
            except Exception:
                pass

        # 发送设置
        send_sec = cfg.get("section_sending", {})
        self.send_as_forward: bool = send_sec.get("send_as_forward", True)
        self.image_storage_dir: str = send_sec.get("image_storage_dir", "files/agnes")
        self.save_generated: bool = send_sec.get("save_generated", True)
        self.max_cache_files: int = send_sec.get("max_cache_files", 100)
        self.proxy: str = send_sec.get("proxy", "")

        self._storage_dir: Optional[Path] = None

    async def initialize(self):
        """初始化存储目录，验证配置，清理缓存"""
        data_dir = Path(get_data_path())
        self._storage_dir = data_dir / self.image_storage_dir
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        if not self.api_key:
            logger.warning(
                "[agnes_image_gen] API Key 未配置，请在插件设置中填写 Agnes AI 密钥"
            )
        else:
            selfie_info = (
                f", 角色形象参考图={'已配置' if self.selfie_image_path else '未配置'}"
            )
            logger.info(
                "[agnes_image_gen] 已就绪 "
                f"(模型={self.model}, 默认尺寸={self.default_size}, "
                f"默认风格={self.default_style}, 合并转发={self.send_as_forward}"
                f"{selfie_info})"
            )

        await self._cleanup_cache()

    async def terminate(self):
        """清理资源（无持久连接需关闭）"""
        pass

    # ── 缓存管理 ─────────────────────────────────────────────────

    async def _cleanup_cache(self):
        """清理超出上限的旧缓存文件"""
        if self.max_cache_files <= 0 or not self._storage_dir:
            return
        try:
            files = sorted(
                self._storage_dir.glob("agnes_*.png"),
                key=lambda f: f.stat().st_mtime,
            )
            excess = len(files) - self.max_cache_files
            if excess > 0:
                for f in files[:excess]:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                logger.info(f"[agnes_image_gen] 清理了 {excess} 个过期缓存文件")
        except Exception as e:
            logger.warning(f"[agnes_image_gen] 缓存清理失败: {e}")

    # ── Agnes AI API 调用 ────────────────────────────────────────

    # 重试配置
    _API_MAX_RETRIES: int = 3
    _API_RETRY_DELAY: float = 5.0  # 秒

    async def _call_agnes_api(
        self,
        prompt: str,
        size: str = "1024x1024",
        n: int = 1,
        reference_image_url: Optional[str] = None,
    ) -> Optional[List[str]]:
        """调用 Agnes AI 图片生成 API（带自动重试）

        Args:
            prompt: 图片描述提示词（英文）
            size: 图片尺寸
            n: 生成数量
            reference_image_url: 参考图片（URL 或本地文件路径，图生图模式）

        Returns:
            生成的图片 URL 列表，失败返回 None
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "n": n,
        }

        if reference_image_url:
            ref_url = await self._resolve_reference_image(reference_image_url)
            if ref_url:
                payload["extra_body"] = {
                    "image": [ref_url],
                }

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        kwargs_base = {"headers": headers, "json": payload, "timeout": timeout}
        if self.proxy:
            kwargs_base["proxy"] = self.proxy

        last_error = ""
        for attempt in range(1, self._API_MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_base, **kwargs_base) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            urls = [
                                item["url"]
                                for item in result.get("data", [])
                                if item.get("url")
                            ]
                            if not urls:
                                logger.error(
                                    "[agnes_image_gen] API 返回中没有图片 URL"
                                )
                            return urls if urls else None

                        error_text = await resp.text()
                        logger.warning(
                            f"[agnes_image_gen] API 返回 {resp.status} "
                            f"(第 {attempt}/{self._API_MAX_RETRIES} 次): "
                            f"{error_text[:200]}"
                        )

                        # 4xx 客户端错误不重试（401/403/400 等是参数或认证问题）
                        if 400 <= resp.status < 500 and resp.status != 429:
                            logger.error(
                                f"[agnes_image_gen] 客户端错误 {resp.status}，"
                                f"不重试: {error_text[:200]}"
                            )
                            return None

                        # 429 限流 / 5xx 服务端错误 → 等待后重试
                        if attempt < self._API_MAX_RETRIES:
                            delay = self._API_RETRY_DELAY * attempt
                            logger.info(
                                f"[agnes_image_gen] "
                                f"{'限流' if resp.status == 429 else '服务端异常'}，"
                                f"{delay:.0f} 秒后重试..."
                            )
                            await asyncio.sleep(delay)
                            continue

                        last_error = f"HTTP {resp.status}: {error_text[:200]}"
                        return None

            except asyncio.TimeoutError:
                logger.warning(
                    f"[agnes_image_gen] 请求超时 "
                    f"(第 {attempt}/{self._API_MAX_RETRIES} 次)"
                )
                if attempt < self._API_MAX_RETRIES:
                    await asyncio.sleep(self._API_RETRY_DELAY)
                    continue
                last_error = "请求超时"
                return None

            except aiohttp.ClientError as e:
                logger.warning(
                    f"[agnes_image_gen] 网络异常 (第 {attempt}/{self._API_MAX_RETRIES} 次): {e}"
                )
                if attempt < self._API_MAX_RETRIES:
                    await asyncio.sleep(self._API_RETRY_DELAY)
                    continue
                last_error = f"网络错误: {e}"
                return None

            except Exception as e:
                logger.error(f"[agnes_image_gen] API 调用异常: {e}")
                return None

        logger.error(
            f"[agnes_image_gen] 重试 {self._API_MAX_RETRIES} 次后仍失败: {last_error}"
        )
        return None

    async def _resolve_reference_image(self, reference: str) -> Optional[str]:
        """解析参考图片来源，支持 URL 和本地文件路径

        本地文件路径会被自动解析为绝对路径并转为 base64 data URL，
        以兼容 Agnes API 的 image 参数。

        Returns:
            可用的 URL 字符串，解析失败返回 None
        """
        if not reference:
            return None

        # 已经是 URL，直接返回
        if reference.startswith(("http://", "https://", "data:")):
            return reference

        # 本地文件路径：解析并转为 base64 data URL
        try:
            ref_path = Path(reference)
            # 相对路径 → 尝试相对 data/ 目录解析
            if not ref_path.is_absolute():
                data_dir = Path(get_data_path())
                ref_path = data_dir / reference

            if not ref_path.is_file():
                logger.warning(
                    f"[agnes_image_gen] 参考图文件不存在: {ref_path}"
                )
                return None

            # 读取并转换为 base64 data URL
            img_data = ref_path.read_bytes()
            suffix = ref_path.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(suffix, "image/png")
            b64 = base64.b64encode(img_data).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
            logger.info(
                f"[agnes_image_gen] 本地参考图已转换: {ref_path} "
                f"({len(img_data)} bytes)"
            )
            return data_url
        except Exception as e:
            logger.error(f"[agnes_image_gen] 参考图解析失败: {e}")
            return None

    # ── 图片下载 ─────────────────────────────────────────────────

    async def _download_image(self, url: str, filename: str) -> Optional[str]:
        """下载单张图片到本地存储目录

        Returns:
            本地绝对路径，失败返回 None
        """
        timeout = aiohttp.ClientTimeout(total=60)
        kwargs: dict = {"timeout": timeout}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, **kwargs) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"[agnes_image_gen] 下载失败 {resp.status}: {url[:100]}"
                        )
                        return None

                    data = await resp.read()
                    local_path = self._storage_dir / filename
                    local_path.write_bytes(data)
                    return str(local_path.absolute())
        except asyncio.TimeoutError:
            logger.error(f"[agnes_image_gen] 下载超时: {url[:100]}")
            return None
        except Exception as e:
            logger.error(f"[agnes_image_gen] 下载异常: {e}")
            return None

    # ── Session ID 构造 ──────────────────────────────────────────

    @staticmethod
    def _get_sid(event: KiraMessageBatchEvent) -> str:
        """从批量事件中提取 session_id

        格式: {adapter_name}:{session_type}:{target_id}
        例: qq:gm:123456789 或 qq:dm:987654321
        """
        if event.sid:
            return event.sid

        adapter = event.adapter.name if event.adapter else "qq"

        if event.messages:
            last_msg = event.messages[-1]
            if hasattr(last_msg, "group") and last_msg.group:
                return f"{adapter}:gm:{last_msg.group.group_id}"
            else:
                sender_id = (
                    last_msg.sender.user_id if hasattr(last_msg, "sender") and last_msg.sender else "0"
                )
                return f"{adapter}:dm:{sender_id}"

        return f"{adapter}:dm:0"

    # ── 消息发送 ─────────────────────────────────────────────────

    async def _send_image_directly(
        self, event: KiraMessageBatchEvent, local_path: str
    ) -> bool:
        """通过 MessageChain 直接发送单张图片"""
        try:
            sid = self._get_sid(event)
            img = Image(local_path, caption="")
            chain = MessageChain([img])
            result = await self.ctx.message_processor.send_message_chain(sid, chain)
            return result.ok
        except Exception as e:
            logger.error(f"[agnes_image_gen] 直接发送失败: {e}")
            return False

    async def _send_forward_images(
        self, event: KiraMessageBatchEvent, local_paths: List[str]
    ) -> bool:
        """以合并转发形式发送多张图片（仅 QQ 平台）

        参考 pixiv_image_searcher 插件的实现模式。
        非 QQ 平台或不支持时返回 False，调用方应回退到直接发送。
        """
        # 平台筛选：合并转发仅 QQ 支持
        if event.adapter.platform != "QQ":
            logger.debug("[agnes_image_gen] 非 QQ 平台，跳过合并转发")
            return False

        sid = self._get_sid(event)
        try:
            parts = sid.split(":")
            if len(parts) < 3:
                return False
            adapter_name = parts[0]
            session_type = parts[1]
            target_id = parts[2]
        except (ValueError, IndexError):
            return False

        if session_type not in ("gm", "dm"):
            return False

        # 获取 QQ 客户端
        try:
            adapter_inst = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if not adapter_inst:
                logger.error(f"[agnes_image_gen] 未找到适配器: {adapter_name}")
                return False
            client = adapter_inst.get_client()
            if not client:
                logger.error(f"[agnes_image_gen] 适配器 {adapter_name} 无可用客户端")
                return False
        except Exception as e:
            logger.error(f"[agnes_image_gen] 获取适配器失败: {e}")
            return False

        # 获取机器人信息
        adapter_config = adapter_inst.config if hasattr(adapter_inst, "config") else {}
        bot_nick = (
            adapter_config.get("nickname", "")
            or adapter_config.get("bot_name", "")
            or "Kira"
        )

        if event.messages:
            last_msg = event.messages[-1]
            self_id = str(last_msg.self_id) if last_msg.self_id else str(event.self_id)
        else:
            self_id = str(event.self_id) if hasattr(event, "self_id") else ""

        # 构造合并转发节点
        nodes = []
        for path in local_paths:
            abs_path = os.path.abspath(path)
            nodes.append({
                "type": "node",
                "data": {
                    "name": bot_nick,
                    "uin": self_id,
                    "content": [
                        {"type": "image", "data": {"file": abs_path}}
                    ],
                },
            })

        # 发送合并转发消息
        try:
            if session_type == "gm":
                await client.send_action(
                    "send_forward_msg",
                    {"group_id": int(target_id), "messages": nodes},
                )
            else:
                await client.send_action(
                    "send_forward_msg",
                    {"user_id": int(target_id), "messages": nodes},
                )
            logger.info(
                f"[agnes_image_gen] 合并转发成功 ({len(local_paths)} 张) -> {sid}"
            )
            return True
        except Exception as e:
            logger.error(f"[agnes_image_gen] 合并转发失败: {e}")
            return False

    # ── 下载 + 发送（统一入口）──────────────────────────────────

    async def _download_and_send(
        self, event: KiraMessageBatchEvent, image_urls: List[str]
    ) -> List[str]:
        """下载图片并发送到聊天

        流程: 逐张下载 → 按配置选择发送方式 → 返回成功路径列表
        """
        local_paths: List[str] = []

        for i, url in enumerate(image_urls):
            timestamp = int(time.time() * 1000)
            filename = f"agnes_{timestamp}_{i}.png"
            local_path = await self._download_image(url, filename)
            if local_path:
                local_paths.append(local_path)
            else:
                logger.warning(f"[agnes_image_gen] 第 {i + 1} 张图片下载失败")

        if not local_paths:
            return []

        # 选择发送方式
        if self.send_as_forward and len(local_paths) > 1:
            ok = await self._send_forward_images(event, local_paths)
            if not ok:
                logger.info("[agnes_image_gen] 合并转发失败，回退到逐张直接发送")
                for path in local_paths:
                    await self._send_image_directly(event, path)
        else:
            for path in local_paths:
                await self._send_image_directly(event, path)

        # 不保留则删除本地文件
        if not self.save_generated:
            for path in local_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

        return local_paths

    # ── LLM 工具 ─────────────────────────────────────────────────

    @register.tool(
        name="agnes_image_gen",
        description=(
            "通过 Agnes AI 生成高质量图片并自动发送到聊天。"
            "当用户要求生成图片、画图、AI 绘图、文生图、图生图时调用此工具。"
            "支持多种风格（动漫/写实/油画/水彩）和多种尺寸。"
            "如果用户提供了参考图片的 URL，使用 reference_image_url 参数进入图生图模式。"
            "如果用户要求看 Bot 角色自身的形象图/自拍（如「你长什么样」「发张自拍」「看看你的样子」），"
            "将 use_selfie 设为 true，工具会自动使用 Bot 角色自身的形象参考图进行图生图。"
            "图片生成和发送全自动完成，你只需告知用户结果即可，不要再用 <file> 标签发图。"
        ),
        params={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "详细的英文图片描述提示词。"
                        "如果用户用中文描述，务必翻译并扩写成富有细节的英文提示词，"
                        "包括构图、风格、光照、色彩、背景等要素。"
                        "默认风格为动漫插画。"
                        "如果是生成 Bot 角色自身的形象图，用 'the character' 指代角色，"
                        "不要描述外貌特征（参考图已有）。"
                    ),
                },
                "size": {
                    "type": "string",
                    "description": (
                        "图片尺寸。可选: 1024x1024(正方形), 768x1024(竖图), "
                        "1024x768(横图), 1024x576(宽屏), 576x1024(手机竖屏)。"
                        "默认 1024x1024。"
                    ),
                    "default": "1024x1024",
                },
                "style": {
                    "type": "string",
                    "description": (
                        "图片风格。可选: anime(动漫), realistic(写实), "
                        "oil_painting(油画), watercolor(水彩)。默认 anime。"
                    ),
                    "default": "anime",
                },
                "count": {
                    "type": "integer",
                    "description": "生成图片数量，1~4。默认 1。",
                    "default": 1,
                },
                "reference_image_url": {
                    "type": "string",
                    "description": (
                        "参考图片的 URL 地址。用于图生图模式，基于该图片生成新图片。"
                        "如果用户说「基于这张图」「图生图」「参考这张图」并提供图片，"
                        "填该图片的 URL。不填则为纯文生图。"
                        "注意：如果用户要看 Bot 角色自身的形象图/自拍，应该用 use_selfie=true 而不是传此参数。"
                    ),
                },
                "use_selfie": {
                    "type": "boolean",
                    "description": (
                        "是否使用 Bot 角色自身的形象参考图进行图生图。"
                        "当用户要求看 Bot 角色自身的图片（即「你」的图片），如说"
                        "「你长什么样」「发张自拍」「发张你的照片」「看看你的样子」"
                        "「你的二次元形象」「你换个场景/衣服看看」等时，设为 true。"
                        "设为 true 后无需再填 reference_image_url，工具会自动读取"
                        "Bot 角色的形象参考图。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    )
    async def agnes_image_gen(
        self,
        event: KiraMessageBatchEvent,
        prompt: str,
        size: str = "1024x1024",
        style: str = "anime",
        count: int = 1,
        reference_image_url: str = "",
        use_selfie: bool = False,
    ) -> str:
        """Agnes AI 图片生成工具

        由 LLM 通过 function calling 自动调用。
        生成图片 → 下载 → 发送 → 返回结果提示给 LLM。
        """
        # 配置校验
        if not self.api_key:
            return (
                "错误：Agnes AI API Key 未配置。"
                "请管理员在 KiraAI WebUI 的插件设置中填写 API Key。"
            )

        # 参数校验与回退默认值
        if size not in SIZE_OPTIONS:
            size = self.default_size
        if style not in STYLE_PROMPTS:
            style = self.default_style
        count = max(1, min(self.max_count, count))
        reference_image_url = reference_image_url.strip() or ""

        # 自我形象参考图处理
        if use_selfie:
            if not self.selfie_image_path:
                return (
                    "错误：未配置自我形象参考图。"
                    "请在 KiraAI 系统设置或插件设置中配置形象参考图路径后重试。"
                )
            reference_image_url = self.selfie_image_path

        # 构建完整提示词
        style_prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS["anime"])
        full_prompt = f"{prompt}, {style_prompt}"

        if use_selfie:
            mode_str = "角色形象图生图"
        elif reference_image_url:
            mode_str = "图生图"
        else:
            mode_str = "文生图"
        style_label = STYLE_LABELS.get(style, style)
        size_label = SIZE_LABELS.get(size, size)
        logger.info(
            f"[agnes_image_gen] 开始生成: "
            f"模式={mode_str}, 风格={style_label}, 尺寸={size_label}, "
            f"数量={count}, prompt={full_prompt[:100]}..."
        )

        # 调用 API 生成图片
        urls = await self._call_agnes_api(
            prompt=full_prompt,
            size=size,
            n=count,
            reference_image_url=reference_image_url if reference_image_url else None,
        )

        if not urls:
            return (
                "生成失败：Agnes AI API 不可用（已自动重试 3 次）。"
                "可能原因：API 队列繁忙（高峰期）、API Key 无效、账户余额不足。"
                "请告知用户「服务器繁忙，稍等 10~20 秒后再试」，不要反复立即重试。"
            )

        # 下载并发送
        sent_paths = await self._download_and_send(event, urls)

        if not sent_paths:
            return (
                "生成失败：API 返回了图片链接，但所有图片下载或发送均失败。"
                "请检查网络连接是否正常。"
            )

        # 构造返回给 LLM 的提示文本
        send_mode = (
            "合并转发"
            if (self.send_as_forward and len(sent_paths) > 1)
            else "直接发送"
        )

        paths_str = "\n".join(f"  - {p}" for p in sent_paths)
        return (
            f"已成功以「{send_mode}」发送 {len(sent_paths)} 张图片到当前聊天。\n"
            f"──────────────────────\n"
            f"模式：{mode_str}  风格：{style_label}  尺寸：{size_label}\n"
            f"──────────────────────\n"
            f"这些图片已由工具直接发送完毕，你无需再次发送，也禁止使用 <file> 标签。\n"
            f"请用中文简短告知用户图片已生成即可，不要重复描述图片内容。\n"
            f"生成的文件：\n{paths_str}"
        )

    # ── Prompt 注入 ──────────────────────────────────────────────

    @on.llm_request()
    async def inject_tool_hint(self, event, req: LLMRequest, tag_set, *_):
        """向 LLM 系统提示注入 agnes_image_gen 工具的使用说明"""
        selfie_note = ""
        if self.selfie_image_path:
            selfie_note = (
                "- **Bot 角色自身的形象参考图已配置**。"
                "用户要求看 Bot 角色自身（即「你」）的图片时，将 use_selfie 设为 true——"
                "例如用户说「你长什么样」「发张你的自拍」「看看你的样子」「你的形象图」"
                "「你换个场景/衣服/姿势」「你拍张照」等。"
                "prompt 中用 'the character' 指代 Bot 角色，不要描述外貌特征（参考图已有）。\n"
            )

        for p in req.system_prompt:
            if p.name == "tools":
                hint = (
                    "\n## agnes_image_gen - 图片生成\n"
                    '- 用户说"画一张""生图""AI画图""生成图片"等 -> 调用此工具\n'
                    "- **必须**把中文提示词翻译并扩写为详细的英文提示词（描述构图、风格、光照、色彩等）\n"
                    "- 默认风格是动漫插画，用户可指定写实/油画/水彩\n"
                    "- 如果用户提供了参考图片 URL，填到 reference_image_url 参数中（图生图）\n"
                    + selfie_note +
                    "- 图片由工具自动生成并发送到聊天，你只需回复简短确认，**严禁用 <file> 标签再次发图**\n"
                )
                p.content += hint
                break
