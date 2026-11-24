"""Review permission identities; these do not assign pipeline ownership."""

# Gibb reviews as GA. Sales-owner filters and brief routing keep their own lists.
REVIEW_ACTORS = frozenset({"RG", "NC", "NDL", "MG", "GA"})
