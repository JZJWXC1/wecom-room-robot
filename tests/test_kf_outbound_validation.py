from __future__ import annotations

import pytest

from app.services.kf_contracts import (
    ActionCaption,
    CandidateItem,
    CandidateSet,
    Claim,
    EvidenceItem,
    PreparedOutboundPackage,
    SendAction,
    StructuredTaskPacket,
    TaskAtom,
    ToolEvidenceBundle,
)
from app.services.kf_outbound_validation import (
    OutboundValidationContext,
    ValidationLevel,
    ValidationStatus,
    validate_prepared_outbound_package,
)


def _candidate_set() -> CandidateSet:
    return CandidateSet(
        candidate_set_id="cand-1",
        inventory_snapshot_id="snap-1",
        candidates=[
            CandidateItem(candidate_number=1, listing_id="lst-1", inventory_snapshot_id="snap-1", community="星河苑", room_no="1-101"),
        ],
    )


def _evidence_bundle(candidate_set: CandidateSet | None = None) -> ToolEvidenceBundle:
    return ToolEvidenceBundle(
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        tool_name="inventory.search",
        candidate_set=candidate_set,
        evidence=[
            EvidenceItem(
                evidence_id="evd-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="inventory_listing",
                summary="星河苑 1-101 押一付一 4200",
                metadata={"field_values": {"rent_pay1": "4200", "room_no": "1-101", "community": "星河苑"}},
            )
        ],
    )


def _base_package() -> PreparedOutboundPackage:
    candidate_set = _candidate_set()
    return PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="星河苑 1-101 押一付一 4200。",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-rent",
                listing_id="lst-1",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-1",
                text="星河苑 1-101 押一付一 4200",
                support=["evd-1"],
                legacy_unknown_fields={"field": "rent_pay1", "value": "4200", "evidence_ref": "evd-1"},
            )
        ],
        send_actions=[
            SendAction(
                action_id="send-text",
                action_type="text",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-1",
                payload={"reply_hash": "hash-only"},
            )
        ],
    )


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_valid_package_passes_l0_to_l3_without_touching_send_pipeline() -> None:
    result = validate_prepared_outbound_package(_base_package())

    assert result.status == ValidationStatus.PASS
    assert result.passed is True
    assert result.send_allowed is True
    assert result.to_dict()["l3_rewrite_reasons"] == []


def test_l0_flags_schema_refs_duplicate_actions_and_bad_candidate_ref_type() -> None:
    candidate_set = _candidate_set()
    package = PreparedOutboundPackage(
        schema_version="future-contract",
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="星河苑 1-101 有视频。",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-missing-evidence",
                listing_id="lst-1",
                evidence_id="evd-missing",
                text="星河苑 1-101 有视频",
                support=["evd-missing"],
            )
        ],
        send_actions=[
            SendAction(action_id="send-video", action_type="video", evidence_id="evd-1"),
            SendAction(action_id="send-video", action_type="video", evidence_id="evd-1"),
            SendAction(
                action_id="send-image",
                action_type="image",
                evidence_id="evd-1",
                payload={"candidate_number": "first"},
                metadata={"depends_on_action_ids": ["send-missing"]},
            ),
        ],
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.BLOCKED
    assert result.facts_passed is False
    assert {
        "l0.schema_version",
        "l0.unknown_evidence_ref",
        "l0.duplicate_action",
        "l0.invalid_candidate_number",
        "l0.unknown_action_ref",
    }.issubset(_codes(result))


def test_l0_flags_action_caption_unknown_action_and_type_mismatch() -> None:
    package = _base_package()
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]
    package.action_captions = [
        ActionCaption(action_id="send-missing", action_type="video", text="这是星河苑1-101的视频。"),
        ActionCaption(action_id="send-video", action_type="image", text="这是星河苑1-101的视频。"),
    ]

    result = validate_prepared_outbound_package(package)

    assert {
        "l0.unknown_action_ref",
        "l0.action_caption_type_mismatch",
    }.issubset(_codes(result))


