"""营养数据解析 + 菜单/营养表名称对齐。

麦当劳 `list-nutrition-foods` 返回的 `data` 是一段紧凑文本::

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      ...

而 `query-meals` 的餐品名是门店菜单口径（如「薯条」「可乐」），与营养表口径
（「中薯条」「可乐中杯」）并不一致。本模块提供三级匹配：

1. 精确匹配（标准化后）
2. 别名表（人工沉淀的高频差异）
3. difflib 模糊匹配（带阈值）
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from .models import MenuItem, NutritionFact

__all__ = ["parse_nutrition_table", "NutritionIndex", "attach_nutrition"]

_HEADER_RE = re.compile(r"\[(?P<count>\d+)\]\{(?P<cols>[^}]*)\}")
_ROW_SPLIT = re.compile(r"[\r\n]+")

# 菜单口径 -> 营养表口径 的高频别名
ALIASES: Dict[str, str] = {
    "薯条": "中薯条",
    "可乐": "可乐中杯",
    "雪碧": "雪碧中杯",
    "派": "香芋派",
    "玉米杯": "大杯玉米杯",
    "圆筒冰淇淋": "圆筒冰淇淋",
    "纯牛奶(盒装)": "纯牛奶（盒装）",
    "纯牛奶": "纯牛奶（盒装）",
    "那么大鸡排（椒盐风味）": "那么大鸡排",
    "麦辣鸡翅": "麦辣鸡翅-2块",
    "浓缩咖啡": "浓缩咖啡",
    "鲜萃咖啡": "大杯鲜萃咖啡",
    "锡兰红茶": "锡兰红茶",
    "阳光柠檬红茶": "阳光柠檬红茶中杯",
    "灰焰圆筒": "圆筒冰淇淋",
    "心形薯饼5块": "脆薯饼",
    "经典麦旋风": "奥利奥麦旋风",
    "苹果片一袋(60g)": "苹果片",
    "冰燕麦奶铁中杯": "燕麦奶铁",
    "热燕麦奶铁中杯": "燕麦奶铁",
}


def _to_float(value: str) -> float:
    value = (value or "").strip()
    if not value or value.lower() in {"null", "none", "-", "--"}:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_nutrition_table(raw: str) -> List[NutritionFact]:
    """解析 list-nutrition-foods 返回的紧凑表格字符串。"""
    if not raw:
        return []
    facts: List[NutritionFact] = []
    lines = _ROW_SPLIT.split(raw)
    for line in lines:
        line = line.strip().lstrip("　 ")
        if not line:
            continue
        if _HEADER_RE.search(line):
            # 形如 "[160]{...}:" 或同一行尾部还跟着第一条目
            line = _HEADER_RE.sub("", line).lstrip(":：").strip()
            if not line:
                continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        name, desc = parts[0], parts[1]
        if not name:
            continue
        facts.append(
            NutritionFact(
                product_name=name,
                description="" if desc.lower() == "null" else desc,
                energy_kj=_to_float(parts[2]),
                energy_kcal=_to_float(parts[3]),
                protein=_to_float(parts[4]),
                fat=_to_float(parts[5]),
                carbohydrate=_to_float(parts[6]),
                sodium=_to_float(parts[7]),
                calcium=_to_float(parts[8]),
            )
        )
    return facts


_PUNCT = re.compile(r"[\s（）()【】\[\]“”\"'’·、,，。.!！?？~～\-—_/\\:：™®]+")


def normalize(name: str) -> str:
    """标准化餐品名：去空白、去标点、全角转半角。"""
    if not name:
        return ""
    out = name
    # 全角数字/字母转半角
    out = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in out)
    out = _PUNCT.sub("", out)
    return out.lower()


class NutritionIndex:
    """营养表索引，支持精确/别名/模糊三级查找。"""

    def __init__(self, facts: List[NutritionFact]) -> None:
        self.facts = facts
        self._exact: Dict[str, NutritionFact] = {}
        for f in facts:
            self._exact.setdefault(normalize(f.product_name), f)
        self._norm_keys = list(self._exact.keys())
        for src, dst in ALIASES.items():
            target = self._exact.get(normalize(dst))
            if target:
                self._exact.setdefault(normalize(src), target)

    def __len__(self) -> int:
        return len(self.facts)

    def lookup(self, name: str) -> Tuple[Optional[NutritionFact], float]:
        """返回 (营养事实, 匹配置信度 0~1)。"""
        if not name:
            return None, 0.0
        key = normalize(name)
        if key in self._exact:
            return self._exact[key], 1.0
        best: Optional[NutritionFact] = None
        best_score = 0.0
        for k, fact in self._exact.items():
            if not k or len(k) < 3 or len(key) < 3:
                continue
            if k in key or key in k:
                ratio = min(len(k), len(key)) / max(len(k), len(key))
                score = 0.7 + 0.2 * ratio  # 包含关系：0.7 ~ 0.9
                if score > best_score:
                    best, best_score = fact, score
        if best is not None:
            return best, round(best_score, 3)
        close = difflib.get_close_matches(key, self._norm_keys, n=1, cutoff=0.62)
        if close:
            return self._exact[close[0]], 0.6
        return None, 0.0


def attach_nutrition(
    items: List[MenuItem], index: NutritionIndex, min_confidence: float = 0.6
) -> Dict[str, int]:
    """把营养数据挂到菜单项上，返回统计。"""
    stats = {"matched": 0, "fuzzy": 0, "missing": 0}
    for it in items:
        fact, conf = index.lookup(it.match_name)
        if fact is None:
            fact, conf = index.lookup(it.name)
        if fact and conf >= min_confidence:
            it.nutrition = fact
            it.nutrition_confidence = conf
            if conf >= 0.999:
                stats["matched"] += 1
            else:
                stats["fuzzy"] += 1
        else:
            stats["missing"] += 1
    _estimate_missing(items)
    return stats


def _estimate_missing(items: List[MenuItem]) -> None:
    """对未命中的餐品，用已命中单品的「热量/元」中位数按价格比例估算。

    估算结果会打上 nutrition_estimated 标记，报告里必须显式提示。
    """
    known = [i for i in items if i.nutrition and i.effective_price > 0]
    if not known:
        return
    kcal_per_yuan = sorted(i.kcal / i.effective_price for i in known if i.kcal > 0)
    if not kcal_per_yuan:
        return
    mid = kcal_per_yuan[len(kcal_per_yuan) // 2]
    # 钠同样按「mg/元」中位数估算，否则估算项钠恒为 0，会让控钠约束失真
    na_per_yuan = sorted(
        i.nutrition.sodium / i.effective_price
        for i in known
        if i.nutrition and i.nutrition.sodium > 0
    )
    na_mid = na_per_yuan[len(na_per_yuan) // 2] if na_per_yuan else 0.0
    for i in items:
        if i.nutrition is None:
            est = round(i.effective_price * mid, 1)
            i.nutrition = NutritionFact(
                product_name=i.name,
                energy_kcal=est,
                energy_kj=round(est * 4.184, 1),
                protein=round(est * 0.05, 1),
                fat=round(est * 0.05, 1),
                carbohydrate=round(est * 0.11, 1),
                sodium=round(i.effective_price * na_mid, 1),
            )
            i.nutrition_confidence = 0.0
            i.nutrition_estimated = True
