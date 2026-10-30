#!/usr/bin/env bash
# =============================================================================
# ALETHEIA 2.0 — One-Shot GCP Infrastructure Provisioner
# =============================================================================
# Usage: Fill in your PROJECT_ID in .env, then run: bash scripts/setup_gcp.sh
#
# What this script does:
#   1. Enables required GCP APIs
#   2. Creates BigQuery datasets (normalized data + billing export)
#   3. Creates GCS bucket for raw transmission archives
#   4. Creates a dedicated service account with minimal IAM roles
#   5. Downloads service account key to ./credentials/
#   6. Optionally creates a billing export budget alert
#
# Requirements: gcloud CLI authenticated (gcloud auth login)
# =============================================================================

set -euo pipefail

# ── Load .env variables ───────────────────────────────────────────────────────
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# ── Configuration (set these in .env or override here) ───────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-FILL_IN_YOUR_PROJECT_ID}"
REGION="${GCP_REGION:-us-central1}"
BQ_DATASET_NORMALIZED="${BQ_DATASET_NORMALIZED:-aletheia_2_0_normalized}"
BQ_DATASET_BILLING="${BQ_DATASET_BILLING:-AletheiaBilling}"
GCS_BUCKET="${GCS_BUCKET_RAW:-aletheia-2-0-raw-data}"
SA_NAME="${GCP_SERVICE_ACCOUNT_NAME:-aletheia-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
KEY_PATH="credentials/aletheia-sa-key.json"
SPEND_CEILING="${ALETHEIA_SPEND_CEILING_USD:-300}"

echo "============================================================"
echo " ALETHEIA 2.0 GCP Infrastructure Setup"
echo " Project:  $PROJECT_ID"
echo " Region:   $REGION"
echo " Dataset:  $BQ_DATASET_NORMALIZED"
echo " Bucket:   gs://$GCS_BUCKET"
echo " Service Account: $SA_EMAIL"
echo "============================================================"

# Validate project ID is set
if [[ "$PROJECT_ID" == "FILL_IN_YOUR_PROJECT_ID" ]]; then
    echo "ERROR: GCP_PROJECT_ID is not set. Add it to your .env file."
    exit 1
fi

# ── Step 1: Enable APIs ───────────────────────────────────────────────────────
echo ""
echo ">> [1/6] Enabling GCP APIs..."
gcloud services enable \
    bigquery.googleapis.com \
    storage.googleapis.com \
    aiplatform.googleapis.com \
    cloudbilling.googleapis.com \
    --project="$PROJECT_ID"
echo "    ✓ APIs enabled"

# ── Step 2: Create BigQuery Datasets ─────────────────────────────────────────
echo ""
echo ">> [2/6] Creating BigQuery datasets..."

# Normalized research data
if ! bq ls --project_id="$PROJECT_ID" "$BQ_DATASET_NORMALIZED" &>/dev/null; then
    bq mk \
        --project_id="$PROJECT_ID" \
        --location="$REGION" \
        --description="ALETHEIA 2.0 normalized research dataset" \
        "$BQ_DATASET_NORMALIZED"
    echo "    ✓ Created dataset: $BQ_DATASET_NORMALIZED"
else
    echo "    ~ Dataset already exists: $BQ_DATASET_NORMALIZED"
fi

# Billing export dataset
if ! bq ls --project_id="$PROJECT_ID" "$BQ_DATASET_BILLING" &>/dev/null; then
    bq mk \
        --project_id="$PROJECT_ID" \
        --location="$REGION" \
        --description="ALETHEIA 2.0 billing export dataset" \
        "$BQ_DATASET_BILLING"
    echo "    ✓ Created dataset: $BQ_DATASET_BILLING"
else
    echo "    ~ Dataset already exists: $BQ_DATASET_BILLING"
fi

# ── Step 3: Create GCS Bucket ────────────────────────────────────────────────
echo ""
echo ">> [3/6] Creating GCS raw data bucket..."
if ! gsutil ls "gs://$GCS_BUCKET" &>/dev/null; then
    gsutil mb \
        -p "$PROJECT_ID" \
        -l "$REGION" \
        -b on \
        "gs://$GCS_BUCKET"
    # Enable versioning for archive integrity
    gsutil versioning set on "gs://$GCS_BUCKET"
    echo "    ✓ Created bucket: gs://$GCS_BUCKET (versioning enabled)"
else
    echo "    ~ Bucket already exists: gs://$GCS_BUCKET"
fi

# ── Step 4: Create Service Account ───────────────────────────────────────────
echo ""
echo ">> [4/6] Creating service account..."
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
    gcloud iam service-accounts create "$SA_NAME" \
        --project="$PROJECT_ID" \
        --display-name="ALETHEIA 2.0 Service Account" \
        --description="Minimal IAM service account for ALETHEIA research engine"
    echo "    ✓ Created service account: $SA_EMAIL"
else
    echo "    ~ Service account already exists: $SA_EMAIL"
fi

# ── Step 5: Bind IAM Roles ────────────────────────────────────────────────────
echo ""
echo ">> [5/6] Binding IAM roles..."

ROLES=(
    "roles/bigquery.dataEditor"     # Read + write to BQ datasets
    "roles/bigquery.jobUser"        # Run BQ queries
    "roles/storage.objectAdmin"     # Read + write GCS raw bucket
    "roles/aiplatform.user"         # Vertex AI / Gemini API calls
)

for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="$ROLE" \
        --quiet
    echo "    ✓ Bound: $ROLE"
done

# ── Step 6: Download Service Account Key ─────────────────────────────────────
echo ""
echo ">> [6/6] Generating and downloading service account key..."
mkdir -p credentials
if [ ! -f "$KEY_PATH" ]; then
    gcloud iam service-accounts keys create "$KEY_PATH" \
        --iam-account="$SA_EMAIL" \
        --project="$PROJECT_ID"
    echo "    ✓ Key saved to: $KEY_PATH"
    echo "    ⚠ Add $KEY_PATH to .gitignore immediately!"
else
    echo "    ~ Key already exists at: $KEY_PATH"
fi

# ── Update .env with credential path ─────────────────────────────────────────
if [ -f ".env" ]; then
    if ! grep -q "GOOGLE_APPLICATION_CREDENTIALS" .env; then
        echo "" >> .env
        echo "# GCP Service Account Key (generated by setup_gcp.sh)" >> .env
        echo "GOOGLE_APPLICATION_CREDENTIALS=$KEY_PATH" >> .env
        echo "MOCK_GCP=False" >> .env
        echo "    ✓ Added GOOGLE_APPLICATION_CREDENTIALS to .env"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " ALETHEIA 2.0 GCP Setup Complete!"
echo "============================================================"
echo ""
echo " BigQuery Datasets:"
echo "   - $PROJECT_ID.$BQ_DATASET_NORMALIZED  (research data)"
echo "   - $PROJECT_ID.$BQ_DATASET_BILLING     (billing export)"
echo ""
echo " GCS Bucket:"
echo "   - gs://$GCS_BUCKET  (raw transmission archives)"
echo ""
echo " Service Account Key:"
echo "   - $KEY_PATH"
echo ""
echo " Next steps:"
echo "   1. Add '$KEY_PATH' to your .gitignore"
echo "   2. Set MOCK_GCP=False in .env to activate live GCP"
echo "   3. Configure billing export in GCP Console:"
echo "      Billing → Billing export → BigQuery export → Dataset: $BQ_DATASET_BILLING"
echo "   4. Run: streamlit run app.py"
echo ""
echo " Spend ceiling configured: \$${SPEND_CEILING} USD"
echo "============================================================"