def test_l1_checks_claim_value_listing_snapshot_and_sensitive_slots() -> None:
    candidate_set = _candidate_set()
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="详情链接：https://example.test/listing/1",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-bad-value",
                listing_id="lst-other",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-other",
                text="星河苑 1-101 押一付一 4500",
                support=["evd-1"],
                legacy_unknown_fields={"field": "rent_pay1", "value": "4500", "evidence_ref": "evd-1"},
            )
        ],
        send_actions=[
            SendAction(
                action_id="send-link",
                action_type="link",
                evidence_id="evd-1",
                payload={"url": "https://example.test/listing/1"},
            )
        ],
    )

    result = validate_prepared_outbound_package(package)

    assert {
        "l1.claim_value_not_in_evidence",
        "l1.listing_mismatch",
        "l1.snapshot_mismatch",
        "l1.sensitive_outside_evidence",
    }.issubset(_codes(result))
    assert all("example.test" not in issue.message for issue in result.issues)


def _fact_evidence_bundle() -> ToolEvidenceBundle:
    return ToolEvidenceBundle(
        inventory_snapshot_id="snap-1",
        tool_name="inventory.search",
        evidence=[
            EvidenceItem(
                evidence_id="evd-fact",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="inventory_listing",
                summary="翰皋名府 8-1403 拱墅区 三室一厅 押一付一 4200",
                field_values={
                    "community": "翰皋名府",
                    "room_no": "8-1403",
                    "area": "拱墅区",
                    "layout": "三室一厅",
                    "rent_pay1": "4200",
                },
            )
        ],
    )


def _fact_claim_package(field: str, value) -> PreparedOutboundPackage:
    return PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        reply_text="翰皋名府 8-1403 的情况我按证据说。",
        evidence_bundle=_fact_evidence_bundle(),
        claims=[
            Claim(
                claim_id="claim-fact",
                listing_id="lst-1",
                evidence_id="evd-fact",
                inventory_snapshot_id="snap-1",
                field=field,
                value=value,
                support=["evd-fact"],
                text=f"{field} {value}",
            )
        ],
    )


def test_l1_claim_value_check_blocks_fabricated_first_class_fact_values() -> None:
    # H3 正向回归:一等属性 schema(claim.field/claim.value,legacy_unknown_fields 为空,
    # 即生产 LLM2 的规范形态)下,伪造的结构化事实值必须被 l1.claim_value_not_in_evidence 拦下。
    # 覆盖:值凭空编造、值搬到错误字段(字段作用域)、中文别名字段名(经 ROW_ALIASES 归一)。
    fabricated_cases = [
        ("area", "西湖区"),          # 证据 area=拱墅区,编造区域
        ("rent_pay1", "9999"),       # 证据 rent_pay1=4200,编造租金
        ("layout", "拱墅区"),        # 把 area 的值安到 layout 字段,字段作用域应拦
        ("押一付一", "9999"),        # 中文别名 -> rent_pay1,同样编造租金
    ]
    for field, value in fabricated_cases:
        result = validate_prepared_outbound_package(_fact_claim_package(field, value))
        assert "l1.claim_value_not_in_evidence" in _codes(result), f"expected block for {field}={value}"

    # 真值(含中文别名与整数)必须放行,不得误报。
    for field, value in [("area", "拱墅区"), ("rent_pay1", 4200), ("layout", "三室一厅"), ("押一付一", "4200")]:
        result = validate_prepared_outbound_package(_fact_claim_package(field, value))
        assert "l1.claim_value_not_in_evidence" not in _codes(result), f"unexpected block for {field}={value}"


def test_l1_claim_value_check_exempts_control_claims_with_matching_flag_key() -> None:
    # H3 误伤防线:控制/兜底类 claim(field=missing_target,value 是话术)不得被当事实核验,
    # 即便其引用的控制证据 field_values 恰好带同名状态标志键(missing_target=True)——这正是
    # 2026-07-05 审计 H3 修复(2)之所以仍红的碰撞点(控制证据 field_values 带控制字段名)。
    # field 不在 ROW_ALIASES(归一为空)即豁免,保证对客有回复而非 send_allowed=False。
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        reply_text="这条我还没法直接发视频，先帮我确认具体哪一套。",
        evidence_bundle=ToolEvidenceBundle(
            inventory_snapshot_id="snap-1",
            tool_name="context_tools",
            evidence=[
                EvidenceItem(
                    evidence_id="evd-target-missing",
                    inventory_snapshot_id="snap-1",
                    evidence_type="missing_target",
                    summary="还没定位到具体房源。",
                    field_values={
                        "error_code": "missing_target",
                        "missing_target": True,
                        "reason": "media_target_unbound",
                        "requires_customer_room_ref": True,
                    },
                )
            ],
        ),
        claims=[
            Claim(
                claim_id="claim-missing-target",
                evidence_id="evd-target-missing",
                inventory_snapshot_id="snap-1",
                field="missing_target",
                value="需要先确认具体房源",
                support=["evd-target-missing"],
                text="需要先确认具体房源",
            )
        ],
    )

    result = validate_prepared_outbound_package(package)

    assert "l1.claim_value_not_in_evidence" not in _codes(result)


