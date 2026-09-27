#!/usr/bin/env bash
# Compila las lambdas Go (go/cmd/*) para Lambda arm64 (provided.al2023) y deja un
# zip por función en .bin/<funcion>.zip, que serverless.yml referencia con
# `package.artifact`. Corre igual en el runner Linux y en Git Bash de Windows:
# build-lambda-zip marca `bootstrap` como ejecutable aunque el FS no tenga permisos Unix.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/.bin"
ZIPPER_VERSION="v1.55.1"

rm -rf "$OUT"
mkdir -p "$OUT"

cd "$ROOT/go"
for dir in cmd/*/; do
  fn="$(basename "$dir")"
  echo "==> $fn"
  mkdir -p "$OUT/$fn"
  GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build \
    -tags lambda.norpc -trimpath -ldflags="-s -w" \
    -o "$OUT/$fn/bootstrap" "./$dir"
  go run "github.com/aws/aws-lambda-go/cmd/build-lambda-zip@$ZIPPER_VERSION" \
    -o "$OUT/$fn.zip" "$OUT/$fn/bootstrap"
done
