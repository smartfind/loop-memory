"""Run a short multi-turn conversation through the loop engine.

No API keys required — uses the bundled EchoLLM and a HashingEmbedder,
so this example is fully self-contained.
"""

from __future__ import annotations

from loop_memory import EchoLLM, HashingEmbedder, LoopEngine


def main() -> None:
    engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=128))

    plan = engine.push_plan("Help Mia plan a weekend trip to Hangzhou.")
    print(f"[plan] {plan.text}\n")

    turns = [
        "Hi, my name is Mia. I really love matcha and I dislike spicy food.",
        "I'm travelling this weekend — somewhere quiet, ideally with tea and a lake.",
        "I'll be coming back next week. Can you summarise what we decided?",
    ]
    for msg in turns:
        result = engine.turn(msg)
        print(f"you>  {msg}")
        print(f"bot>  {result.reply}")
        print(f"     diag={result.diagnostics}")
        print()

    print("[recall: 'tea']")
    for item in engine.recall("tea"):
        print(f"  - ({item.kind}) {item.text}")

    print("\n[final state]")
    print(engine)


if __name__ == "__main__":
    main()
