"""
Генерация сумм транзакций для continuous и discrete.
"""

import math
import random


def _draw_lognormal(min_amount, max_amount, avg, sigma=1.2, max_iterations=100):
    """
    Находит усечённое логнормальное распределение через rejection sampling.
    """
    
    mu = math.log(avg)
    for _ in range(max_iterations):
        z = random.gauss(mu, sigma)
        value = math.exp(z)
        if min_amount <= value <= max_amount:
            return value
    return None


def _rounding_continuous_sum(value):
    """
    Огруляет копейки: [0, 49] -> .00, [50, 99] -> .99.
    """

    total_kopecks = round(value * 100)
    integer_part = total_kopecks // 100
    kopecks = total_kopecks % 100
    if kopecks < 50:
        return float(integer_part)
    return integer_part + 0.99


def generate_continuous_sum(min_amount, max_amount, avg):
    """
    Находит сумму для подкатегории типа continuous.
    """

    value = _draw_lognormal(min_amount, max_amount, avg)
    if value is None:
        return None
    return _rounding_continuous_sum(value)


def generate_discrete_sum(tariffs):
    """
    Находит сумму для подкатегории типа discrete.
    """

    prices = [t["price"] for t in tariffs]
    weights = [t["weight"] for t in tariffs]
    return random.choices(prices, weights=weights, k=1)[0]


if __name__ == "__main__":
    pass