def test_l2_checks_task_completion_video_only_candidate_bounds_and_password_request() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-video", task_type="send_video", user_text="要视频")],
    )
    package = _base_package()
    package.send_actions = [
        SendAction(
            action_id="send-image",
            action_type="image",
            evidence_id="evd-1",
            payload={"candidate_number": 2},
        )
    ]
    package.claims = [
        Claim(
            claim_id="claim-password",
            listing_id="lst-1",
            evidence_id="evd-1",
            text="看房密码已准备",
            support=["evd-1"],
        )
    ]

    result = validate_prepared_outbound_package(
        package,
        context=OutboundValidationContext(task_packet=task_packet, user_asked_password=False),
    )

    assert {
        "l2.task_not_answered",
        "l2.video_only_cannot_send_image",
        "l2.candidate_number_out_of_range",
        "l2.password_not_requested",
    }.issubset(_codes(result))


def test_l2_treats_reply_compose_signal_as_answered_by_visible_text() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-controlled-reply-compose-signal",
                task_type="reply_compose_signal",
                user_text="你好，在吗",
                constraints={"guidance": "引导客户直接问房源表、图片或视频需求"},
            )
        ],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text="你好，在的。你直接发小区、房号、预算、房源表、图片或视频需求，我马上帮你查。",
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_blocks_utility_task_when_reply_only_lists_inventory() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-utility",
                task_type="inventory_search",
                user_text="1号那套水电怎么收？",
                required_tools=["inventory.search"],
            )
        ],
    )
    package = _base_package()
    package.reply_text = "我按房源表查到这套：星河苑1-101，押一付一4200。"
    package.evidence_bundle.evidence[0].metadata = {
        "field_values": {"备注": "水30/月，电1元/度", "room_no": "1-101", "community": "星河苑"}
    }

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert "l2.task_not_answered" in _codes(result)


def test_l2_allows_utility_task_with_explicit_missing_utility_note() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-utility",
                task_type="inventory_search",
                user_text="这套水电怎么算？",
                required_tools=["inventory.search"],
            )
        ],
    )
    package = _base_package()
    package.reply_text = "星河苑1-101房源备注里暂时没有水电信息，我先不编。"
    package.evidence_bundle.evidence[0].metadata = {"field_values": {"room_no": "1-101", "community": "星河苑"}}
    package.claims = []

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_blocks_deposit_condition_without_selfcheck_path() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-deposit", task_type="deposit_policy", user_text="能免押吗？")],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text="支持支付宝无忧住信用免押，需要芝麻分符合风控。你可以先在支付宝里查下自己有没有租房免押额度。",
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert "l2.task_not_answered" in _codes(result)


def test_l2_blocks_deposit_condition_that_only_mentions_rent_channel_without_selfcheck_action() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-deposit", task_type="deposit_policy", user_text="能免押吗？")],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text=(
            "支持支付宝无忧住信用免押，需要芝麻分符合风控，"
            "且支付宝租房板块有可用额度。符合条件需支付5.5%-8%的服务费。"
        ),
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert "l2.task_not_answered" in _codes(result)


def test_l2_allows_deposit_condition_with_selfcheck_path() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-deposit", task_type="deposit_policy", user_text="能免押吗？")],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text=(
            "免押走支付宝无忧住芝麻信用评估。"
            "客户可以打开支付宝：我的 - 芝麻信用 - 我的 - 信用额度 - 租房板块申请额度，有额度再继续走免押流程。"
        ),
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_ignores_embedded_confirmed_room_fields_when_classifying_task_kind() -> None:
    package = _base_package()
    package.reply_text = "杨家新雅苑36-1-1102是100方三房两卫，客厅带阳台。"
    package.claims = [
        Claim(
            claim_id="claim-layout",
            listing_id="lst-1",
            evidence_id="evd-1",
            inventory_snapshot_id="snap-1",
            text="杨家新雅苑36-1-1102是100方三房两卫，客厅带阳台",
            support=["evd-1"],
        )
    ]
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-1-inventory-detail",
                task_type="inventory_search",
                user_text="第一套户型特点怎么样",
                constraints={
                    "confirmed_room": {
                        "label": "杨家新雅苑36-1-1102",
                        "row": {
                            "小区": "杨家新雅苑",
                            "房号": "36-1-1102",
                            "户型": "100方三房两卫客厅带阳台",
                            "has_password": True,
                        },
                    }
                },
                required_tools=["inventory.search"],
            )
        ],
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)
    assert "l2.password_not_requested" not in _codes(result)


