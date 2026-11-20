#!/usr/bin/env bash
# What the search of a knowledge collection brings: things that stand in one passage only are asked of the real
# search, and the result says where that passage stood (first, in the message as a message is filled, among the 40
# the search chooses from, or nowhere). Nothing of the collection is changed. The embedding model has to be reachable.
#   scripts/rag-test.sh                  the collections
#   scripts/rag-test.sh "NAME"           builds a test for the collection once (identifiers, and questions written
#                                        by the decision model), asks it, and keeps it; next time it asks the same
#                                        test again and compares with the run before
#   scripts/rag-test.sh "NAME" --questions 0     a new test asks identifiers only: no model is asked
#   scripts/rag-test.sh "NAME" --new     throws the kept test away and builds a new one
# Exit code 0: the test was asked. 1: it could not be (no such collection, nothing to ask, no embedding model).
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python -m hunterai.kb.ragtest "$@"
