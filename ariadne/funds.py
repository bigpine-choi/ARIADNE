"""Explicit canonical identities; ambiguous names never resolve by guessing."""

import re
import unicodedata

from .models import ValidationError


def _normalize(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Fund name must be a nonempty string")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


class FundRegistry:
    """Register reviewed identities and aliases; no production fund names are seeded."""

    def __init__(self, funds=()):
        self._funds = {}
        self._aliases = {}
        for fund in funds:
            self.register(fund["id"], fund["name"], fund.get("aliases", ()))

    def register(self, fund_id, name, aliases=()):
        if not isinstance(fund_id, str) or not fund_id.strip():
            raise ValidationError("Fund id must be a nonempty string")
        if fund_id in self._funds:
            raise ValidationError("Fund id is already registered")
        if not isinstance(aliases, (list, tuple)):
            raise ValidationError("Fund aliases must be an array")
        aliases = list(aliases)
        names = [fund_id, name, *aliases]
        normalized = [_normalize(alias) for alias in names]
        self._funds[fund_id] = {"id": fund_id, "name": name, "aliases": list(aliases)}
        for alias in normalized:
            self._aliases.setdefault(alias, set()).add(fund_id)

    def resolve(self, name):
        candidates = self._aliases.get(_normalize(name), set())
        if len(candidates) != 1:
            raise ValidationError("Fund alias is ambiguous" if candidates else "Fund alias is unregistered")
        return next(iter(candidates))

    def list_funds(self):
        return [{**fund, "aliases": list(fund["aliases"])} for _, fund in sorted(self._funds.items())]
