---
name: market-research
description: "Market research methodology — search SOP, source evaluation, data collection patterns, and verification techniques for Chinese and global markets."
tags: [kanban-harness, research, methodology, market, competitive-analysis, sop]
trigger: When conducting market research, competitive analysis, or industry data collection
---

# Market Research Methodology (市场调研方法论)

系统化的市场调研 SOP，覆盖搜索策略、信源评估、数据采集和交叉验证。

## 调研 SOP（标准流程）

### Phase 1: 定义调研范围（2 分钟）

1. 明确调研问题（竞品对比？市场规模？用户画像？技术可行性？）
2. 确定关键词集合（中英文各一组）
3. 确定目标信源类型（见下方信源优先级）
4. 设定置信度目标（需要多少独立来源交叉验证）

### Phase 2: 广度搜索（5-10 分钟）

目标：快速建立全景认知，不深入任何单一来源。

**搜索顺序：**
1. 英文关键词 → 获取全球视角、官方数据、英文报告
2. 中文关键词 → 获取国内市场数据、用户反馈、本地竞品
3. 特定站点搜索 → 针对性获取深度内容

**搜索词构造技巧：**
- 双语搜索：同一概念用中英文各搜一次（"project management SaaS" + "项目管理 SaaS"）
- `site:` 限定：`site:g2.com "project management"`, `site:zhihu.com 项目管理工具`
- 时间限定：加年份 `2024` 或 `2025` 过滤过时信息
- 排除噪音：`-site:pinterest.com -site:youtube.com` 排除低信息密度站点
- 精确匹配：用引号 `"exact phrase"` 搜索特定产品名或术语
- 竞品发现：`alternatives to [ProductName]`, `[ProductName] vs`
- 定价发现：`[ProductName] pricing 2025`, `site:producthunt.com [category]`

### Phase 3: 深度挖掘（10-20 分钟）

对 Phase 2 发现的高价值来源，用 `web_extract` 深入读取：

**工具选择决策树：**

```
需要信息 → 知道具体 URL 吗？
  ├─ 是 → 页面是静态内容吗？
  │    ├─ 是 → web_extract（快速、低成本）
  │    └─ 否（JS渲染/需要交互） → browser_navigate + browser_snapshot
  └─ 否 → web_search 发现来源 → 拿到 URL 后再决定用 extract 还是 browser
```

**三种工具对比：**

| 工具 | 速度 | 适用场景 | 限制 |
|------|------|----------|------|
| `web_search` | 快（1-3s） | 发现来源、获取结果列表、快速扫描 | 只返回摘要，不返回全文 |
| `web_extract` | 中（3-10s） | 已知 URL 提取全文、结构化数据 | 无法处理 JS 渲染、登录墙、反爬 |
| `browser_*` | 慢（10-30s/步） | JS 渲染站、需要交互、需要截图证据 | 消耗资源多，每步都要等待 |

**深度挖掘目标：**
- 竞品官网：功能列表、定价页、客户案例
- 评测站点：G2/Capterra 评分、用户评价关键词
- 行业报告：市场规模数据、增长率、渗透率
- 开发者社区：GitHub stars/issues、Stack Overflow 讨论热度

### Phase 4: 交叉验证（5 分钟）

- 关键数据点需要 ≥2 个独立来源确认
- 来源之间矛盾时，标注冲突并说明各来源可信度
- 无法验证的数据标注 `[未验证]` 并说明原因

### Phase 5: 结构化输出

按调研报告模板组织产出（见 industry-advisor skill）。

## 信源优先级

| 优先级 | 来源类型 | 可信度 | 适用场景 |
|--------|----------|--------|----------|
| 1 | 行业报告（Gartner, IDC, Statista） | 高 | 市场规模、趋势、份额 |
| 2 | 官方数据（公司财报、官网、SEC filing） | 高 | 营收、用户数、定价 |
| 3 | 评测平台（G2, Capterra, ProductHunt） | 中-高 | 用户评分、功能对比、差评 |
| 4 | 技术社区（GitHub, HN, Reddit, V2EX） | 中 | 开发者态度、技术评价 |
| 5 | 媒体报道（TechCrunch, 36kr, InfoQ） | 中 | 融资、战略动向 |
| 6 | 社区讨论（知乎、Twitter/X、论坛） | 低-中 | 用户痛点、口碑 |
| 7 | SEO 内容（CSDN 搬运、营销软文） | 低 | 仅作参考，不作为依据 |

