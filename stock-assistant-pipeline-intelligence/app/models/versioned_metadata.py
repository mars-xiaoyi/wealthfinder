from dataclasses import dataclass


@dataclass
class VersionedMetadata[T]:
    """A computation's output together with the version of the logic that produced it."""

    version: str
    output: T
