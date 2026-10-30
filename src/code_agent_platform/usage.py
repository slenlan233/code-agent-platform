"""Provider token accounting: cache hits are a subset of input tokens."""


def provider_usage(messages):
    result = {"input_tokens": 0, "output_tokens": 0}
    reported, cache_known, cache_read = False, True, 0
    for message in messages:
        details = getattr(message, "usage_metadata", None) or {}
        if not details:
            continue
        reported |= "input_tokens" in details and "output_tokens" in details
        incoming = details.get("input_tokens", 0)
        result["input_tokens"] += incoming
        result["output_tokens"] += details.get("output_tokens", 0)
        cached = details.get("input_token_details", {}).get("cache_read")
        if isinstance(cached, int) and 0 <= cached <= incoming:
            cache_read += cached
        else:
            cache_known = False
    result["cache_read_tokens"] = cache_read if reported and cache_known else None
    result["uncached_input_tokens"] = result["input_tokens"] - cache_read if reported and cache_known else None
    return result, reported


def accumulate_usage(target, usage):
    previous_cache = target.get("cache_read_tokens", 0 if target["input_tokens"] == 0 else None)
    target["input_tokens"] += usage["input_tokens"]
    target["output_tokens"] += usage["output_tokens"]
    current_cache = usage["cache_read_tokens"]
    target["cache_read_tokens"] = previous_cache + current_cache if previous_cache is not None and current_cache is not None else None
    target["uncached_input_tokens"] = target["input_tokens"] - target["cache_read_tokens"] if target["cache_read_tokens"] is not None else None
