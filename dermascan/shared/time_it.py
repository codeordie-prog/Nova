"""
Author: Dermascan team
Date: 2026-03-12
License: MIT

A decorator that times the execution of a function.
The function is reusable across different functions.

"""

import asyncio
import time
import logging
from typing import Any, Callable

from dermascan.shared.logger import get_logger

# Create logger
logger: logging.Logger = get_logger(__name__)

# Decorator
def timeit(label: str):
    """
    A decorator that times the execution of a function.
    
    Args:
        label (str): The label to use for the timing message.
    
    Returns:
        Callable: The decorated function.
    """

    def decorator(func: Callable) -> Callable:
        """
        A decorator that times the execution of a function.
        
        Args:
            func (Callable): The function to decorate.
        
        Returns:
            Callable: The decorated function.
        """

        async def async_wrapper(*args, **kwargs):
            """
            A wrapper for async functions.
            
            Args:
                *args: The arguments to pass to the function.
                **kwargs: The keyword arguments to pass to the function.
            
            Returns:
                Any: The result of the function.
            """
            # implementation of async timing
            start: float = asyncio.get_event_loop().time()
            result: Any = await func(*args, **kwargs)
            stop: float = asyncio.get_event_loop().time()
            logger.info(f"{label} took {stop - start} seconds")
            return result

        def sync_wrapper(*args, **kwargs):
            """
            A wrapper for sync functions.
            
            Args:
                *args: The arguments to pass to the function.
                **kwargs: The keyword arguments to pass to the function.
            
            Returns:
                Any: The result of the function.
            """
            # implementation of sync timing
            start: float = time.time()
            result: Any = func(*args, **kwargs)
            stop: float = time.time()
            logger.info(f"{label} took {stop - start} seconds")
            return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator



    
    
