from __future__ import annotations
import os

from dermascan.ai_service.modal.inference_pipeline import (
    InferenceRequest,
    ModalMedGemmaProvider,
)


def main() -> None:
    # Initialize the provider
    provider = ModalMedGemmaProvider()
    
    # Get image bytes
    with open("sample.jpg", "rb") as f:
        img_bytes = f.read()

    prompt = "Describe the image and the likely diagnosis"

    req = InferenceRequest(image_bytes=img_bytes, prompt=prompt)

    try:
        response = provider.generate_one(request=req)
        print(response.generated_text)
        return
    except Exception as e:
        try:
            import modal

            if not isinstance(e, modal.exception.ExecutionError):
                raise
        except Exception:
            raise

    from io import BytesIO

    from PIL import Image

    from dermascan.ai_service.modal.medgemma_service.model import load_medgemma_pipeline
    from dermascan.ai_service.modal.model_config.loader import load_config

    cfg = load_config()
    img = Image.open(BytesIO(img_bytes)).convert("RGB")
    if cfg.image_size > 0:
        img = img.resize((cfg.image_size, cfg.image_size))

    token = os.environ.get("HF_TOKEN")
    pipe = load_medgemma_pipeline(config=cfg, hf_token=token)
    result = pipe(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_new_tokens=cfg.max_new_tokens,
        temperature=cfg.temperature,
    )
    print(result[0]["generated_text"])


if __name__ == "__main__":
    main()
