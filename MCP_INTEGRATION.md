# MCP 集成说明

> 说明本项目实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。
> MCP Server 接入地址：`https://mcp.mcd.cn`（Streamable HTTP），
> 参考文档：<https://github.com/M-China/mcd-mcp-server>

---

## 1. 接入方式

传输协议：**Streamable HTTP**，请求头携带 `Authorization: Bearer <MCP_TOKEN>`。

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer ${MCD_MCP_TOKEN}"
      }
    }
  }
}
```

脱敏配置文件见仓库根目录 [`mcp-config.example.json`](./mcp-config.example.json)，
**仅含环境变量占位符，不含任何真实凭证**。

客户端实现见 `src/mcd_mcp/client.py`，自己处理了这些细节：

| 关注点 | 实现 |
| --- | --- |
| 响应体兼容 | 同时支持 `application/json` 与 `text/event-stream`（SSE）两种返回 |
| 会话保持 | 自动回传 `Mcp-Session-Id` |
| 限流 | 令牌桶限速（官方上限 600 次/分钟，本项目按 8 次/秒保守取值） |
| 重试 | 指数退避，最多 3 次 |
| 错误语义 | 401 → `McpAuthError`，429 → `McpRateLimitError` |
| 大响应 | 短连接 + `Connection: close`，规避分块传输的 `IncompleteRead` |
| 缓存 | 工具名 + 参数哈希做磁盘缓存，TTL 默认 600s，避免重复消耗额度 |

---

## 2. 实际使用的 Tool 清单

该 MCP Server 共开放 35 个工具，本项目真实调用其中 **9 个**：

| # | Tool | 在本项目中的用途 | 关键入参 | 业务价值 |
| --- | --- | --- | --- | --- |
| 1 | `now-time-info` | 获取服务器时间，判定早/午/下午茶/晚餐/夜宵时段 | — | 不同时段供应品类与优惠力度不同，避免推荐当前买不到的餐品 |
| 2 | `campaign-calendar` | 拉取当日及邻近日期的营销活动 | `specifiedDate` | 把「今天有什么活动」从翻 App 变成一条命令 |
| 3 | `available-coupons` | 盘点麦麦省当前可领券 | — | 让用户知道还有哪些券没领 |
| 4 | `query-my-coupons` | 查询券包已有资产 | `page` / `pageSize` | 避免重复领券，评估可用优惠 |
| 5 | `query-store-coupons` | 查询**当前门店 + 当前订单类型**下真正可用的券 | `storeCode` / `orderType` / `beType` | 券包里的券不保证能用，这一步做可用性校验 |
| 6 | `query-nearby-stores` | 按城市 + 地标搜索门店，取 `storeCode` | `beType` / `searchType` / `city` / `keyword` | 后续所有菜单、价格、券都绑定到具体门店 |
| 7 | `query-meals` | 拉取门店实时菜单（分类、餐品编码、标签、现价/原价） | `storeCode` / `orderType` / `beType` | 求解器的候选池来源 |
| 8 | `list-nutrition-foods` | 拉取 160 条官方餐品营养基线 | — | 热量/蛋白/脂肪/碳水/钠约束的数据基础 |
| 9 | `calculate-price` | **①** 批量反查官方标准名与参考价；**②** 对最终购物车整单核价 | `storeCode` / `items[]` / `orderType` / `beType` | 保证推荐结果的**价格是真实可下单的价格** |

### 明确不默认调用的工具

| Tool | 原因 |
| --- | --- |
| `auto-bind-coupons` | 会真实领券到用户账户。默认关闭，需显式 `confirm=True` |
| `create-order` / `cancel-order` | 会产生真实订单。默认关闭，需显式 `confirm=True` |
| `delivery-create-address` | 会写入用户地址簿。本项目不调用 |
| `draw-lottery` | 会消耗积分。本项目不调用 |

> 代码中对应位置：`pipeline.py` 的 `Planner.claim_all_coupons(confirm=...)` 与
> `Planner.create_order(confirm=..., ...)`，未传 `confirm=True` 时直接抛错。

---

## 3. 调用流程

```
┌──────────────── 只读采集阶段（默认执行） ────────────────┐
│                                                        │
│  now-time-info ──────► 时段判定（早/午/下午茶/晚/夜宵）    │
│  campaign-calendar ──► 当日 + 邻近活动                   │
│  available-coupons ──► 麦麦省可领券                      │
│  query-my-coupons ───► 券包资产                         │
│  query-store-coupons ► 本门店可用券                      │
│  query-nearby-stores ► 门店定位 → storeCode              │
│  query-meals ────────► 菜单（121 项，分类/标签/价格）      │
│  calculate-price ×5 ─► 批量反查官方标准名 + 参考价         │
│  list-nutrition-foods ► 160 条营养基线                   │
│                                                        │
└──────────────────────────┬─────────────────────────────┘
                           ▼
              ┌─── 本地求解（Beam Search）───┐
              │  预算 × 热量 × 蛋白 × 钠 × 多样性 │
              └──────────────┬──────────────┘
                             ▼
