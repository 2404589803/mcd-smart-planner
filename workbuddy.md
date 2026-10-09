# workbuddy.md

> 本文件记录本项目使用 **腾讯 WorkBuddy** 智能体开发的对话上下文，
> 用于核验麦当劳程序员节创意开发大赛「WorkBuddy 专项奖励」的联动条件。

---

## 一、项目信息

| 项 | 内容 |
| --- | --- |
| 项目名称 | 麦麦精算师 · McSmart |
| 项目定位 | 基于麦当劳中国 MCP 的多目标点餐决策引擎 |
| 开发工具 | 腾讯 WorkBuddy（Agent 模式） |
| MCP 接入 | 通过 WorkBuddy「自定义连接器 → 配置 MCP」接入 `mcd-mcp` |
| 开发时间 | 2026-10-09 |

## 二、WorkBuddy 在本项目中的实际作用

1. **需求拆解与方案选型**
   拉取大赛仓库 README 与《活动规则》，确认必须交付的 5 类文件，
   并确认排名机制为「GitHub 公开 Star 数」，据此把项目定位为
   「工程完整度高 + 可一键复现 + 有可视化」的方向。

2. **MCP 能力探测**
   通过直连 `https://mcp.mcd.cn` 完成 `initialize` / `tools/list`，
   确认该 Server 实际开放 **35 个工具**，并逐个导出入参 schema，
   据此选定最终要编排的 9 个工具。

3. **真实接口联调（关键）**
   WorkBuddy 直接对线上 MCP 发起真实调用，逐项验证返回结构：
   - `query-nearby-stores` → 门店数组（storeCode / 距离 / 营业时间）
   - `query-meals` → `{categories, meals}` 双层结构，121 项菜单
   - `list-nutrition-foods` → `data` 为紧凑文本表格，160 条营养记录
   - `campaign-calendar` / `available-coupons` → Markdown 而非 JSON
   - `calculate-price` → 金额单位为「分」，且 `productList[].productName` 是官方标准名

   联调过程中发现并解决了两个真实问题：
   - **大响应 `IncompleteRead`**：分块传输导致读取中断，改为短连接 `Connection: close` 解决。
   - **菜单展示价 ≠ 实付价**：同一餐品在不同购物车中核价不同，据此改为
     「规划用菜单价、结算用 `calculate-price` 整单核价」的双口径设计。

4. **代码生成与迭代**
   由 WorkBuddy 生成 `client.py` / `pipeline.py` / `solver.py` / `nutrition.py` /
   `menu.py` / `parsers.py` / `web.py` / `cli.py` 及可视化前端，
   并根据真实返回数据反复修正解析逻辑（营养表解析、名称对齐阈值、预算淘汰策略）。

5. **文档与 Skill 产出**
   生成 README.md、MCP_INTEGRATION.md、脱敏配置与
   可直接安装到 WorkBuddy 的 Skill 包（`skills/mcd-smart-planner/SKILL.md`）。

## 三、目录对照

| WorkBuddy 产出 | 位置 |
| --- | --- |
| MCP 客户端（限流/重试/SSE 解析） | `src/mcd_mcp/client.py` |
| 9 工具编排链路 | `src/mcd_mcp/pipeline.py` |
| 多目标求解器 | `src/mcd_mcp/solver.py` |
| 营养解析与名称对齐 | `src/mcd_mcp/nutrition.py` |
| 可视化界面 | `src/mcd_mcp/web.py` + `src/mcd_mcp/static/index.html` |
| WorkBuddy Skill 包 | `skills/mcd-smart-planner/SKILL.md` |

## 四、一键复现

```bash
export MCD_MCP_TOKEN=你的Token
pip install -e .
mcd plan --city 上海 --kw 人民广场 --budget 40 --kcal-max 900
mcd serve            # Web 可视化
```

## 五、合规声明

- 本文件及项目中**不包含任何真实 Token、密钥或个人信息**，
  配置示例仅使用 `${MCD_MCP_TOKEN}` 占位符。
- 本项目为参赛作品，非麦当劳官方产品；餐品、价格与供应状态以麦当劳官方渠道实时结果为准。
