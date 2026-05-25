"""Configuration and constants."""

from dataclasses import dataclass, field

from uvrad.models import Location

BASEL = Location(lat=47.5596, lon=7.5886, alt_m=260.0, name="Basel, Switzerland")


@dataclass
class Config:
    default_location: Location = field(default_factory=lambda: BASEL)
    http_timeout: float = 12.0
    bfs_timeout: float = 15.0
    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    bfs_schauinsland_url: str = (
        "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe"
        "/_documents/schauinsland_node.html"
    )
    bfs_schauinsland_alt_m: float = 1284.0


DEFAULT = Config()
