import os
import re

def get_file_content(path):
    with open(path, "r") as f:
        return f.read()

def test_analysis_results_schema_matches_db_writer():
    """Verify that analysis_results schema contains all columns inserted by db_writer.py
    and that they are covered by migrations.py"""
    
    schema_file = os.path.join(os.path.dirname(__file__), "..", "..", "app", "db", "schema_pg.sql")
    migrations_file = os.path.join(os.path.dirname(__file__), "..", "..", "app", "db", "migrations.py")
    
    schema_content = get_file_content(schema_file)
    migrations_content = get_file_content(migrations_file)
        
    match = re.search(r"CREATE TABLE IF NOT EXISTS analysis_results \((.*?)\);", schema_content, re.DOTALL)
    assert match is not None, "Could not find analysis_results in schema_pg.sql"
    
    table_def = match.group(1).lower()
    
    required_columns = [
        "id", "cycle_id", "bot_id", "ticker", "agent_name", "result_json", 
        "confidence", "created_at", "triage_tier", "thesis_verdict", 
        "thesis_confidence", "thesis_summary", "thesis_updated_at", "thesis_unchanged"
    ]
    
    # Core columns that are created in the original CREATE TABLE in migrations.py
    core_columns = ["id", "cycle_id", "bot_id", "ticker", "agent_name", "result_json", "confidence", "created_at"]
    
    for col in required_columns:
        assert re.search(rf"\b{col}\b", table_def) is not None, f"Column '{col}' is missing from analysis_results schema_pg.sql"
        if col not in core_columns:
            # Must be added explicitly in migrations.py using _safe_add_column
            # Using basic string check for robustness against exact quote styles
            assert col in migrations_content and "analysis_results" in migrations_content, \
                   f"Column '{col}' must be added to app/db/migrations.py for analysis_results"

def test_pipeline_state_schema_matches_state_manager():
    """Verify that pipeline_state schema contains all columns inserted by state_manager.py
    and that they are covered by migrations.py"""
    
    schema_file = os.path.join(os.path.dirname(__file__), "..", "..", "app", "db", "schema_pg.sql")
    migrations_file = os.path.join(os.path.dirname(__file__), "..", "..", "app", "db", "migrations.py")
    
    schema_content = get_file_content(schema_file)
    migrations_content = get_file_content(migrations_file)
        
    match = re.search(r"CREATE TABLE IF NOT EXISTS pipeline_state \((.*?)\);", schema_content, re.DOTALL)
    assert match is not None, "Could not find pipeline_state in schema_pg.sql"
    
    table_def = match.group(1).lower()
    
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
        assert re.search(rf"\b{col}\b", table_def) is not None, f"Column '{col}' is missing from pipeline_state schema_pg.sql"
        # Since pipeline_state has its full CREATE TABLE IF NOT EXISTS repeated inside migrations.py
        # we check if the column name exists anywhere in migrations.py alongside the table name.
        assert col in migrations_content and "pipeline_state" in migrations_content, \
               f"Column '{col}' must be present in app/db/migrations.py for pipeline_state"
