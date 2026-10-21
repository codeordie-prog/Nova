"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-05-18
License: MIT

Description: Pinecone index builder utilities.

"""


from __future__ import annotations

import os
import time
from dataclasses import dataclass
from logging import Logger

from dotenv import load_dotenv
from pinecone import Pinecone

from dermascan.shared.logger import get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger: Logger = get_logger(__name__)

PINECONE_API_KEY_QUTIS = str(os.environ.get("PINECONE_API_KEY_QUTIS", ""))


@dataclass(slots=True)
class IndexConfig:
    """
    Pinecone index configuration.
    """
    index_name: str = "qutis-index"
    api_key: str = PINECONE_API_KEY_QUTIS
    cloud: str = "aws"
    region: str = "us-east-1"
    dimension: int = 1536
    metric: str = "cosine"
    wait_timeout_s: int = 300
    poll_interval_s: float = 2.0


class IndexBuilder:
    """
    Create and validate Pinecone index for large-scale ingestion.

    """

    def __init__(self, config: IndexConfig) -> None:
        """
        Initialize the IndexBuilder.
        
        Args:
            config (IndexConfig): Configuration for the index.
        """
        self.config = config

    def _client(self) -> Pinecone:
        """
        initialize pinecone client
        
        Returns:
            Pinecone: Pinecone client
        """
        if not self.config.api_key:
            raise ValueError("PINECONE_API_KEY_QUTIS is required")
        return Pinecone(api_key=self.config.api_key)

    def ensure_index(self) -> None:
        """
        Create index if missing, wait until ready, validate key settings.
        
        Raises:
            ValueError: If PINECONE_API_KEY_QUTIS is not set
            TimeoutError: If index does not become ready within timeout
            ValueError: If index dimension or metric does not match configuration
        """
        pc: Pinecone = self._client()

        if not pc.has_index(self.config.index_name):
            logger.info("Creating index %s...", self.config.index_name)
            pc.create_index(
                name=self.config.index_name,
                dimension=self.config.dimension,
                metric=self.config.metric,
                spec={
                    "serverless": {
                        "cloud": self.config.cloud,
                        "region": self.config.region,
                    }
                },
            )
            logger.info("Index %s created.", self.config.index_name)
        else:
            logger.info("Index %s already exists.", self.config.index_name)

        self._wait_until_ready(pc)
        self._validate_index(pc)

    def _wait_until_ready(self, pc: Pinecone) -> None:
        """
        Wait until the index is ready.
        
        Args:
            pc (Pinecone): Pinecone client
            
        Raises:
            TimeoutError: If index does not become ready within timeout
        """
        deadline = time.time() + self.config.wait_timeout_s
        while time.time() < deadline:
            description = pc.describe_index(name=self.config.index_name)
            status = getattr(description, "status", None)
            is_ready = bool(getattr(status, "ready", False)) if status is not None else False
            if is_ready:
                logger.info("Index %s is ready.", self.config.index_name)
                return
            time.sleep(self.config.poll_interval_s)
        raise TimeoutError(f"Timed out waiting for index {self.config.index_name} to become ready")

    def _validate_index(self, pc: Pinecone) -> None:
        """
        Validate that the index has the correct configuration.

        Args:
            pc (Pinecone): Pinecone client

        Raises:
            ValueError: If index dimension or metric does not match configuration
        """
        description = pc.describe_index(name=self.config.index_name)
        index_dimension = int(getattr(description, "dimension", -1))
        index_metric = str(getattr(description, "metric", ""))

        if index_dimension != self.config.dimension:
            raise ValueError(
                f"Index {self.config.index_name} dimension mismatch: "
                f"expected {self.config.dimension}, got {index_dimension}"
            )
        if index_metric and index_metric != self.config.metric:
            raise ValueError(
                f"Index {self.config.index_name} metric mismatch: "
                f"expected {self.config.metric}, got {index_metric}"
            )

    def delete_index(self) -> None:
        """
        Delete the index if it exists.

        Use this to recreate an index with a different configuration (e.g., dimension).
        """
        pc: Pinecone = self._client()
        if pc.has_index(self.config.index_name):
            logger.warning("Deleting index %s...", self.config.index_name)
            pc.delete_index(name=self.config.index_name)
            logger.info("Index %s deleted.", self.config.index_name)
        else:
            logger.info("Index %s does not exist, nothing to delete.", self.config.index_name)

    def describe_index_usage(self) -> dict[str, Any]:
        """
        Get index usage statistics (vector count, storage size, etc.).

        Returns:
            dict[str, Any]: Index usage details.
        """
        from typing import Any

        pc: Pinecone = self._client()
        if not pc.has_index(self.config.index_name):
            return {"exists": False}

        description = pc.describe_index(name=self.config.index_name)
        return {
            "exists": True,
            "name": description.name,
            "dimension": getattr(description, "dimension", None),
            "metric": getattr(description, "metric", None),
            "status": getattr(getattr(description, "status", None), "ready", None),
        }


if __name__ == "__main__":
    IndexBuilder(IndexConfig()).ensure_index()

    # Get index info
    data = IndexBuilder(IndexConfig()).describe_index_usage()

    print(data)

    