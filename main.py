import os

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import MessageChain

IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff")

import re
from collections import defaultdict

# ---------- 结构正则（基于此前验证过的排版规律） ----------
IMG_REF_RE     = re.compile(r'!\[[^\]]*\]\(([^()\s]+)[^)]*\)')
CAPTION_NUM_RE = re.compile(r'^\s*#{0,4}\s*(图|Fig\.?|Figure)\s*\S+\s*[-–—.．、:]')
CAPTION_LOOSE_RE = re.compile(r'^\s*[【#\s]*(图|Fig\.?|Figure)\s*[:：]')
SECTION_MARK_RE  = re.compile(r'^\s*(#+\s|\*\*|【)')
SUB_LABEL_RE     = re.compile(r'^\s*[（(]?[a-kA-K①-⑩]{1,3}[)）.．、]')
CONT_RE          = re.compile(r'[（(]\s*续\s*[)）]|continued', re.I)
FIG_NO_RE        = re.compile(r'(?:图|Fig\.?|Figure)\s*'
                              r'([0-9]{1,2}[--‐–—.．、][0-9]{1,3}'
                              r'(?:[--‐–—.．、][0-9]{1,3}){0,2})')
MAX_SOFT_CHARS   = 60     # 组内非标注文字上限
MAX_GROUP_SIZE   = 16     # 安全阀（"续"体例的疑难病例可能到 A~K）
MAX_SEND_IMAGES  = 24     # 一次最多发送张数


def norm_key(s: str) -> str:
    """规范化：剥掉所有标点/符号/空白，只留中英文和数字。
    解决书名中 · - （ ） 第x版 等写法不一致问题。"""
    return re.sub(r'[^\w\u4e00-\u9fff]+', '', s).lower()


