# AGENTS.md — com.fsqhn.image-searcher

给接手这个插件的编码代理看。里面是边界、架构约定和实测踩过的坑；改代码前先扫一遍，能省掉大半返工。

## 边界

- 只改本目录。不改 MaiBot 主程序（`src/`、`dashboard/`、`config/`）。
- 不用 `@Action`，只用 `@Tool` / `@Command` / `@HookHandler`。
- 任何 key、代理地址、群号一律走 `config.toml`，不要写进代码。
- `config.toml` 已 gitignore（含真实密钥），仓库只提交 `config.example.toml`。
- 部署到生产（拷贝、装依赖、重载、重启）必须先汇报并取得同意。

## 架构

```
plugin.py           配置模型 + 生命周期 + 取图 + 编排 + Tool/Command/Hook
core/engines.py     Lens / Yandex / 百度 / SauceNAO 适配 + detect_ai + verify_candidate
core/hashing.py     dHash 指纹 + prepare_upload（压到 480KB）
core/formatting.py  合并转发四节点、路线行、重放文案
core/tasks.py       SearchOutcome（含 route）、TaskStore（TTL / 上限 / 落盘恢复）
core/commands.py    指令正则构建
core/maibot_compat.py  能力返回值解包
```

**两条入口一套逻辑**：`image_search` Tool 与 `@Command` 都走 `_start_task` → `_run_task` → `_search`。不要给其中一条单独写搜索流程。

**异步任务化**：搜索 30~60 秒，Tool 立即返回 `task_id`，不能 await 到结果。

**指纹而不是视觉模型**：判断「是不是同一张图」用 dHash。别引入视觉模型依赖，MaiBot 默认 `visual=false`，而且模型给的相似度不可靠。

**图片字节自给自足**：引擎都上传图片字节，不依赖 QQ 图链时效；只有 Lens 上传失败才回退图链。

**分流靠结果不靠输入**：不判断输入是照片还是绘制（像素统计实测分不开），用 SauceNAO 相似度 ≥ `route_similarity` 反推是插画，再切引擎顺序。

**配置热更新有限**：`self.config` 会热更新，但 `@Command` 的 `pattern` 是模块导入时生成的。改 `command` / `aliases` 必须重载插件。

**用户可见文案不要写死插件名**：转发标题、补图提示、「更多」提示统一用 `COMMAND`（来自配置），只有 `_manifest.json`、包名和 README 用固定的「麦麦寻人」。标题里写死会导致用户把指令改成别的名字后，卡片还顶着旧名字。

## 平台事实（2026-10-22 实测）

| 事实 | 影响 |
|---|---|
| **消息入库有延迟**：消息刚到时 `message.get_by_id` 常返回 `None`，几秒后同样的 ID 就能查到 | 必须挂 `chat.receive.before_process` 钩子先缓存图片二进制，否则取图失败 |
| **`get_by_id` 的 `chat_id` 是 `session_id`（哈希）**，指令侧拿到的 `stream_id` 未必是同一形式 | 传错的 `chat_id` 会静默返回 `None`；先不带 `chat_id` 查，失败再带 |
| **图片消息的 `processed_plain_text` 可能是空字符串**（VLM 没出描述时） | 靠「整条消息只有 `[…]`」的正则接图会漏掉，要用钩子接 |
| Command 传入的 `message` 是 `include_binary_data=False` 的 | 图片没有 base64；但钩子缓存的副本有 |
| 钩子的 `stream_id` 参数可能是空的 | 从 `message['session_id']` 兜底 |
| 图片段的 base64 键名是 `binary_data_base64` | 兼容 `base64` / `image_base64` / `file_base64` |
| `ctx.send.forward` 节点是 `{"nickname": ..., "content": [{"type": "text"/"image", "content": ...}]}` | `content` 由 Host 转成 `binary_data_base64` |
| `ctx.send.text(..., set_reply=True)` 失败时只返回 `{"success": false}`，不抛异常 | 要检查返回值并回落普通发送，否则用户什么都看不到 |
| 能力返回值被 SDK 解包：`message.get_by_id` → 消息字典本身，`message.get_recent` → list | 判断 `success` 字段一定失败，统一走 `unwrap_capability` |
| 指令匹配用 `compiled_pattern.search(text)` | 带图消息的文本是 `[image] 麦麦找鸡`，`^麦麦找鸡$` 会漏；用 `core/commands.py` 的 `build_command_pattern()` |
| SerpAPI 的 `thumbnail` 在 gstatic，国内不可达；`image` 才是推文原图 | 验证与配图都用 `image` |
| SerpAPI Image API 上限 500KB | `prepare_upload` 压到 480KB |
| Yandex 国内直连返回「under construction」，走代理 + `yandex.ru` 正常 | hosts 顺序 `yandex.ru → yandex.com.tr → yandex.com`；读超时要重试一次 |
| SauceNAO 匿名 API 返回 403 | 无 key 时自动跳过，不当成错误；免费档 `short_limit=4`、每天 100 次 |
| 百度 `image_src` 是 `w=889&h=500` 裁剪图 | 真实匹配常落「疑似」（距离 10~14），不要为此放宽 `hash_threshold` |
| 部分 CDN 直接封服务器 IP：`cdn.donmai.us` 返回 403，`lookaside.instagram.com` 返回的不是图片 | 只能标「未验证」放参考区；换 UA / Referer / 第三方图片代理都绕不过 |
| Sightengine 单次 `genai` 计 5 ops | 只在低置信分支调用 |

