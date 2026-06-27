import asyncio
from pathlib import Path

from app.services.kf_agentic_rag import AgenticRagAssessment, KfAgenticRagService


def test_agentic_rag_assessment_keeps_status_and_fallback_aliases() -> None:
    assessment = AgenticRagAssessment(
        action="fallback",
        reason="bad_reply",
        fallback_text="我再帮你确认一下这套房源。",
    )

    assert assessment.action == "fallback"
    assert assessment.status == "fallback"
    assert assessment.fallback_text == "我再帮你确认一下这套房源。"
    assert assessment.fallback_reply == "我再帮你确认一下这套房源。"

    legacy_assessment = AgenticRagAssessment(
        action="",
        status="retry",
        fallback_reply="我换个方式重新查一下。",
    )

    assert legacy_assessment.action == "retry"
    assert legacy_assessment.status == "retry"
    assert legacy_assessment.fallback_text == "我换个方式重新查一下。"
    assert legacy_assessment.fallback_reply == "我换个方式重新查一下。"


def test_agentic_rag_current_inventory_query_not_polluted_by_previous_deposit_context(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="拱墅万达这边有没有2000以内的一室一厅？",
            conversation_context="客户: 能免押吗？服务费怎么算。\n客服: 免押是支付宝无忧住服务。",
            rooms=[
                {
                    "小区": "荣润府",
                    "房号": "15-2-801B",
                    "户型": "一室一厅",
                    "押一付一": "1600",
                }
            ],
            inventory_snapshot="荣润府15-2-801B 一室一厅 押一付一1600",
        )
    )

    assert "deposit_waiver" not in result.need.topics
    assessment = service.assess_reply(
        content="拱墅万达这边有没有2000以内的一室一厅？",
        reply_text="有的，拱墅万达附近2000以内的一室一厅查到荣润府15-2-801B，押一付一1600。",
        rag_result=result,
        retry_attempted=True,
    )
    assert assessment.status == "pass"


