#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# One-shot MinIO setup, run by the `minio-init` compose service. Idempotent.
#
# Creates the buckets and one least-privilege access key per consumer:
#   PAIRLY_S3_ACCESS_KEY    read/write on PAIRLY_S3_BUCKET only (API and worker)
#   LANGFUSE_S3_ACCESS_KEY  read/write on the `langfuse` bucket only (observability profile)
# Buckets stay private: no anonymous access. The app hands out presigned URLs.

set -eu

: "${MINIO_ROOT_USER:?missing}"
: "${MINIO_ROOT_PASSWORD:?missing}"
: "${PAIRLY_S3_BUCKET:?missing}"
: "${PAIRLY_S3_ACCESS_KEY:?missing}"
: "${PAIRLY_S3_SECRET_KEY:?missing}"
: "${LANGFUSE_S3_ACCESS_KEY:?missing}"
: "${LANGFUSE_S3_SECRET_KEY:?missing}"

LANGFUSE_BUCKET=langfuse
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export MC_CONFIG_DIR="$WORK/mc"

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null

bucket_policy() {
  # $1 = bucket name. Read/write on objects in that bucket, nothing else.
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::$1"]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::$1/*"]
    }
  ]
}
EOF
}

setup() {
  # $1 = bucket, $2 = access key, $3 = secret key, $4 = policy name
  mc mb --ignore-existing "local/$1"
  mc anonymous set none "local/$1" >/dev/null
  bucket_policy "$1" > "$WORK/$4.json"
  mc admin policy create local "$4" "$WORK/$4.json"
  # `user add` updates the secret when the user already exists.
  mc admin user add local "$2" "$3" >/dev/null
  # The image has no grep, so match with `case`.
  info=$(mc admin user info local "$2")
  case "$info" in
    *"$4"*) ;;
    *) mc admin policy attach local "$4" --user "$2" >/dev/null ;;
  esac
  echo "ready: bucket $1, access key $2 (policy $4)"
}

setup "$PAIRLY_S3_BUCKET" "$PAIRLY_S3_ACCESS_KEY" "$PAIRLY_S3_SECRET_KEY" pairly-app-rw
setup "$LANGFUSE_BUCKET" "$LANGFUSE_S3_ACCESS_KEY" "$LANGFUSE_S3_SECRET_KEY" langfuse-rw
