# **简介**

- **插件名称**: astrbot_plugin_kb_image
- **功能**: 从知识库检索结果中解析图片引用（本地相对/绝对路径或远程 URL），并通过 LLM 工具 `send_kb_image` 将图片发送给用户。

## **主要功能**

- **图片解析**: 支持 http/https、file://、绝对路径、相对路径（相对于 `kb_root`）以及仅文件名的解析。
- **索引加速**: 启动时扫描 `kb_root` 下的图片文件并按文件名建立索引，便于通过文件名快速定位图片。
- **LLM 集成**: 作为 `llm_tool` 暴露 `send_kb_image`，使用 `yield event.chain_result([...])` 返回图片并通过 `return` 回传给 LLM 一个提示文本，驱动 LLM 继续生成图像说明。

## **安装与启动**

- 将本文件夹放入 AstrBot 插件目录并按常规方式在 WebUI 中启用插件。
- 插件会在初始化时扫描 `kb_root`（参见下文配置），若 `kb_root` 为空则使用插件数据目录 `data/plugin_data/astrbot_plugin_kb_image/images`（由 `StarTools.get_data_dir("astrbot_plugin_kb_image")` 解析）。

## **配置**

- **kb_root**: 知识库文件根目录（包含 md 文件和 images 文件夹的公共父目录）。优先使用此绝对路径；留空时使用插件数据目录。
- **post_send_prompt**: 图片发送成功后回传给 LLM 的提示语（用于指导 LLM 接下来如何回复）。在 WebUI 中可自定义，留空时使用内置默认文案。

示例 `_conf_schema.json` 中的配置字段见 [._conf_schema.json](_conf_schema.json).

在 WebUI 的插件配置中填写 `kb_root`（例如 `E:/data/kb_docs`），或将图片直接放到插件数据目录 `data/plugin_data/astrbot_plugin_kb_image/images` 以便快速测试。

## **使用说明（与 LLM 协作）**

- 当知识库检索结果包含图片引用（例如 `![](./images/xxx.jpg)` 或直接的图片 URL）时，LLM 应提取图片引用并调用工具 `send_kb_image`，传入 `image_ref`（可以是文件名、相对路径或 URL）。
- 插件会尝试按如下顺序解析引用：
  - 远程 URL（http/https） → 直接返回
  - file:// 前缀 → 去掉前缀并返回本地路径
  - 绝对路径且存在 → 返回
  - 在 `kb_root` 下解析相对路径 → 返回
  - 仅文件名 → 使用启动时建立的索引查找
- 插件通过 `yield event.chain_result([img])` 把图片作为工具结果返回，框架会发送图片并将 `return` 的字符串回传给 LLM，驱动 LLM 继续生成对图片的文字说明。

## **实现细节**

- 核心文件: [main.py](main.py)
- 配置模板: [._conf_schema.json](_conf_schema.json)
- 关键方法/行为:
  - `KbImagePlugin.initialize()` 扫描并建立图片索引。
  - `KbImagePlugin._resolve(image_ref)` 负责解析图片引用。
  - `send_kb_image` 为 LLM 工具，先 `yield event.chain_result([img])` 发送图片，再 `return` 指令文本（来自 `post_send_prompt` 或内置默认）。

**示例流程**

1. LLM 根据知识库文本找到图片引用 `./images/abc.jpg`。
2. LLM 调用工具 `send_kb_image`，传入 `image_ref = "./images/abc.jpg"`。
3. 插件解析到本地文件并通过 `yield event.chain_result([img])` 返回图片；随后 `return` 一个提示文本给 LLM（由 `post_send_prompt` 决定）。
4. LLM 接收到工具结果后，继续生成针对图片的文字说明（注意不要重复输出图片路径或 markdown 语法）。

**注意事项**

- 如果 WebUI 中 `kb_root` 未配置，请把图片放在插件数据目录 `data/plugin_data/astrbot_plugin_kb_image/images`，或在插件配置中设置 `kb_root` 为知识库根目录。
- `post_send_prompt` 留空和不存在的语义已在插件中明确：留空将使用内置默认提示，避免工具返回空字符串导致框架认为“无返回值”。

**联系与贡献**

- 代码位于 [main.py](main.py)，欢迎提交 issue 或 PR 改进索引、支持更多图片来源或增强错误处理。
