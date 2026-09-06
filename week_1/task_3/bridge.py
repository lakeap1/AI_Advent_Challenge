"""Exact reference and route validation; never sent to the model."""
from functools import lru_cache
from heapq import heappop, heappush
from itertools import combinations

TIMES = (1, 3, 6, 8, 12)


@lru_cache
def optimum():
    start = (0, 0)
    goal = ((1 << len(TIMES)) - 1, 1)
    distance, previous, queue = {start: 0}, {}, [(0, start)]
    while queue:
        elapsed, state = heappop(queue)
        if elapsed != distance[state]:
            continue
        if state == goal:
            moves = []
            while state != start:
                state, group = previous[state]
                moves.append([TIMES[i] for i in group])
            return {"minutes": elapsed, "moves": list(reversed(moves))}
        mask, side = state
        available = [i for i in range(len(TIMES)) if (mask >> i) & 1 == side]
        for count in (1, 2):
            for group in combinations(available, count):
                next_mask = mask
                for i in group:
                    next_mask ^= 1 << i
                next_state = (next_mask, 1 - side)
                cost = elapsed + max(TIMES[i] for i in group)
                if cost < distance.get(next_state, float("inf")):
                    distance[next_state] = cost
                    previous[next_state] = (state, group)
                    heappush(queue, (cost, next_state))
    raise RuntimeError("No route")


def check_route(moves, claimed):
    left, right, side, elapsed = set(TIMES), set(), 0, 0
    steps, issues = [], []
    if not isinstance(moves, list) or not moves or len(moves) > 100:
        return {"status": "invalid", "minutes": 0, "issues": ["Нужен полный маршрут из 1–100 ходов."], "steps": []}
    for number, group in enumerate(moves, 1):
        if (not isinstance(group, list) or not 1 <= len(group) <= 2
                or any(type(person) is not int or person not in TIMES for person in group)
                or len(set(group)) != len(group)):
            issues.append(f"Ход {number}: нужны один или два разных участника из условия.")
            break
        source, target = (left, right) if side == 0 else (right, left)
        if not set(group) <= source:
            issues.append(f"Ход {number}: участник находится не на стороне фонаря.")
            break
        source.difference_update(group)
        target.update(group)
        elapsed += max(group)
        steps.append({"people": group, "direction": "→" if side == 0 else "←", "cost": max(group), "elapsed": elapsed})
        side = 1 - side
    if left:
        issues.append("Не все участники переправились.")
    if type(claimed) is not int or claimed != elapsed:
        issues.append(f"Заявленный итог не совпадает с суммой ходов: {elapsed} мин.")
    return {"status": "invalid" if issues else ("optimal" if elapsed == optimum()["minutes"] else "suboptimal"),
            "minutes": elapsed, "issues": issues, "steps": steps}
