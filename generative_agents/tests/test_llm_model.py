from types import SimpleNamespace

from pydantic import BaseModel

from modules.model.llm_model import OpenAILLMModel


class _StructuredReply(BaseModel):
    res: str


class _CapturingCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[SimpleNamespace(
                        function=SimpleNamespace(arguments='{"res": "ok"}')
                    )],
                    content=None,
                )
            )]
        )


def test_v4_flash_disables_thinking_for_structured_tool_calls():
    completions = _CapturingCompletions()
    model = object.__new__(OpenAILLMModel)
    model._model = "deepseek-v4-flash"
    model._handle = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    assert model._completion("ping", _StructuredReply) == "ok"
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
