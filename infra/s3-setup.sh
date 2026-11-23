#!/usr/bin/env bash
# Create the private S3 bucket for listing photos (spec §9). Idempotent: safe to re-run.
#
#   - private bucket, all public access blocked
#   - lifecycle: objects expire after 30 days
#   - IAM user with access to this bucket only; its access key is written to .env
#
# Run with an admin AWS identity (e.g. after `aws login`):
#   infra/s3-setup.sh [bucket-name]        # default: olx-auto-<account-id>
set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
IAM_USER="olx-auto"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${1:-olx-auto-$ACCOUNT_ID}"
echo "account $ACCOUNT_ID, bucket $BUCKET, region $REGION"

# ---- bucket --------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  echo "bucket created"
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{
  "Rules": [{
    "ID": "expire-30d",
    "Status": "Enabled",
    "Filter": {},
    "Expiration": {"Days": 30},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
  }]
}'
echo "public access blocked, lifecycle set"

# ---- IAM user scoped to this bucket ----------------------------------------
if ! aws iam get-user --user-name "$IAM_USER" >/dev/null 2>&1; then
  aws iam create-user --user-name "$IAM_USER" >/dev/null
  echo "IAM user $IAM_USER created"
fi

aws iam put-user-policy --user-name "$IAM_USER" --policy-name "$BUCKET-access" --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:DeleteObject\"],
     \"Resource\": \"arn:aws:s3:::$BUCKET/*\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$BUCKET\"}
  ]
}"
echo "policy attached"

# ---- credentials into .env (the secret is never printed) -------------------
touch "$ENV_FILE"
set_env() {  # set_env KEY VALUE: replace or append
  grep -v "^$1=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
  echo "$1=$2" >> "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}

if grep -q '^AWS_ACCESS_KEY_ID=.' "$ENV_FILE"; then
  echo ".env already has an access key; not creating another (delete it from .env to rotate)"
else
  read -r KEY_ID SECRET < <(aws iam create-access-key --user-name "$IAM_USER" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
  set_env AWS_ACCESS_KEY_ID "$KEY_ID"
  set_env AWS_SECRET_ACCESS_KEY "$SECRET"
  echo "access key $KEY_ID written to .env"
fi
set_env S3_BUCKET "$BUCKET"
set_env AWS_REGION "$REGION"
chmod 600 "$ENV_FILE"
echo "done"
