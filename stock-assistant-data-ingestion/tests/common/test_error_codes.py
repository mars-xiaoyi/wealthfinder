from app.common.error_codes import (
    DocumentParseErrorCode,
    ErrorCode,
    NetworkErrorCode,
)


class TestErrorCodeBase:
    def test_error_code_is_frozen(self):
        code = NetworkErrorCode.HTTP_403
        assert isinstance(code, ErrorCode)
        # Frozen dataclass — assignment raises
        try:
            code.error_code = "SADI-9999"
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestNetworkErrorCode:
    def test_type_is_network(self):
        assert NetworkErrorCode.HTTP_403.error_type == "NETWORK"
        assert NetworkErrorCode.HTTP_404.error_type == "NETWORK"
        assert NetworkErrorCode.NETWORK_ERROR.error_type == "NETWORK"
        assert NetworkErrorCode.BROWSER_FETCH_FAILED.error_type == "NETWORK"

    def test_codes_are_sadi_61xx(self):
        assert NetworkErrorCode.HTTP_403.error_code == "SADI-6101"
        assert NetworkErrorCode.HTTP_404.error_code == "SADI-6102"
        assert NetworkErrorCode.NETWORK_ERROR.error_code == "SADI-6103"
        assert NetworkErrorCode.BROWSER_FETCH_FAILED.error_code == "SADI-6104"

    def test_all_have_messages(self):
        for code in (
            NetworkErrorCode.HTTP_403,
            NetworkErrorCode.HTTP_404,
            NetworkErrorCode.NETWORK_ERROR,
            NetworkErrorCode.BROWSER_FETCH_FAILED,
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


class TestNoCodeCollisions:
    def test_all_error_codes_are_unique(self):
        all_codes = [
            NetworkErrorCode.HTTP_403,
            NetworkErrorCode.HTTP_404,
            NetworkErrorCode.NETWORK_ERROR,
            NetworkErrorCode.BROWSER_FETCH_FAILED,
            DocumentParseErrorCode.PARSE_ERROR,
            DocumentParseErrorCode.PDF_ENCRYPTED,
            DocumentParseErrorCode.PDF_PARSE_ERROR,
        ]
        code_strings = [c.error_code for c in all_codes]
        assert len(code_strings) == len(set(code_strings))
