# Stretch 4 — Terraform (WRITE-ONLY, khong apply).
#
# Ban "that" cua nhung gi state/snapshot.py lam voi --backend fs/minio:
#
#   snapshot.put(region="a")  -> PutObject vectors.sqlite, model.bin, MANIFEST.json vao bucket
#                                aws_s3_bucket.primary (us-east-1). Day la "region A".
#   S3 Cross-Region Replication (resource aws_s3_bucket_replication_configuration ben duoi)
#                             -> S3 tu copy 3 object do sang aws_s3_bucket.replica (us-west-2).
#                                Do tre cua buoc nay CONG THEM vao RPO (state/replicate.py --every N
#                                chi la mot nua cau chuyen). RTC cam ket 99.99% object < 15 phut.
#   snapshot.get(region="b")  -> GetObject tu bucket REPLICA o region B — khong bao gio doc tu
#                                bucket primary, vi luc failover region A coi nhu da mat.
#   MANIFEST.json versioning  -> S3 Object Versioning: moi lan put la mot VersionId moi. Restore ve
#                                snapshot cu = GetObject(Key="MANIFEST.json", VersionId=...).
#                                Lab ghi de MANIFEST.json tren dia; S3 giu lai ca lich su.
#
# Bay can biet (khong co trong lab): CRR replicate tung object DOC LAP, khong co thu tu. Region B
# co the thay MANIFEST.json moi truoc khi vectors.sqlite moi toi. Vi vay put() that nen:
#   (1) upload data truoc, MANIFEST sau cung; (2) ghi VersionId/ETag cua data vao MANIFEST;
#   (3) get() kiem tra ETag cua data khop MANIFEST, khong khop thi dung version MANIFEST truoc do.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "bucket_prefix" {
  type    = string
  default = "lab23-dr-artifacts"
}

variable "primary_region" {
  type    = string
  default = "us-east-1"
}

variable "dr_region" {
  type    = string
  default = "us-west-2"
}

provider "aws" {
  region = var.primary_region
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
}

# ---------- Region A: noi state/replicate.py goi put() ----------
resource "aws_s3_bucket" "primary" {
  bucket = "${var.bucket_prefix}-${var.primary_region}"
}

resource "aws_s3_bucket_versioning" "primary" {
  bucket = aws_s3_bucket.primary.id
  versioning_configuration {
    status = "Enabled" # bat buoc cho CRR; cung la "lich su MANIFEST.json"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "primary" {
  bucket = aws_s3_bucket.primary.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------- Region B: noi dr/failover.py buoc 2_restore_snapshot goi get() ----------
resource "aws_s3_bucket" "replica" {
  provider = aws.dr
  bucket   = "${var.bucket_prefix}-${var.dr_region}"
}

resource "aws_s3_bucket_versioning" "replica" {
  provider = aws.dr
  bucket   = aws_s3_bucket.replica.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "replica" {
  provider = aws.dr
  bucket   = aws_s3_bucket.replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Giu 30 ngay snapshot cu de con PITR-o-muc-snapshot (rollback MANIFEST ve version truoc).
resource "aws_s3_bucket_lifecycle_configuration" "replica" {
  provider = aws.dr
  bucket   = aws_s3_bucket.replica.id
  rule {
    id     = "expire-old-snapshots"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  depends_on = [aws_s3_bucket_versioning.replica]
}

# ---------- IAM role S3 dung de replicate ----------
data "aws_iam_policy_document" "assume_s3" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "replication" {
  name               = "${var.bucket_prefix}-crr"
  assume_role_policy = data.aws_iam_policy_document.assume_s3.json
}

data "aws_iam_policy_document" "replication" {
  statement {
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.primary.arn]
  }
  statement {
    actions = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl",
    "s3:GetObjectVersionTagging"]
    resources = ["${aws_s3_bucket.primary.arn}/*"]
  }
  statement {
    actions   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
    resources = ["${aws_s3_bucket.replica.arn}/*"]
  }
}

resource "aws_iam_role_policy" "replication" {
  name   = "${var.bucket_prefix}-crr"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

# ---------- CRR: tuong duong "put o A, get o B" ----------
resource "aws_s3_bucket_replication_configuration" "primary_to_dr" {
  bucket = aws_s3_bucket.primary.id
  role   = aws_iam_role.replication.arn

  rule {
    id       = "dr-artifacts-to-dr-region"
    status   = "Enabled"
    priority = 1

    filter {} # ca bucket: vectors.sqlite, model.bin, MANIFEST.json

    # Xoa nham o primary KHONG duoc lan sang ban DR (ban DR la cai de cuu ban primary).
    delete_marker_replication {
      status = "Disabled"
    }

    destination {
      bucket        = aws_s3_bucket.replica.arn
      storage_class = "STANDARD" # restore can doc ngay, khong dung Glacier cho RTO 5 phut

      # Replication Time Control: SLA 15 phut + metric de alert khi lag tang (= RPO tang).
      replication_time {
        status = "Enabled"
        time {
          minutes = 15
        }
      }
      metrics {
        status = "Enabled"
        event_threshold {
          minutes = 15
        }
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.primary, aws_s3_bucket_versioning.replica]
}

output "primary_bucket" {
  value = aws_s3_bucket.primary.bucket
}

output "replica_bucket" {
  description = "Bucket ma failover (region B) restore tu do: DR_BUCKET cho state/snapshot.py get"
  value       = aws_s3_bucket.replica.bucket
}
