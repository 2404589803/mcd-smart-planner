"""领域模型：菜单项、营养成分、优惠券、活动、方案。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "MenuItem",
    "NutritionFact",
    "Coupon",
    "Campaign",
    "Store",
    "PlanItem",
    "Plan",
    "Constraints",
]


@dataclass
class NutritionFact:
    """单品营养成分（每份）。"""

    product_name: str
    energy_kj: float = 0.0
    energy_kcal: float = 0.0
    protein: float = 0.0  # g
    fat: float = 0.0  # g
    carbohydrate: float = 0.0  # g
    sodium: float = 0.0  # mg
    calcium: float = 0.0  # mg
    description: str = ""

    def scaled(self, quantity: int) -> "NutritionFact":
        return NutritionFact(
            product_name=self.product_name,
            energy_kj=self.energy_kj * quantity,
            energy_kcal=self.energy_kcal * quantity,
            protein=self.protein * quantity,
            fat=self.fat * quantity,
            carbohydrate=self.carbohydrate * quantity,
            sodium=self.sodium * quantity,
            calcium=self.calcium * quantity,
            description=self.description,
        )


@dataclass
class MenuItem:
    """菜单中的可售餐品。"""

    code: str
    name: str
    price: float  # 菜单展示价（元）
    original_price: float = 0.0  # 菜单划线价（元）
    official_price: Optional[float] = None  # calculate-price 回传的官方核价（元）
    image: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    # calculate-price 反查得到的官方标准名（用于与营养表对齐）
    canonical_name: str = ""
    nutrition: Optional[NutritionFact] = None
    nutrition_confidence: float = 0.0  # 0~1
    nutrition_estimated: bool = False

    @property
    def match_name(self) -> str:
        return self.canonical_name or self.name

    @property
    def effective_price(self) -> float:
        """规划基准价：菜单展示价与批量核价参考价取大者。

        实测同一餐品的「菜单展示价」与「核价」可能不一致（部分套餐展示的是
        需券/凑单才成立的促销价），取大值可避免把预算算乐观。
        """
        if self.official_price is None:
            return self.price
        return max(self.price, self.official_price)

    @property
    def price_reliable(self) -> bool:
        """菜单价与核价口径是否一致（偏差 ≤ 15%）。"""
        if self.official_price is None:
            return True
        base = max(self.price, 1e-6)
        return abs(self.official_price - self.price) / base <= 0.15

    @property
    def price_gap(self) -> float:
        """批量核价参考价 - 菜单展示价（仅作提示，不作为结算依据）。"""
        if self.official_price is None:
            return 0.0
        return round(self.official_price - self.price, 2)

    @property
    def kcal(self) -> float:
        return self.nutrition.energy_kcal if self.nutrition else 0.0

    @property
    def protein(self) -> float:
        return self.nutrition.protein if self.nutrition else 0.0

    @property
    def has_nutrition(self) -> bool:
        return self.nutrition is not None

    @property
    def discount_rate(self) -> float:
        if self.original_price and self.original_price > self.price:
            return round(1 - self.price / self.original_price, 4)
        return 0.0


@dataclass
class Coupon:
    """优惠券（来自麦麦省 / 我的券包 / 门店可用券）。"""

    title: str
    source: str  # available | mine | store
    status: str = ""
    image: str = ""
    coupon_id: str = ""
    coupon_code: str = ""
    valid_range: str = ""
    products: List[str] = field(default_factory=list)


@dataclass
class Campaign:
    """营销活动日历条目。"""

    date: str
    title: str
    content: str = ""
    image: str = ""
    phase: str = ""  # 往期回顾 / 进行中 / 即将开始


@dataclass
class Store:
    """门店。"""

    store_code: str
    name: str = ""
    address: str = ""
    distance: float = 0.0
    be_code: str = ""
    business_status: bool = True
    business_hours: str = ""


@dataclass
class PlanItem:
    """方案中的一行。"""

    item: MenuItem
    quantity: int = 1

    @property
    def subtotal(self) -> float:
        return round(self.item.effective_price * self.quantity, 2)

    @property
    def menu_subtotal(self) -> float:
        return round(self.item.price * self.quantity, 2)

    @property
    def kcal(self) -> float:
        return round(self.item.kcal * self.quantity, 1)

    @property
    def protein(self) -> float:
        return round(self.item.protein * self.quantity, 1)


@dataclass
class Plan:
    """一套点餐方案。"""

    name: str
    items: List[PlanItem] = field(default_factory=list)
    score: float = 0.0
    note: str = ""
    verified_price: Optional[float] = None  # calculate-price 对整单的真实核价（元）
    verified_discount: Optional[float] = None
    price_gap: float = 0.0  # 实付 - 菜单展示价合计（>0 表示凑单后更贵）
    over_budget: bool = False

    @property
    def total_price(self) -> float:
        if self.verified_price is not None:
            return round(self.verified_price, 2)
        return round(sum(i.subtotal for i in self.items), 2)

    @property
    def list_price(self) -> float:
        """菜单展示价合计。"""
        return round(sum(i.menu_subtotal for i in self.items), 2)

    @property
    def total_kcal(self) -> float:
        return round(sum(i.kcal for i in self.items), 1)

    @property
    def total_protein(self) -> float:
        return round(sum(i.protein for i in self.items), 1)

    @property
    def total_fat(self) -> float:
        v = sum((i.item.nutrition.fat if i.item.nutrition else 0) * i.quantity for i in self.items)
        return round(v, 1)

    @property
    def total_carbohydrate(self) -> float:
        v = sum(
            (i.item.nutrition.carbohydrate if i.item.nutrition else 0) * i.quantity
            for i in self.items
        )
        return round(v, 1)

    @property
    def total_sodium(self) -> float:
        v = sum((i.item.nutrition.sodium if i.item.nutrition else 0) * i.quantity for i in self.items)
        return round(v, 1)

    @property
    def codes(self) -> Dict[str, int]:
        return {i.item.code: i.quantity for i in self.items}


@dataclass
class Constraints:
    """用户约束。"""

    budget: float = 40.0  # 预算上限（元）
    kcal_min: float = 0.0
    kcal_max: float = 900.0
    protein_min: float = 0.0
    sodium_max: float = 100000.0
    max_items: int = 5  # 不同品类数量上限
    require_drink: bool = True
    people: int = 1
    strict_price: bool = True  # 只使用「菜单价≈核价」的餐品参与规划
