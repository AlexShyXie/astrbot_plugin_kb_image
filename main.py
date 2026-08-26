import os

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image

IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff")


@register("astrbot_plugin_kb_image", "YourName", "知识库图片发送工具", "1.0.0")
class KbImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 知识库文件根目录（所有 md 和 images 文件夹的公共父目录）
        self.kb_root = config.get("kb_root", "")
        self.img_index = {}  # 文件名 -> 绝对路径

    async def initialize(self):
        if not self.kb_root:
            # 未配置时，默认使用插件数据目录下的 images 文件夹：
            # data/plugin_data/astrbot_plugin_kb_image/images
            self.kb_root = os.path.join(
                StarTools.get_data_dir("astrbot_plugin_kb_image"), "images"
            )
        if not os.path.isdir(self.kb_root):
            # 自动创建默认图片目录，方便直接把图片上传进去
            os.makedirs(self.kb_root, exist_ok=True)
            logger.info(f"[kb_image] 已创建默认图片目录: {self.kb_root}")
        # 扫描知识库目录，按文件名建索引（epub 内容哈希文件名全局唯一）
        count = 0
        for root, _, files in os.walk(self.kb_root):
            for f in files:
                if f.lower().endswith(IMG_EXTS):
                    self.img_index[f] = os.path.join(root, f)
                    count += 1
        logger.info(f"[kb_image] 图片索引完成，共 {count} 张")

    def _resolve(self, image_ref: str):
        """把 LLM 传来的图片引用解析成可发送的资源：
        - 远程 http(s) URL：原样返回
        - 本地 file:// 协议：去掉协议头
        - 绝对路径且存在：原样返回
        - 相对路径：在 kb_root 下解析
        - 仅文件名：用启动时建立的索引查找
        """
        ref = image_ref.strip().strip("!()[]'\" ")
        if ref.startswith(("http://", "https://")):
            return ref
        if ref.startswith("file://"):
            return ref[len("file://"):]
        if os.path.isabs(ref) and os.path.exists(ref):
            return ref
        if self.kb_root:
            candidate = os.path.join(self.kb_root, ref.lstrip("./\\"))
            if os.path.exists(candidate):
                return candidate
        filename = os.path.basename(ref)
        return self.img_index.get(filename)

    @filter.llm_tool(name="send_kb_image")
    async def send_kb_image(self, event: AstrMessageEvent, image_ref: str):
        '''当用户想查看知识库中的案例图、影像图片、示意图时调用此工具。
        知识库检索到的文本中包含形如 ![](./images/xxx.jpg) 或图片 URL 的图片引用时，
        提取其中的图片文件名或 URL 传给本工具即可发送。需要发送多张图时，逐个调用本工具。
        Args:
            image_ref(str): 图片文件名、相对路径或 URL，来自知识库文本中的图片引用，
                例如 "./images/eb31f8365628a87e906febc58429cbaf.jpg"、
                "eb31f8365628a87e906febc58429cbaf.jpg" 或 "https://example.com/xxx.jpg"
        '''
        resolved = self._resolve(image_ref)
        if not resolved:
            yield event.plain_result(
                f"未找到图片: {image_ref}，请确认该文件名或 URL 确实来自知识库检索到的文本。"
            )
            return
        try:
            if resolved.startswith(("http://", "https://")):
                img = Image(url=resolved)
            else:
                img = Image.fromFileSystem(resolved)
            # 通过 yield 返回图片，框架会自动发送并回传工具结果给 LLM
            yield event.chain_result([img])
            logger.info(f"[kb_image] 已发送图片: {resolved}")
            # 关键：返回给 LLM 的结果，驱动它解说（可在 WebUI 中自定义）
            prompt = self.config.get("post_send_prompt", "") or (
                "图片已成功发送给用户。接下来请按用户要求和人格要求进行回复"
                "不要在文字中重复输出图片路径或markdown语法。"
            )
            return prompt

        except Exception as e:
            logger.error(f"[kb_image] 发送失败 {resolved}: {e}")
            yield event.plain_result(f"图片发送失败: {e}")
