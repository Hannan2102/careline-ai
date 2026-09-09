# Provider abstraction

Three interfaces in `backend/app/ai/providers/base.py`. Business logic imports the
interface; only the factory imports an implementation.

```python
class STTProvider(Protocol):
    name: str
    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]: ...

class LLMProvider(Protocol):
    name: str
    async def generate(self, req: LLMRequest) -> LLMResponse: ...
    async def tool_call(self, req: ToolCallRequest) -> ToolCallResponse: ...

class TTSProvider(Protocol):
    name: str
    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]: ...
```

## Implementations

| Kind | Cloud | Local (Phase 17) | Deterministic |
|---|---|---|---|
| STT | `DeepgramSTTProvider` | `WhisperSTTProvider` | `MockSTTProvider` |
| LLM | `OpenAILLMProvider` | `OllamaLLMProvider` | `MockLLMProvider` |
| TTS | `ElevenLabsTTSProvider` | `PiperTTSProvider` | `MockTTSProvider` |

Mock providers are first-class, not test doubles bolted on afterwards: they are what the
test suite and default development mode use, so the interfaces stay honest.

## Rules

1. No vendor SDK import outside `ai/providers/<kind>/`.
2. No `if provider == "openai"` anywhere in business logic.
3. Every provider reports usage to the metering service — including mocks, which report
   zero cost, so the accounting path is exercised in tests.
4. Every provider is constructed through the factory, which consults the budget guard.
5. Provider-specific errors are translated into shared error types at the adapter boundary.

## Selection

```env
AI_MODE=cloud    # cloud | local | mock
LLM_PROVIDER=openai
STT_PROVIDER=deepgram
TTS_PROVIDER=elevenlabs
```

`AI_MODE=mock` forces all three to mocks regardless of the individual settings — a single
switch that guarantees zero spend. `TEXT_ONLY_MODE=true` additionally disables STT and TTS
construction entirely.

## Adding a provider

Implement the Protocol, translate errors, report usage, register in the factory, add a
contract test that runs the same assertions as every other implementation of that kind.
Nothing else in the codebase changes — if it does, the abstraction has leaked.