### 信源可信度判断规则

- **高可信度**：有明确数据来源、可追溯、有方法论说明
- **中可信度**：来自知名平台但无法追溯原始数据
- **低可信度**：匿名发布、无来源标注、明显营销导向
- **不可用**：自相矛盾、明显过时（>2年）、孤证无法验证

## 调研类型模板

### A. 竞品分析

```
搜索策略：
1. "[产品名] alternatives 2025"
2. "site:g2.com [category]" → 获取 top 10 列表
3. 逐个竞品：官网定价页 + G2 评分页 + GitHub（如开源）

输出表格：
| 竞品 | 定价 | 核心功能 | G2评分 | 差评关键词 | 技术栈 | 目标用户 |

关键指标：
- 定价区间和计费模式
- 功能差异矩阵（有/无/部分）
- 用户评分分布（不只看均分，看 1-2 星评价内容）
- 差评高频词（反映真实痛点）
```

### B. 市场规模估算

```
搜索策略：
1. "[market] market size 2024 2025" → 找行业报告摘要
2. "site:statista.com [keyword]" → 找统计数据
3. 上下游推算：用户基数 × n
输出格式：
- TAM（总可寻址市场）
- SAM（可服务市场）
- SOM（可获得市场）
- 数据来源和计算方法
- 置信度评估
```

### C. 用户画像调研

```
搜索策略：
1. "[产品类别] user persona" / "[产品类别] 用户画像"
2. 竞品的客户案例页面
3. 社区讨论：谁在用、为什么用、痛点是什么

输出格式：
- 主要用户群体（角色、公司规模、行业）
- 使用场景和工作流
- 核心痛点排序
- 付费意愿和决策因素
```

### D. 技术可行性调研

```
搜索策略：
1. GitHub 搜索相关项目：stars、最近更新、issue 活跃度
2. "[技术方案] production experience" / "[技术] 生产环境"
3. Stack Overflow 相关问题数量和解决率

输出格式：
- 技术方案对比矩阵
- 社区成熟度评估
- 已知坑和限制
- 学习曲线评估
```

## 搜索引擎特性

### SearXNG（默认后端）
- 聚合多引擎结果，去重排序
- 支持 Bing、Baidu、Sogou、360、Wikipedia、GitHub
- 中文搜索质量依赖 Bing CN 和百度

### 搜索失败处理
- 搜索无结果 → 换关键词（更宽泛或更具体）
- 搜索结果质量差 → 加 `site:` 限定到高质量站点
- 连续 3 次失败 → 停止搜索，用已有信息产出，标注 `[数据不足]`

## 工具使用详解

### web_search — 发现阶段的主力

**用途：** 搜索引擎查询，返回结果列表（标题+摘要+URL）

**最佳实践：**
- 每次搜索用不同角度的关键词，不要重复相似查询
- 英文搜索获取全球数据，中文搜索获取国内市场信息
- 搜索结果的摘要往往就够判断来源价值，不必每个都深入读取
- 一个调研主题最多搜索 5-8 次，超过说明关键词策略有问题

**搜索词模式：**
```
竞品发现：  "[产品名] alternatives" / "[产品名] vs" / "[类别] tools 2025"
定价情报：  "[产品名] pricing plans" / "site:producthunt.com [产品名]"
市场数据：  "[行业] market size TAM 2024" / "[行业] growth rate forecast"
用户反馈：  "[产品名] review complaints" / "site:g2.com [产品名]"
技术评估：  "[技术] production experience" / "[框架] pros cons"
开源情报：  "site:github.com [关键词] stars:>1000"
```

### web_extract — 深度阅读的利器

**用途：** 给定 URL，提取页面全文内容（markdown 格式）

**适用场景：**
- 竞品官网的功能页、定价页（大多是静态 HTML）
- 博客文章、技术文档、新闻报道
- GitHub README、issue 讨论
- 行业报告的公开摘要页

**不适用场景（需要升级到 browser）：**
- 返回内容为空或只有导航框架 → 页面是 JS 渲染的 SPA
- 返回 403/429 → 有反爬保护
- 需要点击"展开更多"、翻页、切换 tab 才能看到完整内容
- 需要登录才能查看的内容

**使用技巧：**
- 先用 web_search 拿到 URL 列表，挑 2-3 个最有价值的用 web_extract 深入
- 不要对每个搜索结果都 extract，只对高价值来源深入
- 如果 extract 返回内容很短或结构破碎，说明需要用 browser

