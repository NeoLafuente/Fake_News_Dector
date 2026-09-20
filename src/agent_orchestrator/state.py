from typing import TypedDict, List, Dict, Any

class Fact(TypedDict):
    id: int
    claim: str

class SearchResult(TypedDict):
    snippet: str
    url: str

class NLIAnalysis(TypedDict):
    claim_id: int
    claim: str
    urls: List[str]
    label: str
    confidence: float

class GraphState(TypedDict):
    raw_transcript: str
    extracted_facts: List[Fact]
    search_results: Dict[int, List[SearchResult]]
    final_nli_analysis: List[NLIAnalysis]