def norm_fig(s: str) -> str:
    """统一图号写法：7-2-13 / 7．2．13 / 7—2-13b → '7-2-13'"""
    s = s.lower().replace('．', '-').replace('。', '-').replace('、', '-') \
             .replace('—', '-').replace('–', '-').replace('‐', '-')
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[a-k]$', '', s)      # 去掉尾部子图字母 5-8-8cd → 5-8-8
    s = re.sub(r'-+', '-', s).strip('-')
    return s


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

        # ---- 第二部分：md 结构索引 ----
        self.md_root = self.config.get("md_root", "") or os.path.join(
            StarTools.get_data_dir("astrbot_plugin_kb_image"), "md")

        self.blocks_of = {}    # md路径 -> [block dict]
        self.md_meta  = {}     # md路径 -> {book, title, captions}
        self.fig_index = defaultdict(list)   # 规范图号 -> [md路径]

        n_files = 0
        if os.path.isdir(self.md_root):
            for root, dirs, files in os.walk(self.md_root):
                rel_book = os.path.basename(root)
                for f in sorted(files):
                    if not f.lower().endswith(".md"):
                        continue
                    path = os.path.join(root, f)
                    try:
                        text = open(path, encoding="utf-8", errors="ignore").read()
                    except Exception as e:
                        logger.warning(f"[kb_image] 读md失败 {path}: {e}")
                        continue
                    blocks = self._parse_blocks(text)
                    stem = os.path.splitext(f)[0]
                    all_caps = " ".join(b["cap_norm"] for b in blocks)
                    self.blocks_of[path] = blocks
                    self.md_meta[path] = {
                        "book":  norm_key(rel_book),
                        "title": norm_key(stem),
                        "captions": all_caps,
                    }
                    for b in blocks:
                        for fg in b["figs"]:
                            self.fig_index[fg].append(path)
                    n_files += 1
        logger.info(f"[kb_image] md索引完成: {n_files} 个文件, "
                    f"{len(self.fig_index)} 个图号")


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

    def _parse_blocks(self, text: str) -> list:
        """把 md 解析为图片块列表。
        block = {'images':[文件名], 'caps':[题注原文], 'cap_norm':规范串,
                 'figs':['7-2-13',...]}"""
        blocks = []

        def flush(images, caps):
            if not images:
                return [], False
            figs = []
            for cl in caps:
                for fm in FIG_NO_RE.finditer(cl):
                    g = norm_fig(fm.group(1))
                    if g and g not in figs:
                        figs.append(g)
            blocks.append({
                "images": list(images),
                "caps": list(caps),
                "cap_norm": norm_key("".join(caps)),
                "figs": figs,
            })
            return [], False

        images, caps = [], []
        cont_mode = False
        soft = 0
        last_end = 0

        for m in IMG_REF_RE.finditer(text):
            gap = text[last_end:m.start()]
            last_end = m.end()

            for raw in gap.splitlines():
                ln = raw.strip()
                if not ln:
                    continue
                if CAPTION_NUM_RE.match(ln) or CAPTION_LOOSE_RE.match(ln):
                    if CONT_RE.search(ln):           # "（续）"并入当前组
                        caps.append(ln)
                        cont_mode = True
                        continue
                    images, cont_mode = flush(images, caps + [ln])  # 题注收尾
                    caps, soft = [], 0
                    continue
                if SECTION_MARK_RE.match(ln):
                    if cont_mode:                    # 续段内的 **说明** 不断组
                        caps.append(ln)
                        continue
                    images, cont_mode = flush(images, caps)
                    caps, soft = [], 0
                    continue
                if cont_mode:                        # 续段内任意文字都容忍
                    caps.append(ln)
                    continue
                short = len(ln) <= 30 and (SUB_LABEL_RE.match(ln) or len(ln) <= 12)
                soft += len(ln)
                if short and soft <= MAX_SOFT_CHARS:
                    caps.append(ln)
                    continue
                if soft > MAX_SOFT_CHARS:            # 长正文 = 边界，正文本身丢弃
                    images, cont_mode = flush(images, caps)
                    caps, soft = [], 0
            if len(images) >= MAX_GROUP_SIZE:
                images, cont_mode = flush(images, caps)
                caps, soft = [], 0
            images.append(os.path.basename(m.group(1)))

        flush(images, caps)
        return blocks


    # @filter.llm_tool(name="send_kb_image")
    # async def send_kb_image(self, event: AstrMessageEvent, image_refs: str):
    #     '''当用户想查看知识库中的案例图、影像图片、示意图时调用此工具。
    #     知识库检索到的文本中包含形如 ![](./images/xxx.jpg) 或图片 URL 的图片引用时，
    #     把所有需要发送的图片引用一次性传给本工具，多个引用之间用逗号、分号或换行分隔。
    #     用户想看多张图时必须一次调用全部传完，禁止多次调用本工具。
    #     Args:
    #         image_refs(str): 多个图片引用，用逗号/分号/换行分隔。
    #             每个引用可以是文件名、相对路径或 URL，来自知识库文本中的图片引用，例如：
    #             "./images/eb31f8365628a87e906febc58429cbaf.jpg, ./images/aa12bb34.jpg"
    #             或 "eb31f8365628a87e906febc58429cbaf.jpg; https://example.com/xxx.jpg"
    #     '''
    #     # 1. 拆分：兼容逗号、分号、中文逗号/分号、换行、以及成对引号包裹
    #     import re as _re
    #     raw_list = _re.split(r"[,;，；\n]+", image_refs)
    #     refs = [r.strip().strip("!()[]'\" ") for r in raw_list if r.strip()]

    #     # 去重且保序（防止 LLM 把同一张图传两遍）
    #     seen = set()
    #     refs = [r for r in refs if not (r in seen or seen.add(r))]

    #     if not refs:
    #         yield event.plain_result("没有解析到任何有效的图片引用，请检查传入的内容。")
    #         return

    #     # 2. 逐个解析 + 构建，收集成功与失败
    #     images = []
    #     sent = []
    #     failed = []
    #     for ref in refs:
    #         resolved = self._resolve(ref)
    #         if not resolved:
    #             failed.append(ref)
    #             logger.warning(f"[kb_image] 未找到图片: {ref}")
    #             continue
    #         try:
    #             if resolved.startswith(("http://", "https://")):
    #                 images.append(Image(url=resolved))
    #             else:
    #                 images.append(Image.fromFileSystem(resolved))
    #             sent.append(resolved)
    #         except Exception as e:
    #             failed.append(ref)
    #             logger.error(f"[kb_image] 构建图片失败 {resolved}: {e}")

    #     # 3. 一次性发送所有图片（一条消息带多个图片组件）
    #     if images:
    #         try:
    #             await event.send(MessageChain(chain=images))
    #             logger.info(f"[kb_image] 已发送 {len(images)} 张图片: {sent}")
    #         except Exception as e:
    #             logger.error(f"[kb_image] 批量发送失败: {e}")
    #             yield event.plain_result(f"图片发送失败: {e}")
    #             return

    #     # 4. 组装回传给 LLM 的结果（自定义 post_send_prompt 支持 {count} 占位符）
    #     default_prompt = (
    #         "接下来请按用户要求和人格要求进行回复，"
    #         "不要在文字中重复输出图片路径或markdown语法。"
    #     )
    #     custom_prompt = self.config.get("post_send_prompt", "").strip() or default_prompt
    #     custom_prompt = custom_prompt.replace("{count}", str(len(sent)))

    #     if failed:
    #         result_text = (
    #             f"已成功发送 {len(sent)} 张图片。以下 {len(failed)} 个引用未找到，"
    #             f"请确认它们确实来自知识库文本，不要凭空编造：{'、'.join(failed)}"
    #         )
    #     else:
    #         result_text = f"已成功发送 {len(sent)} 张图片给用户。{custom_prompt}"
    #     yield result_text

    @filter.llm_tool(name="send_kb_image")
    async def send_kb_image(self, event: AstrMessageEvent,
                               book: str = "", keywords: str = "",
                               figure_no: str = ""):
        '''当用户想查看知识库中的案例图、影像图片、示意图时调用此工具。
        知识库检索到的文本中包含形如 ![](./images/xxx.jpg) 或图片 URL 的图片引用时，
        把所有需要发送的图片引用一次性传给本工具，多个引用之间用逗号、分号或换行分隔。
        用户想看多张图时必须一次调用全部传完，禁止多次调用本工具。
        使用优先级：
        1. 已知图号时必须填 figure_no，例如知识库文本出现"图7-2-13"就传 "7-2-13" 或 "图7-2-13"。
        2. 不知图号时用 keywords 描述图片主题词（如"肝海绵状血管瘤"），可选搭配 book 书名缩小范围。
        3. 三个参数必须全要；提供越多定位越准。
        Args:
            book(str): 书名的关键部分即可，无需精确全称，如 "医学影像诊断学"、"肝胆胰脾"。需要。
            keywords(str): 图片内容的主题关键词，来自用户提问或知识库检索文本。需要。
            figure_no(str): 知识库文本中出现的编号图号，如 "7-2-13"。需要。
        '''
        candidates = []   # [(md_path, 命中blocks)]
        reason = ""

        # ---- 路1：图号精确定位 ----
        fg = norm_fig(figure_no) if figure_no else ""
        if fg and fg in self.fig_index:
            for p in self.fig_index[fg]:
                blks = [b for b in self.blocks_of[p] if fg in b["figs"]]
                candidates.append((p, blks))
            reason = f"图号 {fg}"
        else:
            nb, nk = norm_key(book), norm_key(keywords)
            if not nb and not nk:
                yield event.plain_result("请至少提供书名、关键词或图号之一。")
                return
            scored = []
            for p, meta in self.md_meta.items():
                sc = 0
                if nb and nb in meta["book"]:      sc += 3
                if nk and nk in meta["title"]:     sc += 2
                if nk and nk in meta["captions"]:  sc += 2
                if sc > 0:
                    scored.append((sc, p))
            scored.sort(reverse=True)

            if not scored:
                yield event.plain_result(
                    f"知识库中未找到与「{book} {keywords}」匹配的内容。")
                return
            top_sc, top_p = scored[0]
            if len(scored) > 1 and scored[1][0] == top_sc and not nk:
                # 只给了书名且存在多个同分文件 → 请模型澄清，列前5个
                opts = [os.path.splitext(os.path.basename(q))[0]
                        for _, q in scored[:5]]
                yield (f"找到多节内容，请补充更具体的关键词或图号再调用本工具。"
                       f"候选章节：{'；'.join(opts)}")
                return
            blks = self.blocks_of[top_p]
            if nk:
                blks = [b for b in blks if nk in b["cap_norm"]] or blks
            candidates.append((top_p, blks))
            reason = f"匹配 {os.path.basename(top_p)}"

        # ---- 展开发送（限量保护）----
        planned, shown_paths, dup = [], set(), set()
        for p, blks in candidates:
            for b in blks:
                for name in b["images"]:
                    r = self.img_index.get(name)
                    if not r:
                        logger.warning(f"[kb_image] md引用但磁盘缺失: {name}")
                        continue
                    if r in dup:
                        continue
                    dup.add(r)
                    if len(planned) < MAX_SEND_IMAGES:
                        planned.append(r)
        if not planned:
            yield event.plain_result("定位到了对应章节，但其图片未在本地索引中找到。")
            return
        try:
            chain = [Image.fromFileSystem(p) for p in planned]
            await event.send(MessageChain(chain=chain))
        except Exception as e:
            logger.error(f"[kb_image] 发送失败: {e}")
            yield event.plain_result(f"图片发送失败: {e}")
            return

        n_total = len(dup)
        note = (f"，另有 {n_total - len(planned)} 张超出单次上限未发送"
                if n_total > len(planned) else "")
        default_prompt = ("接下来请按人格要求结合本次发送的图片进行解说，"
                          "不要输出图片路径或markdown语法。")
        cp = self.config.get("post_send_prompt", "").strip() or default_prompt
        yield (f"已依据[{reason}]发送 {len(planned)} 张图片{note}。{cp}")
