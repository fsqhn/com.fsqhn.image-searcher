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
plugin.py              配置模型 + 生命周期 + 取图 + 编排 + Tool/Command/Hook
core/engines.py        Lens / Yandex / 百度 / SauceNAO 适配 + detect_ai + verify_candidate + ENGINES_VERSION
core/hashing.py        dHash 指纹 + prepare_upload（压到 480KB）
core/formatting.py     合并转发节点、路线行、重放文案
core/extract.py        平台识别、账号页判定与归一、账号去重
core/resultcache.py    按图缓存（纯函数 + 原子落盘）
core/vision.py         视觉复核：拼对照图、写 prompt、解析回复（纯函数，不碰网络）
core/grok.py           Grok 兜底：写 prompt、拼请求体、解析帖子/账号链接（纯函数，不碰网络）
core/fxtwitter.py      推文 ID 解析、fxtwitter 返回解析、核对判定 hit/miss/unknown（纯函数，不碰网络）
core/tasks.py          SearchOutcome（含 route / cached）、TaskStore（TTL / 上限 / 落盘恢复）
core/commands.py       指令正则 + split_force（「重搜」解析）
core/httpclient.py     会话与图片下载
core/maibot_compat.py  能力返回值解包
```

**两条入口一套逻辑**：`image_search` Tool 与 `@Command` 都走 `_start_task` → `_run_task` → `_search`。不要给其中一条单独写搜索流程；`force`（重搜）也要从两条入口一起传下去。

**异步任务化**：搜索 30~60 秒，Tool 立即返回 `task_id`，不能 await 到结果。

**指纹是主路，视觉只兜底**：判断「是不是同一张图」仍然只看 dHash。只有整轮一个 `match` 都没有、`[vision] enabled` 开着、且不是插画路线时，才把目标图和最接近的候选拼成一张编号网格交给 `vlm` 任务，问的是「有没有同一个人」。命中项标 `status="vision"`，文案必须写「视觉复核」。拼图、prompt、解析在 `core/vision.py`（纯函数，有单测）。

提示词不能要求必须露脸——很多目标是只有腿、背影或局部裁切的图。门槛是「个体化证据」：脸、纹身、痣、手型、饰品、图案位置这类属于具体某个人的细节；只能说出角色名、同款服装、发色、体型、姿势的，一律返回空数组。拼图 4 列 × 320，格子小到只能认角色就是这套机制失效的根因。

**Grok 兜底与视觉并行，分两档**：同一个触发条件、`[grok] enabled` 开着且有 `api_key` 时，两边一起发出去（`asyncio.gather`），各自超时。发给它的是原图 + 前 8 条候选（`账号｜平台｜标题`），让它先核对候选里谁是发布者，都对不上才自己另搜（`core/grok.py` 的 `build_hints()`，日志里有 `候选=`）。判据只看返回链接：带 `status/<id>` 算「找到」，只有账号主页算「线索」。只给账号时会用 `cdn.syndication.twimg.com` 确认账号真实存在，确认不了整条丢掉。

**视觉命中还要过闸门**：判定集中在 `core/vision.py` 的 `verdict_of()`，检查顺序有意义，别调换——先 `VERDICT_COMMERCE`（候选落在速卖通 / 阿里 / 淘宝 / 1688 / Amazon / eBay 这类电商站，只是同款衣服），再 `VERDICT_UNTRACEABLE`（整条不可追踪：不是社交平台、也没有账号），最后 `VERDICT_NO_ACCOUNT`（落在抖音、小红书这类平台页面上，却解析不出发布者，点进去也确认不了是谁发的）。只有 `VERDICT_VISION` 能当结论，后三档一律降级成 `suspect` 只进参考区，日志记 `降级=N`，说明文案在 `DOWNGRADE_NOTES`。视觉命中发出去时，结论必须带免责声明，文案在 `core/formatting.py` 的 `_best_block()`。

**命中时首次回复只给结论**：只要 `status == "ok"`（指纹、视觉、Grok 任一命中），`build_forward_nodes` 与 `build_text_summary` 都不列参考区；没找到时才把参考区发出来。改配图或转发结构时注意这条，别让命中场景又刷回四五条。

**「更多」靠任务编号定位**：首次回复的页脚必须带 `task_id`，用户回「更多 <task_id>」重发那一次；不带编号退回本会话最近一次。结果默认保留 1 天（`task_ttl_minutes`），过期后带编号再问要明确回「缓存已清，重新搜」，不能装作没看见。页脚在「更多」那行之后还要给一条「重搜」示例（发图时带上「指令 重搜」），缓存秒回时用户才知道怎么强制重跑。`TaskStore._evict` 的 80 条上限也是为这个 TTL 服务的。

**按图缓存挡在引擎前面**：`_search()` 一进来就按「`prepare_upload` 后的图片 sha256 + `ENGINES_VERSION`」查缓存，命中直接返回，一个引擎都不调，标题标「缓存」、页脚写明复用。规则：

- **只缓存 `status == "ok"` 且有 best 的结果**。没找到的不缓存——实测同一张图过半天就能搜到，存下来只会挡住后来出现的原帖。Grok 单独给出帖子的那种「命中」也不缓存（脱离 Grok 只剩空结论）。
- 缓存里不存 Grok 的结论：它依赖当天 X 上的内容。
- 改动结果解析、候选字段含义时必须把 `core/engines.py` 的 `ENGINES_VERSION` 加一，否则旧缓存会带着旧字段回来。
- 想无视缓存：`force=True`。指令侧由 `core/commands.py` 的 `split_force()` 解析「重搜 / 重新搜 / 刷新 / 强制 / force / fresh」，等待补图窗口会把标记存进 `_pending_force` 再带到任务里；Tool 侧是 `image_search(force=true)`。
- 纯函数与落盘在 `core/resultcache.py`，有单测。

**账号只认账号页，且同一个号只报一次**：链接先过 `match_account()`（`core/extract.py`），`x.com/intent`、`weibo.com/status`、`instagram.com/p/`、`pixiv.net/artworks` 在 `ACCOUNT_PATH_DENY` 里，一律不当账号，退回标题里的 `@handle`。排序之后过 `dedupe_accounts()`，按「平台 + 账号」只留质量最好的那条（状态优先、其次距离），位置按第一次出现。pixiv `member.php?id=` 归一成 `/users/<id>`。

**Grok 给的帖子链接必须过指纹复核**：Grok 会给出对不上的链接，甚至编一条，光有 `x.com/.../status/...` 不能当结论。`_verify_tweet()` 用 `api.fxtwitter.com`（免登录，走 `[plugin] proxy`）拉那条帖子的配图，跟原图算 dHash：

- `hit`（距离 ≤ `hash_threshold`）：才算真找到，理由里带上距离。
- `miss`：有账号就降成「线索」，但**帖子链接保留**，文案里标成「存疑参考（配图相似但非同图，可能是同一套的另一张，请自行核对）」——账号通常是对的，只是那一组图里没有用户发的那张；同景同角色换个人拍，指纹也是这样，所以不能升级成「找到」。没账号才整条丢掉、清空 `post_url`。此时 `reference_url` 记下核对时距离最近的那张配图，结果首图就用它（用户可以直接肉眼比），下载不到才退回原图。
- `unknown`（图没下到、解不出指纹）：保持原样，**不要**当 `miss` —— 图床一次 403 不能把真结果判死。判定逻辑在 `core/fxtwitter.py::judge()`，有单测。

**Grok 一次给出多条帖子就逐条核**：它常常一口气列好几条看着像的帖子，只核第一条的话，它挑错一条整单就废了。`_verify_tweet()` 按顺序跑，最多 `_GROK_MAX_POSTS`（8）条，命中立刻停；全都没命中时用 `grok.pick_best_check()` 挑距离最近的一条当「存疑参考」。上限取 8 不是 5：实测一条真原帖在 Grok 的枚举结果里正好排第 5，卡 5 条太险。`x.com/i/status/<id>` 这种没有作者名的链接也算候选，别过滤掉。改这里别退回只取第一条。

**`unknown` 要重核一次**：配图偶发下载失败会让距离变成 -1，同一个帖子第一次 `unknown`、重跑一次就是 `hit`（实测过）。批量核对时漏掉一条就全废，所以 `_verify_tweet()` 里 `unknown` 且没有明确错误时再跑一次。

**参考图按指纹距离挑，并把距离写进文案**：核对全 miss 或整单没找到时，参考图取「候选里跟原图最接近的一张」（`_best_image()`，按距离而非引擎顺序），Grok 有 `reference_url` 时优先用它。图注必须带上距离（`_reference_distance()`，如「指纹距离 24，不是同一张」）——不写距离，用户会把一张毫不相干的配图当成结果。用户明确要求保留图：「有个图至少有个谱，没图连谱都没了」，所以别改成只给链接。

**`x.com/i/status/<id>` 是有用的链接**：这种 X 内部跳转链接没有作者名，但 status id 是真的，fxtwitter 能顺着它解析出真实作者。`find_posts()` 只对这一种保留（handle 记空），别的保留路径（`home`、`search` 等）照样丢。核对成功后要把解析出来的账号回填到 `info["handle"]`，否则 `miss` 分支会因为「没账号」把帖子整个扔掉。

**`xid` 指令是同一套核对的入口**：`麦麦寻人 xid <推文链接或 ID>`，图和指令发在同一条消息里，直接回「对得上 / 对不上 / 没法比对」+ 作者 + 链接。它只做核对，不搜索，不进任务表。

**图片字节自给自足**：引擎都上传图片字节，不依赖 QQ 图链时效；只有 Lens 上传失败才回退图链。

**分流靠结果不靠输入**：不判断输入是照片还是绘制，用 SauceNAO 相似度 ≥ `route_similarity` 反推是插画，再切引擎顺序。

**分流在验证之前**：`_sauce_route()` 只看相似度、不下载图片，所以在 `_verify()` 之前调用。判成插画后不再逐张比对：其他引擎各留 `drawing_other_limit` 条参考、状态保持 `unverified`，最佳匹配从 SauceNAO 的 `match` 里挑，并优先 pixiv 主站（`is_pixiv_platform()`）。

**取数分两档**：`deep_scan=false` 时每引擎取 `max_results` 条、验证 `verify_top_n`（Lens 2N）、「更多」展示 `more_limit`；`deep_scan=true` 时每引擎取到 `deep_scan_limit`、验证 `deep_verify_top_n`、「更多」展示 `deep_more_limit`。改取数只动这一处，别在引擎里另加截断。

**比对必须用原图**：Yandex 的 `thumb` 是裁成 148×90 的方框，裁剪会把 dHash 打乱。`to_yandex_candidate()` 优先取 `originalImage.url`，没有才回退 `thumb` 并在 note 里标明。新增引擎照这个规矩来。

**照片路线优先核对社交平台**：`_verify()` 里按每引擎配额取完候选后，还要把配额之外的社交平台候选（`is_social_platform()`）补进验证队列，上限 `social_verify_limit`，开关 `social_verify_all`。原帖基本都在这些平台，按固定条数截断最容易正好漏掉。

**配置热更新有限**：`self.config` 会热更新，但 `@Command` 的 `pattern` 是模块导入时生成的。改 `command` / `aliases` 必须重载插件。

**用户可见文案不要写死插件名**：转发标题、补图提示、「更多」提示统一用 `COMMAND`（来自配置），只有 `_manifest.json`、包名和 README 用固定的「麦麦寻人」。

**SauceNAO 库策略**：`db` 是全量库，`db_strict` 是收紧库，`db_policy_enabled` 开关（默认开启），`db_full_groups` 名单，`db_policy_invert` 反转。选库逻辑在 `core/engines.py` 的 `resolve_saucenao_db()`，纯函数、有独立用例（`tests/test_dbpolicy.py`）。开关关闭时一律 `db`；开着时名单上的群走 `db`、其他群走 `db_strict`，**名单为空等于全部收紧**，别把「名单空回退全量」加回去。

**群号到会话 ID 的映射**：入站钩子里 `_remember_group()` 把 `stream_id` 和群号记到内存表（`_stream_groups`），`_group_of()` 反查。注意这张表是内存态，重载后为空。

## 平台事实（2026-10-22 实测）

| 事实 | 影响 |
|---|---|
| **消息入库有延迟**：消息刚到时 `message.get_by_id` 常返回 `None`，几秒后同样的 ID 就能查到 | 必须挂 `chat.receive.before_process` 钩子先缓存图片二进制 |
| **`get_by_id` 的 `chat_id` 是 `session_id`（哈希）** | 传错的 `chat_id` 会静默返回 `None`；先不带 `chat_id` 查，失败再带 |
| **图片消息的 `processed_plain_text` 可能是空字符串**（VLM 没出描述时） | 靠「整条消息只有 `[…]`」的正则接图会漏，要用钩子接 |
| Command 传入的 `message` 是 `include_binary_data=False` 的 | 图片没有 base64；钩子缓存的副本有 |
| 钩子的 `stream_id` 参数可能是空的 | 从 `message['session_id']` 兜底 |
| 图片段的 base64 键名是 `binary_data_base64` | 兼容 `base64` / `image_base64` / `file_base64` |
| `ctx.send.forward` 节点是 `{"nickname": ..., "content": [{"type": "text"/"image", "content": ...}]}` | `content` 由 Host 转成 `binary_data_base64` |
| `ctx.send.text(..., set_reply=True)` 失败只返回 `{"success": false}`，不抛异常 | 要检查返回值并回落普通发送 |
| 能力返回值被 SDK 解包 | 统一走 `unwrap_capability` 判断 |
| 指令匹配用 `compiled_pattern.search(text)` | 用 `core/commands.py` 的 `build_command_pattern()`，它容忍前置/嵌套占位符 |
| SerpAPI 的 `thumbnail` 在 gstatic，国内不可达；`image` 才是推文原图 | 验证与配图都用 `image` |
| SerpAPI Image API 上限 500KB | `prepare_upload` 压到 480KB |
| Yandex 国内直连返回「under construction」，走代理 + `yandex.ru` 正常 | hosts 顺序 `yandex.ru → yandex.com.tr → yandex.com`；读超时重试一次 |
| SauceNAO 匿名 API 返回 403 | 无 key 时自动跳过；免费档 `short_limit=4`、每天 100 次 |
| 百度 `image_src` 是 `w=889&h=500` 裁剪图 | 真实匹配常落「疑似」（距离 10~14），不要为此放宽 `hash_threshold` |
| 部分 CDN 直接封服务器 IP（`cdn.donmai.us` 403、`lookaside.instagram.com` 返回非图片） | 只能标「未验证」放参考区 |
| Sightengine 单次 `genai` 计 5 ops | 只在低置信分支调用 |

## 踩过的坑

1. **像素统计判不了「照片 vs 绘制」**：试过六组特征全部无法分开。已删掉 `core/imagetype.py`，不要重造。
2. **指令正则不要全文锚定**：`^麦麦寻人$` 匹配不到 `[image] 麦麦寻人`；引用消息时占位符还会嵌套一层方括号；图直接发在指令前面时，图片段在纯文本里是空的，只剩前导空格（` 麦麦寻人`），所以开头要允许 `\s*`。
3. **不要抓 x.com 取账号**：要登录还要跑 JS。handle 从结果 URL 或标题里的 `@handle` 取。
4. **不要把「疑似」当最佳匹配**：只有 `status=match`（以及视觉/Grok 命中）才算 best。
5. **插画模式曾经整条没图**：现在代理失败会自动直连，最佳匹配图失败会并发取后面 4 条候选图，全失败退回用户原图并注明。
6. **等图窗口要按「会话 + 用户」配对**，重搜标记 `_pending_force` 也跟着这个 key 走。
7. **`enabled=false` 要下沉到 `_start_task`**：只挡 Tool 不挡 Command 会漏。
8. **Tool 与 Command 的 `user_id` 不一致**：查最近任务要 `latest_for` 失败后 `latest_in_stream` 兜底。
9. **没匹配上不等于不发图**：`not_found` 时也要取候选里跟原图指纹最近的那张（`_best_image()` 按距离排序，不是按引擎顺序），挂在参考区并在图注里写出距离。
10. **单条合并转发最多放 6 张图**：实测 6 张能过、13 张被拒（错误信息为空）。「更多」在图片超过 6 张时按引擎拆多条，每条带 `1/N` 页码，最佳匹配只在第一批、页脚只在最后一批。
11. **候选图下载要分线路**：`httpclient.download_routes()` 按主机决定顺序（Yandex 系直连优先，其余代理优先），`download_image_multi()` 负责回退。新增引擎别忘了走 multi 版本。
12. **展示按引擎分组，配图分两档**：首次回复每个引擎只贴最接近那张，回「更多」时每个候选都贴图，图压到 60KB 左右。`outcome.images` 只在内存、不落盘。
13. **vlm 组里的模型都能读图，但推理型的会截断正文**：`dots3-note-prev` 把 600 输出额度全花在 reasoning 上、`content` 为空。所以 `[vision] model` 默认留空、按任务组顺序挑；真想点名就填 `Qwen/Qwen3-VL-32B-Instruct`。测多模态一定要用真实尺寸的图。
14. **视觉调用要传 `task_name`**：`model` 传任务名会被当成具体模型名。正确写法 `ctx.call_capability("llm.generate", prompt=..., task_name="vlm", ...)`，图片走 Data URL。
15. **Grok 读水印会读错，别拿它当 OCR**：判定只认返回链接，账号存在性靠 `_account_exists()` 兜。
16. **Grok 只能走 `/v1/responses` + `tools:[{"type":"x_search"}]`**：`chat/completions` 带 `search_parameters` 会被打回 410；图片只认 data URL。一次完整检索约 48 秒，服务端抖动时超时不能省。
17. **拼图要逐张解码**：`vision.build_grid` 是解码→缩放→贴画布→丢引用，`MAX_SOURCE_PIXELS` 是兜底，别去掉。
18. **缓存别存「没找到」**：实测同一张图 12 小时后就能搜到，存下来反而挡住后来的原帖。
19. **Grok 给的链接不是证据**：它会把别人的帖子当答案，也可能报一个格式正确但内容无关的 status。必须用 fxtwitter 拉配图比指纹再定档；`unknown` 不能当 `miss`。
20. **xid 只在有图时才有意义**：图和指令不在同一条消息时不要进等待窗口（那会被当成普通搜图），直接提示重新发。
21. **dHash 的 8×8 别再加密**：同一张图的两个来源（orig 与 webp large），8 格距离 0、16 格 3、32 格 14——32 格已经超过阈值 8，等于把同一张图判成对不上。加密放大的还有裁切、白边、缩放位移。当前那些距离 13/24 的误判不是分辨率不够，加密只会把它们拉得更开。
22. **pHash 不用换**：实测同图的 webp 版 dHash 0 / pHash 0，粉色床单那张 dHash 24 / pHash 24，判别结果一模一样；耗时也都是 38ms（解码 37.6ms，哈希本身 0.5ms，服务器 2 核）。想再准只能换判据模型，或者在文案上说清「dHash 只负责同一张图」。
23. **视觉提示词拦不住「认衣服」**：门槛改成「个体化证据」后，模型照样把「刺绣细节」「袜口褶皱」当证据，理由是换了词、判断没变。真正起效的是分辨率：同一批 13 张候选，5 列 × 224 挑 6 条，4 列 × 320 只挑 1 条。所以别改回 5 列 / 224；也**别用减少候选数换分辨率**——候选是按距离截断的，砍掉后半段就是把真命中扔了。
24. **QQ 转发压缩不改变指纹**：同一张原图压到 JPEG q55、缩到 720 宽、转 WebP q70，距离全是 0；加 20px 白边是 5、裁 5% 是 4。所以看到距离 13、24 就是「真不是同一张」，别拿压缩当借口放宽 `hash_threshold`。
25. **别给「同角色 / 同款服装 / 同发色」当证据**：实测这几种理由撑起了绝大多数视觉误判。视觉只做兜底，`hash_threshold` 也不因为它远就放宽——dHash 距离和「是不是同一个人」没有对应关系，按距离阈值切档是伪精度。

## 排障顺序

1. 插件没加载：看 `_manifest.json` 是否合法、`create_plugin()` 是否存在、生命周期方法是否齐全。
2. 指令没反应：确认 `command` 与配置一致（改过要重载），确认正则容忍占位符。
3. 一直「没看到图片」：看日志有没有「已缓存入站图片」；有缓存还失败，看「按消息 ID 没有取回消息体」。
4. 结果全是「未验证」：候选图下载失败，检查代理与 `download_timeout`；403 属于对端封 IP。
5. 引擎全失败：看任务文件里 `summary.errors` 的 HTTP 状态；Yandex / SerpAPI 先怀疑代理。
6. 追问「更多」没结果：任务过期（`task_ttl_minutes`）或插件重启过（内存任务表重建）。
7. 结果秒回但明显是旧的：那是按图缓存命中，日志里有「按图缓存命中」；要新结果用「重搜」。

## 代理与配置段

只有一个代理设置：`[plugin] proxy`，引擎、候选图下载、AI 检测全走它。写 `direct` / `none` / `off` 表示强制直连，留空等同直连。已去掉引擎段里的 proxy 与 `image_proxy`，不要再加回来。

四个引擎是顶层配置段 `[lens]` / `[yandex]` / `[baidu]` / `[saucenao]`，与 `[vision]` / `[grok]` / `[ai_detect]` 平级。**不要**改回 `[engines.lens]` 这种二阶嵌套：SDK 只把顶层配置拆成独立区块，二阶嵌套在 WebUI 里会渲染成 `[object Object]`。

`0.5.5` 把 `vision_*` / `grok_*` 从 `[plugin]` 拆成了 `[vision]` / `[grok]`（字段名去掉前缀，`fallback` 改名 `enabled`）。老配置靠 `core/config.py` 的 `migrate_legacy_sections()` 走 `normalize_plugin_config` 一次性搬过去，只在目标键还不存在时写入，别删掉这段，否则已有用户的 Grok key 会丢。

## 测试

```bash
python -m unittest discover -s tests -v
```

目前 227 项。新增引擎适配、改阈值、改指纹逻辑、改缓存或账号归一规则时补用例。改动 `_verify` / `_rank` / 取图链路后，用真实图跑一次端到端，确认「匹配 → 最佳匹配 → 带图」不断。

改完 `plugin.py` 必须重载验证：日志里应出现 `麦麦寻人已加载：… 上限=…` 与 `vX.Y.Z 加载成功`，且无 traceback。改源码会重启插件进程，内存里的任务表会重建。

## 发布前检查

- **`host_application.max_version` 不能写 999.999.999**：插件市场的校验规则是前两位不能为 99 或 999，写错不会报错，只会把 Release 静默记进 `plugin_versions.json` 的 `rejected_releases`，市场索引永远停在最后一个通过的版本。现在写 `1.9.999`（第三位不限）。改完拿市场仓库的 `.github/scripts/validate_manifest.cjs` 跑一遍 `collectManifestErrors(manifest, {historical:false})`，确认零错误再发版。
- `config.toml` 不要提交；确认仓库里只有 `config.example.toml`。
- 仓库内不要留 `plugin.py.upload_tmp`、`__pycache__`、日志、任务数据与缓存文件（`image_searcher_cache.json`）。
- 不要在 README / AGENTS / 代码里出现真实 key、代理 IP、群号。
- 下载候选图统一走 `download_image_multi`，并且先过 `httpclient.is_public_url`。
- **取图窗口要能解释清楚**：模型侧优先按 `msg_id` 取图，其次引用，最后才是「最近 3 分钟内的图」；超窗时 `_resolve_image` 带回过期秒数，由 `stale_image_hint()` 告诉模型传 msg_id。
- 解码前先看像素数（`hashing.MAX_PIXELS`）：**不要改 `Image.MAX_IMAGE_PIXELS`**，那是 PIL 的模块级全局，会连累同一进程里的其他插件。
- 不提交个人环境定制值（改过的 `command`、`nickname`、代理地址、`db_full_groups` 里的真实群号）。
- **截图必须脱敏**：群名、昵称、头像、账号、露骨标题都要遮盖；真人裸露照片不要放原图，效果图优先用虚构字段的示意图。
- 提交前扫一遍：`grep -rn "@[A-Za-z0-9_]\{2,\}" README.md AGENTS.md` 与 `grep -rn "127.0.0.1\|OnlyFans" README.md`。
