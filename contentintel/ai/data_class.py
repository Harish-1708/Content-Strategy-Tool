"""Data classes: Public, Internal, Restricted, and the rule that decides which data may go to which provider.

Public      text anyone can already see (a public ad, a public post, a search result).
Internal    the agency's own material that is not public (briefs, decisions, notes, the catalogue before launch).
Restricted  unreleased products, offers and client confidential information.

A provider has a ceiling (the highest class its written terms allow) and a list of classes a person has approved.
Both must allow the data. Anything unknown is treated as Restricted: the check fails closed, never open.
"""

PUBLIC, INTERNAL, RESTRICTED = "public", "internal", "restricted"
ORDER = (PUBLIC, INTERNAL, RESTRICTED)


class DataClassError(ValueError):
    pass


def rank(data_class):
    try:
        return ORDER.index(data_class)
    except ValueError:
        raise DataClassError(f"{data_class!r} is not a data class (use {', '.join(ORDER)})") from None


def validate(data_class):
    rank(data_class)
    return data_class


def highest(*classes):
    """The most sensitive of several classes: text built from several sources is as sensitive as its most sensitive part."""
    return max((validate(c) for c in classes), key=rank) if classes else RESTRICTED


def within(ceiling, data_class):
    """True when data of this class is no more sensitive than the ceiling."""
    return rank(data_class) <= rank(ceiling)
