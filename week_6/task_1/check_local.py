"""Run the three fixed Day 26 prompts or inspect the local runtime."""

from __future__ import annotations

import argparse
import json

from local_llm import LocalAgent, load_scenarios, runtime_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка локальной Qwen3.5 через Ollama")
    parser.add_argument("--save-text", action="store_true", help="сохранить запросы и ответы в локальный ledger")
    parser.add_argument("--runtime-check", action="store_true", help="проверить version/tags/ps/show без генерации")
    args = parser.parse_args()
    agent = LocalAgent()
    if args.runtime_check:
        status = runtime_status(agent)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1 if any(isinstance(status[key], dict) and "error" in status[key] for key in ("version", "tags", "ps", "show")) else 0

    scenarios = load_scenarios()
    for scenario in scenarios:
        print(f"\n{scenario['level']}: {scenario['title']}\n{scenario['prompt']}\n", flush=True)
        result = agent.run(scenario["prompt"], scenario_id=scenario["id"], save_text=args.save_text)
        receipt = result["receipt"]
        if result["status"] != "ok":
            reason = receipt["error"] or receipt["output_policy"]["reason"] or receipt["input_policy"]["reason"] or "неизвестные счётчики usage"
            print(f"Остановлено: {result['status']} — {reason}")
            print(f"Receipt: {receipt['request_id']}; input={receipt['usage']['input_tokens']}, output={receipt['usage']['output_tokens']}, total={receipt['usage']['total_tokens']}")
            return 1
        print(result["answer"])
        print(f"Токены: input={receipt['usage']['input_tokens']}, output={receipt['usage']['output_tokens']}, total={receipt['usage']['total_tokens']}; время={receipt['durations_ns']['total_duration']} нс; API USD={receipt['pricing']['api_cost_usd']}; накоплено={receipt['pricing']['cumulative_api_cost_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
