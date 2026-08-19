import os
import re

def test_migrations_contain_all_schema_tables():
    """
    Ensures that every table defined in schema_pg.sql with CREATE TABLE IF NOT EXISTS
    is also present in migrations.py with a corresponding CREATE TABLE IF NOT EXISTS block.
    
    This prevents schema drift where a developer adds a table to the initialization script
    but forgets to add it to the migration script for existing deployments.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema_path = os.path.join(base_dir, "scripts", "migration", "schema_pg.sql")
    migrations_path = os.path.join(base_dir, "scripts", "migration", "pg_migrations.py")
    
    assert os.path.exists(schema_path), f"schema_pg.sql not found at {schema_path}"
    assert os.path.exists(migrations_path), f"migrations.py not found at {migrations_path}"
    
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_content = f.read()
        
    with open(migrations_path, "r", encoding="utf-8") as f:
        migrations_content = f.read()
        
    # Extract all CREATE TABLE IF NOT EXISTS blocks from schema_pg.sql
    schema_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_\.]+)", schema_content, re.IGNORECASE))
    
    # Core tables that have existed since day 1 and don't need migrations because they were created
    # before migrations were necessary. 
    # For now we will just enforce ALL tables to be safely in migrations as IF NOT EXISTS.
    # It is harmless to have CREATE TABLE IF NOT EXISTS in migrations for old tables too.
    
    # Extract all CREATE TABLE IF NOT EXISTS from migrations.py
    mig_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_\.]+)", migrations_content, re.IGNORECASE))
    
    # Case insensitive compare
    schema_tables_lower = {t.lower() for t in schema_tables}
    mig_tables_lower = {t.lower() for t in mig_tables}
    
    missing_tables = schema_tables_lower - mig_tables_lower
    
    assert not missing_tables, (
        f"Found tables in schema_pg.sql that are missing from migrations.py! "
        f"Please add a CREATE TABLE IF NOT EXISTS block to migrations.py for: {sorted(list(missing_tables))}"
    )

def test_migrations_contain_schema_alter_tables():
    """
    Ensures any ALTER TABLE statements in schema_pg.sql are represented in migrations.py
    using _safe_add_column.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema_path = os.path.join(base_dir, "scripts", "migration", "schema_pg.sql")
    migrations_path = os.path.join(base_dir, "scripts", "migration", "pg_migrations.py")
    
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_content = f.read()
        
    with open(migrations_path, "r", encoding="utf-8") as f:
        migrations_content = f.read()
        
    # Extract all ALTER TABLE ADD COLUMN IF NOT EXISTS
    # Example: ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS health_score INTEGER DEFAULT 50;
    alter_matches = re.findall(r"ALTER TABLE\s+([a-zA-Z0-9_]+)\s+ADD COLUMN IF NOT EXISTS\s+([a-zA-Z0-9_]+)", schema_content, re.IGNORECASE)
    
    # Extract _safe_add_column calls
    # Example: _safe_add_column(conn, "watchlist", "health_score", ...)
    safe_adds = re.findall(r'_safe_add_column\([^,]+,\s*"([^"]+)",\s*"([^"]+)"', migrations_content)
    
    safe_adds_lower = {(t.lower(), c.lower()) for t, c in safe_adds}
    
    missing_columns = []
    for table, col in alter_matches:
        if (table.lower(), col.lower()) not in safe_adds_lower:
            missing_columns.append(f"{table}.{col}")
            
    assert not missing_columns, (
        f"Found ALTER TABLE columns in schema_pg.sql that are missing from migrations.py! "
        f"Please add _safe_add_column to migrations.py for: {missing_columns}"
    )
