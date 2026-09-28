""" 
Author: Dermascan Team
Date: 2026-12-03
License: MIT

Modal app for MedGemma inference.

Model loads once per container in `@enter`. Inference runs in `run`.
Use `modal run app.py` from the project root (where pyproject.toml lives).

Documentation for the inference parameter configurations is provided in:
dermascan/ai_service/docs/inference.md
"""

import base64
import json
import os
from io import BytesIO
from pathlib import Path
import subprocess
import time
import uuid
import logging

import modal
from PIL import Image
import requests
import httpx
from httpx import Response

from typing import List, Dict, Any, Optional

from dermascan.ai_service.modal.medgemma_service.model import load_medgemma_pipeline
from dermascan.ai_service.modal.medgemma_service.confidence_score import compute_confidence_score
from dermascan.ai_service.modal.model_config.loader import ModelConfig, load_config

from dermascan.shared import get_logger
from dermascan.shared.time_it import timeit


# Path to the local config file
_LOCAL_CONFIG_JSON: Path = (
    Path(__file__).resolve().parents[1] / "model_config" / "config.json"
)

# Path to the local gateway config file
_LOCAL_GATEWAY_CONFIG_JSON: Path = Path(__file__).resolve().parent / "gateways" / "config.json"

# Create Modal image with dependencies, attach config file
# Pin Python <3.14: vLLM -> numba does not support 3.14+ (Modal default image may use latest).
image: modal.Image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "torch",
    "transformers",
    "accelerate",
    "pillow",
    "requests",
    "httpx",
    "vllm",
    "fastapi",
    "python-multipart",
    "redis",
).add_local_python_source("dermascan").add_local_file(
    str(_LOCAL_CONFIG_JSON),
    remote_path="/root/dermascan/ai_service/modal/model_config/config.json",
).add_local_file(
    str(_LOCAL_GATEWAY_CONFIG_JSON),
    remote_path="/root/dermascan/ai_service/modal/medgemma_service/gateways/config.json",
)

# Create secret: `modal secret create huggingface HF_TOKEN=your_token`
hf_secret: modal.Secret = modal.Secret.from_name("huggingface")

# Create secret: `modal secret create redis REDIS_HOST=your_host REDIS_PORT=your_port REDIS_USERNAME=your_username REDIS_PASSWORD=your_password REDIS_DB=your_db`
redis_secret: modal.Secret = modal.Secret.from_name("redis")


# Modal app definition
app: modal.App = modal.App("medgemma-service")

# Logger initialization
log: logging.Logger = get_logger(__name__)

# MedGemma 4B on a single L4 can still exceed Modal/vLLM default 300s startup budgets during cold start.
_VLLM_STARTUP_TIMEOUT_S: float = float(os.environ.get("VLLM_STARTUP_TIMEOUT_S", "1200"))
_CLS_STARTUP_TIMEOUT_S: int = int(_VLLM_STARTUP_TIMEOUT_S) + 120
_CLS_TIMEOUT_S: int = int(_VLLM_STARTUP_TIMEOUT_S) + 300

