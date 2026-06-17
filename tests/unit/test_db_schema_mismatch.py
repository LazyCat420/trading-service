import os
import re

def test_analysis_results_schema_matches_db_writer():
    """Verify that analysis_results schema contains all columns inserted by db_writer.py"""
    schema_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "app", "db", "schema_pg.sql"
    )
    
    with open(schema_file, "r") as f:
        content = f.read()
        
    # Extract analysis_results table definition
    match = re.search(r"CREATE TABLE IF NOT EXISTS analysis_results \((.*?)\);", content, re.DOTALL)
    assert match is not None, "Could not find analysis_results in schema_pg.sql"
    
    table_def = match.group(1).lower()
    
    # These are the columns db_writer.py tries to insert
    required_columns = [
        "id", "cycle_id", "bot_id", "ticker", "agent_name", "result_json", 
        "confidence", "created_at", "triage_tier", "thesis_verdict", 
        "thesis_confidence", "thesis_summary", "thesis_updated_at", "thesis_unchanged"
    ]
    
    for col in required_columns:
        # Check if the column exists in the table definition
        assert re.search(rf"\b{col}\b", table_def) is not None, f"Column '{col}' is missing from analysis_results schema"

def test_pipeline_state_schema_matches_state_manager():
    """Verify that pipeline_state schema contains all columns inserted by state_manager.py"""
    schema_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "app", "db", "schema_pg.sql"
    )
    
    with open(schema_file, "r") as f:
        content = f.read()
        
    # Extract pipeline_state table definition
    match = re.search(r"CREATE TABLE IF NOT EXISTS pipeline_state \((.*?)\);", content, re.DOTALL)
    assert match is not None, "Could not find pipeline_state in schema_pg.sql"
    
    table_def = match.group(1).lower()
    
    # These are the columns state_manager.py tries to insert
    required_columns = [
        "singleton_id", "status", "cycle_id", "started_at", "finished_at",
        "requested_pipeline_version", "effective_pipeline_version",
        "benchmark_group", "execution_mode", "v2_stage",
        "tickers", "progress", "error", "phase",
        "operational_phase", "step_count", "total_steps",
        "collect_flag", "analyze_flag", "trade_flag",
        "max_tickers", "discovered_tickers", "dynamic_selection_mode",
        "updated_at"
    ]
    
    for col in required_columns:
        # Check if the column exists in the table definition
        assert re.search(rf"\b{col}\b", table_def) is not None, f"Column '{col}' is missing from pipeline_state schema"
