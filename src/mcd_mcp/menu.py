"""菜单解析：把 `query-meals` 的原始载荷转换为 MenuItem 列表，
并可选地用 `calculate-price` 批量反查官方标准名（用于营养表对齐 + 真实核价）。"""

from __future__ import annotations

import logging
from typing import Dict, List, Sequence

from .models import MenuItem

logger = logging.getLogger(__name__)

__all__ = ["parse_menu_payload", "resolve_official_prices"]


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_menu_payload(data: dict) -> List[MenuItem]:
    """解析 query-meals 的 data 节点。"""
    if not data:
        return []
    detail: Dict[str, dict] = data.get("meals") or {}
    items: Dict[str, MenuItem] = {}

    for cat in data.get("categories") or []:
        cat_name = (cat.get("name") or "").replace("\n", "")
        for entry in cat.get("meals") or []:
            code = str(entry.get("code") or "")
            if not code:
                continue
            meta = detail.get(code) or {}
            item = items.get(code)
            if item is None:
                item = MenuItem(
                    code=code,
                    name=meta.get("name") or code,
                    price=_f(meta.get("currentPrice")),
                    original_price=_f(meta.get("originalPrice")),
                    image=meta.get("image") or "",
                )
                items[code] = item
            if cat_name and cat_name not in item.categories:
                item.categories.append(cat_name)
            for tag in entry.get("tags") or []:
                if tag and tag not in item.tags:
                    item.tags.append(tag)

    # 兜底：只在 meals 映射里出现、未挂在任何分类下的餐品
    for code, meta in detail.items():
        if code not in items:
            items[code] = MenuItem(
                code=str(code),
                name=meta.get("name") or str(code),
                price=_f(meta.get("currentPrice")),
                original_price=_f(meta.get("originalPrice")),
                image=meta.get("image") or "",
            )
    return list(items.values())


def resolve_official_prices(
    calc_batch_fn,
    items: Sequence[MenuItem],
    chunk_size: int = 25,
) -> Dict[str, Dict[str, Any]]:
    """用 calculate-price 批量反查「官方标准名 + 官方核价」。

    `calculate-price` 会回传官方口径的 productName（菜单里叫「薯条」，它回传
    「中薯条」）与结算价 subtotal。实测两者都可能与 query-meals 的展示价不同
    ——例如燕麦奶铁菜单展示 ¥9.9，官方核价 ¥23.0——因此这里把官方口径作为
    求解与结算的基准，菜单展示价仅作对照。
    """
    resolved: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(items), chunk_size):
        chunk = list(items[i : i + chunk_size])
        payload = [{"productCode": it.code, "quantity": 1} for it in chunk]
        try:
            product_list = calc_batch_fn(payload) or []
        except Exception as exc:  # noqa: BLE001 - 核价失败不阻断主流程
            logger.warning("批量核价失败（第 %d 批）: %s", i // chunk_size + 1, exc)
            continue
        for row in product_list:
            code = str(row.get("productCode") or "")
            if not code:
                continue
            name = (row.get("productName") or "").strip()
            subtotal = _f(row.get("subtotal"), 0.0) / 100
            entry = resolved.setdefault(code, {"name": name, "price": 0.0, "hits": 0})
            entry["hits"] += 1
            if name:
                entry["name"] = name
            # 同一 code 多次出现时取最大值，避免「第二份半价」类促销污染单份基准价
            entry["price"] = max(entry["price"], subtotal)
    return resolved