def test_l2_counts_image_send_action_as_answering_image_task() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-1-image-request", task_type="send_image", user_text="第二套图片")],
    )
    package = _base_package()
    package.reply_text = ""
    package.claims = []
    package.send_actions = [SendAction(action_id="send-image", action_type="image", evidence_id="evd-1")]

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_allows_password_task_to_be_answered_by_field_target_error() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-1-viewing-guidance",
                task_type="viewing_guidance",
                user_text="密码没有或者不对的话联系谁？",
            )
        ],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text="看房方式和密码要按具体房源查。你把小区+房号发我，我马上按那套确认。",
        evidence_bundle=ToolEvidenceBundle(
            tool_name="tool_resolver",
            evidence=[
                EvidenceItem(
                    evidence_id="evd-field-target-error",
                    evidence_type="field_target_error",
                    summary="看房方式/密码要按具体房源查。",
                    metadata={"controlled_error_code": "field_target_error"},
                )
            ],
        ),
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l3_returns_rewrite_reasons_without_mutating_facts() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        inherited_constraints={"budget": "4000-4500"},
        tasks=[TaskAtom(task_id="task-video", task_type="send_video", user_text="星河苑 1-101 视频")],
    )
    package = _base_package()
    package.reply_text = "listing_id=lst-1，XX小区我稍后发你视频，你预算多少？"
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert result.l3_rewrite_reasons
    assert {
        "l3.internal_name_leak",
        "l3.template_talk",
        "l3.repeats_known_condition",
        "l3.action_tense_error",
    }.issubset(_codes(result))
    assert package.reply_text == "listing_id=lst-1，XX小区我稍后发你视频，你预算多少？"


def test_l3_checks_action_caption_tense_and_internal_words_without_blocking_facts() -> None:
    package = _base_package()
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]
    package.action_captions = [
        ActionCaption(action_id="send-video", action_type="video", text="listing_id=lst-1，稍后发你视频。"),
    ]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert {
        "l3.internal_name_leak",
        "l3.action_tense_error",
    }.issubset(_codes(result))


def test_l3_rewrites_media_sending_claim_without_media_action() -> None:
    package = _base_package()
    package.reply_text = "马上把1号和3号房源的视频发您。"
    package.send_actions = []

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert "l3.action_tense_error" in _codes(result)


def test_l3_rewrites_generic_waiting_reply_without_blocking_facts() -> None:
    package = _base_package()
    package.reply_text = "我先帮您确认一下最新房态，避免发错，稍后给您回复。"

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert "l3.generic_waiting_reply" in _codes(result)


