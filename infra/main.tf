terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.50" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

# Region guard — refuses any region other than ap-northeast-1
locals { allowed_regions = ["ap-northeast-1"] }
resource "null_resource" "region_guard" {
  lifecycle {
    precondition {
      condition     = contains(local.allowed_regions, var.region)
      error_message = "Region ${var.region} not allowed. All endpoints must remain in ap-northeast-1."
    }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "hl-scanner", ManagedBy = "terraform" } }
}

data "aws_availability_zones" "available" { state = "available" }

# ---------- VPC ----------
resource "aws_vpc" "main" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "hl-scanner-vpc" }
}
resource "aws_internet_gateway" "igw" { vpc_id = aws_vpc.main.id }

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags                    = { Name = "hl-scanner-public-a" }
}
resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.10.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
}
resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.11.0/24"
  availability_zone = data.aws_availability_zones.available.names[1]
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
# Free S3 Gateway Endpoint
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

# ---------- Security groups ----------
resource "aws_security_group" "ec2" {
  name   = "hl-scanner-ec2"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "rds" {
  name   = "hl-scanner-rds"
  vpc_id = aws_vpc.main.id
}
resource "aws_security_group_rule" "rds_in" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ec2.id
  security_group_id        = aws_security_group.rds.id
}

# ---------- SSH key ----------
resource "aws_key_pair" "scanner" {
  key_name   = "hl-scanner"
  public_key = var.ssh_public_key
}

# ---------- IAM instance profile (S3 access only) ----------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "scanner" {
  name               = "hl-scanner-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}
resource "aws_iam_role_policy" "scanner_s3" {
  name = "s3-rw"
  role = aws_iam_role.scanner.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
      Resource = [
        "arn:aws:s3:::hl-scanner-${var.account_id}-${var.region}",
        "arn:aws:s3:::hl-scanner-${var.account_id}-${var.region}/*"
      ]
    }]
  })
}
resource "aws_iam_instance_profile" "scanner" {
  name = "hl-scanner"
  role = aws_iam_role.scanner.name
}

# ---------- S3 ----------
resource "aws_s3_bucket" "lake" {
  bucket        = "hl-scanner-${var.account_id}-${var.region}"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "tier-down"
    status = "Enabled"
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 180
      storage_class = "GLACIER_IR"
    }
  }
}

# ---------- EC2 ----------
data "aws_ami" "ubuntu_arm" {
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
}
resource "aws_instance" "scanner" {
  ami                         = data.aws_ami.ubuntu_arm.id
  instance_type               = "c7g.2xlarge"
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  key_name                    = aws_key_pair.scanner.key_name
  iam_instance_profile        = aws_iam_instance_profile.scanner.name
  associate_public_ip_address = false
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }
  tags = { Name = "hl-scanner" }
}
resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = 200
  type              = "gp3"
  iops              = 3000
  throughput        = 125
  encrypted         = true
  tags              = { Name = "hl-scanner-data" }
}
resource "aws_volume_attachment" "data" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.scanner.id
}
resource "aws_eip" "scanner" {
  instance = aws_instance.scanner.id
  domain   = "vpc"
}

# ---------- RDS ----------
resource "random_password" "rds" {
  length  = 28
  special = false
}
resource "aws_db_subnet_group" "main" {
  name       = "hl-scanner-db-subnets"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}
resource "aws_db_instance" "pg" {
  identifier              = "hl-scanner-pg"
  engine                  = "postgres"
  engine_version          = "16.14"
  instance_class          = "db.t4g.micro"
  allocated_storage       = 20
  max_allocated_storage   = 100
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "scanner"
  username                = "scanner"
  password                = random_password.rds.result
  vpc_security_group_ids  = [aws_security_group.rds.id]
  db_subnet_group_name    = aws_db_subnet_group.main.name
  multi_az                = false
  publicly_accessible     = false
  backup_retention_period = 7
  backup_window           = "17:00-18:00" # = 02:00–03:00 JST
  maintenance_window      = "Sun:18:00-Sun:19:00"
  skip_final_snapshot     = true
  deletion_protection     = false
}

# ---------- CloudWatch alarm (minimum viable) ----------
resource "aws_sns_topic" "alerts" {
  name = "hl-scanner-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_cloudwatch_metric_alarm" "ec2_down" {
  alarm_name          = "hl-scanner-ec2-down"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "StatusCheckFailed_Instance"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  dimensions          = { InstanceId = aws_instance.scanner.id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