def test_agentic_rag_assessment_allows_inventory_bound_ambiguous_community_clarification(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="杨家府附近还有房子吗？客户名字可能说得不准。",
            conversation_context="",
            rooms=[],
            inventory_snapshot="兴业杨家府 杨乐府 杨家新雅苑 杨乐府北区",
        )
    )
    assessment = service.assess_reply(
        content="杨家府附近还有房子吗？客户名字可能说得不准。",
        reply_text="你说的“杨家府”我这边有几个相近小区：兴业杨家府、杨乐府、杨家新雅苑、杨乐府北区。你确认下是哪一个，我再按最新房源表查。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.status == "pass"


def test_agentic_rag_allows_sequence_guidance_with_concrete_inventory_list(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="新天地这边有没有4000左右的两室一厅？",
            conversation_context="",
            rooms=[
                {"小区": "长浜龙吟轩", "房号": "11-1603", "户型": "两室一厅", "押一付一": "4200", "押二付一": "3900"},
                {"小区": "嘉樘星绣府", "房号": "9-603", "户型": "两室一厅", "押一付一": "4200", "押二付一": "3900"},
            ],
            inventory_snapshot="长浜龙吟轩11-1603 两室一厅 押一付一4200 押二付一3900",
        )
    )
    assessment = service.assess_reply(
        content="新天地这边有没有4000左右的两室一厅？",
        reply_text=(
            "有的，新天地附近4000左右两室一厅目前查到这两套：\n"
            "1. 长浜龙吟轩11-1603，两室一厅，押一付一4200，押二付一3900，民用水电\n"
            "2. 嘉樘星绣府9-603，两室一厅，押一付一4200，押二付一3900，民用水电\n"
            "如需视频、图片或者看房方式，你直接回序号就行。"
        ),
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.status == "pass"


def test_agentic_rag_still_rejects_empty_robotic_template_reply(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="租房咨询",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )
    assessment = service.assess_reply(
        content="租房咨询",
        reply_text="感谢您的咨询，如需了解更多房源，请提供小区名和房号。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.status == "retry"
    assert assessment.reason == "robotic_template_reply"


def test_agentic_rag_does_not_require_budget_repeat_for_media_followup(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="前两套视频先发我，拱墅万达1500左右的一室",
            conversation_context="",
            rooms=[
                {"小区": "合嵣悦府", "房号": "6-1-1204B", "户型": "一室一厅", "押一付一": "1500"},
                {"小区": "荣润府", "房号": "15-2-801B", "户型": "一室一厅", "押一付一": "1600"},
            ],
            inventory_snapshot="合嵣悦府6-1-1204B 一室一厅 押一付一1500",
        )
    )
    assessment = service.assess_reply(
        content="前两套视频先发我，拱墅万达1500左右的一室",
        reply_text="这两套房源我查到了，但本地暂时没找到视频：合嵣悦府6-1-1204B、荣润府15-2-801B。这次没有可发送的视频。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.status == "pass"


def test_agentic_rag_topics_use_original_content_not_hallucinated_rewrite(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="用户在拱墅万达找2000以内一室一厅，并隐含希望了解免押服务费。",
            original_content="拱墅万达这边有没有2000以内的一室一厅？",
            rooms=[{"小区": "荣润府", "房号": "15-2-801B"}],
            inventory_snapshot="荣润府15-2-801B",
        )
    )

    assert "deposit_waiver" not in result.need.topics


def test_agentic_rag_rewrites_retrieves_and_formats_deposit_evidence(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "deposit.md").write_text(
        "# 免押\n\n## 口径\n免押是支付宝芝麻信用无忧住服务，需要支付免押服务费。",
        encoding="utf-8",
    )
    service = KfAgenticRagService(knowledge_dir=knowledge)

    result = service.retrieve_for_reply(
        content="这个房子能不能免押，服务费多少",
        conversation_context="",
        rooms=[],
        inventory_snapshot="",
    )

    import asyncio

    result = asyncio.run(result)
    assert result.enabled
    assert result.used
    assert result.need.needs_knowledge
    assert result.need.topics == ["deposit_waiver"]
    assert "支付宝芝麻信用无忧住" in result.context_text
    assert result.trace[0].startswith("rewrite:")


def test_agentic_rag_assessment_retries_bad_deposit_reply(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "deposit.md").write_text(
        "# 免押\n\n## 口径\n免押是支付宝芝麻信用无忧住服务，需要支付免押服务费。",
        encoding="utf-8",
    )
    service = KfAgenticRagService(knowledge_dir=knowledge)

    import asyncio

    result = asyncio.run(
        service.retrieve_for_reply(
            content="免押是免费的吗",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="免押是免费的吗",
        reply_text="可以免费免押，不用付任何费用。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "deposit_reply_claims_free"

    fallback = service.assess_reply(
        content="免押是免费的吗",
        reply_text="可以免费免押，不用付任何费用。",
        rag_result=result,
        retry_attempted=True,
    )
    assert fallback.action == "fallback"
    assert fallback.fallback_text == ""
    assert fallback.report is not None
    assert "免押不是免费" in fallback.report.retry_instruction


def test_agentic_rag_skips_plain_video_send_request(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    import asyncio

    result = asyncio.run(
        service.retrieve_for_reply(
            content="棠润府1-602A视频发我",
            conversation_context="",
            rooms=[{"小区": "棠润府", "房号": "1-602A"}],
            inventory_snapshot="棠润府1-602A",
            row_video_paths=[Path("棠润府1-602A.mp4")],
        )
    )

    assert result.enabled
    assert result.used
    assert not result.need.needs_knowledge
    assert "实时素材库匹配结果" in result.context_text
    assert "棠润府1-602A.mp4" in result.context_text


def test_agentic_rag_assessment_blocks_misleading_video_sent_reply_when_pending(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="视频发我",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
            recent_context={
                "pending_video_sends": {
                    "paths": [Path("b.mp4"), Path("c.mp4")],
                    "labels": ["棠润府1-602A", "棠润府10-1004C"],
                    "reason": "send_limit",
                    "requested_count": 3,
                    "sent_count": 1,
                }
            },
        )
    )

    assert "待补发视频：2 个" in result.context_text
    assessment = service.assess_reply(
        content="视频发我",
        reply_text="已直接发送相关视频。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "video_send_pending"

    fallback = service.assess_reply(
        content="视频发我",
        reply_text="已直接发送相关视频。",
        rag_result=result,
        retry_attempted=True,
    )
    assert fallback.action == "fallback"
    assert fallback.fallback_text == ""
    assert fallback.report is not None
    assert "微信限制没发完" in fallback.report.retry_instruction


def test_agentic_rag_includes_dynamic_inventory_media_and_password_when_requested(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    import asyncio

    result = asyncio.run(
        service.retrieve_for_reply(
            content="棠润府1-602A多少钱，密码多少，有视频吗",
            conversation_context="",
            rooms=[
                {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "户型": "一室一厅",
                    "押一付一": "1900",
                    "押二付一": "1700",
                    "看房方式密码": "160188#",
                    "备注": "水30/月，电1元/度",
                }
            ],
            inventory_snapshot="棠润府1-602A",
            media_images=["https://img.test/a.jpg"],
            media_videos=["https://video.test/a.mp4"],
            row_video_paths=[Path("a.mp4")],
            row_image_paths=[Path("a.jpg")],
        )
    )

    assert result.used
    assert result.dynamic_evidence
    assert "动态工具证据" in result.context_text
    assert "押一付一：1900" in result.context_text
    assert "看房方式密码：160188#" in result.context_text
    assert "本地房间视频：1 个" in result.context_text


def test_agentic_rag_masks_password_when_user_did_not_ask_for_viewing(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    import asyncio

    result = asyncio.run(
        service.retrieve_for_reply(
            content="棠润府1-602A多少钱",
            conversation_context="",
            rooms=[
                {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "押一付一": "1900",
                    "看房方式密码": "160188#",
                }
            ],
            inventory_snapshot="棠润府1-602A",
        )
    )

    assert result.used
    assert "看房方式字段：有" in result.context_text
    assert "160188#" not in result.context_text


def test_agentic_rag_retrieves_refund_cancel_policy(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "refund_cancel.md").write_text(
        "# 退租转租\n\n## 口径\n合同期内退租分两种：未找到转租直接退租，押金不退；找到转租后退租，押金全退，剩余租金按天退还。",
        encoding="utf-8",
    )
    service = KfAgenticRagService(knowledge_dir=knowledge)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="合同期内提前退租押金怎么退",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assert result.used
    assert "refund_cancel" in result.need.topics
    assert "未找到转租" in result.context_text
    assert "剩余租金按天退还" in result.context_text


def test_agentic_rag_dynamic_utility_evidence_uses_room_remark(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="这套水电怎么算",
            conversation_context="",
            rooms=[
                {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "备注": "民用水电，租金含物业费",
                }
            ],
            inventory_snapshot="",
        )
    )

    assert result.used
    assert "utilities" in result.need.topics
    assert "实时房源表匹配" in result.context_text
    assert "备注：民用水电，租金含物业费" in result.context_text


def test_agentic_rag_assessment_retries_wrong_refund_reply(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "refund_cancel.md").write_text(
        "# 退租转租\n\n## 口径\n未找到转租直接退租，押金不退；找到转租后退租，押金全退。",
        encoding="utf-8",
    )
    service = KfAgenticRagService(knowledge_dir=knowledge)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="提前退租押金可以退吗",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="提前退租押金可以退吗",
        reply_text="可以退押金，剩余租金也会退。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "refund_cancel_missing_two_cases"


def test_agentic_rag_assessment_retries_maintenance_without_split(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "maintenance.md").write_text(
        "# 维修\n\n## 口径\n租客原因损坏由租客承担；自然原因损坏，我方承担工费，租客承担易损件材料费。",
        encoding="utf-8",
    )
    service = KfAgenticRagService(knowledge_dir=knowledge)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="家电坏了谁维修",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="家电坏了谁维修",
        reply_text="这个我们会安排维修。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "maintenance_reply_missing_responsibility_split"


def test_agentic_rag_action_blocks_image_for_deposit_fee_question(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(
        content="客户还问如果这套做免押，服务费大概怎么算？",
        action="send_image",
    )

    assert assessment.action == "fallback"
    assert assessment.reason == "send_image_conflicts_with_non_media_intent"


def test_agentic_rag_action_allows_explicit_room_image_request(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(
        content="这套房间图片发我一下",
        action="send_image",
    )

    assert assessment.action == "pass"


def test_agentic_rag_action_allows_confirmation_to_continue_media_action(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(content="是的", action="send_video")

    assert assessment.action == "pass"


def test_agentic_rag_action_allows_contract_contact_for_spoken_booking(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(
        content="看完想定的话联系谁？",
        action="send_contract_contact",
    )

    assert assessment.action == "pass"


def test_agentic_rag_action_blocks_contract_contact_without_booking_intent(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(
        content="这套视频发我一下",
        action="send_contract_contact",
    )

    assert assessment.action == "fallback"
    assert assessment.reason == "contract_contact_action_without_booking_intent"


def test_agentic_rag_action_allows_same_community_inventory_fact(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    assessment = service.assess_action(
        content="客户想先看同小区最便宜的一套，是几号房？",
        action="reply_inventory_fact",
    )

    assert assessment.action == "pass"


def test_agentic_rag_reference_check_confirms_single_typo_community(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="华丰新苑视频发我",
            inventory_rows=[{"小区": "华丰欣苑", "房号": "14-2-901"}],
        )
    )

    confirmation = result.need.reference_confirmation
    assert confirmation is not None
    assert confirmation.status == "needs_confirmation"
    assert confirmation.kind == "community"
    assert confirmation.raw_text == "华丰新苑"
    assert confirmation.suggested_text == "华丰欣苑"
    assert confirmation.rewritten_query == "华丰欣苑视频发我"


def test_agentic_rag_reference_check_asks_when_community_is_ambiguous(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="杨家府4200那套还有吗",
            inventory_rows=[
                {"小区": "杨乐府", "房号": "9-1002"},
                {"小区": "杨家新雅苑", "房号": "15-1-603"},
                {"小区": "兴业杨家府", "房号": "10-1-304"},
            ],
        )
    )

    confirmation = result.need.reference_confirmation
    assert confirmation is not None
    assert confirmation.status == "ambiguous"
    assert confirmation.kind == "community"
    assert set(confirmation.options) == {"杨乐府", "杨家新雅苑", "兴业杨家府"}


def test_agentic_rag_reference_check_confirms_fuzzy_room_no(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)

    result = asyncio.run(
        service.retrieve_for_reply(
            content="皋塘运都9-2-402B现在空不空，密码多少",
            inventory_rows=[{"小区": "皋塘运都", "房号": "9-402B"}],
        )
    )

    confirmation = result.need.reference_confirmation
    assert confirmation is not None
    assert confirmation.status == "needs_confirmation"
    assert confirmation.kind == "room"
    assert confirmation.suggested_text == "皋塘运都9-402B"
    assert confirmation.rewritten_query == "皋塘运都9-402B现在空不空，密码多少"


def test_agentic_rag_assessment_fixes_wrong_canonical_room_name(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="棠润府哪个1600的还在吗",
            conversation_context="",
            rooms=[{"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600"}],
            inventory_snapshot="棠润府15-2-801B",
        )
    )

    assessment = service.assess_reply(
        content="棠润府哪个1600的还在吗",
        reply_text="查到了，荣润府15-2-801B还在。押一付1600。",
        rag_result=result,
        retry_attempted=True,
    )

    assert assessment.action == "fallback"
    assert assessment.reason == "canonical_room_name_mismatch"
    assert assessment.fallback_text == ""
    assert assessment.report is not None
    assert "棠润府15-2-801B" in assessment.report.retry_instruction
    assert "荣润府" not in assessment.report.retry_instruction


def test_agentic_rag_assessment_blocks_sequence_without_numbered_list(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="视频发我",
            conversation_context="",
            rooms=[{"小区": "棠润府", "房号": "15-2-801B"}],
            inventory_snapshot="棠润府15-2-801B",
        )
    )

    assessment = service.assess_reply(
        content="视频发我",
        reply_text="棠润府15-2-801B还在，需要看视频的话回房号或序号。",
        rag_result=result,
        retry_attempted=True,
    )

    assert assessment.action == "fallback"
    assert assessment.reason == "orphan_sequence_instruction"
    assert assessment.fallback_text == ""
    assert assessment.report is not None
    assert "序号" not in assessment.report.retry_instruction


def test_agentic_rag_assessment_requires_direct_availability_answer(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="荣润府1600还有吗",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="荣润府1600还有吗",
        reply_text="我先确认一下。",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "availability_question_not_answered"


def test_agentic_rag_assessment_blocks_known_wanda_city_hallucination(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="万达1500左右有哪些",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="万达1500左右有哪些",
        reply_text="请问您指的是哪个城市的万达广场附近？",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "retry"
    assert assessment.reason == "known_area_city_hallucination"


def test_agentic_rag_assessment_requires_budget_in_listing_reply(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="拱墅万达1500左右有哪些",
            conversation_context="",
            rooms=[
                {"小区": "荣润府", "房号": "15-2-801B", "押一付": "1600"},
                {"小区": "合嵣悦府", "房号": "6-1-1204B", "押一付": "1500"},
            ],
            inventory_snapshot="荣润府15-2-801B 押一1600\n合嵣悦府6-1-1204B 押一1500",
        )
    )

    assessment = service.assess_reply(
        content="拱墅万达1500左右有哪些",
        reply_text="拱墅万达附近查到这些：\n1. 荣润府15-2-801B，押一1600\n2. 合嵣悦府6-1-1204B，押一1500",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "pass"

    bad_assessment = service.assess_reply(
        content="拱墅万达1500左右有哪些",
        reply_text="拱墅万达附近查到这些：\n1. 荣润府15-2-801B，押一1600\n2. 合嵣悦府6-1-1204B，押一1600",
        rag_result=result,
        retry_attempted=False,
    )

    assert bad_assessment.action == "retry"
    assert bad_assessment.reason == "budget_constraint_omitted"


def test_agentic_rag_assessment_does_not_treat_original_video_as_room_availability(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="有没有更清楚的原视频",
            conversation_context="",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="有没有更清楚的原视频",
        reply_text="原视频直达链接发你，点开可以看更清楚的版本：\nhttps://example.com/a.mp4",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "pass"


def test_agentic_rag_assessment_does_not_treat_clarity_followup_as_room_availability(tmp_path: Path) -> None:
    service = KfAgenticRagService(knowledge_dir=tmp_path)
    result = asyncio.run(
        service.retrieve_for_reply(
            content="有没有清楚一点的",
            conversation_context="客服: 原视频直达链接发你",
            rooms=[],
            inventory_snapshot="",
        )
    )

    assessment = service.assess_reply(
        content="有没有清楚一点的",
        reply_text="原视频直达链接发你，点开可以看更清楚的版本：\nhttps://example.com/a.mp4",
        rag_result=result,
        retry_attempted=False,
    )

    assert assessment.action == "pass"
