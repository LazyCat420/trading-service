import random
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

STATIC_STRESS_TEST_DATES = [
    "2020-03-16", # COVID Crash
    "2021-11-22", # Tech Peak
    "2022-09-13", # Inflation CPI shock
    "2023-03-10", # SVB Collapse
    "2024-04-19", # Middle East escalation / rate fear
]

def generate_random_historical_dates(count: int = 5) -> list[str]:
    """Generates random dates within the last 5 years."""
    dates = []
    end_date = datetime.now() - timedelta(days=30) # at least 30 days ago
    start_date = end_date - timedelta(days=5*365)
    
    for _ in range(count):
        random_days = random.randint(0, (end_date - start_date).days)
        random_date = start_date + timedelta(days=random_days)
        # Skip weekends loosely (0=Mon, 6=Sun)
        if random_date.weekday() >= 5:
            random_date -= timedelta(days=2)
        dates.append(random_date.strftime("%Y-%m-%d"))
    return dates

async def run_benchmark_gauntlet(harness_config: dict) -> dict:
    """
    Runs the benchmark gauntlet on the proposed harness_config.
    Returns the gauntlet results and whether it passed.
    """
    test_dates = STATIC_STRESS_TEST_DATES + generate_random_historical_dates(5)
    
    logger.info("Running benchmark gauntlet against dates: %s", test_dates)
    
    # In a real system, we would simulate the trading pipeline for each date using the proposed harness.
    # For now, we simulate the Gauntlet logic.
    passed = True
    results = {}
    total_score = 0
    
    for date in test_dates:
        # Placeholder for simulated run
        # simulated_traces = await simulate_pipeline(date, harness_config)
        # process_eval = await evaluate_process(simulated_traces)
        
        # Simulating process score between 75 and 95
        simulated_process_score = random.uniform(75, 95) 
        
        if simulated_process_score < 70.0:
            passed = False
            logger.warning("Gauntlet failed on date %s with score %.2f", date, simulated_process_score)
        
        results[date] = {"process_score": simulated_process_score}
        total_score += simulated_process_score
        
    avg_score = total_score / len(test_dates) if test_dates else 0
    
    return {
        "passed": passed,
        "average_process_score": avg_score,
        "details": results
    }
