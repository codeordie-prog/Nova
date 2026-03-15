
"""
Author: Dermascan Team
Date: 2026-3-12
License: MIT

Web endpoints for MedGemma service.
"""

from __future__ import annotations
 

import anyio
import base64
import os
import time
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
import modal

from dermascan.ai_service.modal.medgemma_service.app import MedGemmaService, app, image
from dermascan.shared.logger import get_logger
from dermascan.ai_service.modal.medgemma_service.schemas import (
    DiagnoseBase64Request,
    DiagnoseBatchResponse,
    DiagnoseResponse,
)

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
    _get_client_ip,
    _run_inference_with_guards
)

# Create FastAPI app
web_app = FastAPI(title="DermaScan MedGemma API", version="0.1")

# Initialize logger
logger = get_logger(__name__)


# instantiate the service
service: MedGemmaService = MedGemmaService()


# Diagnose endpoint
@web_app.post("/diagnose", response_model=DiagnoseResponse)
async def diagnose(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
) -> dict:
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

        boot_id, text = await _run_inference_with_guards(
            lambda: service.run_one.remote(image_bytes=image_bytes, prompt=prompt)
        )
        return DiagnoseResponse(boot_id=boot_id, text=text)


    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")
    except anyio.TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")
        
    except Exception:
        logger.exception("Unhandled error in /diagnose")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        try:
            await image.close()
        except Exception:
            logger.debug("Failed to close upload")

# Diagnose batch endpoint
@web_app.post("/diagnose_batch", response_model=DiagnoseBatchResponse)
async def diagnose_batch(
    request: Request,
    images: list[UploadFile] = File(...),
    prompts: list[str] = Form(...),
) -> dict:
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

        requests: list[tuple[bytes, str]] = []
        for img, p in zip(images, prompts, strict=True):
            if img.content_type is not None and not img.content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail=f"Unsupported content type: {img.content_type}")

            img_bytes: bytes = await _read_upload_limited(img, max_bytes=MAX_IMAGE_BYTES)
            _validate_image_bytes(img_bytes)
            requests.append((img_bytes, p))

        boot_id, texts = await _run_inference_with_guards(lambda: service.run_batch.remote(requests))
        logger.info(f"Batch diagnosis completed with boot_id: {boot_id}")
        return DiagnoseBatchResponse(boot_id=boot_id, texts=texts)
        
    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")
    except anyio.TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")

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
@web_app.post("/diagnose_base64", response_model=DiagnoseResponse)
async def diagnose_base64(
    request: Request,
    payload: DiagnoseBase64Request = Body(...),
) -> DiagnoseResponse:
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

        boot_id, text = await _run_inference_with_guards(
            lambda: service.run_one.remote(image_bytes=image_bytes, prompt=payload.prompt)
        )
        logger.info(f"Diagnosis completed with boot_id: {boot_id}")
        return DiagnoseResponse(boot_id=boot_id, text=text)

    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")
    except anyio.TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")

    except Exception:
        logger.exception("Unhandled error in /diagnose_base64")
        raise HTTPException(status_code=500, detail="Internal server error")


# FastAPI app - Modal integration
@app.function(image=image) # Run on Modal
@modal.asgi_app() # Expose as ASGI app
def fastapi_app() -> FastAPI:
    logger.info("FastAPI app initialized")
    return web_app

