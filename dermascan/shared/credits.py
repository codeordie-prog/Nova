"""
Author: Dermascan Team
Date: 2026-08-16
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: This module contains implementation of the credit deduction contract that will be applied across the following credits deduction entry points:

1. Orchestrator API - Input and Output
2. Tool calls - Input and Output
3. Medgemma API - Input and output

4. Escalations - will be counted separately.


PSEUDOCODE:

n_cost_per_token = WILL BE MAPPED WITH RESPECT TO PROVIDER'S RATES

FUNCTION charge_input_tokens(token_count: int, n_cost_per_token: float) -> None:
    # Calculate the total cost
    total_cost = token_count * n_cost_per_token
    Return total_cost


FUNCTION charge_output_tokens(token_count: int, n_cost_per_token: float) -> None:
    # Calculate the total cost
    total_cost = token_count * n_cost_per_token
    Return total_cost


FUNCTION at_model_start(model_name: str, input: RequestInput) -> None:
    # Count the tokens
    token_count = count_tokens(input)
    Return token_count

FUNCTION at_model_end(model_name: str, output: RequestOutput) -> None:
    # Count the tokens
    token_count = count_tokens(output)
    Return token_count

FUNCTION at_tool_start(tool_name: str, input: ToolInput) -> None:
    # Count the tokens
    token_count = count_tokens(input)
    Return token_count


A redis billing key for a given account_id would be atomically deducted at each point of incured cost."""


from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any

from redis import Redis


class RequestInput(BaseModel):
    pass

class RequestOutput(BaseModel):
    pass

class ToolInput(BaseModel):
    pass

class ToolOutput(BaseModel):
    pass


class CreditDeductionContract(ABC):

    @abstractmethod
    def charge_input_tokens(self, token_count: int, n_cost_per_token: float) -> None:
        pass


    @abstractmethod
    def charge_output_tokens(self, token_count: int, n_cost_per_token: float) -> None:
        pass
    


class CountTokens(ABC):

    @abstractmethod
    def count_tokens(self, input: RequestInput) -> int:
        pass

    
    @abstractmethod
    def at_model_start(self, model_name: str, input: RequestInput) -> None:
        pass

    @abstractmethod
    def at_model_end(self, model_name: str, output: RequestOutput) -> None:
        pass

    @abstractmethod
    def at_tool_call_start(self, tool_name: str, input: ToolInput) -> None:
        pass

    @abstractmethod
    def at_tool_call_end(self, tool_name: str, output: ToolOutput) -> None:
        pass


class RedisDeductionSystem(CreditDeductionContract):

    def __init__(self, redis_client: Redis, account_id: str):
        self.redis_client = redis_client
        self.account_id = account_id

    @property
    def account_id_key(self) -> str:
        return f"accounts:{self.account_id}"

    def _charge(self, token_count: int, n_cost_per_token: float) -> None:

        total_cost = token_count * n_cost_per_token

        # Redis atomic deduction goes 

        return 
        
    def charge_input_tokens(self, token_count: int, n_cost_per_token: float) -> None:
        pass

    def charge_output_tokens(self, token_count: int, n_cost_per_token: float) -> None:
        pass
