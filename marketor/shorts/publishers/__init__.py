"""
Native publishers, one module per platform.

No third-party posting service: each platform is talked to directly, so the
running cost is whatever the platform itself charges. That means the approval
burden lands on you instead — TikTok needs an app audit and Instagram needs Meta
app review before either will post publicly. YouTube needs neither, which is why
it is the one to start with.
"""

from .base import PostCopy, Publisher, PublishError, PublishResult

__all__ = ["PostCopy", "Publisher", "PublishError", "PublishResult", "get_publisher"]


def get_publisher(platform: str):
    """Imports a publisher lazily, so one missing SDK can't break the others."""
    if platform == "youtube":
        from .youtube import YouTubePublisher
        return YouTubePublisher()
    if platform == "tiktok":
        from .tiktok import TikTokPublisher
        return TikTokPublisher()
    if platform == "instagram":
        from .instagram import InstagramPublisher
        return InstagramPublisher()
    raise PublishError(f"unknown platform {platform!r}")