def test_l3_rewrites_filter_count_contradiction() -> None:
    package = _base_package()
    package.reply_text = (
        "根据你独立厨房或独卫优先的需求，目前匹配到3套："
        "1. 骏塘名庭8-1101A；2. 琬秋铭府3-702A；3. 京漾东韵府4-2-601D暂不满足独立厨卫条件，已剔除。"
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert "l3.filter_contradiction" in _codes(result)


def test_l3_rewrites_filter_count_contradiction_with_demonstrative_count() -> None:
    package = _base_package()
    package.reply_text = (
        "按您独立厨房或独卫的优先要求，目前匹配到这3套："
        "1. 骏塘名庭8-1101A；2. 琬秋铭府3-702A；3. 京漾东韵府4-2-601D暂不满足独立厨卫条件，已剔除。"
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert "l3.filter_contradiction" in _codes(result)


@pytest.mark.parametrize(
    "reply_text",
    [
        "合同联系人已通过系统发送，请注意查收。",
        "合同、定金和订房联系方式已由受控通道绑定。",
        "合同联系人信息已通过受控渠道发送给您，请注意查收。",
        "我已为您安排专属联系通道。",
    ],
)
def test_l3_rewrites_customer_visible_channel_leakage(reply_text: str) -> None:
    package = _base_package()
    package.reply_text = reply_text

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert "l3.forbidden_human_phrase" in _codes(result)


@pytest.mark.parametrize(
    "reply_text",
    [
        "工具未绑定，暂时无法发送图片。",
        "上一轮只有 0 套候选，请重新选择。",
        "上一轮只有 3 套候选，请重新选择。",
        "候选1 京漾东韵府 4-2-601D 租金1700",
        "星桥锦绣嘉苑20-1606A:图片 暂未找到可发送图片。",
        "客户要查询星河苑1-101的视频。",
        "客户选择了第1套。",
    ],
)
def test_l3_rewrites_outbound_forbidden_incident_phrases(reply_text: str) -> None:
    package = _base_package()
    package.reply_text = reply_text

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert "l3.outbound_forbidden_incident_phrase" in _codes(result)


def test_l3_rewrites_outbound_forbidden_incident_phrase_in_action_caption() -> None:
    package = _base_package()
    package.reply_text = ""
    package.send_actions = [SendAction(action_id="send-image", action_type="image", evidence_id="evd-1")]
    package.action_captions = [
        ActionCaption(action_id="send-image", action_type="image", text="星桥锦绣嘉苑20-1606A:图片 暂未找到可发送图片。"),
    ]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert "l3.outbound_forbidden_incident_phrase" in _codes(result)


def test_l3_allows_partial_missing_media_reply_when_evidence_exists() -> None:
    package = _base_package()
    package.reply_text = "这是星河苑1-101的视频，另一套暂时没有视频。"
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-video-1")]
    package.action_captions = [
        ActionCaption(action_id="send-video", action_type="video", text="这是星河苑1-101的视频。"),
    ]
    package.evidence_bundle = ToolEvidenceBundle(
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        tool_name="media.send",
        candidate_set=package.candidate_set,
        evidence=[
            EvidenceItem(
                evidence_id="evd-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="inventory_listing",
                summary="星河苑 1-101 押一付一 4200",
                metadata={"field_values": {"rent_pay1": "4200", "room_no": "1-101", "community": "星河苑"}},
            ),
            EvidenceItem(
                evidence_id="evd-video-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="video",
                summary="星河苑 1-101 视频",
            ),
            EvidenceItem(
                evidence_id="evd-missing-media-1",
                inventory_snapshot_id="snap-1",
                evidence_type="missing_media",
                summary="另一套暂未找到可发送视频。",
            ),
        ],
    )
    package.missing_items = ["另一套:视频"]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.action_tense_error" not in _codes(result)


def _inventory_row_package(
    reply_text: str,
    *,
    layout: str = "一室一厅",
    area: str = "闸弄口 新塘 元宝塘 东站",
    community: str = "翰皋名府",
    room_no: str = "8-1403",
    field_values_override: dict | None = None,
) -> PreparedOutboundPackage:
    field_values = {
        "listing_id": "lst-row-1",
        "community": community,
        "room_no": room_no,
        "area": area,
        "layout": layout,
        "rent_pay1": 5300,
        "rent_pay2": 5000,
    }
    if field_values_override is not None:
        field_values = field_values_override
    return PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text=reply_text,
        evidence_bundle=ToolEvidenceBundle(
            inventory_snapshot_id="snap-1",
            candidate_set_id="cand-1",
            tool_name="inventory.search",
            evidence=[
                EvidenceItem(
                    evidence_id="evd-row-1",
                    listing_id="lst-row-1",
                    inventory_snapshot_id="snap-1",
                    evidence_type="inventory_candidate",
                    summary=f"候选1 {community} {room_no} 租金5300",
                    field_values=field_values,
                )
            ],
        ),
    )


def test_l3_flags_layout_and_area_claims_conflicting_evidence_production_timeline() -> None:
    # 生产实证固化（2026-07-04 23:58）：证据行=翰皋名府8-1403/东站组/一室一厅，
    # LLM2 回复却声明"新天地""两室"——户型与区域双重失实必须触发 L3 rewrite，且不升级为 blocking。
    package = _inventory_row_package(
        "新天地这边有一套5000元以上的两室：翰皋名府 8-1403，押一付一5300元/月，押二付一5000元/月。"
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert not result.blocking_issues
    assert {"l3.layout_claim_mismatch", "l3.area_claim_mismatch"}.issubset(_codes(result))
    layout_message = next(issue.message for issue in result.issues if issue.code == "l3.layout_claim_mismatch")
    area_message = next(issue.message for issue in result.issues if issue.code == "l3.area_claim_mismatch")
    assert "两室" in layout_message and "一室一厅" in layout_message
    assert "新天地" in area_message


def test_l3_allows_broad_layout_claim_covering_specific_evidence_layout() -> None:
    package = _inventory_row_package("这边有一套两室：翰皋名府 8-1403。", layout="两室一厅")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)


def test_l3_allows_colloquial_layout_claim_mapped_to_evidence() -> None:
    package = _inventory_row_package("这套翰皋名府 8-1403 是大两房。", layout="两室一厅")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)


