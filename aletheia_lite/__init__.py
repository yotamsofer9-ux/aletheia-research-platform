"""A small, bounded research pipeline for Project Aletheia."""

from .config import CaseConfig, SourceConfig, load_case_config
from .pipeline import CollectionPipeline, CollectionSummary

__all__ = [
    "CaseConfig",
    "SourceConfig",
    "load_case_config",
    "CollectionPipeline",
    "CollectionSummary",
]
