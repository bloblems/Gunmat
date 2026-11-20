#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
if ! command -v uv >/dev/null; then
  echo "Install uv using the link in README.md, then open this file again."
  exit 1
fi
exec uv run --no-project --python 3.12 --with-requirements requirements.txt -m cadraw "$@"
