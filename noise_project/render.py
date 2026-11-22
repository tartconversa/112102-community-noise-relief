"""Human-oriented rendering of evidence and remediation reports."""

from __future__ import annotations

from .analysis import EvidenceReport, PlanReport
from .sources import format_nights


def _db(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def render_evidence(report: EvidenceReport) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add(f"社区夜间证据  截至第 {report.asof_day} 天（口径摘要 {report.digest[:12]}）")
    add("=" * 78)

    add("")
    add("[证据窗口]")
    add(f"  试营夜次（运营排期并集）: {format_nights(report.trial_nights)}")
    add(f"  试营前基线夜次:           {format_nights(report.pretrial_nights)}")
    add(f"  观测夜次:                 {format_nights(report.observed_nights)}")
    add(
        f"  口径: 同一固定观察窗的等效声级；校正互斥取最新已学习；"
        f"人群按听到的运营方均分"
    )

    add("")
    add("[一、观测到的夜间覆盖]（只陈述测到的夜晚，不把有数据的夜晚当成所有夜晚）")
    for area in report.coverage:
        add(f"  {area.area}: 暴露监测点 {', '.join(area.exposed_sensors) or '无'}")
        add(
            f"    完整覆盖 {format_nights(area.full_coverage_nights)}；"
            f"部分覆盖 {format_nights(area.partial_coverage_nights)}；"
            f"无暴露数据 {format_nights(area.unobserved_nights)}"
        )
        add(
            f"    湿夜 {format_nights(area.wet_trial_nights)}；"
            f"离线传感器-夜 {area.offline_sensor_nights}"
            f"（其中湿夜 {area.wet_offline_sensor_nights}）；"
            f"试营期实测比例 {area.trial_measured_fraction:.0%}"
        )

    add("")
    add("[二、可归因于试营的额外扰动]（扣独立道路背景，并按对照子区校正比较）")
    for area in report.attribution:
        add(f"  {area.area}: 状态 {area.status}")
        if area.extra_db_mean is not None:
            add(
                f"    额外声级（残余能量聚合）平均 {area.extra_db_mean:+.1f} dB，"
                f"范围 {area.extra_db_min:+.1f} 至 {area.extra_db_max:+.1f} dB"
            )
            if area.per_operator_share_db is not None:
                add(
                    f"    听到的运营方 {', '.join(area.operators_heard)}；"
                    f"按能量均分每方约 {area.per_operator_share_db:+.1f} dB（约定口径，非实测拆分）"
                )
        add(
            f"    适用人群（已验证暴露居民）{area.comparable_group_residents} 人；"
            + (
                "按运营方 "
                + "，".join(
                    f"{op} {count}"
                    for op, count in area.per_operator_residents.items()
                )
                if area.per_operator_residents
                else "无可用人群口径"
            )
        )
        for reason in area.reasons:
            add(f"    - {reason}")
        add(f"    有效时段: {area.validity_note}")
        for control in area.controls:
            where = "" if control.area == area.area else f"，位于 {control.area}"
            add(
                f"    对照 {control.sensor}{where}: {'可用' if control.clean else '不可用'}"
                f"（{control.reason}）"
            )

    add("")
    add("[三、数据不足与需补测]")
    for item in report.insufficient:
        severity = item.get("severity")
        label = {
            "fails_boundary": "未达边界",
            "partial_nights": "已通过但仍有未确证夜",
        }.get(severity, "")
        head = f"  [{item['scope']}] {item['id']}"
        if label:
            head += f"（{label}）"
        add(f"{head}: 补测夜次 {format_nights(item.get('re_measure_nights', []))}")
        for reason in item["reasons"]:
            add(f"    - {reason}")

    add("")
    add("[四、居住片区逐栋证据]")
    for building in report.buildings:
        status = "已验证" if building.verified else "未达边界"
        add(
            f"  {building.building}（{building.area}, 权重 {building.resident_weight}, "
            f"边界 {building.cutoff_db:g} dB）: {status}"
        )
        add(
            f"    确证 {format_nights(building.confirmed_nights)}；"
            f"仅单点 {format_nights(building.single_sensor_nights)}；"
            f"互相矛盾 {format_nights(building.conflicting_nights)}；"
            f"无覆盖 {format_nights(building.coverage_gap_nights)}"
        )
        if building.pending_correction_nights:
            add(f"    有待生效校正: {format_nights(building.pending_correction_nights)}")
        if building.operator_resident_share:
            add(
                "    按运营方均分人口: "
                + "，".join(
                    f"{op} {share:.1f}"
                    for op, share in building.operator_resident_share.items()
                )
            )
        for reason in building.reasons:
            add(f"    - {reason}")

    add("")
    add("[来源指纹]")
    for name, digest in sorted(report.source_hashes.items()):
        add(f"  {name}: {digest[:16]}")
    add(f"  校正编号: {', '.join(report.revision_ids) or '无'}")
    return "\n".join(lines)


def render_plan(plan: PlanReport) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add(f"整改预算方案  截至第 {plan.asof_day} 天（口径摘要 {plan.digest[:12]}）")
    add("=" * 78)
    for area in plan.areas:
        add("")
        add(f"[{area.area}] 预算 {area.funding_cents} 分；验收名额 {area.inspection_slots} 个")
        add(
            f"  已签承诺保留 {area.reserved_cents} 分；新分配 {area.allocated_cents} 分；"
            f"当前可用 {area.available_cents} 分"
        )
        add(
            f"  尚欠整改: {area.outstanding_units:g} 单位 / {area.outstanding_cents} 分；"
            f"验收名额已用 {area.slots_used}，余 {area.slots_remaining}"
        )
        for line in area.funded:
            tag = "已签" if line.committed else "新批"
            nights = format_nights(line.nights) if line.nights else "—"
            target = f" / {line.building}" if line.building else ""
            add(f"  · {line.remedy} [{tag}] {line.kind}{target} {line.cents} 分，夜次 {nights}")
        for line in area.acceptances:
            add(
                f"  ✓ 验收记录 {line.remedy} {line.building or ''} {line.units:g} 单位，"
                f"第 {line.day} 天"
            )
        for delivery in area.deliveries:
            state = "已履行" if delivery.fully_delivered else "未履行完毕"
            add(
                f"    {delivery.remedy} {state}: 承诺 {delivery.promised_units:g}，"
                f"已验收 {delivery.accepted_units:g}，尚欠 {delivery.outstanding_units:g}"
            )
            add(f"      {delivery.scope_note}")
        for item in area.suspended:
            add(f"  ! 暂停 {item.remedy}: {item.reason}")
        for item in area.deferred:
            add(f"  ~ 待复测/暂缓 {item.remedy}: {item.reason}")

    if plan.withheld_by_boundary:
        add("")
        add("[校正跨过资格边界的影响]")
        for item in plan.withheld_by_boundary:
            add(f"  {item['building']}（{item['area']}）: {item['change']}")
            add(f"    之前确证 {format_nights(item['confirmed_before'])}；"
                f"现在确证 {format_nights(item['confirmed_now'])}")
            if item["paused_proposals"]:
                add(f"    暂停新提案: {', '.join(item['paused_proposals'])}")
            if item["signed_commitments_kept"]:
                add(f"    仍须履行的已签承诺: {', '.join(item['signed_commitments_kept'])}")
            add(f"    {item['note']}")

    if plan.pending_re_measure:
        add("")
        add("[留待复测]")
        for item in plan.pending_re_measure:
            add(
                f"  [{item['scope']}] {item['id']}: "
                f"复测夜次 {format_nights(item.get('nights_to_re_measure', []))}"
            )
            add(f"    {item['reason']}")

    add("")
    add("[说明]")
    for note in plan.notes:
        add(f"  - {note}")
    return "\n".join(lines)
