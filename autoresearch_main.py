import asyncio
import logging
import os
import sys

local_dir = os.path.dirname(os.path.abspath(__file__))
if local_dir not in sys.path:
    sys.path.insert(0, local_dir)

from app.autoresearch.eval_worker import poll_system_commands

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("autoresearch_main")

if __name__ == "__main__":
    logger.info("Starting autoresearch backend...")
    try:
        asyncio.run(poll_system_commands())
    except KeyboardInterrupt:
        logger.info("Shutting down autoresearch worker...")