### browser — 重型武器，按需启用

**用途：** 完整的浏览器自动化，可以渲染 JS、点击、滚动、填表、截图

**核心工具链：**
```
browser_navigate(url)     → 打开页面
browser_snapshot()        → 获取当前 DOM 结构（文本形式）
browser_vision(question)  → 视觉理解页面内容（截图+AI分析）
browser_click(ref)        → 点击元素
browser_scroll(direction) → 滚动页面
browser_type(ref, text)   → 输入文本
browser_press(key)        → 按键（Tab, Enter 等）
browser_cons     → 查看控制台输出
```

**必须用 browser 的场景：**

| 场景 | 原因 | 典型站点 |
|------|------|----------|
| SPA/JS 渲染 | web_extract 拿不到内容 | 知乎、小红书、Product Hunt |
| 需要交互 | 点击展开、翻页、筛选 | G2 评价列表、Capterra 对比 |
| 动态加载 | 滚动加载更多内容 | Twitter/X、Reddit |
| 反爬严格 | 需要真实浏览器指纹 | Glassdoor、LinkedIn |
| 需要视觉证据 | 截图作为调研附件 | 竞品 UI、定价截图 |

**browser 调研模式（标准流程）：**

```
1. browser_navigate(url) — 打开目标页面
2. browser_snapshot() — 获取 DOM，理解页面结构
3. 判断：内容是否已经足够？
   ├─ 是 → 提取数据，结束
   └─ 否 → 需要交互
4. browser_vision(question="页面上有哪些可交互元素？", annotate=true)
   → 获取带标注的截图，识别按钮/链接/tab
5. browser_click(ref="@eN") — 点击需要的元素
6. browser_snapshot() — 获取交互后的新内容
7. 重复 3-6 直到获取足够数据
```

**browser 使用纪律：**
- 每个页面最多 5 步交互，超过说明该页面不适合自动化采集
- 不要在一个站点停留超过 3 分钟，避免触发反爬
- 优先用 `browser_snapshot()` 获取文本，只在需要视觉判断时用 `browser_vision()`
- 如果页面需要登录，停止并标注 `[需要登录，无法获取]`，不要尝试绕过

**browser 用于竞品 UI 截图：**
```
browser_navigate(url="https://competitor.com/features")
browser_vision(question="截图这个页面的功能列表区域")
→ 截图可作为调研附件上传
```

### 工具升级路径

当低成本工具失败时，按以下路径升级：

```
web_search（发现来源）
    ↓ 找到有价值的 URL
web_extract（提取内容）
    ↓ 内容为空/不完整/被拦截
browser_navigate + snapshot（JS 渲染）
    ↓ 需要交互才能看到内容
browser_click/scroll/type（交互操作）
    ↓ 仍然无法获取
标注 [无法获取] + 原因，继续下一个来源
```

**关键原则：不要跳级。** 能用 web_extract 解决的不要上 browser，能用 web_search 摘要判断的不要 extract 全文。每次升级都有时间成本。

### 工具调用预算

单次调研任务的工具调用上限：

| 工具 | 建议上限 | 硬上限 |
|------|----------|--------|
| web_search | 5-8 次 | 12 次 |
| web_extract | 3-5 次 | 8 次 |
| browser 操作 | 10-15 步 | 25 步 |

超过建议上限时，停下来评估：是关键词策略有问题，还是这个信息确实难以获取？
超过硬上限时，必须停止并用已有信息产出，标注数据不足的部分。

## 数据可信度分级

对每个关键数据点标注可信度：

- 🟢 **已验证**：≥2 个独立来源一致
- 🟡 **单源**：仅 1 个来源，但来源可信
- 🔴 **存疑**：来源可信度低，或多源矛盾
- ⚪ **推测**：基于已有数据的合理推断，非直接证据

## 来源引用规范

调研产出中所有支撑性材料必须标注来源。按数据类型分为「必须引用」和「尽可能引用」两档：

### 必须引用（缺失则视为未验证，PM 可打回）

这些数据直接影响定价、投入、方向决策，错误代价大：

