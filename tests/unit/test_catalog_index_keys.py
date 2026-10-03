"""Catalog-index keys: barcode validation, model codes, title helpers."""

from __future__ import annotations

from app_shared.catalog_index import keys


def test_gtin_key_validates_the_check_digit_and_pads_to_14():
    assert keys.gtin_key("3337875597296", strict=True) == "03337875597296"
    assert keys.gtin_key("3337875597297", strict=True) is None


def test_upc_and_its_ean13_form_are_one_key():
    assert keys.gtin_key("036000291452", strict=True) == "00036000291452"
    assert keys.gtin_key("0036000291452", strict=True) == "00036000291452"


def test_gtin8_counts_only_in_the_barcode_field():
    assert keys.gtin_key("73513537", strict=True) == "00000073513537"
    assert keys.gtin_key("73513537", strict=False) is None


def test_store_numbers_that_look_like_barcodes_are_refused():
    assert keys.gtin_key("1001500001", strict=False) is None       # 10 digits
    assert keys.gtin_key("0000000000000", strict=True) is None     # one repeated digit
    assert keys.gtin_key("SKU-3337875597296", strict=False) is None


def test_gtin_key_reads_arabic_digits_separators_and_float_suffix():
    assert keys.gtin_key("٣٣٣٧٨٧٥٥٩٧٢٩٦", strict=True) == "03337875597296"
    assert keys.gtin_key("3337-8755 97296", strict=True) == "03337875597296"
    assert keys.gtin_key("3337875597296.0", strict=False) == "03337875597296"


def test_barcode_keys_reads_all_three_fields_without_duplicates():
    assert keys.barcode_keys(gtin="3337875597296", sku="3337875597296", mpn="8809634610034") == [
        "03337875597296",
        "08809634610034",
    ]
    assert keys.barcode_keys(gtin=None, sku="AB-12", mpn="") == []


def test_model_keys_keep_codes_and_refuse_measurements_dimensions_and_specs():
    got = keys.model_keys(
        sku="SKU 1",
        mpn="CE505A",
        title="HP 05A Black Toner CE505A PG-045 473ml 100x200cm SPF50 IP68 10W30 300Mbps Air11",
    )
    assert got == ["ce505a", "pg045"]


def test_model_keys_are_capped():
    title = " ".join(f"AB{i}00{i}X" for i in range(20))
    assert len(keys.model_keys(title=title)) == keys.MAX_MODEL_KEYS


def test_numbers_conflict_only_when_both_sides_have_different_numbers():
    assert keys.numbers_conflict("CeraVe Cleanser 473 ml", "CeraVe Cleanser 236 ml")
    assert not keys.numbers_conflict("CeraVe Cleanser 473 ml", "سيرافي غسول 473 مل")
    assert not keys.numbers_conflict("CeraVe Cleanser", "CeraVe Cleanser 236 ml")
    assert not keys.numbers_conflict("Serum 1.50 oz", "Serum 1.5 oz")


def test_title_query_text_puts_brand_first_then_latin_words():
    assert keys.title_query_text("CeraVe Hydrating Cleanser 473 ml", "CeraVe") == "cerave hydrating cleanser"
    assert keys.title_query_text("غسول Hydrating من للبشرة", "CeraVe") == "cerave hydrating غسول للبشرة"


def test_title_query_text_needs_a_brand_and_three_words():
    assert keys.title_query_text("CeraVe Hydrating Cleanser 473 ml", None) is None
    assert keys.title_query_text("New Balance", "New Balance") is None
    assert keys.title_query_text("حقيبه نسائية", "") is None


def test_jaccard():
    assert keys.jaccard(["a", "b"], ["b", "c"]) == 1 / 3
    assert keys.jaccard([], ["b"]) == 0.0


def test_domain_helpers_handle_path_stores():
    assert keys.normalise_domain("https://WWW.Shop.com/") == "shop.com"
    assert keys.host_of("salla.sa/My-Store") == "salla.sa"
    assert keys.store_domain_of_url("https://salla.sa/My-Store/p123?x=1") == "salla.sa/my-store"
    assert keys.store_domain_of_url("https://www.shop.com/ar/p/1") == "shop.com"
