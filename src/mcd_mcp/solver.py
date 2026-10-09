"""多目标点餐求解器。

把「今天吃什么」形式化为一个带约束的多目标整数规划问题：

    决策变量  x_i ∈ {0..max_qty}   每个餐品的份数
    硬约束    Σ price_i·x_i ≤ budget
              kcal_min ≤ Σ kcal_i·x_i ≤ kcal_max
              Σ protein_i·x_i ≥ protein_min
              Σ sodium_i·x_i ≤ sodium_max
              品类约束（必须有饮品或已含套餐）
    软目标    w = (蛋白, 热量, 价格, 钠, 多样性) 的加权线性组合

全局最优需要 ILP 求解器，这里采用 **定向 Beam Search + 可行性剪枝**：
候选规模 ~120、份数上限 3 时，可在百毫秒级给出近似最优且结果稳定可复现。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .models import Constraints, MenuItem, Plan, PlanItem

__all__ = ["SolverConfig", "PRESETS", "is_drink", "is_combo", "plan_optimizer"]

DRINK_KEYWORDS = (
    "可乐", "雪碧", "咖啡", "红茶", "绿茶", "奶茶", "牛奶", "奶昔", "果汁",
    "苹果汁", "柠檬", "气泡", "纯悦", "怡泉", "水", "燕麦奶", "阿芙佳朵",
    "可可", "朱古力",
)
DRINK_CATEGORIES = ("饮品", "麦咖啡")
COMBO_KEYWORDS = ("三件套", "四件套", "套餐", "双人", "分享餐", "随心配", "乐园餐", "亲子")
EXCLUDE_KEYWORDS = ("帽", "玩具", "周边", "公仔", "联名复古", "糖包", "奶精", "餐具",
                    "纸巾", "吸管", "杯套", "包装")
# 蘸酱/调味包不是正餐组成，单独出现在方案里没有意义。
# 只有「名字以酱/料/粉结尾」才判定为调料，避免误伤「蘸酱麦麦脆汁鸡」这类主餐。
CONDIMENT_SUFFIX = ("酱", "料", "粉", "沙司", "糖浆")


def is_drink(item: MenuItem) -> bool:
    name = item.match_name
    cat_hit = any(("饮品" in c or "麦咖啡" in c) for c in item.categories)
    return cat_hit or any(k in name for k in DRINK_KEYWORDS)


def is_combo(item: MenuItem) -> bool:
    return any(k in item.name for k in COMBO_KEYWORDS)


def is_condiment(item: MenuItem) -> bool:
    name = item.match_name.strip()
    return len(name) <= 14 and name.endswith(CONDIMENT_SUFFIX)


def is_excluded(item: MenuItem) -> bool:
    if any(k in item.name for k in EXCLUDE_KEYWORDS):
        return True
    return is_condiment(item)


@dataclass
class SolverConfig:
    """求解器超参。"""

    beam_width: int = 260
    max_qty: int = 3
    max_distinct: int = 4
    top_k: int = 3
    allow_duplicate_combo: bool = False


# 目标预设：权重越大表示该维度越被偏好（负号表示惩罚）
PRESETS: Dict[str, Dict[str, float]] = {
    "cheapest": {  # 最省钱
        "protein": 0.15,
        "price": 1.0,
        "kcal_dev": 0.004,
        "sodium": 0.0004,
        "variety": 0.6,
        "discount": 1.2,
    },
    "high_protein": {  # 增肌/高蛋白
        "protein": 2.2,
        "price": 0.18,
        "kcal_dev": 0.006,
        "sodium": 0.0008,
        "variety": 0.4,
        "discount": 0.4,
    },
    "light": {  # 控卡/轻负担
        "protein": 0.9,
        "price": 0.25,
        "kcal_dev": 0.03,
        "sodium": 0.0015,
        "variety": 0.5,
        "discount": 0.6,
    },
    "balanced": {  # 均衡
        "protein": 1.2,
        "price": 0.45,
        "kcal_dev": 0.012,
        "sodium": 0.0009,
        "variety": 1.0,
        "discount": 0.8,
    },
}

PRESET_LABELS = {
    "cheapest": "最省钱包",
    "high_protein": "高蛋白增肌",
    "light": "轻负担控卡",
    "balanced": "营养均衡",
}


def _range_penalty(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo - value
    if value > hi:
        return value - hi
    return 0.0


class _Basket:
    """搜索过程中的一个状态（不可变风格，便于 beam 复制）。"""

    __slots__ = ("counts",)

    def __init__(self, counts: Optional[Dict[str, int]] = None) -> None:
        self.counts: Dict[str, int] = dict(counts or {})

    def copy(self) -> "_Basket":
        return _Basket(self.counts)

    def add(self, code: str) -> "_Basket":
        b = self.copy()
        b.counts[code] = b.counts.get(code, 0) + 1
        return b

    @property
    def size(self) -> int:
        return sum(self.counts.values())

    @property
    def distinct(self) -> int:
        return len(self.counts)

    def key(self) -> tuple:
        return tuple(sorted(self.counts.items()))


def plan_optimizer(
    items: Sequence[MenuItem],
    constraints: Constraints,
    preset: str = "balanced",
    config: Optional[SolverConfig] = None,
) -> List[Plan]:
    """在给定约束与目标偏好下搜索若干套方案。"""
    cfg = config or SolverConfig()
    weights = PRESETS.get(preset, PRESETS["balanced"])

    pool = [
        i
        for i in items
        if i.effective_price > 0
        and not is_excluded(i)
        and (i.price_reliable or not constraints.strict_price)
    ]
    pool.sort(key=lambda i: (i.kcal <= 0, i.effective_price))
    index = {i.code: i for i in pool}
    if not pool:
        return []

    people = max(constraints.people, 1)
    kcal_lo = constraints.kcal_min * people
    kcal_hi = constraints.kcal_max * people
    protein_min = constraints.protein_min * people
    sodium_max = constraints.sodium_max * people
    budget = constraints.budget
    kcal_target = (kcal_lo + kcal_hi) / 2 if kcal_hi > kcal_lo else kcal_hi

    def evaluate(basket: _Basket) -> Optional[tuple]:
        total_price = 0.0
        kcal = 0.0
        protein = 0.0
        sodium = 0.0
        has_drink = False
        has_combo = False
        cats = set()
        discount_value = 0.0
        for code, qty in basket.counts.items():
            it = index[code]
            total_price += it.effective_price * qty
            if it.nutrition:
                kcal += it.kcal * qty
                protein += it.protein * qty
                sodium += it.nutrition.sodium * qty
            discount_value += max(it.original_price - it.effective_price, 0.0) * qty
            cats.update(it.categories)
            if is_drink(it):
                has_drink = True
            if is_combo(it):
                has_combo = True

        if total_price > budget + 1e-6:
            return None
        if kcal > 0:  # kcal=0 表示营养数据缺失，不参与热量硬约束
            if kcal > kcal_hi + 1e-6:
                return None
            if kcal_lo > 0 and kcal < kcal_lo - 1e-6:
                return None
        if protein_min > 0 and protein < protein_min - 1e-6:
            return None
        if sodium > sodium_max + 1e-6:
            return None
        if constraints.require_drink and not (has_drink or has_combo):
            return None
        if not cfg.allow_duplicate_combo:
            combos = [c for c in basket.counts if is_combo(index[c])]
            if len(combos) > 1:
                return None

        kcal_dev = _range_penalty(kcal, kcal_lo, kcal_hi) if kcal > 0 else 0.0
        score = (
            weights["protein"] * protein
            - weights["price"] * total_price
            - weights["kcal_dev"] * (abs(kcal - kcal_target) + kcal_dev)
            - weights["sodium"] * sodium
            + weights["variety"] * len(cats)
            + weights["discount"] * discount_value
        )
        return score, total_price, kcal, protein

    # ---------------- Beam Search ----------------
    beam: List[_Basket] = [_Basket()]
    best: Dict[tuple, float] = {}
    seen: set = set()

    for _depth in range(cfg.max_distinct * cfg.max_qty):
        candidates: List[tuple] = []
        for basket in beam:
            if basket.size >= cfg.max_distinct * cfg.max_qty:
                continue
            for it in pool:
                qty = basket.counts.get(it.code, 0)
                if qty >= cfg.max_qty:
                    continue
                if basket.distinct >= cfg.max_distinct and qty == 0:
                    continue
                # 剪枝：加一份就超预算的直接跳过
                base = sum(index[c].effective_price * q for c, q in basket.counts.items())
                if base + it.effective_price > budget + 1e-6:
                    continue
                nb = basket.add(it.code)
                k = nb.key()
                if k in seen:
                    continue
                seen.add(k)
                res = evaluate(nb)
                if res is None:
                    continue
                candidates.append((res[0], nb, res))
        if not candidates:
            break
        candidates.sort(key=lambda t: t[0], reverse=True)
        beam = [c[1] for c in candidates[: cfg.beam_width]]
        for score, nb, res in candidates:
            best[nb.key()] = score

    # 汇总可行解并按分数排序
    scored: List[tuple] = []
    for key in best:
        basket = _Basket(dict(key))
        res = evaluate(basket)
        if res is None:
            continue
        if basket.size == 0:
            continue
        scored.append((res[0], basket, res))
    scored.sort(key=lambda t: t[0], reverse=True)

    plans: List[Plan] = []
    used_signatures: List[set] = []
    for score, basket, res in scored:
        plan_items = [
            PlanItem(item=index[code], quantity=qty)
            for code, qty in sorted(basket.counts.items(), key=lambda kv: -index[kv[0]].effective_price)
        ]
        signature = {p.item.code for p in plan_items}
        # 去重：与已选方案至少要有 1 个不同餐品
        if any(signature == s for s in used_signatures):
            continue
        overlap = max((len(signature & s) / len(signature | s) for s in used_signatures), default=0)
        if overlap > 0.75 and len(plans) > 0:
            continue
        used_signatures.append(signature)
        plan = Plan(
            name=f"{PRESET_LABELS.get(preset, preset)}方案{len(plans) + 1}",
            items=plan_items,
            score=round(score, 3),
        )
        plans.append(plan)
        if len(plans) >= cfg.top_k:
            break

    # 若约束过严导致无解，退化为「预算内最优单品」
    if not plans:
        affordable = sorted(
            (i for i in pool if i.effective_price <= budget),
            key=lambda i: -(i.protein * 2 - i.effective_price * 0.5),
        )
        if affordable:
            it = affordable[0]
            note = "当前约束下无完整可行解，已退化为预算内最优单品，请调宽条件后重试。"
        else:
            # 连最便宜的都买不起：给出最低消费参考，明确提示预算不可达
            it = min(pool, key=lambda i: i.effective_price)
            note = (
                f"预算 ¥{budget:.2f} 低于本店最低单品价 ¥{it.effective_price:.2f}，"
                "已给出最低消费参考。"
            )
        plans.append(
            Plan(
                name="兜底方案（约束过严，已放宽）",
                items=[PlanItem(item=it, quantity=1)],
                score=0.0,
                note=note,
            )
        )
    return plans
