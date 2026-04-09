
"""
Author: Dermascan Team
Date: 2026-3-12
License: MIT

Fast API Web endpoints for MedGemma service.
These endpoints are used to interact with the MedGemma service.
Through a distributed asynchronous processing system.

The async POST web endpoints receive requests and enqueue them to a Redis stream.
GET endpoints are used to check the status of the jobs.

The async workers process the jobs from the stream.
"""

from __future__ import annotations
 

import anyio
import base64
import os
import time
import uuid
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
import redis

from dermascan.shared.logger import get_logger
from dermascan.ai_service.modal.medgemma_service.schemas import (
    BatchEnqueueResponse,
    DiagnoseBase64Request,
    EnqueueResponse,
    JobStatusResponse,
)

from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env

from dermascan.ai_service.modal.medgemma_service.gateways.custom_exceptions import (
    ImageValidationError,
    EmptyImageError,
    ImageTooLargeError,
)
from dermascan.ai_service.modal.medgemma_service.gateways.validation import (
    MAX_IMAGE_BYTES,
    _read_upload_limited,
    _validate_image_bytes,
)

from dermascan.ai_service.modal.medgemma_service.gateways.rate_limiter import (
    _enforce_rate_limit,
)

# Create FastAPI app
web_app = FastAPI(title="DermaScan MedGemma API", version="0.1")

# Initialize logger
logger = get_logger(__name__)

# get redis client
_redis: redis.Redis = redis_client_from_env().get_redis()

# job stream configuration - used for async job processing
_JOB_STREAM: str = os.environ.get("MEDGEMMA_JOB_STREAM", "medgemma:jobs")
_JOB_TTL_S: int = int(os.environ.get("MEDGEMMA_JOB_TTL_S", "86400"))


def _enqueue_job(image_bytes: bytes, prompt: str) -> str:
    """
    Enqueue a job to the Redis stream.
    
    Args:
        image_bytes: The image bytes to enqueue
        prompt: The prompt to use for the diagnosis
        
    Returns:
        str: The job ID
    """
    job_id: str = uuid.uuid4().hex
    image_b64: str = base64.b64encode(image_bytes).decode("ascii")
    job_key: str = f"medgemma:job:{job_id}"
    now_s: int = int(time.time())

    # set job metadata
    _redis.hset(
        job_key,
        mapping={
            "job_id": job_id,
            "status": "queued",
            "created_at": str(now_s),
        },
    )

    # set job ttl
    _redis.expire(job_key, _JOB_TTL_S)
    
    # add job to stream
    _redis.xadd(
        _JOB_STREAM,
        {
            "job_id": job_id,
            "prompt": prompt,
            "image_base64": image_b64,
            "created_at": str(now_s),
        },
    )
    return job_id


def _enqueue_jobs_batch(jobs: list[tuple[bytes, str]]) -> list[str]:
    """
    Enqueue multiple jobs to the Redis stream.
    
    Args:
        jobs: List of tuples containing (image_bytes, prompt)
        
    Returns:
        list[str]: List of job IDs
    """
    now_s: str = str(int(time.time()))
    pipe = _redis.pipeline(transaction=False)
    job_ids: list[str] = []

    # add each job to the pipeline
    for image_bytes, prompt in jobs:
        job_id: str = uuid.uuid4().hex
        job_ids.append(job_id)
        image_b64: str = base64.b64encode(image_bytes).decode("ascii")
        job_key: str = f"medgemma:job:{job_id}"

        # set job metadata, 
        pipe.hset(
            job_key,
            mapping={
                "job_id": job_id,
                "status": "queued",
                "created_at": now_s,
            },
        )

        # set job ttl
        pipe.expire(job_key, _JOB_TTL_S)
        
        # add job to stream
        pipe.xadd(
            _JOB_STREAM,
            {
                "job_id": job_id,
                "prompt": prompt,
                "image_base64": image_b64,
                "created_at": now_s,
            },
        )

    pipe.execute()
    return job_ids


def _get_job_data(job_id: str) -> dict[str, str]:
    """
    Get job data from Redis.
    
    Args:
        job_id: The job ID
        
    Returns:
        dict[str, str]: The job data
    """
    return _redis.hgetall(f"medgemma:job:{job_id}")


