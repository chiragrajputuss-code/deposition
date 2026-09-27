"""A real agent, recorded: Claude plus one tool.

Needs the Anthropic SDK and an API key:

    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/03_anthropic_agent.py

Auto-instrumentation is still landing, so this example records the LLM call
explicitly. When `postflight.init()` patches the Anthropic client, the
`postflight.log("llm_call", ...)` below becomes unnecessary and the trace looks
the same.
"""

import os
import sys
import time

import postflight

MODEL = "claude-sonnet-5"
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather in a given city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def get_weather(city: str) -> str:
    return f"18C and overcast in {city}"


def main() -> int:
    try:
        import anthropic
    except ImportError:
        print("this example needs the Anthropic SDK:  pip install anthropic", file=sys.stderr)
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY to run this example", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    postflight.init(project="examples")

    @postflight.record(name="weather-agent")
    def run_agent(question: str) -> str:
        messages = [{"role": "user", "content": question}]

        started = time.monotonic()
        response = client.messages.create(
            model=MODEL, max_tokens=512, tools=TOOLS, messages=messages
        )
        call = postflight.log(
            "llm_call",
            {
                "provider": "anthropic",
                "gen_ai.request.model": MODEL,
                "messages": messages,
                "response": [block.model_dump() for block in response.content],
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
                },
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
            },
        )

        for block in response.content:
            if block.type != "tool_use":
                continue
            # caused_by points at the model output that asked for this call.
            with postflight.step("tool_call", tool=block.name, caused_by=[call]) as body:
                body["arguments"] = block.input
                body["result"] = get_weather(**block.input)

        return "".join(b.text for b in response.content if b.type == "text") or "(tool call)"

    print(run_agent("What is the weather in Mumbai? Use the tool."))
    postflight.shutdown()
    print("\nTrace written to ./postflight/ - run `postflight view` on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
