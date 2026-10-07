from dataclasses import dataclass


@dataclass
class Page:
    number: int  # 1-based page or slide number
    text: str