┌──────────── 结算校验阶段 ─────────────┐
│  对每套方案调用 calculate-price 整单核价   │
│  实付 > 预算 → 淘汰，换下一候选           │
└───────────────────────────────────────┘
                             ▼
                    4 套可直接下单的方案
```

一次 `mcd plan` 的实际调用序列（`--city 上海 --kw 人民广场 --budget 40`）：

| 顺序 | Tool | 次数 | 说明 |
| --- | --- | --- | --- |
| 1 | `now-time-info` | 1 | 时段判定（内部调用 2 次：一次取时间，一次算时段） |
| 2 | `campaign-calendar` | 1 | 传 `specifiedDate` 只取当日及邻近三天 |
| 3 | `available-coupons` | 1 | 麦麦省可领券 |
| 4 | `query-my-coupons` | 1 | 券包 |
| 5 | `query-store-coupons` | 1 | 绑定门店后的可用券 |
| 6 | `query-nearby-stores` | 1 | `searchType=2` 按位置搜索 |
| 7 | `query-meals` | 1 | 该店 121 项菜单 |
| 8 | `calculate-price` | 5 | 每批 25 项，批量反查官方标准名与参考价 |
| 9 | `list-nutrition-foods` | 1 | 营养基线 |
| 10 | `calculate-price` | 4~8 | 对候选方案逐套整单核价 |

全流程约 20–60 秒，结果落盘缓存，重复执行几乎瞬时返回。

---

## 4. 两个由 MCP 数据特性反推出的设计

### 4.1 用 `calculate-price` 反查官方标准名，对齐菜单与营养表

`query-meals` 返回的是**门店菜单口径**（「薯条」「可乐」「麦咖啡™美式」），
`list-nutrition-foods` 返回的是**营养表口径**（「中薯条」「可乐中杯」「冰美式中杯」），
直接字符串匹配会大面积落空。

观察到 `calculate-price` 的 `productList[].productName` 回传的是**官方标准名**：

| query-meals 名称 | calculate-price 回传 |
| --- | --- |
| 薯条 | 中薯条 |
| 麦当劳联名复古棒球帽 之外的单品 | 标准名 |
| 苹果片 | 苹果片一袋(60g) |
| 麦咖啡™美式 | 麦咖啡™冰美式中杯 |

于是用「批量 `calculate-price`」一次性拿到 121 项的标准名，再做
**精确匹配 → 别名表 → 包含匹配 → difflib 模糊匹配** 四级对齐。
命中率从约 30% 提升到：精确 38 + 模糊 18（共 121 项中命中 56 项单品，
其余多为「三件套/四件套」类组合餐，营养表中本身没有对应条目）。

### 4.2 菜单展示价 ≠ 实付价，因此必须整单核价

实测发现同一餐品在不同购物车里价格会变：

| 餐品 | 菜单展示价 | 单点核价 | 凑单核价 |
| --- | --- | --- | --- |
| 牛奶 | ¥19.0 | ¥19.0 | ¥9.5 |
| 燕麦奶铁 | ¥9.9 | ¥23.0 | ¥9.5 |
| 酥酥多笋卷四件套 | ¥28.0 | ¥55.0 | ¥55.0 |

结论：**批量核价得到的单价不可靠**（会被凑单优惠污染），
但**对最终购物车整单核价是可靠的**。因此本项目：

1. 规划阶段用菜单展示价，保证候选池覆盖完整；
2. 结算阶段用 `calculate-price` 对整单核价，超预算的方案直接淘汰；
3. 额外提供「价格口径可信度」过滤：菜单价与参考价偏差 >15% 的餐品默认不参与规划。

---

## 5. 业务价值

| 维度 | 没有 MCP 时 | 有了 MCP 之后 |
| --- | --- | --- |
| 数据实时性 | 靠爬取/手工维护，必然过期 | 门店菜单、价格、活动、券全部实时 |
| 价格准确性 | 静态价目表，与实付脱节 | `calculate-price` 给出真实可下单价格 |
| 营养可信度 | 第三方数据库或不完整 | 麦当劳官方 160 条营养基线 |
| 券的可用性 | 只能看到「有什么券」 | 能看到「这个门店、这种下单方式下**能用**的券」 |
| 决策质量 | 凭感觉 | 带约束的多目标优化，四套方案可对比 |

---

## 6. 复现方式

```bash
export MCD_MCP_TOKEN=你的Token
mcd plan --city 上海 --kw 人民广场 --budget 40 --kcal-max 900
```

输出末尾会打印完整的 **MCP 调用链**（工具名 + 状态 + 耗时），可直接对照本文档核验。
