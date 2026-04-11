import hashlib
from unittest.mock import patch

from app.common.text_utils import compute_hash, normalise


class TestNormalise:
    def test_strips_leading_and_trailing_whitespace(self):
        assert normalise("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self):
        assert normalise("a  b\n\tc") == "a b c"

    def test_nfkc_fullwidth_to_halfwidth(self):
        # fullwidth digits/letters → halfwidth
        assert normalise("ＡＢＣ１２３") == "ABC123"

    def test_empty_string(self):
        assert normalise("") == ""

    def test_pure_whitespace(self):
        assert normalise("   \n\t  ") == ""

    def test_mixed_chinese_and_punctuation(self):
        # NFKC also normalises punctuation widths
        assert normalise("騰訊 ABC ") == "騰訊 ABC"

    def test_returns_original_on_exception(self):
        # Force an internal failure and ensure we return original input
        with patch(
            "app.common.text_utils.unicodedata.normalize",
            side_effect=RuntimeError("boom"),
        ):
            assert normalise("hello") == "hello"


class TestComputeHash:
    def test_returns_64_char_hex(self):
        h = compute_hash("hello")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_matches_sha256(self):
        text = "騰訊控股業績公告"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert compute_hash(text) == expected

    def test_distinct_inputs_produce_distinct_hashes(self):
        assert compute_hash("a") != compute_hash("b")

    def test_same_input_produces_same_hash(self):
        assert compute_hash("x") == compute_hash("x")
