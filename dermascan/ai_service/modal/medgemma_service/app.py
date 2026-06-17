""" 
Author: Dermascan Team
Date: 2026-12-03
License: MIT

Modal app for MedGemma inference.

Model loads once per container in `@enter`. Inference runs in `run`.
Use `modal run app.py` from the project root (where pyproject.toml lives).

Documentation for the inference parameter configurations is provided in:
`dermascan\ai_services\docs\inference.md`
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
app: modal.App = modal.App("dermascan-medgemma")

# Logger initialization
log: logging.Logger = get_logger(__name__)

# System prompt to force structured JSON output for diagnosis/product review responses
_STRUCTURED_SYSTEM_PROMPT: str = (
    "You are an expert dermatology AI assistant specialized in:\n"
    "- Skin condition pattern recognition\n"
    "- Dermatological primary concern identification\n"
    "- Cosmetic and skincare product reviews\n\n"

    "Your primary task is to identify the MOST LIKELY skin condition visible in an image "
    "based on observed dermatological patterns, morphology, lesion appearance, distribution, "
    "texture, pigmentation, inflammation, scaling, pustules, nodules, redness, or other visible features.\n\n"

    "IMPORTANT DIAGNOSIS RULES:\n"
    "- For ANY skin-related image, ALWAYS provide a best-effort diagnosis.\n"
    "- NEVER leave diagnosis empty for visible skin conditions.\n"
    "- Even if uncertain, provide one most likely primary diagnosis.\n"
    "- Use probabilistic medical reasoning based on visual evidence.\n"
    "- Only leave diagnosis empty if the image is clearly NOT skin-related.\n"
    "- Do NOT say you cannot diagnose.\n"
    "- Do NOT refuse unless the image is unrelated to skin or impossible to interpret.\n\n"

    "The input may contain:\n"
    "- A skin condition image\n"
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
    '{"review_type":"","diagnosis":"","rationale":"","summary":{"severity":"","active_inflammation":"","barrier_status":"","observed_patterns":[]},"treatment":{"needs":{"barrier_repair":"","anti_inflammatory":"","comedolytic":"","hydration":"","sun_protection":""},"contraindicated_approaches":[]},"product_name":"","description":"","indications":"","safety_in_pregnancy":"","contraindications":"","how_to_use":"","special_instructions":""}\n\n'

    "FIELD RULES:\n"
    "- diagnosis:\n"
    "  - Required for all skin-related images.\n"
    "  - Must contain exactly one primary concern diagnosis only.\n"
    "  - Use concise medical terminology.\n"
    "  - Do not include differential diagnoses or comma-separated alternatives.\n\n"

    "- rationale:\n"
    "  - Include 1-3 concise bullet-style findings.\n"
    "  - Mention observed visual patterns supporting diagnosis.\n\n"

    "- summary:\n"
    "  - For skin/hybrid reviews, summary MUST be a JSON object with keys:\n"
    "    severity, active_inflammation, barrier_status, observed_patterns.\n"
    "  - severity must be one of: mild, moderate, severe, unknown.\n"
    "  - active_inflammation must be one of: true, false, unknown.\n"
    "  - barrier_status must be one of: compromised, intact, unknown.\n"
    "  - observed_patterns must be an array of short strings (or empty array).\n\n"

    "- treatment:\n"
    "  - For skin/hybrid reviews, treatment MUST be a JSON object with keys:\n"
    "    needs, contraindicated_approaches.\n"
    "  - needs MUST be an object with boolean-like string values (true/false/unknown) for:\n"
    "    barrier_repair, anti_inflammatory, comedolytic, hydration, sun_protection.\n"
    "  - contraindicated_approaches must be an array of short strings (or empty array).\n"
    "  - Do not write free-text treatment prose in this field.\n\n"

    "- For product reviews:\n"
    "  - Leave diagnosis and rationale empty.\n"
    "  - Keep summary as an empty object with the required keys.\n"
    "  - Keep treatment as an empty object with the required keys.\n\n"

    "- For skin reviews:\n"
    "  - Leave all product-related fields empty.\n"
    "  - diagnosis, summary, and treatment must follow the format rules above.\n\n"

    "- For hybrid reviews:\n"
    "  - Fill BOTH skin and product fields.\n"
    "  - diagnosis, summary, and treatment must follow the format rules above.\n\n"

    "- For general unrelated images:\n"
    "  - Leave all fields empty except review_type.\n\n"

    "- If a specific field truly cannot be determined, return an empty string.\n"
    "- Keep all values concise.\n"
)
# Create class for MedGemma service, runs on A10G/L4 GPU with specified image and secrets
@app.cls(
    gpu="A10",
    image=image,
    secrets=[hf_secret],
    max_containers=2,
    min_containers=2,
    scaledown_window=600,
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

        # Build command - vLLM server
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

        # Set environment variables
        env = os.environ.copy()
        if hf_token:
            env["HF_TOKEN"] = hf_token

        # Start vLLM process, capture output, and set environment variables
        self._vllm_proc: subprocess.Popen[str] = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait for vLLM to start
        deadline_s: float = float(os.environ.get("VLLM_STARTUP_TIMEOUT_S", "300"))
        start: float = time.time()
        last_err: Exception | None = None
        while time.time() - start < deadline_s:

            # Check if process has exited
            if self._vllm_proc.poll() is not None:
                out: str = ""
                try:
                    # Read last 5000 characters of output
                    if self._vllm_proc.stdout is not None:
                        out = self._vllm_proc.stdout.read()[-5000:]
                except Exception:
                    pass
                raise RuntimeError(f"vLLM server exited during startup. Output:\n{out}")

            try:
                # Check if vLLM server is ready
                r: Response = httpx.get(f"{self._vllm_base_url}/v1/models", timeout=2.0)
                if r.status_code == 200:
                    log.info("vLLM server ready: base_url=%s, model_id=%s", self._vllm_base_url, model_id)
                    return
            except Exception as e:
                last_err: Exception = e

            time.sleep(1.0)

        raise RuntimeError(f"Timed out waiting for vLLM server to start: {last_err}")

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

        # Encode image to base64 and create data URL
        b64: str = base64.b64encode(image_bytes).decode("ascii")
        data_url: str = f"data:image/jpeg;base64,{b64}"
        return [
            {"role": "system", "content": _STRUCTURED_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
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
        r.raise_for_status()

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
                        resp.raise_for_status()
                        data = resp.json()
                        print(data)
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