# Diagnose endpoint
@web_app.post("/diagnose", response_model=EnqueueResponse)
async def diagnose(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
) -> EnqueueResponse:
    """
    Diagnose an image using MedGemma.
    
    Args:
        image: The image to diagnose
        prompt: The prompt to use for the diagnosis
        
    Returns:
        dict: The diagnosis result
    """
    logger.info("Diagnose endpoint called")

    try:
        await _enforce_rate_limit(request=request)

        if not prompt.strip():
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        if image.content_type is not None and not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Unsupported content type: {image.content_type}")

        # Read and validate image
        image_bytes: bytes = await _read_upload_limited(image, max_bytes=MAX_IMAGE_BYTES)
        _validate_image_bytes(image_bytes)

        # Enqueue job
        job_id: str = await anyio.to_thread.run_sync(_enqueue_job, image_bytes, prompt)

        return EnqueueResponse(job_id=job_id, status="queued")


    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except redis.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    except Exception:
        logger.exception("Unhandled error in /diagnose")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        try:
            await image.close()
        except Exception:
            logger.debug("Failed to close upload")


@web_app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    data: dict[str, str] = await anyio.to_thread.run_sync(_get_job_data, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=job_id,
        status=data.get("status", "unknown"),
        boot_id=data.get("boot_id"),
        text=data.get("text"),
        error=data.get("error"),
    )

# Diagnose batch endpoint
@web_app.post("/diagnose_batch", response_model=BatchEnqueueResponse)
async def diagnose_batch(
    request: Request,
    images: list[UploadFile] = File(...),
    prompts: list[str] = Form(...),
) -> BatchEnqueueResponse:
    """
    Diagnose multiple images using MedGemma.
    
    Args:
        images: The images to diagnose
        prompts: The prompts to use for the diagnosis
        
    Returns:
        dict: The diagnosis results
    """
    logger.info("Diagnose batch endpoint called")

    try:
        await _enforce_rate_limit(request=request)

        if len(images) != len(prompts):
            raise HTTPException(
                status_code=400,
                detail=f"images and prompts must have the same length: {len(images)} != {len(prompts)}",
            )

        if any(not p.strip() for p in prompts):
            raise HTTPException(status_code=400, detail="All prompts must be non-empty")

        enqueue_jobs: list[tuple[bytes, str]] = []
        for img, p in zip(images, prompts, strict=True):
            if img.content_type is not None and not img.content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail=f"Unsupported content type: {img.content_type}")

            img_bytes: bytes = await _read_upload_limited(img, max_bytes=MAX_IMAGE_BYTES)
            _validate_image_bytes(img_bytes)
            enqueue_jobs.append((img_bytes, p))

        job_ids: list[str] = await anyio.to_thread.run_sync(_enqueue_jobs_batch, enqueue_jobs)

        return BatchEnqueueResponse(job_ids=job_ids, status="queued")
        
    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except redis.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    except Exception:
        logger.exception("Unhandled error in /diagnose_batch")
        raise HTTPException(status_code=500, detail="Internal server error")

    finally:
        for img in images:
            try:
                await img.close()
            except Exception:
                logger.debug("Failed to close upload")


# Diagnose base64 endpoint
@web_app.post("/diagnose_base64", response_model=EnqueueResponse)
async def diagnose_base64(
    request: Request,
    payload: DiagnoseBase64Request = Body(...),
) -> EnqueueResponse:
    """
    Diagnose an image using MedGemma.
    
    Args:
        image_base64: The base64 encoded image
        prompt: The prompt to use for the diagnosis
        
    Returns:
        dict: The diagnosis result
    """
    logger.info("diagnose_base64 endpoint called")

    try:
        await _enforce_rate_limit(request=request)

        image_bytes: bytes = base64.b64decode(payload.image_base64, validate=True)
    except Exception as e:
        logger.error(f"Invalid base64: {e}")
        raise HTTPException(status_code=400, detail="Invalid base64")

    try:
        if not payload.prompt.strip():
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        _validate_image_bytes(image_bytes)

        job_id: str = await anyio.to_thread.run_sync(_enqueue_job, image_bytes, payload.prompt)
        return EnqueueResponse(job_id=job_id, status="queued")

    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except redis.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    except Exception:
        logger.exception("Unhandled error in /diagnose_base64")
        raise HTTPException(status_code=500, detail="Internal server error")



