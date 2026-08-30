import logging

from app.common.error_codes import (
    CommonErrorCode,
    CrawlErrorCode,
    DocumentParseErrorCode,
    ErrorCode,
)


class TestErrorCodeBase:
    def test_error_code_is_frozen(self):
        code = CrawlErrorCode.URL_GET_FAILED
        assert isinstance(code, ErrorCode)
        # Frozen dataclass — assignment raises
        try:
            code.error_code = "SADI-9999"
            assert False, "Should have raised"
        except AttributeError:
            pass

    def test_log_level_defaults_to_warning(self):
        # An individual failure (one URL, one PDF) at fleet scale isn't
        # ops-actionable on its own — WARNING is the sensible default.
        assert CrawlErrorCode.URL_GET_FAILED.log_level == logging.WARNING
        assert DocumentParseErrorCode.PARSE_ERROR.log_level == logging.WARNING

    def test_dependency_and_internal_errors_escalate_to_error(self):
        # These represent a dependency being down or a genuinely unexpected
        # failure — ops-actionable, so they override the WARNING default.
        assert CommonErrorCode.INTERNAL_ERROR.log_level == logging.ERROR
        assert CommonErrorCode.SERVICE_UNAVAILABLE.log_level == logging.ERROR


class TestCrawlErrorCode:
    def test_type_is_crawl(self):
        assert CrawlErrorCode.URL_GET_FAILED.error_type == "CRAWL"
        assert CrawlErrorCode.BROWSER_FETCH_FAILED.error_type == "CRAWL"

    def test_codes_are_sadi_61xx(self):
        assert CrawlErrorCode.URL_GET_FAILED.error_code == "SADI-6101"
        assert CrawlErrorCode.BROWSER_FETCH_FAILED.error_code == "SADI-6104"

    def test_all_have_messages(self):
        for code in (
            CrawlErrorCode.URL_GET_FAILED,
            CrawlErrorCode.BROWSER_FETCH_FAILED,
        ):
            assert code.dev_message
            assert code.message


class TestDocumentParseErrorCode:
    def test_type_is_parse(self):
        assert DocumentParseErrorCode.PARSE_ERROR.error_type == "PARSE"
        assert DocumentParseErrorCode.PDF_ENCRYPTED.error_type == "PARSE"
        assert DocumentParseErrorCode.PDF_PARSE_ERROR.error_type == "PARSE"

    def test_codes_are_sadi_62xx(self):
        assert DocumentParseErrorCode.PARSE_ERROR.error_code == "SADI-6201"
        assert DocumentParseErrorCode.PDF_ENCRYPTED.error_code == "SADI-6202"
        assert DocumentParseErrorCode.PDF_PARSE_ERROR.error_code == "SADI-6203"

    def test_all_have_messages(self):
        for code in (
            DocumentParseErrorCode.PARSE_ERROR,
            DocumentParseErrorCode.PDF_ENCRYPTED,
            DocumentParseErrorCode.PDF_PARSE_ERROR,
        ):
            assert code.dev_message
            assert code.message


class TestCommonErrorCode:
    def test_type_is_common(self):
        for code in (
            CommonErrorCode.MALFORMED_REQUEST,
            CommonErrorCode.VALIDATION_FAILED,
            CommonErrorCode.NOT_FOUND,
            CommonErrorCode.RATE_LIMITED,
            CommonErrorCode.INTERNAL_ERROR,
            CommonErrorCode.SERVICE_UNAVAILABLE,
        ):
            assert code.error_type == "COMMON"

    def test_codes_are_common_4xxx_or_5xxx(self):
        assert CommonErrorCode.MALFORMED_REQUEST.error_code == "COMMON-4000"
        assert CommonErrorCode.VALIDATION_FAILED.error_code == "COMMON-4001"
        assert CommonErrorCode.NOT_FOUND.error_code == "COMMON-4004"
        assert CommonErrorCode.RATE_LIMITED.error_code == "COMMON-4029"
        assert CommonErrorCode.INTERNAL_ERROR.error_code == "COMMON-5000"
        assert CommonErrorCode.SERVICE_UNAVAILABLE.error_code == "COMMON-5001"

    def test_all_have_messages(self):
        for code in (
            CommonErrorCode.MALFORMED_REQUEST,
            CommonErrorCode.VALIDATION_FAILED,
            CommonErrorCode.NOT_FOUND,
            CommonErrorCode.RATE_LIMITED,
            CommonErrorCode.INTERNAL_ERROR,
            CommonErrorCode.SERVICE_UNAVAILABLE,
        ):
            assert code.dev_message
            assert code.message


class TestNoCodeCollisions:
    def test_all_error_codes_are_unique(self):
        all_codes = [
            CommonErrorCode.MALFORMED_REQUEST,
            CommonErrorCode.VALIDATION_FAILED,
            CommonErrorCode.NOT_FOUND,
            CommonErrorCode.RATE_LIMITED,
            CommonErrorCode.INTERNAL_ERROR,
            CommonErrorCode.SERVICE_UNAVAILABLE,
            CrawlErrorCode.URL_GET_FAILED,
            CrawlErrorCode.BROWSER_FETCH_FAILED,
            DocumentParseErrorCode.PARSE_ERROR,
            DocumentParseErrorCode.PDF_ENCRYPTED,
            DocumentParseErrorCode.PDF_PARSE_ERROR,
        ]
        code_strings = [c.error_code for c in all_codes]
        assert len(code_strings) == len(set(code_strings))
