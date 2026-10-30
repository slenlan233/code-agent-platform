"""Preserve DeepSeek thinking fields across tool turns with the pinned adapter."""
from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import AIMessage
from .cancellation import CancellableModelMixin


class DeepSeekModel(CancellableModelMixin, ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        # langchain-deepseek 1.1.1 reads this extension, but its current parent
        # serializer drops it. DeepSeek thinking+tools requires it on replay.
        for source, outgoing in zip(messages, payload["messages"], strict=True):
            if isinstance(source, AIMessage) and "reasoning_content" in source.additional_kwargs:
                outgoing["reasoning_content"] = source.additional_kwargs["reasoning_content"] or ""
        thinking = (payload.get("extra_body") or {}).get("thinking", {}).get("type", "enabled")
        choice = payload.get("tool_choice")
        # DeepSeek rejects forced function selection in thinking mode. The
        # structured-output instruction names the final schema tool instead.
        if thinking == "enabled" and (isinstance(choice, dict) or choice in {"any", "required"}):
            payload["tool_choice"] = "auto"
        return payload
