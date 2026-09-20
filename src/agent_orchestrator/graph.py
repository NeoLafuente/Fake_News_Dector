from langgraph.graph import StateGraph, END
from .state import GraphState
from .nodes import extract_facts_node, search_node, evaluate_nli_node

def build_graph():
    # Define a new StateGraph
    workflow = StateGraph(GraphState)
    
    # Add nodes
    workflow.add_node("fact_extractor", extract_facts_node)
    workflow.add_node("evidence_search", search_node)
    workflow.add_node("nli_evaluator", evaluate_nli_node)
    
    # Define edges (sequential flow)
    workflow.set_entry_point("fact_extractor")
    workflow.add_edge("fact_extractor", "evidence_search")
    workflow.add_edge("evidence_search", "nli_evaluator")
    workflow.add_edge("nli_evaluator", END)
    
    # Compile Graph
    app = workflow.compile()
    return app
