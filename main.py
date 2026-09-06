import os

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import MessageChain
import difflib

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
        # ---- 第三部分：文件名 -> 所属figure整组的反向映射（用于补齐） ----
        # ---- 第三部分：反向映射 + 题注映射 ----
        self.img_group = {}       # 仅多图块成员 -> 整组成员列表
        self.group_caption = {}   # 所有块的成员 -> 该块第一条题注（含单图块）
        for p, blks in self.blocks_of.items():
            for b in blks:
                members = b["images"]
                cap0 = b["caps"][0] if b["caps"] else ""
                if len(members) >= 2:                 # 只有 ≥2 张才需要补齐
                    for name in members:
                        merged = self.img_group.get(name, [])
                        for n in members:
                            if n not in merged:
                                merged.append(n)
                        self.img_group[name] = merged
                if cap0:
                    for name in members:              # 题注对所有块都记
                        if name not in self.group_caption:
                            self.group_caption[name] = cap0

    def _resolve(self, image_ref: str):
        """把 LLM 传来的图片引用解析成可发送的资源：
        - 远程 http(s) URL：原样返回
        - 本地 file:// 协议：去掉协议头
        - 绝对路径且存在：原样返回
        - 相对路径：在 kb_root 下解析
        - 仅文件名：用启动时建立的索引查找
        - 精确匹配失败：按子串（前缀/后缀/中间均可）唯一匹配兜底，
        再失败则按编辑距离唯一匹配兜底
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

        # ---- 1. 精确匹配 ----
        hit = self.img_index.get(filename)
        if hit:
            return hit

        stem = os.path.splitext(filename)[0].lower()

        # ---- 2. 子串兜底：片段出现在库内文件名的任意位置（前缀/后缀/中间）----
        if len(stem) >= 8:  # 太短的不猜，避免误匹配
            candidates = [
                f for f in self.img_index
                if stem in os.path.splitext(f)[0].lower()
            ]
            if len(candidates) == 1:
                logger.warning(
                    f"[kb_image] 引用 {ref} 不完整（子串匹配），"
                    f"匹配到 {candidates[0]}"
                )
                return self.img_index[candidates[0]]
            if len(candidates) > 1:
                logger.warning(
                    f"[kb_image] 引用 {ref} 子串匹配到 {len(candidates)} 个候选，"
                    f"放弃模糊匹配: {candidates}"
                )

        # ---- 3. 编辑距离兜底：处理抄错个别字符（如 l/1、O/0）----
        if len(stem) >= 16:  # 编辑距离匹配要求更长的片段，避免误匹配
            stems = [os.path.splitext(f)[0].lower() for f in self.img_index]
            close = difflib.get_close_matches(stem, stems, n=2, cutoff=0.9)
            if len(close) == 1:
                logger.warning(
                    f"[kb_image] 引用 {ref} 不完整（编辑距离匹配），"
                    f"匹配到 {close[0]}"
                )
                return self.img_index[close[0] + os.path.splitext(filename)[1]]
            if len(close) > 1:
                logger.warning(
                    f"[kb_image] 引用 {ref} 编辑距离匹配到多个候选，放弃: {close}"
                )

        return None



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


    @filter.llm_tool(name="send_kb_image")
    async def send_kb_image(self, event: AstrMessageEvent, image_refs: str):
        '''当用户想查看知识库中的案例图、影像图片、示意图时调用此工具。
        知识库检索到的文本中包含形如 ![](./images/xxx.jpg) 或图片 URL 的图片引用时，
        把手头已有的图片引用传给本工具，多个引用之间用逗号、分号或换行分隔。
        【重要】即使检索文本中只有某图的部分分图引用（同一案例的其他分图可能被
        切分到了别的文本段），也只需传现有的引用即可，本工具会自动识别并补齐
        同一案例图的其余分图。因此禁止为了"凑齐多张"而多次调用本工具。
        Args:
            image_refs(str): 多个图片引用，用逗号/分号/换行分隔。
                每个引用可以是文件名、相对路径或 URL，来自知识库文本中的图片引用，例如：
                "./images/eb31f8365628a87e906febc58429cbaf.jpg, ./images/aa12bb34.jpg"
                或 "eb31f8365628a87e906febc58429cbaf.jpg; https://example.com/xxx.jpg"
        '''
        # 1. 拆分：兼容逗号、分号、中文逗号/分号、换行、以及成对引号包裹
        import re as _re
        raw_list = _re.split(r"[,;，；\n]+", image_refs)
        refs = [r.strip().strip("!()[]'\" ") for r in raw_list if r.strip()]

        # 去重且保序（防止 LLM 把同一张图传两遍）
        seen = set()
        refs = [r for r in refs if not (r in seen or seen.add(r))]

        if not refs:
            yield event.plain_result("没有解析到任何有效的图片引用，请检查传入的内容。")
            return

        # 2. 解析 + 组展开：任何命中本地图的引用都还原为其所属figure整组
        resolved_list = []
        failed = []
        for ref in refs:
            r = self._resolve(ref)
            if not r:
                failed.append(ref)
                logger.warning(f"[kb_image] 未找到图片: {ref}")
                continue
            resolved_list.append(r)

        # ---------- 组展开：任何命中本地图的引用都还原为其所属figure整组 ----------
        expanded = []          # [(路径或URL, 文件名或None), ...] 最终要发的内容
        seen = set()           # 去重
        for r in resolved_list:
            if r.startswith(("http://", "https://")):
                if r not in seen:
                    seen.add(r)
                    expanded.append((r, None))
                continue
            name = os.path.basename(r)
            group = self.img_group.get(name) or [name]
            if len(group) > 1:
                logger.info(f"[kb_image] 单引用 {name} → 补齐为 {len(group)} 张组图")
            for n in group:
                p = self.img_index.get(n)
                if p and p not in seen:
                    seen.add(p)
                    expanded.append((p, n))

        # ---------- 截断 ----------
        total_found = len(expanded)              # 截断前的命中总数
        to_send = expanded[:MAX_SEND_IMAGES]     # 实际尝试发送的部分

        images, sent = [], []
        caps_all, cap_seen = [], set()
        for path, fname in to_send:
            try:
                if path.startswith(("http://", "https://")):
                    images.append(Image(url=path))
                else:
                    images.append(Image.fromFileSystem(path))
                sent.append(path)
            except Exception as e:
                logger.error(f"[kb_image] 构建图片失败 {path}: {e}")
                continue
            # 题注收集：只针对真正发出去的本地图，按内容去重
            if fname:
                c = self.group_caption.get(fname)
                if c:
                    key = re.sub(r"\s+", "", c)   # 按内容去重，比 id() 可靠
                    if key not in cap_seen:
                        cap_seen.add(key)
                        caps_all.append(c.strip())

        # ---------- 发送 ----------
        if images:
            try:
                await event.send(MessageChain(chain=images))
                logger.info(f"[kb_image] 已发送 {len(images)} 张图片: {sent}")
            except Exception as e:
                logger.error(f"[kb_image] 批量发送失败: {e}")
                yield event.plain_result(f"图片发送失败: {e}")
                return

        # 截断提示：命中数超过单次上限时告知模型
        note = ""
        if total_found > MAX_SEND_IMAGES:
            note = f"（另有 {total_found - MAX_SEND_IMAGES} 张因超出单次上限未发送）"

        # ---------- 回传给 LLM 的结果 ----------
        default_prompt = (
            "接下来请按用户要求和人格要求进行回复，"
            "不要在文字中重复输出图片路径或markdown语法。"
        )
        custom_prompt = self.config.get("post_send_prompt", "").strip() or default_prompt
        custom_prompt = custom_prompt.replace("{count}", str(len(to_send)))

        if failed:
            result_text = (
                f"已发送 {len(to_send)} 张图片。以下 {len(failed)} 个引用未找到，"
                f"请确认它们确实来自知识库文本，不要凭空编造：{'、'.join(failed)}"
            )
        else:
            result_text = f"已发送 {len(to_send)} 张图片给用户{note}"

        if caps_all:
            if len(caps_all) == 1:
                cap_short = caps_all[0][:80] + ("…" if len(caps_all[0]) > 80 else "")
                result_text += f"，对应知识库图注：{cap_short}"
            else:
                items = []
                for i, c in enumerate(caps_all[:6], 1):   # 最多列6条防刷屏
                    items.append(f"{i}. {c[:60]}")
                result_text += f"，共涉及 {len(caps_all)} 个图组：" + "；".join(items)

        result_text += f"。{custom_prompt}"
        yield result_text