# System prompt: dual-write legacy + cosmetic fields during pivot (schema_version cosmetic_v1)
_STRUCTURED_SYSTEM_PROMPT: str = (
    "You are a dual-role dermatology vision assistant:\n"
    "1) COSMETIC ROLE (user-facing): describe what is visible using appearance language "
    "in primary_observation — supportive, non-alarming, not a confirmed diagnosis for the user.\n"
    "2) DERMATOLOGICAL ROLE (internal): state the most likely clinical pattern in diagnosis "
    "for research, routing, and specialist review — this field is REQUIRED for every skin "
    "snapshot and is never shown verbatim to the user.\n"
    "You also read skincare/cosmetic product labels when present.\n\n"

    "Your primary task is to describe what is visible in the image using observed "
    "patterns, texture, pigmentation, redness, dryness, bumps, or product packaging cues.\n\n"

    "IMPORTANT OBSERVATION RULES:\n"
    "- Use observed visual evidence only.\n"
    "- primary_observation: cosmetic/appearance language ONLY (e.g. 'acne-prone appearance', "
    "'visible redness', 'dry/flaky patches'). Do NOT use clinical labels here.\n"
    "- diagnosis: REQUIRED for Skin review and Hybrid review. Always populate with the "
    "best-supported internal clinical pattern label (e.g. 'inflammatory acne', "
    "'allergic contact dermatitis', 'likely rosacea', 'post-inflammatory hyperpigmentation'). "
    "Use standard dermatology terms when the image supports them. Never leave diagnosis "
    "empty or omit the key for skin/hybrid reviews.\n"
    "- UNCERTAINTY RULE (narrow): use 'uncertain' ONLY when the skin appears clearly "
    "normal/healthy with no visible dermatological patterns (no redness, bumps, scaling, "
    "discoloration, inflammation, or other abnormality worth noting).\n"
    "- When ANY visible skin pattern is present, you MUST name the best-supported "
    "condition in diagnosis — do NOT refuse or default to 'uncertain'. Use 'likely' or "
    "'consistent with' when confidence is moderate; still output the leading pattern.\n"
    "- If two similar conditions fit the same visible pattern (e.g. eczema vs dermatitis), "
    "pick the most likely one — do NOT use 'uncertain' just because they overlap.\n"
    "- Never invent findings unsupported by the image.\n"
    "- For Product review only, leave diagnosis and primary_observation empty.\n"
    "- For General review, leave skin fields empty except review_type and schema_version.\n\n"

    "DUAL-WRITE TRANSITION (required):\n"
    "- Populate BOTH legacy fields (diagnosis, rationale, summary, treatment) AND cosmetic fields "
    "(primary_observation, observation_rationale, skin_state, cosmetic_implications).\n"
    "- diagnosis and primary_observation serve different roles; do NOT mirror or copy them.\n"
    "- Legacy rationale should mirror observation_rationale.\n"
    "- summary and skin_state must contain the same values.\n"
    "- treatment and cosmetic_implications must contain equivalent needs/contraindicated data; "
    "cosmetic_implications also includes avoid_for_now (cosmetic guidance strings).\n\n"

    "RISK AND IMAGE QUALITY:\n"
    "- risk_tier must be one of: cosmetic, watch, urgent.\n"
    "  - cosmetic: mild appearance findings; routine cosmetic guidance is enough.\n"
    "  - watch: noticeable persistent inflammatory or acne-prone appearance, spreading "
    "irritation, or concern that may benefit from optional professional review after user consent.\n"
    "  - urgent: strongly suggest seeking in-person care soon; populate diagnosis with the "
    "most likely serious pattern supported by vision; user-facing fields stay non-definitive.\n"
    "- When user/intake context is present in the prompt, use it for risk_tier:\n"
    "  - Moderate or severe inflammatory/acne-prone appearance AND no current treatment or "
    "medications -> usually watch (offer specialist after consent, not urgent).\n"
    "  - User already on prescribed acne/skin treatment -> usually cosmetic unless vision "
    "shows worsening or severe inflammation.\n"
    "  - Do not assign watch/urgent for mild, stable, or purely cosmetic texture/color goals.\n"
    "- image_quality must be one of: good, poor, unusable.\n\n"

    "The input may contain:\n"
    "- A skin image\n"
    "- A skincare/cosmetic product image\n"
    "- Both skin and product images\n"
    "- A general unrelated image\n\n"

    "Classify the image as exactly one of:\n"
    '- "Skin review"\n'
    '- "Product review"\n'
    '- "Hybrid review"\n'
    '- "General review"\n\n'

    "Return ONLY valid compact JSON.\n"
    "Do not return markdown.\n"
    "Do not return explanations outside JSON.\n"
    "Do not add extra keys.\n\n"

    "Return EXACTLY this JSON schema:\n"
    '{"schema_version":"cosmetic_v1","review_type":"","diagnosis":"","primary_observation":"",'
    '"rationale":"","observation_rationale":"",'
    '"summary":{"severity":"","active_inflammation":"","barrier_status":"","observed_patterns":[]},'
    '"skin_state":{"severity":"","active_inflammation":"","barrier_status":"","observed_patterns":[]},'
    '"treatment":{"needs":{"barrier_repair":"","anti_inflammatory":"","comedolytic":"","hydration":"","sun_protection":""},'
    '"contraindicated_approaches":[]},'
    '"cosmetic_implications":{"needs":{"barrier_repair":"","anti_inflammatory":"","comedolytic":"","hydration":"","sun_protection":""},'
    '"contraindicated_approaches":[],"avoid_for_now":[]},'
    '"risk_tier":"cosmetic","image_quality":"good",'
    '"product_name":"","description":"","indications":"","safety_in_pregnancy":"",'
    '"contraindications":"","how_to_use":"","special_instructions":""}\n\n'

    "FIELD RULES:\n"
    "- schema_version: always set to cosmetic_v1.\n"
    "- primary_observation (COSMETIC ROLE):\n"
    "  - Required for Skin review and Hybrid review.\n"
    "  - Describe the visible appearance when patterns are present; use 'uncertain' only "
    "for clearly normal skin with nothing to report.\n"
    "  - User-facing; non-clinical wording only.\n"
    "- diagnosis (DERMATOLOGICAL ROLE):\n"
    "  - REQUIRED for Skin review and Hybrid review — never empty, never omitted.\n"
    "  - When visible patterns exist: always output the best-supported internal clinical "
    "pattern (e.g. inflammatory acne, atopic dermatitis, melasma). Refusing to name a "
    "condition when patterns are visible is incorrect.\n"
    "  - Use 'uncertain' ONLY for clearly normal/healthy skin with no abnormality.\n"
    "  - Use dermatology terms when supported; prefix with 'likely' or 'consistent with' "
    "when confidence is moderate — still name the leading pattern.\n"
    "  - Empty string is INVALID for skin/hybrid reviews except Product-only payloads.\n\n"
    "- observation_rationale / rationale:\n"
    "  - 1-3 concise visual findings supporting the observation.\n"
    "  - rationale must mirror observation_rationale.\n\n"
    "- summary and skin_state:\n"
    "  - severity: mild, moderate, severe, unknown.\n"
    "  - active_inflammation: true, false, unknown.\n"
    "  - barrier_status: compromised, intact, unknown.\n"
    "  - observed_patterns: array of short appearance descriptors.\n\n"
    "- treatment and cosmetic_implications:\n"
    "  - needs values: true, false, unknown for each need key.\n"
    "  - contraindicated_approaches: cosmetic caution strings.\n"
    "  - avoid_for_now (cosmetic_implications only): short cosmetic avoid guidance.\n\n"
    "- For product reviews:\n"
    "  - Leave skin observation fields empty or uncertain defaults.\n"
    "  - Extract product_name and label fields when readable.\n"
    "  - Do not assess product safety; only read label content.\n\n"
    "- For skin reviews:\n"
    "  - Leave product fields empty.\n"
    "  - diagnosis and primary_observation are BOTH mandatory.\n\n"
    "- For hybrid reviews:\n"
    "  - Fill both skin observation and product label fields.\n"
    "  - diagnosis and primary_observation are BOTH mandatory.\n\n"
    "- For general unrelated images:\n"
    "  - Leave fields empty except review_type and schema_version.\n\n"

    "- Keep all values concise.\n"
)
# Create class for MedGemma service, runs on L4 GPU with specified image and secrets
@app.cls(
    gpu="L4",
    image=image,
    secrets=[hf_secret],
    max_containers=2,
    min_containers=0,
    scaledown_window=1200,
    timeout=_CLS_TIMEOUT_S,
    startup_timeout=_CLS_STARTUP_TIMEOUT_S,
    # Image already mounts dermascan via add_local_python_source (.py only).
    # Default include_source=True would auto-mount the whole package tree,
    # including frontend node_modules (~40k+ files) and block startup for hours.
    include_source=False,
)
@modal.concurrent(max_inputs=16)
class MedGemmaService:
    """
    MedGemma inference service. Model loads once in @enter.
    
    Attributes:
        boot_id: Unique identifier for this container instance
        cfg: Model configuration loaded from config.json
        _engine: Inference engine to use ("vllm" or "torch")
        _vllm_base_url: Base URL for vLLM server
        pipe: Inference pipeline (torch mode)
        _vllm_process: vLLM server process (when using vLLM)
        
    """

    @modal.enter()
    def load_model(self) -> None:
        """
        Load the MedGemma model once when the container starts.
        
        This method is called when the container is created and loads the model
        into memory for inference.
        
        Args:
            None
            
        Returns:
            None
        """
        # Generate unique boot ID for this container instance
        self.boot_id: str = uuid.uuid4().hex
        log.info("Container boot: boot_id=%s", self.boot_id)

        # Get Hugging Face token from environment
        token: str = os.environ.get("HF_TOKEN")
        if not token:
            raise ValueError("HF_TOKEN environment variable is required")
        
        # Load model configuration
        self.cfg: ModelConfig = load_config()

        # Determine inference engine
        self._engine: str = os.environ.get("MEDGEMMA_ENGINE", "vllm").strip().lower()
        self._vllm_base_url: str = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000")
        self._vllm_request_timeout_s: float = float(os.environ.get("VLLM_REQUEST_TIMEOUT_S", "120"))

        if self._engine == "vllm":
            # Start vLLM server
            self._start_vllm_server(hf_token=token)
        else:
            # Create inference pipeline
            self.pipe = load_medgemma_pipeline(config=self.cfg, hf_token=token)

    # Start vLLM server
    @timeit("vLLM server start")
    def _start_vllm_server(self, *, hf_token: str | None) -> None:
        """
        Start the vLLM server in a subprocess.
        
        Args:
            hf_token: Hugging Face token for model access
            
        Returns:
            None
        """
        # Build vLLM command
        model_id: str = self.cfg.model_id
        gpu_memory_utilization: float = self.cfg.gpu_memory_utilization
        max_model_len: int = self.cfg.max_model_len
        max_num_seqs: int = self.cfg.max_num_seqs
        max_num_batched_tokens: int = self.cfg.max_num_batched_tokens

        # Build command - vLLM server (single-GPU; tensor_parallel_size kept in config for future use)
        cmd: list[str] = [
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--model",
            model_id,
            "--gpu-memory-utilization",
            str(gpu_memory_utilization),
            "--max-model-len",
            str(max_model_len),
            "--max-num-seqs",
            str(max_num_seqs),
            "--max-num-batched-tokens",
            str(max_num_batched_tokens),
        ]

        log.info(
            "Starting vLLM subprocess: model_id=%s, gpu=L4, startup_timeout_s=%s",
            model_id,
            _VLLM_STARTUP_TIMEOUT_S,
        )
        log.info("vLLM command: %s", " ".join(cmd))

        # Set environment variables
        env = os.environ.copy()
        if hf_token:
            env["HF_TOKEN"] = hf_token
            env.setdefault("HUGGINGFACE_HUB_TOKEN", hf_token)

        # Inherit stdout/stderr so vLLM logs stream to Modal and the pipe cannot deadlock.
        self._vllm_proc: subprocess.Popen[str] = subprocess.Popen(
            cmd,
            env=env,
        )

        # Wait for vLLM to start
        deadline_s: float = _VLLM_STARTUP_TIMEOUT_S
        start: float = time.time()
        last_err: Exception | None = None
        next_progress_log_s: float = start + 30.0
        while time.time() - start < deadline_s:

            # Check if process has exited
            if self._vllm_proc.poll() is not None:
                raise RuntimeError(
                    "vLLM server exited during startup. "
                    "Check Modal container logs above for vLLM stderr/stdout."
                )

            if time.time() >= next_progress_log_s:
                elapsed_s: float = time.time() - start
                log.info(
                    "Still waiting for vLLM readiness: elapsed_s=%.0f, deadline_s=%.0f, pid=%s",
                    elapsed_s,
                    deadline_s,
                    self._vllm_proc.pid,
                )
                next_progress_log_s += 30.0

            try:
                # Check if vLLM server is ready
                r: Response = httpx.get(f"{self._vllm_base_url}/v1/models", timeout=2.0)
                if r.status_code == 200:
                    log.info("vLLM server ready: base_url=%s, model_id=%s", self._vllm_base_url, model_id)
                    return
            except Exception as e:
                last_err = e

            time.sleep(1.0)

        raise RuntimeError(
            f"Timed out waiting for vLLM server to start after {deadline_s:.0f}s: {last_err}. "
            "Check Modal container logs for vLLM download/load errors."
        )

    # Decode image bytes to PIL Image
    def _decode_image_bytes(self, image_bytes: bytes) -> Image.Image:
        """
        Decode image bytes to PIL Image.
        
        This method opens the image from bytes and converts it to RGB format.
        If image_size is configured, it resizes the image to the specified size.

        NB necessary during batching to ensure consistent image sizes.
        
        Args:
            image_bytes: Image bytes
            
        Returns:
            PIL Image
        """
        # Open image from bytes and convert to RGB
        img: Image.Image = Image.open(BytesIO(image_bytes)).convert("RGB")

        # Resize image if configured
        if self.cfg.image_size > 0:
            img = img.resize((self.cfg.image_size, self.cfg.image_size))
        return img

    def _encode_image_for_vllm(self, image_bytes: bytes) -> bytes:
        """
        Decode, resize, and JPEG-encode image bytes for vLLM data URLs.

        vLLM receives the same resized inputs as the transformers path so visual
        token count stays within ``max_model_len``.

        Args:
            image_bytes: Raw uploaded image bytes.

        Returns:
            bytes: JPEG-encoded image bytes.
        """
        img: Image.Image = self._decode_image_bytes(image_bytes)
        buf: BytesIO = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _truncate_vllm_user_prompt(self, prompt: str) -> str:
        """
        Trim user prompt text to the configured MedGemma audit context budget.

        Args:
            prompt: User prompt accompanying the image.

        Returns:
            str: Original or truncated prompt.
        """
        max_chars: int = max(
            200,
            int(os.environ.get("MEDGEMMA_AUDIT_CONTEXT_MAX_CHARS", "800")),
        )
        normalized: str = str(prompt or "").strip()
        if len(normalized) <= max_chars:
            return normalized

        suffix: str = " [truncated for MedGemma context budget]"
        keep: int = max(0, max_chars - len(suffix))
        return f"{normalized[:keep].rstrip()}{suffix}"

    @staticmethod
    def _raise_vllm_http_error(resp: Response) -> None:
        """
        Raise a descriptive error when vLLM returns a non-success HTTP status.

        Args:
            resp: HTTP response from the vLLM OpenAI-compatible API.

        Raises:
            RuntimeError: Always raised with status code and response body excerpt.
        """
        body: str = (resp.text or "")[:2000]
        raise RuntimeError(
            f"vLLM chat/completions failed: status={resp.status_code} body={body}"
        )

    # Format messages for MedGemma
    def _messages(self, *, img: Image.Image, prompt: str) -> list[dict]:
        """
        Format messages for MedGemma.
        
        This method formats the messages for MedGemma by adding the image and prompt.
        
        Args:
            img: PIL Image
            prompt: Prompt string
            
        Returns:
            List of messages
        """
        # Format messages for MedGemma
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

    # Format messages for vLLM
    def _vllm_messages(self, *, image_bytes: bytes, prompt: str) -> list[dict]:
        """
        Format messages for vLLM server.
        Takes raw image bytes and formats them as a data URL.
        Returns a list of messages in the format expected by vLLM.
        
        Args:
            image_bytes: Raw image bytes (PNG, JPEG, etc.)
            prompt: Text prompt to accompany the image
            
        Returns:
            List of messages in vLLM format
        """

        # Resize and JPEG-encode so vLLM visual token count matches transformers path.
        prepared_bytes: bytes = self._encode_image_for_vllm(image_bytes)
        truncated_prompt: str = self._truncate_vllm_user_prompt(prompt)

        # Encode image to base64 and create data URL
        b64: str = base64.b64encode(prepared_bytes).decode("ascii")
        data_url: str = f"data:image/jpeg;base64,{b64}"
        return [
            {"role": "system", "content": _STRUCTURED_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": truncated_prompt},
                ],
            },
        ]

    # Infer one image with MedGemma
    @timeit("MedGemma inference")
    def _infer_one(self, *, img: Image.Image, prompt: str) -> str:
        """
        Infer one image with MedGemma.
        
        This method infers one image with MedGemma by formatting the messages and calling 
        the pipe method.
        
        Args:
            img: PIL Image
            prompt: Prompt string
            
        Returns:
            Generated text
        """

        # Format messages for MedGemma
        messages: List[Dict[str, Any]] = self._messages(img=img, prompt=prompt)
        result: List[Dict[str, Any]] = self.pipe(
            messages,
            max_new_tokens=self.cfg.max_new_tokens,
            temperature=self.cfg.temperature,
        )
        return result[0]["generated_text"]

    # Infer one image with vLLM
    @timeit("MedGemma vLLM inference")
    def _infer_one_vllm(self, *, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        """
        Infer one image with vLLM.
        
        This method infers one image with vLLM by formatting the messages and calling the vLLM server.
        
        Args:
            image_bytes: Raw image bytes (PNG, JPEG, etc.)
            prompt: Prompt string
            
        Returns:
            Generated text
        """
        payload: Dict[str, Any] = {
            "model": self.cfg.model_id,
            "messages": self._vllm_messages(image_bytes=image_bytes, prompt=prompt),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_new_tokens,
            "response_format": {"type": "json_object"},
            "logprobs": True,
        }

        # Call vLLM server, pass the payload as JSON
        r: Response = httpx.post(
            f"{self._vllm_base_url}/v1/chat/completions",
            json=payload,
            timeout=self._vllm_request_timeout_s,
        )
        if r.status_code >= 400:
            log.error(
                "vLLM inference failed: status=%s body=%s",
                r.status_code,
                (r.text or "")[:2000],
            )
            self._raise_vllm_http_error(r)

        # Parse the response
        data: Dict[str, Any] = r.json()
        content: str = data["choices"][0]["message"]["content"]
        confidence: float = compute_confidence_score(data)
        return {"text": content, "confidence": confidence}

    # Run one inference
    @modal.method()
    def run_one(self, image_bytes: bytes, prompt: str) -> tuple[str, Dict[str, Any]]:
        """
        Run one inference request.
        
        This method runs one inference request by either using vLLM or the local MedGemma model.
        
        Args:
            image_bytes: Raw image bytes (PNG, JPEG, etc.)
            prompt: Prompt string
            
        Returns:
            Tuple of (boot_id, generated text)
        """
        log.info("Request received: boot_id=%s", self.boot_id)
        if getattr(self, "_engine", "transformers") == "vllm":
            result: Dict[str, Any] = self._infer_one_vllm(image_bytes=image_bytes, prompt=prompt)
        else:
            img: Image.Image = self._decode_image_bytes(image_bytes)
            text: str = self._infer_one(img=img, prompt=prompt)
            result = {"text": text, "confidence": None}
        return self.boot_id, result

    # Run batched inference requests
    @modal.method()
    def run_batch(self, requests: list[tuple[bytes, str]]) -> tuple[str, list[Dict[str, Any]]]:
        """
        Run batched inference requests.
        
        This method runs batched inference requests by either using vLLM or the local MedGemma model.
        
        Args:
            requests: List of tuples containing (image_bytes, prompt)
            
        Returns:
            Tuple of (boot_id, list of generated texts)
        """
        log.info("Batch request received: boot_id=%s, size=%s", self.boot_id, len(requests))
        outputs: List[Dict[str, Any]] = []

        # Use vLLM for batch inference
        if getattr(self, "_engine", "transformers") == "vllm":

            # Define async function to run all requests
            async def _run_all() -> list[Dict[str, Any]]:

                # Create HTTP client
                async with httpx.AsyncClient(timeout=self._vllm_request_timeout_s) as client:
                    tasks = []
                    for image_bytes, prompt in requests:
                        payload: Dict[str, Any] = {
                            "model": self.cfg.model_id,
                            "messages": self._vllm_messages(image_bytes=image_bytes, prompt=prompt),
                            "temperature": self.cfg.temperature,
                            "max_tokens": self.cfg.max_new_tokens,
                            "response_format": {"type": "json_object"},
                            "logprobs": True,
                        }
                        tasks.append(
                            client.post(f"{self._vllm_base_url}/v1/chat/completions", json=payload)
                        )
                    responses = await asyncio.gather(*tasks)
                    texts: list[Dict[str, Any]] = []
                    for resp in responses:
                        if resp.status_code >= 400:
                            log.error(
                                "vLLM batch inference failed: status=%s body=%s",
                                resp.status_code,
                                (resp.text or "")[:2000],
                            )
                            self._raise_vllm_http_error(resp)
                        data = resp.json()
                        content: str = data["choices"][0]["message"]["content"]
                        confidence: float = compute_confidence_score(data)
                        texts.append({"text": content, "confidence": confidence})
                    return texts

            import asyncio

            # Run all requests asynchronously
            outputs = asyncio.run(_run_all())

        else:
            # Use local MedGemma model
            for image_bytes, prompt in requests:
                img = self._decode_image_bytes(image_bytes)
                outputs.append({"text": self._infer_one(img=img, prompt=prompt), "confidence": None})
        return self.boot_id, outputs

    @modal.method()
    def run(
        self,
        image_source: str | bytes,
        prompt: str,
    ) -> str:
        """
        Run vision-language inference.

        Args:
            image_source: URL (str) or raw image bytes.
            prompt: Text prompt (e.g. "Describe this image.").

        Returns:
            Generated text.
        """
        if isinstance(image_source, str):
            resp: requests.Response = requests.get(image_source, stream=True)
            resp.raise_for_status()
            image_bytes = resp.content
        else:
            image_bytes = image_source

        img = self._decode_image_bytes(image_bytes)
        return self._infer_one(img=img, prompt=prompt)



