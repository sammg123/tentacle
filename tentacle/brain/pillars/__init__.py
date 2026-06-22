"""Orthogonal evidence pillars.

Each pillar reads ONE independent dimension of conviction (price/technical,
options flow, fundamentals) and returns a small neutral-by-default dict. A pillar
that cannot read its data returns a neutral verdict (score 0, no veto) so a data
hiccup never fires or blocks a trade by accident.
"""
