import random
import time
import math
import logging

logger = logging.getLogger(__name__)


def _bezier_ease(t, p1x=0.25, p1y=0.1, p2x=0.75, p2y=0.9):
    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2
        x = 3 * (1 - mid) ** 2 * mid * p1x + 3 * (1 - mid) * mid ** 2 * p2x + mid ** 3
        if x < t:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2
    return 3 * (1 - s) ** 2 * s * p1y + 3 * (1 - s) * s ** 2 * p2y + s ** 3


def human_delay(min_s=0.05, max_s=0.15, label=None):
    delay = random.uniform(min_s, max_s)
    if label:
        logger.debug(f"[HB] {label}: {delay:.2f}s")
    time.sleep(delay)
    return delay


def reading_delay(content_length=0):
    delay = random.uniform(0.05, 0.15)
    time.sleep(delay)
    return delay


def typing_delay(field_length=16):
    delay = random.uniform(0.02, 0.08)
    time.sleep(delay)
    return delay


def form_fill_delay():
    delay = random.uniform(0.03, 0.1)
    time.sleep(delay)
    return delay


def navigation_delay():
    delay = random.uniform(0.03, 0.1)
    time.sleep(delay)
    return delay


def pre_submit_delay():
    delay = random.uniform(0.05, 0.15)
    time.sleep(delay)
    return delay


def between_requests_delay():
    delay = random.uniform(0.05, 0.15)
    time.sleep(delay)
    return delay


def page_interaction_delay(page_length=0):
    delay = random.uniform(0.03, 0.1)
    time.sleep(delay)
    return delay


def checkout_flow_delay(step="generic"):
    delay = random.uniform(0.05, 0.15)
    time.sleep(delay)
    return delay


def retry_delay(attempt, base_min=0.5, base_max=1.5):
    delay = random.uniform(base_min, base_max) * (1.2 ** attempt)
    delay = max(base_min, delay)
    time.sleep(delay)
    return delay
