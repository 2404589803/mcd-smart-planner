"""把麦当劳 MCP 返回的 Markdown / JSON 载荷解析成领域对象。

部分工具返回的是「给 LLM 看的 Markdown」而非纯 JSON，这里做结构化还原。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .models import Campaign, Coupon, Store

__all__ = [
    "parse_campaigns",
    "parse_available_coupons",
    "parse_my_coupons",
    "parse_store_coupons",
    "parse_stores",
]

_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_DATE_HEAD_RE = re.compile(r"^####\s*(?P<date>[^\s]+)\s*(?P<phase>.*)$")


def _first_image(text: str) -> str:
    m = _IMG_RE.search(text or "")
    return m.group(1) if m else ""


def parse_campaigns(text: str) -> List[Campaign]:
    """解析 campaign-calendar 的 Markdown 输出。"""
    out: List[Campaign] = []
    if not text:
        return out
    current_date, current_phase = "", ""
    title, content, image = "", "", ""
    blocks = re.split(r"\n(?=####\s)", text)
    for block in blocks:
        head = block.strip().splitlines()[0] if block.strip() else ""
        m = _DATE_HEAD_RE.match(head.strip())
        if not m:
            continue
        date = m.group("date")
        phase = (m.group("phase") or "").strip() or "进行中"
        # 一个日期下可能有多条活动
        entries = re.split(r"\n\s*-\s+\*\*活动标题\*\*", block)
        for entry in entries[1:]:
            entry = "**活动标题**" + entry
            t = re.search(r"\*\*活动标题\*\*[：:]\s*(.+)", entry)
            c = re.search(r"\*\*活动内容介绍\*\*[：:]\s*(.*?)(?=\n\s*\*\*|\Z)", entry, re.S)
            out.append(
                Campaign(
                    date=date,
                    phase=phase,
                    title=(t.group(1).strip() if t else "").rstrip("\\").strip(),
                    content=(c.group(1).strip() if c else ""),
                    image=_first_image(entry),
                )
            )
        current_date, current_phase = date, phase
    return [c for c in out if c.title]


def parse_available_coupons(text: str) -> List[Coupon]:
    """解析 available-coupons（麦麦省可领券）。"""
    out: List[Coupon] = []
    if not text:
        return out
    blocks = re.split(r"\n\s*-\s+优惠券标题[：:]", text)
    for block in blocks[1:]:
        t = re.search(r"^(.*?)(?:\n|\\|$)", block.strip())
        s = re.search(r"状态[：:]\s*([^\n\\]+)", block)
        out.append(
            Coupon(
                title=(t.group(1) if t else "").strip().rstrip("\\").strip(),
                source="available",
                status=(s.group(1).strip() if s else ""),
                image=_first_image(block),
            )
        )
    return [c for c in out if c.title]


def parse_my_coupons(text: str) -> List[Coupon]:
    """解析 query-my-coupons（券包）。"""
    out: List[Coupon] = []
    if not text:
        return out
    if "暂无" in text:
        return out
    # 形如 "- 券名：xxx  券码：yyy  有效期：zzz"
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"券名[：:]\s*([^\n]+)", line)
        if m:
            out.append(Coupon(title=m.group(1).strip(), source="mine", status="可用"))
    return out


def parse_store_coupons(text: str) -> List[Coupon]:
    """解析 query-store-coupons 的 Markdown 表格。"""
    out: List[Coupon] = []
    if not text:
        return out
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("|")]
    for line in lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= {"-", ":"}:
            continue
        if cells[0] in {"券ID", "couponId"}:
            continue
        out.append(
            Coupon(
                title=cells[3] if len(cells) > 4 else cells[0],
                source="store",
                coupon_id=cells[0],
                coupon_code=cells[1],
                valid_range=cells[2],
                products=[p for p in cells[3].split("<br>") if p] if len(cells) > 3 else [],
            )
        )
    return out


def parse_stores(data: Any) -> List[Store]:
    """解析 query-nearby-stores / delivery-query-stores 的门店数组。"""
    rows = data if isinstance(data, list) else (data or {}).get("data", [])
    if not isinstance(rows, list):
        return []
    out: List[Store] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = row.get("businessStartTime") or ""
        end = row.get("businessEndTime") or ""
        out.append(
            Store(
                store_code=str(row.get("storeCode") or ""),
                name=row.get("storeName") or "",
                address=row.get("address") or "",
                distance=float(row.get("distance") or 0),
                be_code=str(row.get("beCode") or ""),
                business_status=bool(row.get("businessStatus", True)),
                business_hours=f"{start}-{end}" if start or end else "",
            )
        )
    return [s for s in out if s.store_code]
