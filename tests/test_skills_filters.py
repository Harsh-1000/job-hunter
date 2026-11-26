import pytest

from app.filters import (Filters, Query, parse_applicants, parse_list, parse_quiet_hours, parse_threshold,
                         parse_years, queries_for, summary)
from app.skills import SKILLS, find_skills, patterns


# ---------- skills ----------

def test_every_skill_pattern_compiles():
    assert len(patterns()) == len(SKILLS)


@pytest.mark.parametrize("text,present,absent", [
    ("Java and Spring Boot", {"Java", "Spring Boot"}, {"JavaScript"}),
    ("JavaScript only", {"JavaScript"}, {"Java"}),
    ("Golang microservices", {"Go", "Microservices"}, set()),
    ("Go to the office, good food", set(), {"Go"}),
    ("C# and .NET Core", {"C#", ".NET"}, {"C"}),
    ("RAG with LLMs and MCP", {"RAG", "LLMs", "MCP"}, set()),
    ("REST APIs; the rest of the team", {"REST APIs"}, set()),
    ("rest of the team is great", set(), {"REST APIs"}),
    ("ts-node and node", {"Node.js"}, {"TypeScript"}),
    ("solid understanding of things", set(), {"Design Patterns"}),
])
def test_find_skills(text, present, absent):
    found = set(find_skills(text))
    assert present <= found
    assert not (absent & found)


def test_find_skills_counts_mentions():
    assert find_skills("Python, python and PYTHON")["Python"] == 3


# ---------- filters ----------

def test_defaults():
    f = Filters()
    assert f.locations == ["India"] and f.posted == "r86400" and f.min_score == 0
    assert f.experience_levels == ["2", "3", "4"] and "senior" in f.exclude_words


def test_json_roundtrip_and_unknown_keys_ignored():
    f = Filters(keywords=["AI Engineer"], min_score=25, quiet_start=23, quiet_end=8)
    assert Filters.from_json(f.to_json()) == f
    assert Filters.from_json('{"keywords": ["x"], "removed_field": 1}').keywords == ["x"]
    assert Filters.from_json(None) == Filters()


def test_query_params_match_linkedin_codes():
    q = Query("AI Engineer", "India", "r86400", ("4", "2"), ("F",), ("2", "3"), True)
    p = q.params(start=20)
    assert p == {"keywords": "AI Engineer", "location": "India", "f_TPR": "r86400", "sortBy": "DD", "start": 20,
                 "f_E": "2,4", "f_JT": "F", "f_WT": "2,3", "f_AL": "true"}
    assert "f_E" not in Query("x", "India", "r3600").params()


def test_query_key_shared_when_same_search():
    a = Query("AI Engineer", "India", "r86400", ("2", "3"))
    b = Query("ai engineer", "india", "r86400", ("3", "2"))
    c = Query("AI Engineer", "India", "r604800", ("2", "3"))
    assert a.key == b.key and a.key != c.key


def test_queries_for_is_keywords_x_locations():
    f = Filters(keywords=["A", "B"], locations=["India", "Remote"])
    assert len(queries_for(f)) == 4


def test_years_and_max_years():
    f = Filters()
    assert f.years(2.8) == 2.8 and f.max_years_allowed(2.8) == pytest.approx(4.8)
    f.my_years = 3
    assert f.years(2.8) == 3 and f.max_years_allowed(2.8) == 5
    f.max_years = 4
    assert f.max_years_allowed(None) == 4
    f.max_years = -1
    assert f.max_years_allowed(None) is None


@pytest.mark.parametrize("start,end,hour,quiet", [
    (23, 8, 23, True), (23, 8, 3, True), (23, 8, 8, False), (23, 8, 12, False),
    (13, 15, 14, True), (13, 15, 15, False), (None, None, 3, False),
])
def test_quiet_hours(start, end, hour, quiet):
    assert Filters(quiet_start=start, quiet_end=end).in_quiet_hours(hour) is quiet


def test_parse_list():
    assert parse_list("AI Engineer, GenAI engineer\nLLM ; ai engineer,, ") == ["AI Engineer", "GenAI engineer", "LLM"]
    assert parse_list("a,b,c", limit=2) == ["a", "b"]
    assert parse_list("") == []


@pytest.mark.parametrize("text,val", [("20", 20), (" 0 ", 0), ("100%", 100)])
def test_parse_threshold(text, val):
    assert parse_threshold(text) == val


@pytest.mark.parametrize("bad", ["101", "-1", "abc", "", "2 0"])
def test_parse_threshold_rejects(bad):
    with pytest.raises(ValueError):
        parse_threshold(bad)


def test_parse_years_quiet_applicants():
    assert parse_years("2.5") == 2.5 and parse_years("3 yrs") == 3
    with pytest.raises(ValueError):
        parse_years("lots")
    assert parse_quiet_hours("23-8") == (23, 8) and parse_quiet_hours("off") == (None, None)
    with pytest.raises(ValueError):
        parse_quiet_hours("25-3")
    assert parse_applicants("100") == 100 and parse_applicants("any") is None
    with pytest.raises(ValueError):
        parse_applicants("many")


def test_summary_mentions_everything():
    s = summary(Filters(keywords=["AI Engineer"], min_score=20), 2.0)
    for part in ("AI Engineer", "India", "Past 24 hours", "Entry level", "20%", "Max years"):
        assert part in s
    assert "send all" in summary(Filters(), None)
