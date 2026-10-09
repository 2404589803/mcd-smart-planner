# 麦麦精算师 · McSmart

> 基于**麦当劳中国 MCP** 的多目标点餐决策引擎 —— 在**预算、热量、蛋白质、优惠券**四维约束下，
> 用真实的门店菜单、营养数据与官方核价接口，算出「今天到底该点什么」。

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-DA291C)](https://github.com/M-China/mcd-mcp-server)
[![Deps](https://img.shields.io/badge/第三方依赖-0-1f8a4c)]()
[![Contest](https://img.shields.io/badge/麦当劳程序员创意开发大赛-参赛作品-FFC72C)](https://github.com/M-China/mcd-developer-innovation-challenge)

---

## 它解决什么问题？

麦当劳的点餐决策其实是个**被价格结构搞复杂的组合优化问题**：

| 痛点 | 现状 | 本项目的做法 |
| --- | --- | --- |
| 券太多用不明白 | 麦麦省、券包、门店券、随心配各自为政，不知道哪张最值 | 一次性盘点三处券源，纳入价格与组合约束 |
| 控卡/增肌不会点 | 营养表是 PDF 口径，菜单是门店口径，两者对不上 | 自动对齐「菜单 ↔ 营养表」，按热量/蛋白约束求解 |
| 菜单价 ≠ 实付价 | 部分套餐展示的是需券/凑单价，下单时才发现变贵 | 用 `calculate-price` 对整单真实核价，超预算直接淘汰 |
| 活动信息分散 | 今天有什么活动要翻好几个入口 | `campaign-calendar` 拉取当日及邻近活动 |

一句话：**你说预算和热量目标，它给你 4 套可直接下单的方案，并且每套都拿官方接口核过价。**

---

## 效果

### 命令行

```
$ mcd plan --city 上海 --kw 人民广场 --budget 40 --kcal-max 900

──── 麦麦精算师 · 决策报告 ──────────────────────────────
门店：麦当劳上海黄浦华旭国际大厦餐厅（1450713）
时间：2026-10-09 14:15:26  时段：午餐时段
菜单 121 项（价格口径可信 100 项）/ 营养基线 160 项
      (精确命中 38，模糊 18，估算 65)

──── 推荐方案 ──────────────────────────────
  方案 1｜轻负担控卡   ¥37.80   668 kcal   蛋白 38.1 g   钠 905 mg
      ×1  大堡口福三件套          ¥22.90   301 kcal ≈
      ×1  蘸酱麦麦脆汁鸡          ¥11.90   328 kcal
      ×1  韩式烟熏芝士风味酱       ¥3.00    39 kcal ≈
      实付核价 ¥37.80（菜单合计 ¥37.80，优惠 ¥0.00）

  方案 2｜营养均衡     ¥38.70   852 kcal   蛋白 51.8 g
  方案 3｜最省钱包     ¥38.80   510 kcal   蛋白 25.6 g
  方案 4｜高蛋白增肌   ¥39.80   848 kcal   蛋白 53.0 g

──── MCP 调用链 ──────────────────────────────
  now-time-info  ok  3.9s → campaign-calendar ok 3.8s
  → available-coupons → query-my-coupons → query-store-coupons
  → query-meals → calculate-price ×5（标准名/核价反查）
  → list-nutrition-foods → calculate-price ×4（整单核价）
```

### Web 界面

```bash
mcd serve --port 8000   # 打开 http://127.0.0.1:8000
```

左侧填约束，右侧看方案卡片、营养/预算占比条、活动雷达、券包，以及**每一次 MCP 调用的耗时链路**。

---

## 30 秒上手

```bash
# 1. 申请 MCP Token：https://open.mcd.cn/mcp （手机号验证登录 → 控制台 → 激活）

# 2. 安装（零第三方依赖，纯标准库）
git clone https://github.com/<你的账号>/mcd-smart-planner.git
cd mcd-smart-planner
pip install -e .

# 3. 配置 Token
export MCD_MCP_TOKEN=你的Token          # Windows: set MCD_MCP_TOKEN=你的Token

# 4. 跑一条完整链路
mcd plan --city 上海 --kw 人民广场 --budget 40 --kcal-max 900
```

不想装包也可以直接跑：

```bash
MCD_MCP_TOKEN=xxx python -m mcd_mcp.cli plan --city 上海 --kw 人民广场
```

---

## 使用示例

```bash
# 看看这个 Token 能用哪些 MCP 工具
mcd tools

# 找门店
mcd stores --city 上海 --kw 人民广场

# 今天有什么活动
mcd campaigns -v

# 麦麦省还能领什么券（加 --claim 才会真的领，需二次确认）
mcd coupons

# 两人聚餐、预算 80、人均热量不超过 800
mcd plan --city 上海 --kw 人民广场 --people 2 --budget 80 --kcal-max 800

# 增肌场景：蛋白质至少 40g，热量放宽到 1200
mcd plan --store 1450713 --protein-min 40 --kcal-max 1200 --presets high_protein

# 启动 Web 界面
mcd serve
```

---

## 它到底怎么用 MCP 的？

真实调用了 **9 个 MCP 工具**（该 Server 共开放 35 个），详见 [`MCP_INTEGRATION.md`](MCP_INTEGRATION.md)。

```
now-time-info ──► 判断早/午/下午茶/晚餐/夜宵时段，切换推荐策略
      │
campaign-calendar ──► 当日 + 邻近活动雷达
available-coupons ──► 麦麦省可领券盘点
query-my-coupons ───► 已有券包资产
query-store-coupons ► 当前门店可用券
      │
query-nearby-stores ► 门店定位（storeCode）
query-meals ────────► 门店实时菜单（121 项，含分类/标签/价格）
calculate-price ─────► ① 批量反查官方标准名与参考价
                      ② 对最终购物车整单核价（结算口径）
list-nutrition-foods ► 160 条官方营养基线
      │
   [本地求解] 预算 × 热量 × 蛋白 × 钠 × 多样性
      │
   4 套方案 → 逐套 calculate-price 核价 → 超预算者淘汰
```

> `auto-bind-coupons`（一键领券）与 `create-order`（下单）**有副作用**，
> 默认关闭，必须显式传 `confirm=True` 才会执行。

---

## 算法：为什么不是「随便挑几样」

形式化为一个带约束的多目标整数规划：

```
决策变量   xᵢ ∈ {0..3}                     第 i 个餐品的份数
硬约束     Σ priceᵢ·xᵢ ≤ budget
           kcal_min ≤ Σ kcalᵢ·xᵢ ≤ kcal_max
           Σ proteinᵢ·xᵢ ≥ protein_min
           Σ sodiumᵢ·xᵢ ≤ sodium_max
           必须含饮品，或已含套餐
           最多 4 个不同品类
软目标     w_protein·蛋白 − w_price·价格 − w_kcal·热量偏离
           − w_sodium·钠 + w_variety·品类数 + w_discount·折扣额
```

候选规模 ~120、份数上限 3 时，精确 ILP 的搜索空间超过 10⁸，
因此采用 **定向 Beam Search + 可行性剪枝**（beam width 260），
在百毫秒内给出稳定、可复现的近似最优解，并按四种偏好预设各出一套：

| preset | 倾向 |
| --- | --- |
| `cheapest` | 最省钱包 |
| `high_protein` | 高蛋白增肌 |
| `light` | 轻负担控卡 |
| `balanced` | 营养均衡 |

### 两个踩坑与对应的设计

**① 菜单展示价 ≠ 实付价。**
同一杯牛奶单点核价 ¥19.0，凑单核价 ¥9.5；部分「四件套」菜单价 ¥28.0，核价 ¥55.0。
所以本项目**规划用菜单价，结算用 `calculate-price` 整单核价**，并内置「价格口径可信度」过滤：
菜单价与核价偏差 >15% 的餐品默认不参与规划（可用 `--no-strict-price` 放开）。

**② 菜单名与营养表名对不上。**
菜单叫「薯条」，营养表叫「中薯条」；菜单叫「可乐」，营养表叫「可乐中杯」。
本项目利用 `calculate-price` 会回传**官方标准名**的特性做反查对齐（菜单「薯条」→ 官方「中薯条」），
再叠加别名表 + 包含匹配 + difflib 模糊匹配三级策略；仍未命中的按「热量/元」中位数估算，
并在输出里打 `≈` 标记，绝不把估算值伪装成官方数据。

---

## 项目结构

```
mcd-smart-planner/
├── README.md                  ← 你在这儿
├── CONTEST_DECLARATION.md     ← 参赛声明（官方原文，未改动）
├── MCP_INTEGRATION.md         ← MCP 调用流程与业务价值
├── mcp-config.example.json    ← 脱敏配置（仅环境变量占位符）
├── workbuddy.md               ← WorkBuddy 开发上下文
├── skills/
│   └── mcd-smart-planner/
│       └── SKILL.md           ← 可直接安装到 WorkBuddy 的 Skill 包
├── src/mcd_mcp/
│   ├── client.py              ← Streamable HTTP MCP 客户端（限流/重试/SSE解析）
│   ├── pipeline.py            ← 9 个工具的编排链路 + 磁盘缓存
│   ├── solver.py              ← 多目标 Beam Search 求解器
│   ├── nutrition.py           ← 营养表解析 + 三级名称对齐
│   ├── menu.py                ← 菜单解析 + 官方标准名/参考价反查
│   ├── parsers.py             ← Markdown/JSON 载荷结构化
│   ├── models.py              ← 领域模型
│   ├── web.py                 ← 零依赖 Web 服务
│   ├── cli.py                 ← 命令行
│   └── static/index.html      ← 可视化界面
└── tests/                     ← 单元测试（不含真实网络调用）
```

**零第三方依赖**：客户端、限流、缓存、HTTP 服务全部基于 Python 标准库实现，
`pip install -e .` 后即可用，不需要 `requests` / `fastapi` / `uvicorn`。

---

## 目标用户

- **减脂/增肌人群**：要吃麦当劳，但要卡热量和蛋白质
- **预算敏感型用户**：想用最少的钱吃到最合理的组合
- **选择困难症患者**：面对 120 道菜不知道点什么
- **开发者**：想看一个 MCP 多工具编排 + 约束求解的完整工程样例

---

## 测试

```bash
# 零依赖跑法（只用标准库）
python -m unittest discover -s tests -v

# 或者用 pytest
pip install -e ".[dev]" && pytest -q
```

21 个用例覆盖：营养表解析、名称别名对齐、Markdown 活动/券解析、品类判定、
预算/热量/饮品硬约束、严格价格口径过滤、无解兜底。
测试全部基于本地 fixture，**不发起真实网络请求**。

---

## 合规与免责

- 本项目为**麦当劳程序员节创意开发大赛参赛作品**，由参赛者独立开发，**非麦当劳官方产品**。
- 项目中不包含任何真实 Token / 密钥 / 个人信息，配置文件仅使用 `${MCD_MCP_TOKEN}` 占位符。
- 餐品、价格、营养与供应状态**以麦当劳官方渠道的实时结果为准**；
  输出仅供参考，**不构成医疗、营养或其他专业建议**。
- 标 `≈` 的营养值为按价格估算所得，非官方营养数据。
- 领券与下单接口默认关闭，需显式授权才会调用。

© 2026 McDonald's. All Rights Reserved. 本项目不构成对麦当劳及其关联方商标的任何授权。
