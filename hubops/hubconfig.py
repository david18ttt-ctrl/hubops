"""Shared settings for the hub tools: the hub's identity, its clock and where local state lives.

    import hubconfig
    hubconfig.TIME_ZONE                 the hub's time zone (a ZoneInfo)
    hubconfig.operating_day(now)        the night a report is about (moves to the new day at RESET_HOUR)
    hubconfig.work_day(now)             the WMS working day (6 AM to 6 AM) that `now` falls in
    hubconfig.data_path("x.json")       <repo>/data/x.json, with data/ created if missing
"""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA_DIR = REPO / "data"  # every state file, cache and log the tools write (gitignored)

TIME_ZONE = ZoneInfo("America/Chicago")

HUB_CODE = "HOU.H"  # this hub, as the WMS names it
HUB_NAME = "HOU.H"
HUB_ID = 1  # the hub's id in the operations / dashboard calls
TMS_HUB_ID = 1  # the hub's id in the truck (TMS) calls
SORTER_ID = 1  # Sorter 1 in Sorting Records

WORK_DAY_START_HOUR = 6  # the WMS working day runs 6 AM to 6 AM and is named by the day it starts
RESET_HOUR = 11  # reports keep last night's numbers until 11 AM, since people still use them after 6


def data_path(*parts):
    """A path under data/; the folder is created on first use."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def now_local():
    """The hub's local time, without tzinfo (the form every tool compares against)."""
    return datetime.now(TIME_ZONE).replace(tzinfo=None)


def work_day(now):
    """The WMS working day: before 6 AM it is still the previous day's."""
    return (now - timedelta(hours=WORK_DAY_START_HOUR)).date()


def operating_day(now):
    """The night a report is about. Sorting runs 6 AM to 6 AM, but reports move to the new day at RESET_HOUR."""
    return now.date() if now.hour >= RESET_HOUR else datetime.fromordinal(now.toordinal() - 1).date()


def operating_minutes(hour, minute):
    """Minutes into the 6 AM to 6 AM operating day, so 1:00 counts as after 23:30."""
    return hour * 60 + minute + (1440 if hour < WORK_DAY_START_HOUR else 0)
