"""单元测试：全部离线运行，不发起任何真实 MCP 网络调用。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcd_mcp.models import (  # noqa: E402
    Constraints,
    MenuItem,
    NutritionFact,
    Plan,
    PlanItem,
)
from mcd_mcp.nutrition import NutritionIndex, normalize, parse_nutrition_table  # noqa: E402
from mcd_mcp.parsers import (  # noqa: E402
    parse_available_coupons,
    parse_campaigns,
    parse_my_coupons,
    parse_stores,
)
from mcd_mcp.solver import is_combo, is_condiment, is_drink, plan_optimizer  # noqa: E402

NUTRITION_SAMPLE = """[3]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1420,339,4,16,45,246,0
  可乐中杯,null,586,140,0,0,35,18,0
"""

CAMPAIGN_SAMPLE = """#### 2026年10月10日 [今日]
- **活动标题**：韩式风味蘸酱上新
  **活动内容介绍**：就「酱」心有所「薯」
  <img src="https://example.com/a.jpg" width="100"/>
"""

COUPON_SAMPLE = """# 可领优惠券
- 优惠券标题：麦旋风任选\\
  状态：可领取
  <img src="https://example.com/c.jpg" width="100"/>
- 优惠券标题：薯薯任选\\
  状态：可领取
"""


def make_item(code, name, price, kcal=0.0, protein=0.0, sodium=0.0, cats=None, orig=None):
    it = MenuItem(
        code=code,
        name=name,
        price=price,
        original_price=orig if orig is not None else price,
        categories=cats or [],
    )
    it.nutrition = NutritionFact(
        product_name=name, energy_kcal=kcal, protein=protein, sodium=sodium
    )
    return it


class TestNutritionParsing(unittest.TestCase):
    def test_parse_table(self):
        facts = parse_nutrition_table(NUTRITION_SAMPLE)
        self.assertEqual(len(facts), 3)
        self.assertEqual(facts[0].product_name, "猪柳麦满分")
        self.assertEqual(facts[0].energy_kcal, 308)
        self.assertEqual(facts[0].sodium, 781)
        self.assertEqual(facts[1].calcium, 0.0)

    def test_normalize_fullwidth(self):
        self.assertEqual(normalize("纯牛奶（盒装）"), normalize("纯牛奶(盒装)"))
        self.assertEqual(normalize("麦咖啡™美式"), "麦咖啡美式")

    def test_alias_lookup(self):
        idx = NutritionIndex(parse_nutrition_table(NUTRITION_SAMPLE))
        fact, conf = idx.lookup("薯条")  # 菜单口径 -> 中薯条
        self.assertIsNotNone(fact)
        self.assertEqual(fact.product_name, "中薯条")
        self.assertGreaterEqual(conf, 0.99)

    def test_exact_lookup_confidence_is_one(self):
        idx = NutritionIndex(parse_nutrition_table(NUTRITION_SAMPLE))
        fact, conf = idx.lookup("可乐中杯")
        self.assertEqual(conf, 1.0)
        self.assertEqual(fact.energy_kcal, 140)


class TestMarkdownParsers(unittest.TestCase):
    def test_campaigns(self):
        camps = parse_campaigns(CAMPAIGN_SAMPLE)
        self.assertEqual(len(camps), 1)
        self.assertEqual(camps[0].date, "2026年10月10日")
        self.assertEqual(camps[0].title, "韩式风味蘸酱上新")

    def test_coupons(self):
        coupons = parse_available_coupons(COUPON_SAMPLE)
        titles = [c.title for c in coupons]
        self.assertIn("麦旋风任选", titles)
        self.assertIn("薯薯任选", titles)

    def test_my_coupons_empty(self):
        self.assertEqual(parse_my_coupons("暂无可用优惠券"), [])

    def test_stores(self):
        rows = [
            {"storeCode": "1450713", "storeName": "测试餐厅", "distance": "1.2",
             "businessStartTime": "07:00", "businessEndTime": "23:00"},
            {"storeName": "无编码门店"},
        ]
        stores = parse_stores(rows)
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0].business_hours, "07:00-23:00")


class TestClassification(unittest.TestCase):
    def test_drink(self):
        self.assertTrue(is_drink(make_item("1", "可口可乐中杯", 8, cats=["饮品"])))
        self.assertFalse(is_drink(make_item("2", "麦香鱼", 15, cats=["汉堡"])))

    def test_combo(self):
        self.assertTrue(is_combo(make_item("3", "大堡口福三件套", 23.9)))
        self.assertFalse(is_combo(make_item("4", "麦香鸡", 12)))

    def test_condiment_not_main(self):
        # 调料包应被排除，但「蘸酱麦麦脆汁鸡」是主餐，不能误伤
        self.assertTrue(is_condiment(make_item("5", "韩式烟熏芝士风味酱", 3)))
        self.assertFalse(is_condiment(make_item("6", "蘸酱麦麦脆汁鸡", 11.9)))


class TestSolver(unittest.TestCase):
    def setUp(self):
        self.items = [
            make_item("a", "麦香鸡", 12.0, kcal=400, protein=18, sodium=700,
                      cats=["汉堡"], orig=18.0),
            make_item("b", "麦辣鸡翅", 13.0, kcal=320, protein=22, sodium=900,
                      cats=["炸鸡"]),
            make_item("c", "可口可乐中杯", 8.0, kcal=140, protein=0, sodium=18,
                      cats=["饮品"]),
            make_item("d", "中薯条", 11.0, kcal=339, protein=4, sodium=246,
                      cats=["小食"]),
            make_item("e", "超预算大餐", 99.0, kcal=2000, protein=90, sodium=3000,
                      cats=["汉堡"]),
        ]

    def test_budget_respected(self):
        c = Constraints(budget=25, kcal_max=900, require_drink=True, max_items=4)
        plans = plan_optimizer(self.items, c, preset="balanced")
        self.assertTrue(plans)
        for p in plans:
            self.assertLessEqual(p.list_price, 25 + 1e-6)

    def test_drink_required(self):
        c = Constraints(budget=40, kcal_max=900, require_drink=True, max_items=4)
        plans = plan_optimizer(self.items, c, preset="balanced")
        self.assertTrue(plans)
        for p in plans:
            self.assertTrue(
                any(is_drink(pi.item) or is_combo(pi.item) for pi in p.items),
                "每个方案都应含饮品或套餐",
            )

    def test_kcal_ceiling(self):
        c = Constraints(budget=45, kcal_max=500, require_drink=False, max_items=4)
        plans = plan_optimizer(self.items, c, preset="light")
        self.assertTrue(plans)
        for p in plans:
            self.assertLessEqual(p.total_kcal, 500 + 1e-6)

    def test_unreachable_constraint_falls_back(self):
        c = Constraints(budget=1, kcal_max=10, require_drink=False, max_items=4)
        plans = plan_optimizer(self.items, c, preset="balanced")
        self.assertTrue(plans)
        self.assertIn("兜底", plans[0].name)

    def test_plans_are_diverse(self):
        c = Constraints(budget=45, kcal_max=1200, require_drink=True, max_items=4)
        plans = plan_optimizer(self.items, c, preset="balanced")
        signatures = [tuple(sorted(pi.item.code for pi in p.items)) for p in plans]
        self.assertEqual(len(signatures), len(set(signatures)))


class TestModels(unittest.TestCase):
    def test_subtotal_and_total(self):
        it = make_item("x", "测试单品", 10.0)
        plan = Plan(name="t", items=[PlanItem(item=it, quantity=3)])
        self.assertEqual(plan.list_price, 30.0)
        self.assertEqual(plan.total_price, 30.0)

    def test_verified_price_wins(self):
        it = make_item("x", "测试单品", 10.0)
        plan = Plan(name="t", items=[PlanItem(item=it, quantity=2)], verified_price=15.0)
        self.assertEqual(plan.list_price, 20.0)
        self.assertEqual(plan.total_price, 15.0)

    def test_effective_price_takes_the_pessimistic_one(self):
        it = make_item("x", "促销套餐", 9.9)
        it.official_price = 19.0
        # 展示价与核价不一致时按更贵的一侧规划，避免预算算乐观
        self.assertEqual(it.effective_price, 19.0)
        self.assertFalse(it.price_reliable)

    def test_price_reliable_when_aligned(self):
        it = make_item("x", "常规单品", 12.0)
        it.official_price = 12.0
        self.assertTrue(it.price_reliable)
        self.assertEqual(it.price_gap, 0.0)

    def test_strict_price_filters_unreliable_items(self):
        unreliable = make_item("u", "需券促销价", 5.0, kcal=300, protein=20, sodium=100,
                               cats=["汉堡"])
        unreliable.official_price = 30.0  # 核价远高于展示价
        reliable = make_item("r", "常规款", 12.0, kcal=400, protein=20, sodium=500,
                             cats=["汉堡"])
        drink = make_item("d", "可乐中杯", 8.0, kcal=140, sodium=18, cats=["饮品"])
        c = Constraints(budget=40, kcal_max=900, require_drink=True, strict_price=True)
        plans = plan_optimizer([unreliable, reliable, drink], c, preset="balanced")
        self.assertTrue(plans)
        for p in plans:
            self.assertNotIn("u", p.codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
