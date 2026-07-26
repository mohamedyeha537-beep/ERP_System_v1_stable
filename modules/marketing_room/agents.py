"""تعريفات أدوار غرفة وكلاء التسويق (واجهة + خط أنابيب)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCard:
    role: str
    number: int
    title_ar: str
    blurb_ar: str
    pipeline: bool  # يشارك في التشغيل اليومي
    external_href: str | None = None  # رابط خارج الغرفة (مثل صندوق الوارد)
    workspace: bool = False  # صفحة عمل خاصة بالوكيل


AGENT_CARDS: tuple[AgentCard, ...] = (
    AgentCard(
        role="chief",
        number=1,
        title_ar="وكيل الذكاء الاصطناعي الرئيسي",
        blurb_ar="يشرف على الغرفة ويقدّم ملخص النشاط والمسودات بانتظار الموافقة.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="website_manager",
        number=2,
        title_ar="مدير الموقع الإلكتروني",
        blurb_ar="يفهم الخدمات والجمهور من بيانات المطعم والفندق ويستخرج نقاط القوة.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="website_seller",
        number=3,
        title_ar="بياع الموقع",
        blurb_ar="يحوّل الخدمات إلى زوايا بيع يومية وعروض مناسبة للنشر.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="content_manager",
        number=4,
        title_ar="مدير المحتوى",
        blurb_ar="يضع خطة محتوى لعدة أيام ويولّد مسودات منشورات قابلة للمراجعة.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="hashtag",
        number=5,
        title_ar="وكيل الهاشتاجات",
        blurb_ar="يختار كلمات وهاشتاجات مناسبة لكل منشور ومنصة.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="design",
        number=6,
        title_ar="منشئ المحتوى / الديزاين",
        blurb_ar="ينشئ نصوص المنشورات ويولّد صوراً تسويقية (يتطلب مفتاح صور). الفيديو لاحقاً.",
        pipeline=True,
        workspace=True,
    ),
    AgentCard(
        role="campaign_manager",
        number=7,
        title_ar="مدير حملات فيسبوك",
        blurb_ar="يربط Meta (API أو MCP/n8n)، يراجع أداء الحملات، ويوجّه ملاحظات تحسين لباقي الوكلاء.",
        pipeline=False,
        workspace=True,
    ),
    AgentCard(
        role="delivery",
        number=8,
        title_ar="موظف الدليفري (النشر)",
        blurb_ar="النشر التلقائي لاحقاً عبر Meta — حالياً انسخ المنشور المعتمد وانشره يدوياً.",
        pipeline=False,
    ),
    AgentCard(
        role="customer_service",
        number=9,
        title_ar="مدير خدمة العملاء والردود",
        blurb_ar="يراقب الرسائل والردود — صندوق الوارد الحالي + مساعد Hermes عند تفعيله من إعدادات الشات.",
        pipeline=False,
        external_href="/admin/messaging/inbox",
    ),
)

AGENT_BY_ROLE = {a.role: a for a in AGENT_CARDS}
