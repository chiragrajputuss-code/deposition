"""A real agent, recorded automatically: Claude plus one tool.

Needs the Anthropic SDK and an API key:

    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/03_anthropic_agent.py

Note what is *not* here: no logging code around the model call. `deposition.init`
patches the Anthropic client, so the `llm_call` event - request, response, token
usage, latency - is recorded on its own. The only thing the agent declares is its
own tool execution, and even that is linked back to the model output
automatically, because Deposition saw Claude ask for `get_weather` by name.
"""

import os
import sys

import deposition

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

    deposition.init(project="examples")
    client = anthropic.Anthropic()

    @deposition.record(name="weather-agent")
    def run_agent(question: str) -> str:
        messages = [{"role": "user", "content": question}]

        # Recorded automatically - no deposition call needed here.
        response = client.messages.create(
            model=MODEL, max_tokens=512, tools=TOOLS, messages=messages
        )

        for block in response.content:
            if block.type != "tool_use":
                continue
            # No caused_by argument: Deposition already saw Claude request this
            # tool by name, so the causal edge is observed, not guessed.
            with deposition.step("tool_call", tool=block.name) as body:
                body["arguments"] = block.input
                body["result"] = get_weather(**block.input)

        return "".join(b.text for b in response.content if b.type == "text") or "(tool call)"

    print(run_agent("What is the weather in Mumbai? Use the tool."))
    deposition.shutdown()
    print("\nTrace written to ./deposition/ - run `deposition view` on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
