import logging

logger = logging.getLogger(__name__)

class CycleControl:
    @property
    def is_stopped(self) -> bool:
        try:
            from app.services.pipeline_service import PipelineService
            return PipelineService._stop_requested
        except ImportError:
            return False

    @property
    def is_paused(self) -> bool:
        return False

    async def wait_if_paused(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

cycle_control = CycleControl()
