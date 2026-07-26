"""اختبار تمييز الإفطار ومنطق تكلفة الفندق."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from modules.hotel.breakfast_settle import (
    product_is_hotel_breakfast,
    sale_is_hotel_breakfast,
    sale_looks_like_hotel_breakfast,
    text_looks_like_breakfast,
)


class _FakeDb:
    def get(self, _model, _id):
        return None


def test_text_detects_breakfast():
    assert text_looks_like_breakfast("افطار نزيل فندق شخص واحد")
    assert text_looks_like_breakfast("إفطار")
    assert text_looks_like_breakfast("Hotel Breakfast")
    assert not text_looks_like_breakfast("توم اهوك")
    assert not text_looks_like_breakfast("عصير مانجو")


def test_product_flag_and_name():
    assert product_is_hotel_breakfast(
        SimpleNamespace(name_ar="وجبة", is_hotel_breakfast=True, category=None)
    )
    assert product_is_hotel_breakfast(
        SimpleNamespace(name_ar="افطار نزيل", is_hotel_breakfast=False, category=None)
    )
    assert not product_is_hotel_breakfast(
        SimpleNamespace(name_ar="ماء أكوافينا", is_hotel_breakfast=False, category=None)
    )


def test_sale_all_breakfast_lines():
    bf = SimpleNamespace(
        name_ar="افطار نزيل فندق شخص واحد",
        is_hotel_breakfast=False,
        category=None,
        product_id=1,
    )
    sale = SimpleNamespace(
        lines=[
            SimpleNamespace(product=bf, product_id=1, product_name=None, name_ar=None),
            SimpleNamespace(product=bf, product_id=1, product_name=None, name_ar=None),
        ]
    )
    with patch(
        "modules.hotel.breakfast_settle.hotel_breakfast_included_enabled",
        return_value=True,
    ):
        assert sale_is_hotel_breakfast(_FakeDb(), sale) is True
    assert sale_looks_like_hotel_breakfast(_FakeDb(), sale) is True


def test_sale_mixed_not_breakfast_only():
    bf = SimpleNamespace(
        name_ar="افطار", is_hotel_breakfast=True, category=None, product_id=1
    )
    other = SimpleNamespace(
        name_ar="عصير", is_hotel_breakfast=False, category=None, product_id=2
    )
    sale = SimpleNamespace(
        lines=[
            SimpleNamespace(product=bf, product_id=1, product_name=None, name_ar=None),
            SimpleNamespace(product=other, product_id=2, product_name=None, name_ar=None),
        ]
    )
    with patch(
        "modules.hotel.breakfast_settle.hotel_breakfast_included_enabled",
        return_value=True,
    ):
        assert sale_is_hotel_breakfast(_FakeDb(), sale) is False


def test_setting_off_disables_included_mode():
    bf = SimpleNamespace(
        name_ar="افطار نزيل",
        is_hotel_breakfast=True,
        category=None,
        product_id=1,
    )
    sale = SimpleNamespace(
        lines=[
            SimpleNamespace(product=bf, product_id=1, product_name=None, name_ar=None),
        ]
    )
    with patch(
        "modules.hotel.breakfast_settle.hotel_breakfast_included_enabled",
        return_value=False,
    ):
        assert sale_is_hotel_breakfast(_FakeDb(), sale) is False
    assert sale_looks_like_hotel_breakfast(_FakeDb(), sale) is True
