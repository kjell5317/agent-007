"""Commute planning.

Built on Google Maps Distance Matrix (durations + distances) and Open-Meteo
(rain probability — no key required). The week-ahead planner walks every event
on the user's calendar, picks bike vs. public transport per the rules in
[planner.py](planner.py), and writes commute events back to the calendar.

Cached lookups live in `route_cache` so a week's worth of planning costs only
the distinct (origin, destination, mode, hour-bucket) triples.
"""

from app.services.commute.planner import plan_week_commutes

__all__ = ["plan_week_commutes"]
