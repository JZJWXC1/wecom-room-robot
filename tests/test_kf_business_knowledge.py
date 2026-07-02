from __future__ import annotations

from app.services.kf_business_knowledge import KfBusinessKnowledgeService


def test_business_knowledge_retrieves_deposit_markdown(tmp_path) -> None:
    (tmp_path / "deposit_waiver.md").write_text(
        "# 免押\n\n免押是支付宝芝麻信用无忧住服务，不是免费。",
        encoding="utf-8",
    )
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "selfcheck_deposit.md").write_text(
        "# 不应被业务知识直接召回\n\n旧 selfcheck 规则。",
        encoding="utf-8",
    )

    service = KfBusinessKnowledgeService(tmp_path)
    cards = service.retrieve(query_text="免押服务费怎么算", intent="deposit")

    assert [card.id for card in cards] == ["deposit_waiver"]
    assert "支付宝芝麻信用无忧住" in service.format_cards(cards)
    assert "selfcheck" not in service.format_cards(cards)


def test_business_knowledge_retrieves_contract_markdown(tmp_path) -> None:
    (tmp_path / "contract_booking.md").write_text(
        "# 定房与合同\n\n客户看中了必须引导联系三个号码签电子合同。",
        encoding="utf-8",
    )

    service = KfBusinessKnowledgeService(tmp_path)
    cards = service.retrieve(
        query_text="客户看中了怎么定房，合同联系谁",
        intent="contract",
        signals={"wants_contract_contact": True},
    )

    assert [card.id for card in cards] == ["contract_booking"]
    assert "签电子合同" in service.format_cards(cards)
