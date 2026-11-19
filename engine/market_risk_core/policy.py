from typing import Dict, Optional

DEFAULT_THRESHOLDS = {"allow_max": 34, "challenge_max": 54, "review_max": 74}


class PolicyEngine:
    def __init__(self, thresholds: Optional[Dict[str, int]] = None):
        values = dict(DEFAULT_THRESHOLDS)
        if thresholds is not None:
            if not isinstance(thresholds, dict) or set(thresholds) != set(values):
                raise ValueError("policy thresholds must specify exactly allow_max, challenge_max and review_max")
            values = dict(thresholds)
        if any(isinstance(v, bool) or not isinstance(v, int) for v in values.values()):
            raise ValueError("policy thresholds must be integers")
        if not 0 <= values["allow_max"] < values["challenge_max"] < values["review_max"] < 100:
            raise ValueError("policy thresholds must satisfy 0 <= allow_max < challenge_max < review_max < 100")
        self.allow_max = values["allow_max"]
        self.challenge_max = values["challenge_max"]
        self.review_max = values["review_max"]

    def band(self, risk_score: int) -> str:
        if isinstance(risk_score, bool) or not isinstance(risk_score, int) or not 0 <= risk_score <= 100:
            raise ValueError("risk score must be an integer between 0 and 100")
        if risk_score <= self.allow_max:
            return "ALLOW"
        if risk_score <= self.challenge_max:
            return "CHALLENGE"
        if risk_score <= self.review_max:
            return "REVIEW"
        return "RESTRICT"

