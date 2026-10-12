"""Keep import/update requests and their outcomes across backend restarts."""

import logging

from ..config import settings
from ..models.torrent import IndexationJob

logger = logging.getLogger("uvicorn.error")


class IndexationJobStore:
    @staticmethod
    def save(job: IndexationJob) -> None:
        root = settings.data_dir / "indexation_jobs"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{job.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(job.model_dump_json(), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def load_all() -> list[IndexationJob]:
        jobs = []
        for path in (settings.data_dir / "indexation_jobs").glob("*.json"):
            try:
                job = IndexationJob.model_validate_json(path.read_text(encoding="utf-8"))
                if job.id == path.stem and job.job_type in {"index", "update"}:
                    jobs.append(job)
            except (OSError, ValueError):
                logger.exception("Could not restore indexation job %s", path)
        return sorted(jobs, key=lambda job: job.created_at)

    @staticmethod
    def delete(job_id: str) -> None:
        (settings.data_dir / "indexation_jobs" / f"{job_id}.json").unlink(missing_ok=True)