def test_l3_flags_colloquial_layout_claim_conflicting_evidence() -> None:
    package = _inventory_row_package("这套翰皋名府 8-1403 是两房一厅。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert "l3.layout_claim_mismatch" in _codes(result)


def test_l3_skips_negated_and_echoed_layout_mentions() -> None:
    package = _inventory_row_package("你要的两室5000以内暂时没有。这套翰皋名府 8-1403 是一室一厅，要看吗。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)


def test_l3_skips_unmapped_colloquial_layout_wording() -> None:
    package = _inventory_row_package("这套翰皋名府 8-1403 是厅卧一体格局。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)


def test_l3_does_not_flag_undeclared_layout_or_area() -> None:
    # 只拦"声明了且矛盾"，不拦"未声明"：证据行漂移但回复没有复述户型/区域词时不触发
    package = _inventory_row_package("这边有一套5000以上的：翰皋名府 8-1403，押一付一5300元/月。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)
    assert "l3.area_claim_mismatch" not in _codes(result)


def test_l3_allows_area_alias_of_same_region_group() -> None:
    package = _inventory_row_package("皋塘这边有一套一室一厅：翰皋名府 8-1403。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.area_claim_mismatch" not in _codes(result)


def test_l3_skips_area_token_inside_evidence_community_name() -> None:
    package = _inventory_row_package(
        "新塘雅苑 3-201 这套是一室一厅。",
        area="东新园 杭氧 新天地",
        community="新塘雅苑",
        room_no="3-201",
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.area_claim_mismatch" not in _codes(result)


def test_l3_skips_claim_checks_when_evidence_lacks_layout_and_area_fields() -> None:
    package = _inventory_row_package(
        "这边有一套两室：星河苑 1-101。",
        field_values_override={"listing_id": "lst-row-1", "community": "星河苑", "room_no": "1-101", "rent_pay1": 4200},
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.layout_claim_mismatch" not in _codes(result)
    assert "l3.area_claim_mismatch" not in _codes(result)


def test_l3_flags_only_affirmative_sentence_in_mixed_reply() -> None:
    package = _inventory_row_package("新天地的两室暂时没有。这边有一套两室：翰皋名府 8-1403。")

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert "l3.layout_claim_mismatch" in _codes(result)
    assert "l3.area_claim_mismatch" not in _codes(result)


def test_legacy_claim_without_structured_value_still_uses_evidence_refs() -> None:
    package = _base_package()
    package.claims = [
        Claim(
            claim_id="claim-legacy",
            listing_id="lst-1",
            evidence_id="evd-1",
            inventory_snapshot_id="snap-1",
            text="星河苑 1-101 押一付一 4200",
            support=["evd-1"],
        )
    ]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert not result.issues_for_level(ValidationLevel.L1)


def test_l3_flags_hallucinated_layout_in_comma_joined_negation_sentence() -> None:
    # 回归(2026-07-05 审计 H1):证据行=翰皋名府8-1403/一室一厅。客服高频"有A没有B"
    # 逗号句式里,幻觉户型"三室"与否定词"没有"落在同一整段;逗号未纳入分句前
    # 整段命中否定豁免致漏拦。逗号分句后"三室"子句独立受检,否定只豁免其所在子句。
    package = _inventory_row_package("这套翰皋名府8-1403是三室的，其他房型暂时没有。")

    result = validate_prepared_outbound_package(package)

    assert "l3.layout_claim_mismatch" in _codes(result)
    layout_message = next(issue.message for issue in result.issues if issue.code == "l3.layout_claim_mismatch")
    assert "三室" in layout_message and "一室一厅" in layout_message


def test_l3_still_exempts_pure_negation_subclause_after_comma_split() -> None:
    # 正向保护:逗号分句不得误伤——"没有三室"这类纯否定子句仍应豁免,
    # 合法回复(证据是一室一厅,如实说没有三室)不得被判户型矛盾。
    package = _inventory_row_package("翰皋名府8-1403是一室一厅，三室的暂时没有。")

    result = validate_prepared_outbound_package(package)

    assert "l3.layout_claim_mismatch" not in _codes(result)
