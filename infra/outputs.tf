output "eip" {
  value = aws_eip.scanner.public_ip
}

output "rds_endpoint" {
  value = aws_db_instance.pg.address
}

output "rds_password" {
  value     = random_password.rds.result
  sensitive = true
}

output "s3_bucket" {
  value = aws_s3_bucket.lake.id
}

output "region" {
  value = var.region
}

output "dashboard_bucket" {
  value = aws_s3_bucket.dashboard.id
}

output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}
