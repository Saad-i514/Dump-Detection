terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "Dump-Detection", ManagedBy = "Terraform" }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

data "aws_caller_identity" "current" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  bundle = "${path.module}/../.deploy/bundle.tar.gz"
  name   = "dump-detection"
}

resource "aws_s3_bucket" "deployment" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "deployment" {
  bucket                  = aws_s3_bucket.deployment.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "deployment" {
  bucket = aws_s3_bucket.deployment.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_object" "bundle" {
  bucket      = aws_s3_bucket.deployment.id
  key         = "bundles/${filesha256(local.bundle)}.tar.gz"
  source      = local.bundle
  source_hash = filesha256(local.bundle)
}

resource "random_password" "origin" {
  length  = 48
  special = false
}
resource "aws_ssm_parameter" "origin" {
  name  = "/${local.name}/origin-token"
  type  = "SecureString"
  value = random_password.origin.result
}

resource "aws_iam_role" "api" {
  name = "${local.name}-server"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "deployment" {
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.deployment.arn}/bundles/*" },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = aws_ssm_parameter.origin.arn }
    ]
  })
}
resource "aws_iam_instance_profile" "api" {
  name = "${local.name}-server"
  role = aws_iam_role.api.name
}

resource "aws_security_group" "api" {
  name_prefix = "${local.name}-"
  description = "Only CloudFront can reach the HTTP origin; management uses SSM"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "api" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.api.id]
  iam_instance_profile        = aws_iam_instance_profile.api.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region          = var.aws_region
    bucket          = aws_s3_bucket.deployment.id
    bundle_key      = aws_s3_object.bundle.key
    token_parameter = aws_ssm_parameter.origin.name
  })
  root_block_device {
    volume_size = 24
    volume_type = "gp3"
    encrypted   = true
  }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  credit_specification { cpu_credits = "standard" }
  tags       = { Name = local.name }
  depends_on = [aws_iam_role_policy.deployment, aws_iam_role_policy_attachment.ssm]
}
resource "aws_eip" "api" {
  domain   = "vpc"
  instance = aws_instance.api.id
}

data "aws_cloudfront_cache_policy" "disabled" { name = "Managed-CachingDisabled" }
data "aws_cloudfront_origin_request_policy" "viewer" { name = "Managed-AllViewerExceptHostHeader" }

resource "aws_cloudfront_distribution" "api" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "Public Dump-Detection API"
  price_class     = "PriceClass_200"
  origin {
    domain_name = aws_eip.api.public_dns
    origin_id   = "api"
    custom_header {
      name  = "X-Origin-Token"
      value = random_password.origin.result
    }
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }
  default_cache_behavior {
    target_origin_id         = "api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.viewer.id
    compress                 = true
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}

output "api_url" { value = "https://${aws_cloudfront_distribution.api.domain_name}" }
output "docs_url" { value = "https://${aws_cloudfront_distribution.api.domain_name}/docs" }
output "instance_id" { value = aws_instance.api.id }
output "deployment_bucket" { value = aws_s3_bucket.deployment.id }
