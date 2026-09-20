"""Agent nodes. Every external call is metered before it is made."""
import os
from typing import List

from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from src.security import budget
from src.security.config import settings

from . import nli
from .state import Fact, GraphState, NLIAnalysis

# Flash-Lite sits on a far larger free allowance than the Flash aliases
# (hundreds of requests/day vs a couple of dozen), and this workload is a
# single structured extraction call — it does not need a bigger model.
LLM_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

_llm = None


def get_llm() -> ChatGoogleGenerativeAI:
    """Built lazily so importing this module never needs an API key."""
    global _llm
    if _llm is None:
        _llm = ChatGoogleGenerativeAI(
            model=LLM_MODEL,
            temperature=0,
            max_output_tokens=int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "1024")),
        )
    return _llm


class ExtractedFacts(BaseModel):
    facts: List[str] = Field(
        description="A list of standalone factual claims extracted from the raw transcript."
    )


def extract_facts_node(state: GraphState) -> GraphState:
    """Node 1: pull verifiable claims out of the transcript (one LLM call)."""
    transcript = budget.clamp_transcript(state["raw_transcript"])
    max_facts = settings.max_facts_per_run

    prompt = PromptTemplate.from_template(
        "You are an expert fact-checker. Extract the {max_facts} most important "
        "verifiable factual claims from the following transcript. Prefer specific, "
        "checkable statements over opinions.\n\n"
        "Transcript: {transcript}\n\n"
        "Return the output as a list of standalone claims."
    )

    budget.claim(budget.LLM_CALLS)
    chain = prompt | get_llm().with_structured_output(ExtractedFacts)
    result = chain.invoke({"transcript": transcript, "max_facts": max_facts})

    # The cap is enforced here too: the prompt is a request, not a guarantee.
    facts = [Fact(id=i, claim=claim) for i, claim in enumerate(result.facts[:max_facts])]
    return {"extracted_facts": facts}


def _search_duckduckgo(query: str, limit: int) -> List[dict]:
    """Free and keyless, which is what keeps the search bill at exactly zero."""
    from ddgs import DDGS

    with DDGS() as engine:
        hits = engine.text(query, max_results=limit)
    return [
        {"snippet": hit.get("body", ""), "url": hit.get("href", "")}
        for hit in hits
        if hit.get("body")
    ]


def _search_brave(query: str, limit: int) -> List[dict]:
    """Metered since Brave retired its free tier — only used when opted in."""
    import requests

    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        return []
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
        params={"q": query, "count": limit},
        timeout=20,
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])[:limit]
    return [
        {"snippet": r["description"], "url": r["url"]}
        for r in results
        if r.get("description") and r.get("url")
    ]


def search_node(state: GraphState) -> GraphState:
    """Node 2: gather evidence, never exceeding the per-run search ceiling."""
    facts = state["extracted_facts"]
    provider = settings.search_provider.lower()
    engine = _search_brave if provider == "brave" else _search_duckduckgo

    search_results = {}
    searches_left = settings.max_searches_per_run

    for fact in facts:
        if searches_left <= 0:
            search_results[fact["id"]] = []
            continue
        try:
            budget.claim(budget.SEARCHES)
        except budget.BudgetExceeded as exc:
            print(f"[search] {exc}")
            search_results[fact["id"]] = []
            continue

        searches_left -= 1
        try:
            search_results[fact["id"]] = engine(fact["claim"], limit=3)
        except Exception as exc:  # noqa: BLE001 - one bad query must not sink the run
            print(f"[search] Error buscando '{fact['claim'][:60]}': {exc}")
            search_results[fact["id"]] = []

    return {"search_results": search_results}



def evaluate_nli_node(state: GraphState) -> GraphState:
    """Node 3: judge each claim against its evidence with the ONNX cross-encoder."""
    facts = state["extracted_facts"]
    search_results = state["search_results"]
    analysis = []

    for fact in facts:
        claim_id, claim_text = fact["id"], fact["claim"]
        items = search_results.get(claim_id, [])
        urls = [i["url"] for i in items if i.get("url") and i["url"] != "#"]

        pairs = [(item["snippet"], claim_text) for item in items if item.get("snippet")]
        try:
            scored = nli.predict(pairs)
        except nli.NLIUnavailable as exc:
            print(f"[nli] {exc}")
            scored = []

        # Confidence-weighted vote: a hesitant contradiction should not outrank
        # two confident entailments.
        weights = {"contradiction": 0.0, "entailment": 0.0, "neutral": 0.0}
        for result in scored:
            if result["label"] in weights:
                weights[result["label"]] += result["confidence"]

        if not any(weights.values()):
            final_label, confidence = "unknown", 0.0
        else:
            final_label = max(weights, key=weights.get)
            confidence = weights[final_label] / sum(weights.values())

        analysis.append(NLIAnalysis(
            claim_id=claim_id,
            claim=claim_text,
            urls=list(dict.fromkeys(urls)),
            label=final_label,
            confidence=round(confidence, 3),
        ))

    return {"final_nli_analysis": analysis}
