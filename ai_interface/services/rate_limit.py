"""Per-user request limits.

Spend caps stop cost, but they stop it after the fact and only once a day's
budget is gone. A stuck loop or an impatient hand on the Enter key can spend a
daily budget in seconds, so volume is limited too.

Counters live in the cache, keyed to a fixed window. A fixed window can allow up
to twice the limit across a boundary; that is a fair trade for something with no
storage cost and no failure mode worse than "counts reset early".
"""

import frappe
from frappe import _

PREFIX = "ai_interface:ratelimit:"


class RateLimited(frappe.ValidationError):
	pass


def check(user: str, scope: str = "assistant", settings=None):
	"""Raise if this user has asked too much, too fast."""
	settings = settings or frappe.get_cached_doc("AI Settings")

	windows = (
		("minute", 60, settings.get("assistant_rate_per_minute")),
		("hour", 3600, settings.get("assistant_rate_per_hour")),
	)

	for label, seconds, limit in windows:
		limit = int(limit or 0)
		if limit <= 0:
			continue

		used = _bump(user, scope, label, seconds)
		if used > limit:
			frappe.throw(
				_("You have asked {0} questions this {1}. Please wait a moment before asking again.").format(
					limit, label
				),
				exc=RateLimited,
			)


def usage(user: str, scope: str = "assistant") -> dict:
	"""Current counters, without incrementing them."""
	out = {}
	for label, seconds in (("minute", 60), ("hour", 3600)):
		out[label] = int(frappe.cache().get_value(_key(user, scope, label, seconds)) or 0)
	return out


def _bump(user: str, scope: str, label: str, seconds: int) -> int:
	key = _key(user, scope, label, seconds)
	cache = frappe.cache()

	try:
		# Atomic where the backend supports it, so two simultaneous requests
		# cannot both read the same count and both decide they are within limit.
		count = cache.incrby(key, 1)
		if count == 1:
			cache.expire(key, seconds)
		return int(count)
	except Exception:
		# Any cache backend without incrby still gets a usable, if racier, count.
		count = int(cache.get_value(key) or 0) + 1
		cache.set_value(key, count, expires_in_sec=seconds)
		return count


def _key(user: str, scope: str, label: str, seconds: int) -> str:
	import time

	window = int(time.time() // seconds)
	return f"{PREFIX}{scope}:{label}:{window}:{user}"
