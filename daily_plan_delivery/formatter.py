"""Fixed deterministic formatter for already-confirmed Delivery plans."""

from __future__ import annotations

from datetime import date

from .models import DeliveryItem, DeliveryPlan


LABELS = {
    "USER_REQUIRED": "【必须完成】",
    "USER_ARRANGED": "【你安排的】",
    "CARRYOVER": "【顺延】",
    "ROADMAP": "【路线推荐】",
    "SYSTEM_RECOMMENDED": "【系统推荐】",
    "READING": "【阅读】",
}
DISPLAY_ORDER = (
    "USER_REQUIRED", "USER_ARRANGED", "CARRYOVER", "ROADMAP",
    "SYSTEM_RECOMMENDED", "READING",
)


def _ordered_items(plan: DeliveryPlan) -> tuple[DeliveryItem, ...]:
    by_id = {item.item_id: item for item in plan.items}
    ordered = [by_id[item_id] for item_id in plan.execution_order]
    ordered.extend(item for item in sorted(plan.items, key=lambda value: value.item_id)
                   if item.item_id not in set(plan.execution_order))
    return tuple(ordered)


def _item_line(item: DeliveryItem) -> str:
    suffix = f" {item.minutes}min" if item.minutes else ""
    hint = f"（{item.time_hint}）" if item.time_hint else ""
    return f"- {item.title}{suffix}{hint}"


def format_delivery_plan(plan: DeliveryPlan) -> str:
    target = date.fromisoformat(plan.target_date)
    lines = [f"{target.month} 月 {target.day} 日明日计划", ""]
    ordered = _ordered_items(plan)
    if plan.fixed_constraints:
        lines.extend(["【固定 / 不可安排】"])
        lines.extend(f"- {item.title}：{item.detail}" for item in plan.fixed_constraints)

    for source in DISPLAY_ORDER:
        values = [item for item in ordered if item.source == source]
        if not values:
            continue
        if lines[-1] != "":
            lines.append("")
        lines.append(LABELS[source])
        for item in values:
            lines.append(_item_line(item))
            if item.reason:
                lines.append(f"  原因：{item.reason}")

    if plan.execution_order:
        if lines[-1] != "":
            lines.append("")
        lines.append("【执行顺序】")
        by_id = {item.item_id: item for item in plan.items}
        for index, item_id in enumerate(plan.execution_order, 1):
            lines.append(f"{index}. {by_id[item_id].title}")

    lines.extend(["", f"缓冲：{plan.buffer_minutes}min", "状态：已确认"])
    if plan.warnings:
        lines.extend(["", "【警告】"])
        lines.extend(f"- {warning}" for warning in plan.warnings)
    return "\n".join(lines) + "\n"
