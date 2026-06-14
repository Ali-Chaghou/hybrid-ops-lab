# AWS-Provider gegen einen lokalen SQS-kompatiblen Endpoint (ElasticMQ).
# Dummy-Credentials und die skip_*-Flags verhindern, dass OpenTofu echtes AWS anspricht.
provider "aws" {
  region                      = var.aws_region
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true

  endpoints {
    sqs = var.sqs_endpoint_url
  }
}
