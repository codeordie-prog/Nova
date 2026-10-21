"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-04-21
Description: This Module implements the loader method for loading agents configurations

"""


import os
import json
from typing import Type, List, Dict, Any, Optional


def load_agent_configurations(agent_type: str, path: str, provider: str = "google") -> Dict[str, Any]:
    """
    Loads the cofiguration for the specified agent from the specified path.
    
    Args:
        agent_type (str): The type of agent to load.
        path (str): The path to the configuration file.
        provider (str): The provider to use must be one of google | openai.
    
    Returns:
        Dict[str, Any]: The configuration for the specified agent.
    """
    if not os.path.exists(path):
        raise FileNotFoundError("Configuration File not found on the specified Path")

    with open(path, 'r') as f:
        cfg: Dict[str, List[Dict[str, Any]]] = json.loads(f.read())

    if agent_type.strip().lower() == "orchestrator":
        return (cfg['orchestrator_agents'][0] if 
        provider.strip().lower() == "google" else 
        cfg['orchestrator_agents'][1])

    if agent_type.strip().lower() == "diagnose":
        return (cfg['diagnose_agents'][0] if 
        provider.strip().lower() == "google" else 
        cfg['diagnose_agents'][1])

    if agent_type.strip().lower() == "knowledge":
        return (cfg['knowledge_agents'][0] if 
        provider.strip().lower() == "google" else 
        cfg['knowledge_agents'][1])

    if agent_type.strip().lower() == "escalation":
        return (cfg['escalation_agents'][0] if 
        provider.strip().lower() == "google" else 
        cfg['escalation_agents'][1])

