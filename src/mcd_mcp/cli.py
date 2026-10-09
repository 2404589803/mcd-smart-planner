"""命令行入口：麦麦精算师 CLI。

    mcd tools                        查看当前 Token 可用的 MCP 工具
    mcd stores  --city 上海 --kw 人民广场
    mcd campaigns                   今日 / 本月活动雷达
    mcd coupons [--claim]           麦麦省券盘点（--claim 会真实领券）
    mcd plan    --city 上海 --kw 人民广场 --budget 35 --kcal-max 800
    mcd serve   --port 8000         启动 Web 可视化
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .client import McdMcpClient
from .exceptions import McdError
from .models import Constraints
from .pipeline import Planner

__all__ = ["main", "build_parser"]


def _fmt_money(v: float) -> str:
    return f"¥{v:.2f}"


def _rule(title: str = "", width: int = 68) -> None:
    if title:
        print(f"\n{'─' * 4} {title} {'─' * max(0, width - len(title) - 6)}")
    else:
        print("─" * width)


def _planner(args) -> Planner:
    cache_dir = getattr(args, "cache_dir", None) or os.path.join(os.getcwd(), ".cache")
    no_cache = getattr(args, "no_cache", False)
    return Planner(
        token=getattr(args, "token", None),
        cache_dir=None if no_cache else cache_dir,
        cache_ttl=getattr(args, "cache_ttl", 600),
    )


# ---------------------------------------------------------------------- #
def cmd_tools(args) -> int:
    client = McdMcpClient(token=args.token)
    tools = client.list_tools()
    print(f"当前 MCP Server 共开放 {len(tools)} 个工具：\n")
    for t in tools:
        req = ",".join(t.required) or "-"
        print(f"  · {t.name:<28} 必填参数: {req}")
    return 0


def cmd_stores(args) -> int:
    p = _planner(args)
    stores = p.find_stores(args.city, args.kw, be_type=args.be_type)
    if not stores:
        print("未找到门店，换个关键词试试。")
        return 1
    print(f"在「{args.city} · {args.kw}」附近找到 {len(stores)} 家门店：\n")
    for i, s in enumerate(stores[: args.limit], 1):
        status = "营业中" if s.business_status else "休息中"
        print(f"  {i}. {s.name}")
        print(f"     storeCode={s.store_code}  距离 {s.distance:.0f}m  {status}  {s.business_hours}")
        print(f"     {s.address}")
    return 0


def cmd_campaigns(args) -> int:
    p = _planner(args)
    items = p.campaigns(args.date)
    if not items:
        print("暂无活动数据。")
        return 0
    print(f"活动雷达（{len(items)} 条）\n")
    for c in items[: args.limit]:
        print(f"  [{c.date} · {c.phase}] {c.title}")
        if args.verbose and c.content:
            brief = c.content.replace("\n", " ")[:120]
            print(f"      {brief}")
    return 0


def cmd_coupons(args) -> int:
    p = _planner(args)
    available = p.available_coupons()
    mine = p.my_coupons()
    print(f"麦麦省可领取 {len(available)} 张，我的券包 {len(mine)} 张\n")
    for c in available[: args.limit]:
        print(f"  · [{c.status or '可领取'}] {c.title}")
    if mine:
        _rule("我的券包")
        for c in mine[: args.limit]:
            print(f"  · {c.title}  {c.valid_range}")
    if args.claim:
        if not args.yes:
            ans = input("确认一键领取全部可领券？该操作会写入你的麦当劳账户 [y/N] ")
            if ans.strip().lower() != "y":
                print("已取消。")
                return 0
        print(p.claim_all_coupons(confirm=True))
    return 0


def cmd_plan(args) -> int:
    p = _planner(args)
    if args.store:
        stores = p.favorite_stores()
        store = next((s for s in stores if s.store_code == args.store), None)
        if store is None:
            from .models import Store

            store = Store(store_code=args.store, name=f"门店 {args.store}")
    else:
        if not args.city or not args.kw:
            print("请用 --store 指定门店编码，或用 --city/--kw 搜索门店。")
            return 2
        stores = p.find_stores(args.city, args.kw, be_type=args.be_type)
        if not stores:
            print("未找到门店。")
            return 1
        store = stores[0]

    c = Constraints(
        budget=args.budget,
        kcal_min=args.kcal_min,
        kcal_max=args.kcal_max,
        protein_min=args.protein_min,
        sodium_max=args.sodium_max,
        max_items=args.max_items,
        require_drink=not args.no_drink,
        people=args.people,
        strict_price=not args.no_strict_price,
    )
    report = p.recommend(
        store,
        c,
        presets=args.presets.split(",") if args.presets else None,
        order_type=args.order_type,
        be_type=args.be_type,
        verify_price=not args.no_verify,
    )

    _rule("麦麦精算师 · 决策报告")
    print(f"门店：{store.name}（{store.store_code}）")
    print(f"时间：{report.now.get('formatted', '-')}  时段：{report.period}")
    if report.period_tip:
        print(f"提示：{report.period_tip}")
    usable = report.menu_size - report.nutrition_stats.get("unreliable", 0)
    print(
        f"菜单 {report.menu_size} 项（价格口径可信 {usable} 项）/ 营养基线 {report.nutrition_size} 项 "
        f"(精确命中 {report.nutrition_stats.get('matched', 0)}，"
        f"模糊 {report.nutrition_stats.get('fuzzy', 0)}，"
        f"估算 {report.nutrition_stats.get('missing', 0)})"
    )

    if report.campaigns:
        _rule("今日活动")
        for c in report.campaigns[:3]:
            print(f"  · [{c.phase}] {c.title}")
    if report.coupons_available:
        _rule("可领优惠券")
        for c in report.coupons_available[:6]:
            print(f"  · {c.title}")

    _rule("推荐方案")
    for i, plan in enumerate(report.plans, 1):
        over = "  ⚠ 超预算" if plan.over_budget else ""
        print(f"\n  方案 {i}｜{plan.name}{over}   {_fmt_money(plan.total_price)}"
              f"   {plan.total_kcal:.0f} kcal   蛋白 {plan.total_protein:.1f} g"
              f"   钠 {plan.total_sodium:.0f} mg")
        for pi in plan.items:
            flag = " ≈" if pi.item.nutrition_estimated else ""
            print(f"      ×{pi.quantity}  {pi.item.name:<22} {_fmt_money(pi.subtotal):>8}"
                  f"   {pi.kcal:>6.0f} kcal{flag}")
        if plan.verified_price is not None:
            print(f"      实付核价 ¥{plan.verified_price:.2f}（菜单合计 ¥{plan.list_price:.2f}，"
                  f"优惠 ¥{plan.verified_discount or 0:.2f}）")

    _rule("MCP 调用链")
    for t in report.tool_trace:
        print(f"  {t['tool']:<24} {t['status']:<6} {t['ms']} ms")

    if any(pi.item.nutrition_estimated for pl in report.plans for pi in pl.items):
        print("\n  注：标 ≈ 的餐品营养为按价格估算值，非官方营养数据。")
    print("  注：餐品、价格与供应状态以麦当劳官方渠道实时结果为准。")
    return 0


def cmd_serve(args) -> int:
    from .web import serve  # noqa: PLC0415

    serve(
        host=args.host,
        port=args.port,
        token=args.token,
        cache_dir=args.cache_dir,
        open_browser=not args.no_browser,
    )
    return 0


# ---------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd",
        description="麦麦精算师 · 基于麦当劳中国 MCP 的点餐决策引擎",
    )
    parser.add_argument("--token", default=os.environ.get("MCD_MCP_TOKEN"), help="MCP Token")
    parser.add_argument("--cache-dir", default=os.path.join(os.getcwd(), ".cache"))
    parser.add_argument("--cache-ttl", type=int, default=600)
    parser.add_argument("--no-cache", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="列出 MCP 工具").set_defaults(func=cmd_tools)

    sp = sub.add_parser("stores", help="搜索门店")
    sp.add_argument("--city", required=True)
    sp.add_argument("--kw", required=True, help="位置关键词")
    sp.add_argument("--be-type", type=int, default=1, choices=[1, 5])
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(func=cmd_stores)

    cp = sub.add_parser("campaigns", help="活动日历")
    cp.add_argument("--date", help="yyyy-MM-dd")
    cp.add_argument("--limit", type=int, default=12)
    cp.add_argument("-v", "--verbose", action="store_true")
    cp.set_defaults(func=cmd_campaigns)

    up = sub.add_parser("coupons", help="优惠券盘点")
    up.add_argument("--limit", type=int, default=20)
    up.add_argument("--claim", action="store_true", help="一键领取（真实领券）")
    up.add_argument("--yes", action="store_true")
    up.set_defaults(func=cmd_coupons)

    pp = sub.add_parser("plan", help="生成点餐方案")
    pp.add_argument("--city")
    pp.add_argument("--kw")
    pp.add_argument("--store", help="门店编码 storeCode")
    pp.add_argument("--budget", type=float, default=40.0)
    pp.add_argument("--kcal-min", type=float, default=0.0)
    pp.add_argument("--kcal-max", type=float, default=900.0)
    pp.add_argument("--protein-min", type=float, default=0.0)
    pp.add_argument("--sodium-max", type=float, default=100000.0)
    pp.add_argument("--max-items", type=int, default=4)
    pp.add_argument("--people", type=int, default=1)
    pp.add_argument("--presets", default="balanced,cheapest,high_protein,light")
    pp.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6])
    pp.add_argument("--order-type", type=int, default=1, choices=[1, 2])
    pp.add_argument("--no-drink", action="store_true", help="不强制包含饮品")
    pp.add_argument("--no-strict-price", action="store_true",
                   help="允许菜单价与核价不一致的促销餐品参与规划")
    pp.add_argument("--no-verify", action="store_true", help="跳过 calculate-price 核价")
    pp.set_defaults(func=cmd_plan)

    sv = sub.add_parser("serve", help="启动 Web 界面")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--no-browser", action="store_true")
    sv.set_defaults(func=cmd_serve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except McdError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断。")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
