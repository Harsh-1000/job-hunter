import pytest

from app.filters import Filters
from app.matcher import evaluate, excluded_word, required_years, title_relevance
from app.resume import ResumeProfile

ME = ResumeProfile(skills=["Node.js", "TypeScript", "JavaScript", "LLMs", "RAG", "AI Agents", "MCP", "AWS",
                           "AWS Lambda", "MongoDB", "PostgreSQL", "REST APIs", "Generative AI"], years=2.8)
KW = ["AI Engineer", "Backend Developer"]


def ev(title, desc, profile=ME, applicants="", company="Acme", **flt):
    return evaluate(title, company, desc, applicants, profile, Filters(keywords=flt.pop("keywords", KW), **flt))


# ---------- required years ----------

@pytest.mark.parametrize("text,lo,hi", [
    ("We need 2-4 years of experience in Node.js", 2, 4),
    ("Experience: 3 to 5 yrs", 3, 5),
    ("Minimum 3+ years of professional experience", 3, None),
    ("5+ years of experience overall, 2 years with Kafka experience", 5, None),
    ("Great culture, free snacks, founded 10 years ago", None, None),
    ("two years of hands-on experience", 2, None),
    ("", None, None),
])
def test_required_years(text, lo, hi):
    assert required_years(text)[:2] == (lo, hi)


# ---------- title ----------

@pytest.mark.parametrize("title,keywords,expected", [
    ("AI Engineer", ["AI Engineer"], 1.0),
    ("Artificial Intelligence Engineer", ["AI Engineer"], 1.0),
    ("Generative AI Developer", ["GenAI Engineer"], 1.0),
    ("LLM Engineer", ["AI Engineer"], 1.0),                # llm implies ai
    ("Back-End Developer (Node.js)", ["Backend Developer"], 1.0),
    ("Sales Executive", ["AI Engineer", "Backend Developer"], 0.0),
    ("Senior Java Developer", ["Java Spring Developer"], 0.5),
    ("Anything", [], 1.0),
])
def test_title_relevance(title, keywords, expected):
    assert title_relevance(title, keywords) == expected


@pytest.mark.parametrize("title,hit", [
    ("Senior Backend Engineer", "senior"), ("Sr. Software Engineer", "sr"), ("Team Lead - AI", "lead"),
    ("Leading AI Engineer", None), ("Software Engineer", None),
])
def test_excluded_word(title, hit):
    assert excluded_word(title, ["senior", "sr", "lead"]) == hit


# ---------- evaluate ----------

GOOD_JD = ("2-4 years of experience. Node.js, TypeScript, LLMs, RAG with pgvector, AI agents using LangChain, "
           "MCP servers, AWS Lambda and SQS, MongoDB. Python is a plus.")


def test_strong_match_scores_high_and_explains():
    m = ev("AI Engineer", GOOD_JD)
    assert m.ok and m.score >= 70
    assert {"Node.js", "RAG", "MCP"} <= set(m.matched)
    assert "Python" in m.missing and "LangChain" in m.missing
    assert m.years_text == "2-4 yrs"


def test_score_is_monotonic_in_skills():
    weak = ev("Backend Developer", "Go, Rust, Kafka, Kubernetes, Node.js, 2 years experience")
    strong = ev("Backend Developer", "Node.js, TypeScript, MongoDB, AWS, REST APIs, 2 years experience")
    assert strong.score > weak.score


def test_too_many_years_rejected():
    m = ev("Backend Developer", "Requires 8+ years of experience with Node.js")
    assert not m.ok and m.code == "years" and "8+" in m.reason


def test_max_years_any_lets_senior_through():
    assert ev("Backend Developer", "8+ years of experience with Node.js", max_years=-1).ok


def test_fresher_role_rejected_for_experienced():
    m = ev("Backend Developer", "0-1 years experience. Node.js TypeScript AWS")
    assert not m.ok and m.code == "fresher"


def test_excluded_title_and_company_and_applicants():
    assert ev("Senior AI Engineer", GOOD_JD).code == "title"
    assert ev("AI Engineer", GOOD_JD, company="Bad Corp Pvt Ltd", exclude_companies=["bad corp"]).code == "company"
    assert ev("AI Engineer", GOOD_JD, applicants="Over 200 applicants", max_applicants=100).code == "applicants"
    assert ev("AI Engineer", GOOD_JD, applicants="35 applicants", max_applicants=100).ok


def test_unrelated_title_and_skills_rejected():
    m = ev("Sales Executive", "Drive B2B revenue, cold calls, CRM hygiene, 2 years experience")
    assert not m.ok and m.code == "unrelated"


def test_unrelated_title_but_strong_skills_kept():
    assert ev("Platform Engineer", GOOD_JD).ok


@pytest.mark.parametrize("threshold,ok", [(0, True), (20, True), (95, False)])
def test_threshold(threshold, ok):
    m = ev("AI Engineer", GOOD_JD, min_score=threshold)
    assert m.ok is ok
    if not ok:
        assert m.code == "threshold" and m.score is not None


def test_without_resume_no_score_but_keyword_filter():
    m = ev("AI Engineer", GOOD_JD, profile=None)
    assert m.ok and m.score is None
    assert ev("Sales Executive", "x", profile=None).code == "unrelated"


def test_my_years_override_changes_auto_max():
    jd = "Need 6+ years of experience in Node.js"
    assert not ev("Backend Developer", jd).ok
    assert ev("Backend Developer", jd, my_years=5).ok


def test_short_description_is_damped():
    m = ev("AI Engineer", "Node.js")
    assert m.ok and m.score < 90   # one matching keyword shouldn't look like a perfect match
