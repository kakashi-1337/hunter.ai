#!/usr/bin/env bash
# What a knowledge collection holds that would be left out of it today: the output of a build (.next and the like),
# minified and bundled scripts and style sheets, source maps, lock files. A collection that was filled before these
# were left out still has them, and indexing a folder again does not take out what stands in a directory that is
# now passed over.
#   scripts/kb-clean.sh                  the collections
#   scripts/kb-clean.sh "NAME"           says what it found: how much, of which kind, where it stands. Changes nothing.
#   scripts/kb-clean.sh "NAME" --apply   takes them out: their passages and vectors are removed for good. The files
#                                        themselves are not touched, and each stays listed as skipped, with the reason.
# The whole list is kept in the data folder (kb-clean/). Not while a job of the collection is queued or running.
# Exit code 0: it looked, or took them out. 1: it could not (no such collection, a job, no vector store).
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python -m hunterai.kb.clean "$@"
