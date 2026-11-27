from __future__ import annotations

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.helpers.llm import LLM_API_ASSIST, IntentTool, LLMContext, Tool

from .const import DOMAIN
from .personal import PersonalHandler

PROMPT = (
    "When the user says my, mine or me about their calendar, phone, reminders or list, "
    "use the EchoLocal tools: they find out from the device who is speaking."
)


@callback
def async_get_tools(hass: HomeAssistant, llm_context: LLMContext, api_id: str) -> LLMTools | None:
    if api_id != LLM_API_ASSIST:
        return None
    handlers = [h for h in intent.async_get(hass) if isinstance(h, PersonalHandler)]
    if not handlers:
        return None
    tools: list[Tool] = [
        IntentTool(f"{DOMAIN}__{h.intent_type}", h, integration=DOMAIN) for h in handlers
    ]
    return LLMTools(tools=tools, prompt=PROMPT)