## 踩过的坑

1. **像素统计判不了「照片 vs 绘制」**：试过平坦区、颜色多样性、饱和度、边缘锐度、噪声自相关、频谱斜率六组特征，全部无法分开。反例：纹理插画平坦区 1.4%，大片天空照片 79%。已删掉 `core/imagetype.py`，不要重造。
2. **指令正则不要全文锚定**：`^麦麦找鸡$` 匹配不到 `[image] 麦麦找鸡`。
3. **不要抓 x.com 取账号**：要登录还要跑 JS。handle 从结果 URL 或标题里的 `@handle` 取。
4. **不要把「疑似」当最佳匹配**：只有 `status=match` 才算 best，否则会给错账号。
5. **插画模式曾经整条没图**：最佳匹配落在 Yandex，图在 `avatars.mds.yandex.net` 走代理超时。现在代理失败会自动直连，最佳匹配图失败会并发取后面 4 条候选图，全失败退回用户原图并注明。
6. **等图窗口要按「会话 + 用户」配对**：群里可能几个人同时在等图，不要只按会话匹配。
7. **`enabled=false` 要下沉到 `_start_task`**：只挡 Tool 不挡 Command 会漏。
8. **Tool 与 Command 的 `user_id` 不一致**：查最近任务要 `latest_for` 失败后 `latest_in_stream` 兜底。
9. **没匹配上不等于不发图**：早前 `_best_image` 与 `build_forward_nodes` 都以 `best is not None` 为前提，`not_found` 时整条转发一张图都没有，用户没法核对。现在 best 为空也会取候选里第一张能下载的图，挂在参考区并注明「未经指纹确认」；候选全失败退回原图。改配图逻辑时这两处要一起看。

## 排障顺序

1. 插件没加载：看 `_manifest.json` 是否合法、`create_plugin()` 是否存在、生命周期方法是否齐全。
2. 指令没反应：确认 `command` 与配置一致（改过要重载），确认正则容忍占位符。
3. 一直「没看到图片」：看日志有没有「已缓存入站图片」。没有就是钩子没跑到；有缓存还失败，看「按消息 ID 没有取回消息体」。
4. 结果全是「未验证」：候选图下载失败，检查代理配置与 `download_timeout`；403 属于对端封 IP，无法绕过。
5. 引擎全失败：看任务文件里 `summary.errors` 的具体 HTTP 状态；Yandex / SerpAPI 先怀疑代理。
6. 追问「更多」没结果：任务过期（`task_ttl_minutes`）或插件进程重启过（内存任务表重建）。「更多」出来的转发没图时，先看原结果状态：`not_found` 早前就是不带图的，现在会给参考区补一张，若仍无图说明候选图全部下载失败，看日志里的「候选图下载失败」。

## 代理配置

只有一个代理设置：`[plugin] proxy`，引擎、候选图下载、AI 检测全走它。写 `direct` / `none` / `off` 表示强制直连，留空等同直连。已去掉引擎段里的 proxy 与 `image_proxy`，不要再加回来。

四个引擎是顶层配置段 `[lens]` / `[yandex]` / `[baidu]` / `[saucenao]`，与 `[ai_detect]` 平级。**不要**改回 `[engines.lens]` 这种二阶嵌套：SDK 只把顶层配置拆成独立区块，二阶嵌套在 WebUI 里会渲染成 `[object Object]`，用户根本没法编辑。

## 测试

```bash
python -m unittest discover -s tests -v
```

新增引擎适配、改阈值、改指纹逻辑时补用例。改动 `_verify` / `_rank` / 取图链路后，用真实图跑一次端到端，确认「匹配 → 最佳匹配 → 带图」不断。

改完 `plugin.py` 必须重载验证：日志里应出现 `麦麦寻人已加载：… 上限=…`，且无 traceback。注意改源码会重启插件进程，内存里的任务表会重建。改 `command` / `aliases` 同理，必须重载才生效。

## 发布前检查

- `config.toml` 不要提交；确认仓库里只有 `config.example.toml`。
- 仓库内不要留 `plugin.py.upload_tmp`、`__pycache__`、日志、任务数据。
- 不要在 README / AGENTS / 代码里出现真实 key、代理 IP、群号。
- 下载远程图片必须先过 `httpclient.is_public_url`：群里构造的图片段能把 URL 指向本机或云元数据地址，别绕开这道校验。
- 解码前先看像素数（`hashing.MAX_PIXELS`）：小体积不等于小像素，纯色长图能几百 KB 解出上亿像素。
- 不要提交个人环境的定制值（例如改过的 `command`、`nickname`、代理地址）；仓库只保留默认的「麦麦寻人」与示例配置。
- **截图必须脱敏**：群名、昵称、QQ 头像、对方账号、露骨标题都要遮盖或打码；真人裸露照片不要放原图，README 里的效果图优先用虚构字段的示意图（`assets/preview-result.png` 就是脚本生成的）。
- 提交前扫一遍：`grep -rn "@[A-Za-z0-9_]\{2,\}" README.md AGENTS.md` 与 `grep -rn "127.0.0.1\|OnlyFans" README.md`。
