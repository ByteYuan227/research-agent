"""A deep-research agent built on the Claude API."""

from .agent import AgentConfig, Hooks, ResearchAgent, ResearchResult
from .tools import Notebook

__all__ = ["AgentConfig", "Hooks", "Notebook", "ResearchAgent", "ResearchResult"]
__version__ = "0.1.0"
