"""Local multi-attribute matching engine for Lost and Found reports.

Zero external LLM or API dependencies. Evaluates item category compatibility,
distinguishing details, route overlap, and trip time proximity algorithmically.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Category compatibility mappings
CATEGORY_GROUPS: Dict[str, Set[str]] = {
    "phone": {"phone", "iphone", "smartphone", "cellphone", "android", "mobile"},
    "wallet": {"wallet", "purse", "billfold", "cardholder", "moneyclip"},
    "bag": {"bag", "backpack", "tote", "duffel", "briefcase", "handbag", "luggage", "suitcase"},
    "keys": {"keys", "keychain", "car keys", "house keys", "fob"},
    "clothing": {"jacket", "coat", "hoodie", "sweater", "scarf", "hat", "gloves"},
    "electronics": {"laptop", "tablet", "ipad", "headphones", "airpods", "earbuds", "charger"},
    "eyewear": {"glasses", "sunglasses", "spectacles"},
}


class LostFoundMatchingEngine:
    """Evaluates pairs of lost and found records to detect high-confidence matches."""

    def __init__(self, match_threshold: float = 0.75):
        self.match_threshold = match_threshold

    def _normalize_tokens(self, text: str) -> Set[str]:
        """Cleans and extracts meaningful word tokens from free-form text."""
        if not text:
            return set()
        clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        stop_words = {
            "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "my", "was",
            "and", "or", "it", "is", "left", "found", "lost", "cab", "taxi", "driver",
            "passenger", "seat", "floor", "car", "reported", "during"
        }
        tokens = {word for word in clean.split() if len(word) > 1 and word not in stop_words}
        return tokens

    def _get_canonical_category(self, text: str) -> str:
        """Finds the primary category group for an item description."""
        tokens = self._normalize_tokens(text)
        for canonical, aliases in CATEGORY_GROUPS.items():
            if tokens.intersection(aliases) or any(alias in text.lower() for alias in aliases):
                return canonical
        return text.strip().lower()

    def _calculate_category_score(self, lost_item: str, found_item: str) -> float:
        """Determines if the lost and found categories are compatible."""
        cat_lost = self._get_canonical_category(lost_item)
        cat_found = self._get_canonical_category(found_item)

        if cat_lost == cat_found:
            return 1.0

        # Check alias overlap
        tokens_lost = self._normalize_tokens(lost_item)
        tokens_found = self._normalize_tokens(found_item)
        if tokens_lost.intersection(tokens_found):
            return 0.9

        # Incompatible categories
        return 0.0

    def _calculate_token_jaccard(self, text1: str, text2: str) -> float:
        """Computes Jaccard similarity across token sets."""
        tokens1 = self._normalize_tokens(text1)
        tokens2 = self._normalize_tokens(text2)

        if not tokens1 or not tokens2:
            return 0.0

        intersection = tokens1.intersection(tokens2)
        union = tokens1.union(tokens2)
        return len(intersection) / len(union)

    def _calculate_details_score(self, lost: Dict[str, Any], found: Dict[str, Any]) -> float:
        """Evaluates similarity across distinguishing features (color, brand, markings)."""
        lost_details = " ".join([
            str(lost.get("distinguishing_detail_1") or ""),
            str(lost.get("distinguishing_detail_2") or ""),
        ])
        found_details = " ".join([
            str(found.get("distinguishing_detail_1") or ""),
            str(found.get("distinguishing_detail_2") or ""),
        ])

        return self._calculate_token_jaccard(lost_details, found_details)

    def _calculate_route_score(self, lost_route: str, found_route: str) -> float:
        """Checks for route and pickup or dropoff neighborhood overlap."""
        if not lost_route or not found_route:
            return 0.5  # Neutral if one party omitted route

        # Exact or substring match
        lr = lost_route.lower()
        fr = found_route.lower()
        if lr in fr or fr in lr:
            return 1.0

        # Jaccard overlap on location names
        tokens_l = self._normalize_tokens(lost_route)
        tokens_f = self._normalize_tokens(found_route)
        intersection = tokens_l.intersection(tokens_f)
        if intersection:
            return min(1.0, 0.6 + 0.2 * len(intersection))

        return 0.2

    def _calculate_time_score(self, lost_time: str, found_time: str) -> float:
        """Compares approximate trip times and shifts."""
        if not lost_time or not found_time:
            return 0.5

        lt = lost_time.lower()
        ft = found_time.lower()

        # Check for shared time markers
        time_keywords = ["morning", "afternoon", "evening", "night", "today", "yesterday", "am", "pm"]
        shared_keywords = [k for k in time_keywords if k in lt and k in ft]

        if shared_keywords:
            return 0.85

        tokens_l = self._normalize_tokens(lost_time)
        tokens_f = self._normalize_tokens(found_time)
        if tokens_l.intersection(tokens_f):
            return 0.75

        return 0.3

    async def evaluate_pair(
        self,
        lost: Dict[str, Any],
        found: Dict[str, Any],
    ) -> Tuple[float, Dict[str, Any]]:
        """Evaluates a single lost-item record against a found-item record."""
        lost_item_type = str(lost.get("item_type") or "")
        found_item_type = str(found.get("item_type") or "")

        # 1. Category Compatibility (Weight: 35%)
        category_score = self._calculate_category_score(lost_item_type, found_item_type)
        if category_score == 0.0:
            # Completely incompatible categories cannot match
            return 0.0, {
                "overall_score": 0.0,
                "is_high_confidence": False,
                "category_score": 0.0,
                "details_score": 0.0,
                "route_score": 0.0,
                "time_score": 0.0,
                "rejection_reason": f"Incompatible item types: '{lost_item_type}' vs '{found_item_type}'",
            }

        # 2. Distinguishing Details (Weight: 35%)
        details_score = self._calculate_details_score(lost, found)

        # 3. Route / Neighborhood Overlap (Weight: 15%)
        route_score = self._calculate_route_score(
            str(lost.get("route_area") or ""),
            str(found.get("route_area") or ""),
        )

        # 4. Trip Time Proximity (Weight: 15%)
        time_score = self._calculate_time_score(
            str(lost.get("approx_trip_time") or ""),
            str(found.get("approx_trip_time") or ""),
        )

        # Weighted aggregate
        overall_score = (
            (category_score * 0.35)
            + (details_score * 0.35)
            + (route_score * 0.15)
            + (time_score * 0.15)
        )

        overall_score = round(min(1.0, max(0.0, overall_score)), 3)
        is_high_confidence = overall_score >= self.match_threshold

        breakdown = {
            "overall_score": overall_score,
            "is_high_confidence": is_high_confidence,
            "category_score": round(category_score, 2),
            "details_score": round(details_score, 2),
            "route_score": round(route_score, 2),
            "time_score": round(time_score, 2),
        }

        logger.info(
            "Evaluated match pair: lost='%s' found='%s' score=%.3f (high_confidence=%s)",
            lost_item_type,
            found_item_type,
            overall_score,
            is_high_confidence,
        )

        return overall_score, breakdown
