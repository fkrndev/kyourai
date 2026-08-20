"""Context engine — compression and token management for conversations.

Exports:
  - compress_if_needed: async compression of message history
  - estimate_tokens: rough token count for a string
  - should_compress: check if compression is needed
"""

from kyourai.context.compressor import (
    compress_if_needed,
    compress_messages,
    estimate_message_tokens,
    estimate_tokens,
    should_compress,
    split_messages,
)

__all__ = [
    "compress_if_needed",
    "compress_messages",
    "estimate_message_tokens",
    "estimate_tokens",
    "should_compress",
    "split_messages",
]