| 数据类型 | 要求 | 示例 |
|----------|------|------|
| 竞品定价 | 官网定价页 URL + 抓取日期 | `[Pricing](https://example.com/pricing) (2026-05)` |
| 下载量/排名 | App Store 页面或 SimilarWeb URL | 具体产品页链接 |
| 用户评分/差评 | Trustpilot/G2/App Store 页面 URL | 评分页直链 |
| 流量数据 | SimilarWeb/Ahrefs 截图或页面 URL | 域名分析页 |
| 融资/收购新闻 | 新闻原文链接 | TechCrunch/36kr 文章 |
| 官方功能列表 | 产品官网 URL | Features 页面 |
| 政策/合规变更 | 官方文档或法规原文链接 | Apple Developer Docs |
| API/技术规格 | 官方文档 URL | 开发者文档页 |

### 尽可能引用（有则标注，无则说明推理依据）

这些是推断性结论或来源分散的判断，给出推理链即可：

| 数据类型 | 要求 | 说明 |
|----------|------|------|
| 行业趋势判断 | 引 1-2 篇报告或文章 | 如"SEO 竞争加剧"——可引行业分析文 |
| 获客成本估算 | 说明推算逻辑 | 多信号综合推断，标注 `[推算]` |
| 技术壁垒评估 | 引技术文档或社区讨论 | 如 iOS 限制——引 Apple 文档最佳 |
| 市场份额区间 | 说明计算方法 | 逻辑链 > 单一来源，标注置信度 |
| 竞品策略推测 | 引可观察到的证据 | 如"交叉导流"——引产品页面截图 |
| 社区口碑/风评 | 引 1-2 条典型讨论 | 知乎/Reddit/HN 帖子链接 |

### 引用格式

```markdown
## 来源列表

**必引来源：**
- [Tenorshare 定价页](https://www.tenorshare.com/products/ultdata.html) — 抓取于 2026-05-19
- [SimilarWeb 流量分析](https://www.similarweb.com/website/tenorshare.com/) — 月访问量数据
- [G2 评分页](https://www.g2.com/products/ultdata/reviews) — 4.2/5, 基于 156 条评价

**推断来源：**
- [SEO 竞争分析] 基于 Ahrefs 关键词难度指标推算，未找到直接报告
- [获客成本] 基于 SEM 出价 × 预估点击量推算，标注 [推算]
```

### 引用纪律

- **带具体数字的断言 → 必须有来源 URL**（"月流量 200 万"必须说从哪看到的）
- **定性判断 → 给出推理依据即可**（"市场趋于饱和"说明基于哪些信号）
- **无法获取来源时 → 显式标注** `[来源缺失: 原因]`，不要静默省略
- **来源时效 → 标注抓取/发布日期**，超过 1 年的数据标注 `[数据较旧: YYYY]`

### E. 技术实现调研（软件架构/系统权限/底层机制）

```
搜索策略：
1. 官方技术文档："[产品名] system extension" / "[产品名] kernel extension"
2. 安全研究/逆向分析："[产品名] reverse engineering" / "[产品名] entitlements"
3. 平台开发者文档：site:developer.apple.com "[相关API]"
4. 技术社区深度讨论：site:stackoverflow.com / site:forums.developer.apple.com
5. 产品技术白皮书："[产品名] how it works" / "[产品名] technical architecture"
6. 安装包分析："[产品名] installer analysis" / "[产品名] /Library/SystemExtensions"

⚠️ 否定断言特别注意：
- 技术实现细节通常不在营销页面上，必须搜索开发者/技术渠道
- 对于 macOS/iOS 安全相关功能，优先查 Apple Developer Documentation 了解可用技术路径
- "官网没提到" ≠ "没使用"。标注为 ❓未确认，不要断言否定
- 如果无法确定技术实现方式，写"技术实现方式未公开，无法确认"

输出格式：
- 各产品技术实现方式对比矩阵
- 每条结论标注认知状态（✅已确认/❓未确认/❌已否定/⚠️有争议）
- 平台限制和可用 API 路径说明
- 未确认项单独列出，说明已尝试的搜索渠道
```

## 常见调研陷阱

1. **过度搜索**：同一个问题搜索超过 10 次 → 停下来用已有信息产出
2. **信息堆砌**：罗列大量原始数据不做分析 → 必须提炼结论
3. **确认偏误**：只找支持预设结论的证据 → 主动搜索反面证据
4. **时效忽视**：引用 3 年前的数据 → 标注数据年份，优先找最新数据
5. **孤证定论**：一个来源就下结论 → 至少交叉验证一次
6. **完美主义**：等所有数据齐全才产出 → 有 70% 信息就可以给出有条件的结论
