"""
Author: Dermascan Team
Date: 2026-03-20
Description: Redis client for MedGemma service, including connection checking and client creation from environment variables.
"""

from dataclasses import dataclass

from redis import Redis
import os

@dataclass(frozen=True)
class RedisConfig:
    """
    Redis configuration dataclass
    """
    host: str
    port: int
    username: str
    password: str
    db: int
    decode_responses: bool


class Client:
    """
    Redis client for MedGemma service
    
    Attributes:
        config: Redis configuration
        client: Redis client instance
    """
    def __init__(self, config: RedisConfig):

        # Add validation for required fields
        if not config.host or not config.port:
            raise ValueError("Redis host and port are required")

        # Initialize Redis client
        self.config = config
        self.client = Redis(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            db=config.db,
            decode_responses=config.decode_responses
        )

    def get_redis(self) -> Redis:
        """
        Get the Redis client instance
        
        Returns:
            Redis client instance
        """
        return self.client


def redis_client_from_env() -> Client:
    """
    Create a Redis client from environment variables.
    
    Returns:
        Redis client instance
    """
    return Client(
        config=RedisConfig(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "12996")),
            username=os.environ.get("REDIS_USERNAME", "default"),
            password=os.environ.get("REDIS_PASSWORD", ""),
            db=int(os.environ.get("REDIS_DB", "0")),
            decode_responses=True,
        )
    )


def check_redis_connection() -> None:
    """
    Check Redis connection.
    """
    redis_client_from_env().get_redis().ping()


if __name__ == "__main__":
    client = redis_client_from_env().get_redis()
    print(client.ping())
    print("Redis connection successful!")
    key = "medgemma:job:952817780a5946438ab91baab0f21e34"
    print(client.type(key))
    print(client.hgetall(key))
    print(client.xrange("medgemma:jobs"))