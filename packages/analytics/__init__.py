"""Reports over data the project already holds.

Nothing in this package fetches, writes, or decides. Every module reads rows
that exist because of the crawl, apply, and inbox paths, and answers a
question those paths never asked — whether the score predicts anything,
which applications have gone quiet, what a week actually produced.

The reason it is a package rather than a few endpoints: these are the numbers
that say whether the rest of the project works, and they should be testable
without an HTTP client in the room.
"""
