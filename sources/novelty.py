"""Novelty: GitHub repo stars (60 req/hr unauthenticated - plenty at this
cadence) and the score of the current Hacker News top story (two chained
Firebase calls, effectively unlimited)."""

import urllib.parse

from clock import LED_AMBER, LED_WARM_WHITE, show_number_path
from sources import (_cached, _fit_line, _frame, _http_json, is_offline,
                     register_source)

DEFAULT_REPO = "bitcoin/bitcoin"

SYNTHETIC = {"stars": 85_432, "hn_score": 412}


def github_stars(repo):
    if is_offline():
        return SYNTHETIC["stars"]

    def fetch():
        d = _http_json(f"https://api.github.com/repos/"
                       f"{urllib.parse.quote(repo)}")
        try:
            return int(d["stargazers_count"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached(f"gh:{repo}", 1800, fetch)


def hn_top_score():
    if is_offline():
        return SYNTHETIC["hn_score"]

    def fetch():
        ids = _http_json("https://hacker-news.firebaseio.com/v0/"
                         "topstories.json")
        try:
            top = int(ids[0])
        except (TypeError, IndexError, ValueError):
            return None
        item = _http_json(f"https://hacker-news.firebaseio.com/v0/"
                          f"item/{top}.json")
        try:
            return int(item["score"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached("hn_top", 900, fetch)


def _clean_repo(raw):
    """owner/name or empty. Anything else falls back to the default."""
    repo = str(raw or "").strip().strip("/")
    if repo.count("/") == 1 and " " not in repo \
            and all(part for part in repo.split("/")):
        return repo
    return DEFAULT_REPO


def _novelty_frames(options, wanted=None):
    def want(fid):
        return wanted is None or fid in wanted

    frames = []
    if want("github_stars"):
        repo = _clean_repo(options.get("github_repo"))
        stars = github_stars(repo)
        if stars is not None and stars <= 9_999_999:
            name = _fit_line(repo.split("/")[-1].upper())
            frames.append(_frame(
                "github_stars",
                show_number_path(stars, tl=name, br="github stars"),
                number=stars, color=LED_WARM_WHITE))
    if want("hn_top"):
        score = hn_top_score()
        if score is not None and score <= 9_999_999:
            frames.append(_frame(
                "hn_top",
                show_number_path(score, tl="hacker news", br="top points"),
                number=score, color=LED_AMBER))
    return frames


register_source(
    "novelty",
    label="Novelty",
    category="novelty",
    description="Stars on a GitHub repo of your choice, and how spicy the "
                "internet is today (Hacker News top-story score). Keyless.",
    frames=[
        ("github_stars", "GitHub repo stars"),
        ("hn_top", "Hacker News top score"),
    ],
    builder=_novelty_frames,
    options_schema={
        "github_repo": {"type": "text", "label": "GitHub repo",
                        "default": DEFAULT_REPO,
                        "placeholder": "owner/name"},
    },
)
