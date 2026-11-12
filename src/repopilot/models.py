from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryHead:
    full_name: str
    default_branch: str
    sha: str
