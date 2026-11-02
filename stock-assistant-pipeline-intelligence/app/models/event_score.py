from dataclasses import dataclass


@dataclass
class RuleScore:
    """An Event's rule-based score, one value in [0, 10] per dimension."""

    event_type_score: float
    source_authority_score: float
    sentiment_strength_score: float
    source_heat_score: float

    @property
    def base_rule_score(self) -> float:
        """Return the weighted sum of the four dimensions, in [0, 8]."""
        # weights sum to 0.80; recency's 0.20 share is added by the Cache layer
        return (
            self.event_type_score * 0.30
            + self.source_authority_score * 0.25
            + self.sentiment_strength_score * 0.15
            + self.source_heat_score * 0.10
        )
