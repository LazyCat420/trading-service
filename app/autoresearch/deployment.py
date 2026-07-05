import logging
import os
import shutil

logger = logging.getLogger(__name__)

# Assumes trading-service/app/autoresearch/deployment.py
AGENTS_MD_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.agents/AGENTS.md"))
BACKUP_AGENTS_MD_PATH = AGENTS_MD_PATH + ".bak"

def deploy_harness_update(proposed_changes: str):
    """
    Applies the proposed harness changes (e.g., to AGENTS.md).
    Creates a backup of the current verified state before applying.
    """
    logger.info("Deploying new harness update...")
    
    if os.path.exists(AGENTS_MD_PATH):
        # Save current verified state
        shutil.copy2(AGENTS_MD_PATH, BACKUP_AGENTS_MD_PATH)
        
    with open(AGENTS_MD_PATH, "a") as f:
        f.write(f"\n\n# AUTORESEARCH UPDATE\n{proposed_changes}\n")
    
    logger.info("Harness update deployed successfully.")

def rollback_harness():
    """
    Circuit breaker rollback: restores the last verified improvement.
    """
    logger.warning("Circuit breaker tripped! Rolling back to last verified improvement...")
    
    if os.path.exists(BACKUP_AGENTS_MD_PATH):
        shutil.copy2(BACKUP_AGENTS_MD_PATH, AGENTS_MD_PATH)
        logger.info("Rollback successful.")
    else:
        logger.error("No backup found! Cannot rollback.")
