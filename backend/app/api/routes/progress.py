from fastapi import APIRouter, HTTPException

from ...services.progress_jobs import progress_jobs

router = APIRouter(tags=["progress"])


@router.get("/projects/{project_id}/operations")
async def project_operations(project_id: str):
    return {"jobs": [job.model_dump(mode="json") for job in progress_jobs.list_jobs(project_id)]}


@router.get("/operations/{job_id}")
async def get_operation(job_id: str):
    job = progress_jobs.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Operation not found")
    return await progress_jobs.result(job)
