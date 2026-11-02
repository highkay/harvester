#!/usr/bin/env python3

"""Capability-tier constants shared across provider, pipeline and push layers.

The tier *outcome* classes and the ``valid-keys-<tier>.txt`` naming live here so
``provider/capability.py`` (probing), ``stage/definition.py`` (routing),
``storage/persistence.py`` (write side) and ``web/push.py`` (read side) all use
one definition and cannot drift apart.
"""

# Per-tier outcome classes. All but OK keep the key OUT of that tier's usable
# set; the label only changes the recorded reason, never the adversarial
# verdict (an OK-less key is still invalid, but a RETRYABLE tier never kills a
# key that passed another tier).
TIER_OK = "OK"
TIER_NO_ACCESS = "NO_ACCESS"
TIER_NO_QUOTA = "NO_QUOTA"
TIER_NO_MODEL = "NO_MODEL"
TIER_RETRYABLE = "RETRYABLE"


def tier_result_type(tier: str) -> str:
    """Result-type string routing a tier key into ``valid-keys-<tier>.txt``."""
    return f"valid_tier_{tier}"


def tier_file_name(tier: str) -> str:
    """File name for one capability tier, sibling to ``valid-keys.txt``."""
    return f"valid-keys-{tier}.txt"