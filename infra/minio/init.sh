#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# One-shot MinIO setup, run by the `minio-init` compose service. Idempotent.
#
# Creates the buckets and one least-privilege access key per consumer:
#   PAIRLY_S3_ACCESS_KEY    read/write on PAIRLY_S3_BUCKET, and get/list/put (never
#                           delete) on PAIRLY_AUDIT_ANCHOR_BUCKET (API and worker)
#   LANGFUSE_S3_ACCESS_KEY  read/write on the `langfuse` bucket only (observability profile)
# Buckets stay private: no anonymous access. The app hands out presigned URLs.
#
# The audit anchor bucket (WP1.10b) is created with S3 Object Lock (which turns on
# versioning) and a default retention, so a signed chain head cannot be deleted or
# overwritten until it expires. Object lock can only be chosen when a bucket is
# created: an anchor bucket that already exists without it is reported, not changed.

set -eu

: "${MINIO_ROOT_USER:?missing}"
: "${MINIO_ROOT_PASSWORD:?missing}"
: "${PAIRLY_S3_BUCKET:?missing}"
: "${PAIRLY_S3_ACCESS_KEY:?missing}"
: "${PAIRLY_S3_SECRET_KEY:?missing}"
: "${LANGFUSE_S3_ACCESS_KEY:?missing}"
: "${LANGFUSE_S3_SECRET_KEY:?missing}"

LANGFUSE_BUCKET=langfuse
ANCHOR_BUCKET="${PAIRLY_AUDIT_ANCHOR_BUCKET:-pairly-audit-anchors}"
ANCHOR_LOCK_MODE="${PAIRLY_AUDIT_ANCHOR_LOCK_MODE:-GOVERNANCE}"
ANCHOR_RETENTION_DAYS="${PAIRLY_AUDIT_ANCHOR_RETENTION_DAYS:-2555}"
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

app_policy() {
  # The documents bucket read/write, plus the anchor bucket without any delete:
  # anchors are write once.
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::$PAIRLY_S3_BUCKET", "arn:aws:s3:::$ANCHOR_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::$PAIRLY_S3_BUCKET/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:PutObjectRetention"],
      "Resource": ["arn:aws:s3:::$ANCHOR_BUCKET/*"]
    }
  ]
}
EOF
}

if [ "$ANCHOR_LOCK_MODE" = "none" ]; then
  mc mb --ignore-existing "local/$ANCHOR_BUCKET"
else
  mc mb --ignore-existing --with-lock "local/$ANCHOR_BUCKET"
  if mc retention info --default "local/$ANCHOR_BUCKET" >/dev/null 2>&1; then
    mc retention set --default "$ANCHOR_LOCK_MODE" "${ANCHOR_RETENTION_DAYS}d" \
      "local/$ANCHOR_BUCKET" >/dev/null
  else
    echo "warning: $ANCHOR_BUCKET has no object lock; anchors are write once by policy only"
  fi
fi
mc anonymous set none "local/$ANCHOR_BUCKET" >/dev/null

mc mb --ignore-existing "local/$PAIRLY_S3_BUCKET"
mc anonymous set none "local/$PAIRLY_S3_BUCKET" >/dev/null
app_policy > "$WORK/pairly-app-rw.json"
mc admin policy create local pairly-app-rw "$WORK/pairly-app-rw.json"
mc admin user add local "$PAIRLY_S3_ACCESS_KEY" "$PAIRLY_S3_SECRET_KEY" >/dev/null
info=$(mc admin user info local "$PAIRLY_S3_ACCESS_KEY")
case "$info" in
  *pairly-app-rw*) ;;
  *) mc admin policy attach local pairly-app-rw --user "$PAIRLY_S3_ACCESS_KEY" >/dev/null ;;
esac
echo "ready: buckets $PAIRLY_S3_BUCKET and $ANCHOR_BUCKET (lock $ANCHOR_LOCK_MODE)"
setup "$LANGFUSE_BUCKET" "$LANGFUSE_S3_ACCESS_KEY" "$LANGFUSE_S3_SECRET_KEY" langfuse-rw
