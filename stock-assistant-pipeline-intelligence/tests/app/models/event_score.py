import pytest

from app.models.event_score import RuleScore


@pytest.mark.parametrize(
    ("rule_score", "expected"),
    [
        pytest.param(RuleScore(9.0, 8.0, 6.0, 4.0), 6.0, id="weighted"),
        pytest.param(RuleScore(10.0, 10.0, 10.0, 10.0), 8.0, id="maximum"),
    ],
)
def base_rule_score_success(rule_score, expected):
    assert rule_score.base_rule_score == pytest.approx(expected)
