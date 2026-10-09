"""编排层：把麦当劳 MCP 的多个工具串成一条完整决策链路。

链路（默认全部只读；领券/下单需显式授权）：

    now-time-info        → 确定时间段（早餐/正餐/夜宵）
    campaign-calendar    → 今日 & 本月活动雷达
    available-coupons    → 麦麦省可领券盘点
    query-my-coupons     → 券包资产
    query-nearby-stores  → 门店定位
    query-meals          → 门店实时菜单与价格
    calculate-price      → 官方标准名反查 + 真实核价
    list-nutrition-foods → 营养基线
    [本地] 求解器          → 多目标方案
    create-order         → 仅在用户明确确认后调用

所有远程结果默认落盘缓存（TTL 可调），避免重复消耗 600 次/分钟的额度。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .client import McdMcpClient, parse_tool_payload
from .exceptions import PlannerError
from .menu import parse_menu_payload, resolve_official_prices
from .models import Campaign, Constraints, Coupon, MenuItem, Plan, Store
from .nutrition import NutritionIndex, attach_nutrition, parse_nutrition_table
from .parsers import (
    parse_available_coupons,
    parse_campaigns,
    parse_my_coupons,
    parse_store_coupons,
    parse_stores,
)
from .solver import PRESET_LABELS, SolverConfig, plan_optimizer

logger = logging.getLogger(__name__)

__all__ = ["Planner", "RecommendationReport", "MEAL_PERIODS"]


MEAL_PERIODS = [
    (5, 10.5, "早餐时段", "早餐 5:00-10:30 供应，麦满分/热香饼性价比最高"),
    (10.5, 14.5, "午餐时段", "午市三件套与超值套餐折扣最集中"),
    (14.5, 17.5, "下午茶时段", "小食甜品 / 麦咖啡随心配"),
    (17.5, 22, "晚餐时段", "四件套与多人分享餐更划算"),
    (22, 24, "夜宵时段", "夜市限定，注意热量控制"),
    (0, 5, "深夜时段", "部分门店 24h，供应品类会收窄"),
]


@dataclass
class RecommendationReport:
    """一次完整推荐的结果快照（可直接序列化给前端）。"""

    store: Optional[Store] = None
    period: str = ""
    period_tip: str = ""
    now: Dict[str, Any] = field(default_factory=dict)
    campaigns: List[Campaign] = field(default_factory=list)
    coupons_available: List[Coupon] = field(default_factory=list)
    coupons_mine: List[Coupon] = field(default_factory=list)
    coupons_store: List[Coupon] = field(default_factory=list)
    menu_size: int = 0
    nutrition_size: int = 0
    nutrition_stats: Dict[str, int] = field(default_factory=dict)
    plans: List[Plan] = field(default_factory=list)
    menu: List[MenuItem] = field(default_factory=list)
    constraints: Optional[Constraints] = None
    tool_trace: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["constraints"] = asdict(self.constraints) if self.constraints else None
        return d


class _FileCache:
    """极简磁盘缓存，键 = 工具名 + 参数哈希。"""

    def __init__(self, root: Optional[str], ttl: int) -> None:
        self.root = root
        self.ttl = ttl
        if root:
            os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return os.path.join(self.root or "", f"{h}.json")

    def get(self, key: str) -> Optional[str]:
        if not self.root:
            return None
        p = self._path(key)
        if not os.path.exists(p) or (time.time() - os.path.getmtime(p) > self.ttl):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def set(self, key: str, value: str) -> None:
        if not self.root:
            return
        try:
            with open(self._path(key), "w", encoding="utf-8") as f:
                f.write(value)
        except OSError:
            pass


class Planner:
    """麦麦精算师主对象。"""

    def __init__(
        self,
        client: Optional[McdMcpClient] = None,
        token: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_ttl: int = 600,
    ) -> None:
        self.client = client or McdMcpClient(token=token)
        self.cache = _FileCache(cache_dir, cache_ttl)
        self.trace: List[Dict[str, str]] = []

    # ------------------------------------------------------------------ #
    # 基础调用
    # ------------------------------------------------------------------ #
    def _call(self, tool: str, args: Optional[Dict[str, Any]] = None, use_cache: bool = True) -> str:
        key = f"{tool}:{json.dumps(args or {}, sort_keys=True, ensure_ascii=False)}"
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                self.trace.append({"tool": tool, "status": "cache", "ms": "0"})
                return cached
        started = time.time()
        result = self.client.call(tool, args or {})
        cost = int((time.time() - started) * 1000)
        self.trace.append(
            {"tool": tool, "status": "error" if result.is_error else "ok", "ms": str(cost)}
        )
        if result.is_error:
            raise PlannerError(f"{tool} 调用失败：{result.text[:200]}")
        if use_cache:
            self.cache.set(key, result.text)
        return result.text

    # ------------------------------------------------------------------ #
    # 单项能力
    # ------------------------------------------------------------------ #
    def now(self) -> Dict[str, Any]:
        payload = parse_tool_payload(self._call("now-time-info", {}, use_cache=False))
        return (payload or {}).get("data", {}) or {}

    def current_period(self) -> tuple:
        info = self.now()
        clock = str(info.get("formatted", "00:00:00"))
        try:
            hour = int(clock[11:13])
            minute = int(clock[14:16])
        except ValueError:
            hour, minute = 0, 0
        t = hour + minute / 60
        for start, end, name, tip in MEAL_PERIODS:
            if start <= t < end:
                return name, tip
        return "全天", ""

    def campaigns(self, specified_date: Optional[str] = None) -> List[Campaign]:
        args = {"specifiedDate": specified_date} if specified_date else {}
        return parse_campaigns(self._call("campaign-calendar", args))

    def available_coupons(self) -> List[Coupon]:
        return parse_available_coupons(self._call("available-coupons"))

    def my_coupons(self, page: str = "1", page_size: str = "200") -> List[Coupon]:
        return parse_my_coupons(self._call("query-my-coupons", {"page": page, "pageSize": page_size}))

    def store_coupons(self, store_code: str, order_type: int = 1, be_type: int = 1) -> List[Coupon]:
        return parse_store_coupons(
            self._call(
                "query-store-coupons",
                {"storeCode": store_code, "orderType": order_type, "beType": be_type},
            )
        )

    def claim_all_coupons(self, confirm: bool = False) -> str:
        """一键领取麦麦省全部可领券。**有副作用**，必须显式 confirm=True。"""
        if not confirm:
            raise PlannerError(
                "auto-bind-coupons 会真实领取优惠券到你的麦当劳账户，请显式传入 confirm=True。"
            )
        return self._call("auto-bind-coupons", {}, use_cache=False)

    def find_stores(self, city: str, keyword: str, be_type: int = 1) -> List[Store]:
        payload = parse_tool_payload(
            self._call(
                "query-nearby-stores",
                {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword},
            )
        )
        return parse_stores((payload or {}).get("data"))

    def favorite_stores(self, be_type: int = 1) -> List[Store]:
        payload = parse_tool_payload(
            self._call("query-nearby-stores", {"beType": be_type, "searchType": 1})
        )
        return parse_stores((payload or {}).get("data")) if payload else []

    def nutrition_index(self) -> NutritionIndex:
        payload = parse_tool_payload(self._call("list-nutrition-foods")) or {}
        raw = payload.get("data") or ""
        if isinstance(raw, list):
            raw = "\n".join(str(x) for x in raw)
        return NutritionIndex(parse_nutrition_table(raw))

    def menu(
        self,
        store_code: str,
        order_type: int = 1,
        be_type: int = 1,
        resolve_canonical: bool = True,
    ) -> List[MenuItem]:
        payload = parse_tool_payload(
            self._call(
                "query-meals",
                {"storeCode": store_code, "orderType": order_type, "beType": be_type},
            )
        )
        items = parse_menu_payload((payload or {}).get("data") or {})
        if not items:
            return []
        if resolve_canonical:
            resolved = self._resolve_official(store_code, order_type, be_type, items)
            for it in items:
                info = resolved.get(it.code)
                if not info:
                    continue
                it.canonical_name = info.get("name", "")
                if info.get("price"):
                    it.official_price = info["price"]
        return items

    def _resolve_official(
        self, store_code: str, order_type: int, be_type: int, items: Sequence[MenuItem]
    ) -> Dict[str, Dict[str, Any]]:
        def batch(payload):
            text = self._call(
                "calculate-price",
                {
                    "storeCode": store_code,
                    "orderType": order_type,
                    "beType": be_type,
                    "items": payload,
                },
                use_cache=False,
            )
            data = (parse_tool_payload(text) or {}).get("data") or {}
            return data.get("productList") or []

        return resolve_official_prices(batch, list(items))

    def price_check(
        self, store_code: str, codes: Dict[str, int], order_type: int = 1, be_type: int = 1
    ) -> Dict[str, Any]:
        """用 calculate-price 对方案做真实核价，返回以「元」为单位的明细。"""
        items = [{"productCode": c, "quantity": q} for c, q in codes.items() if q > 0]
        if not items:
            return {}
        data = (
            parse_tool_payload(
                self._call(
                    "calculate-price",
                    {
                        "storeCode": store_code,
                        "orderType": order_type,
                        "beType": be_type,
                        "items": items,
                    },
                    use_cache=False,
                )
            )
            or {}
        ).get("data") or {}
        return {
            "price": round(data.get("price", 0) / 100, 2),
            "original_price": round(data.get("originalPrice", 0) / 100, 2),
            "discount": round(data.get("discount", 0) / 100, 2),
            "product_list": data.get("productList") or [],
        }

    def create_order(
        self,
        store_code: str,
        codes: Dict[str, int],
        order_type: int = 1,
        be_type: int = 1,
        confirm: bool = False,
        **extra,
    ) -> Dict[str, Any]:
        """真实下单。**有副作用**，必须显式 confirm=True。"""
        if not confirm:
            raise PlannerError("create-order 会产生真实订单，请显式传入 confirm=True。")
        payload = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": [{"productCode": c, "quantity": q} for c, q in codes.items() if q > 0],
        }
        payload.update(extra)
        res = self._call("create-order", payload, use_cache=False)
        return (parse_tool_payload(res) or {}).get("data") or {"raw": res}

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def recommend(
        self,
        store: Store,
        constraints: Constraints,
        presets: Optional[List[str]] = None,
        with_campaigns: bool = True,
        with_coupons: bool = True,
        verify_price: bool = True,
        order_type: int = 1,
        be_type: int = 1,
        include_menu: bool = False,
        topk_per_preset: int = 1,
    ) -> RecommendationReport:
        """跑完整链路，返回可直接展示的报告。"""
        presets = presets or ["balanced", "cheapest", "high_protein", "light"]
        args_topk = max(1, topk_per_preset)
        report = RecommendationReport(store=store, constraints=constraints)
        self.trace = []

        report.now = self.now()
        period, tip = self.current_period()
        report.period, report.period_tip = period, tip

        if with_campaigns:
            try:
                report.campaigns = self.campaigns(report.now.get("date"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("活动日历获取失败：%s", exc)
        if with_coupons:
            try:
                report.coupons_available = self.available_coupons()
            except Exception as exc:  # noqa: BLE001
                logger.warning("麦麦省券获取失败：%s", exc)
            try:
                report.coupons_mine = self.my_coupons()
            except Exception as exc:  # noqa: BLE001
                logger.warning("我的券包获取失败：%s", exc)
            try:
                report.coupons_store = self.store_coupons(store.store_code, order_type, be_type)
            except Exception as exc:  # noqa: BLE001
                logger.warning("门店券获取失败：%s", exc)

        items = self.menu(store.store_code, order_type, be_type)
        report.menu_size = len(items)
        if include_menu:
            report.menu = items

        index = self.nutrition_index()
        report.nutrition_size = len(index)
        stats = attach_nutrition(items, index)
        stats["unreliable"] = sum(1 for i in items if not i.price_reliable)
        report.nutrition_stats = stats

        cfg = SolverConfig(max_distinct=max(1, min(constraints.max_items, 4)), top_k=4)
        seen: set = set()
        for preset in presets:
            accepted = 0
            best_over: Optional[Plan] = None
            for plan in plan_optimizer(items, constraints, preset=preset, config=cfg):
                sig = tuple(sorted(plan.codes.items()))
                if sig in seen:
                    continue
                seen.add(sig)
                if verify_price:
                    try:
                        checked = self.price_check(store.store_code, plan.codes, order_type, be_type)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("核价失败：%s", exc)
                        checked = {}
                    if checked:
                        plan.verified_price = checked["price"]
                        plan.verified_discount = checked["discount"]
                        plan.price_gap = round(checked["price"] - plan.list_price, 2)
                        if checked["price"] > constraints.budget + 1e-6:
                            plan.over_budget = True
                            if best_over is None or checked["price"] < (best_over.verified_price or 1e9):
                                best_over = plan
                            continue
                plan.name = PRESET_LABELS.get(preset, preset)
                report.plans.append(plan)
                accepted += 1
                if accepted >= args_topk:
                    break
            if accepted == 0 and best_over is not None:
                best_over.name = PRESET_LABELS.get(preset, preset)
                report.plans.append(best_over)

        report.plans.sort(key=lambda p: (p.over_budget, p.total_price))
        report.tool_trace = self.trace
        return report
